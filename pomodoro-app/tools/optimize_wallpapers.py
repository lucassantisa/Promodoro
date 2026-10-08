#!/usr/bin/env python3
"""
Optimiza los fondos animados de la tienda.

Qué hace, por cada objeto de tipo 'wallpaper' que haya en SHOP_ITEMS (app.py):
  1. Descarga el video desde su 'video_url'.
  2. Lo analiza (códec, resolución y fps) y te avisa si es de los que no todos
     los navegadores reproducen (por ejemplo HEVC/H.265) o si es muy pesado.
  3. Lo reconvierte a un MP4 liviano y compatible con todo: H.264, máximo
     1280x720, máximo 30 fps, sin audio.
  4. Guarda una foto de portada (primer cuadro).

Los resultados quedan en  static/wallpapers/<id>.mp4  y  <id>.jpg
(más un manifest.json que recuerda de qué enlace salió cada copia).
La app usa esas copias automáticamente. Si cambias el 'video_url' de un fondo,
solo hay que volver a correr este script: detecta el cambio y lo rehace.

Requisito: ffmpeg. Lo más fácil (sirve igual en Windows, macOS y Linux):
    pip install imageio-ffmpeg
Si ya tienes ffmpeg instalado en el sistema, se usa ese.

Uso (desde la carpeta del proyecto, la que tiene app.py):
  python tools/optimize_wallpapers.py                 # todos los fondos pendientes
  python tools/optimize_wallpapers.py wallpaper_zelda # solo uno (por su id)
  python tools/optimize_wallpapers.py --force         # rehacer todos aunque ya estén
"""
import argparse
import ast
import json
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_DIR / 'static' / 'wallpapers'
MANIFEST = OUTPUT_DIR / 'manifest.json'
SOURCE_DIR = Path(__file__).resolve().parent / 'source'   # videos descargados a mano (opcional)
VIDEO_EXTS = ('.mp4', '.webm', '.mov', '.mkv', '.m4v')

MAX_WIDTH = 1280      # ancho máximo (720p). Más que esto no se nota detrás del panel.
MAX_FPS = 30
CRF = 26              # calidad de H.264: más bajo = mejor calidad y más peso (23-28 es lo normal)
SAFE_CODECS = {'h264'}

FFMPEG = None         # ruta del ejecutable, se define en find_ffmpeg()


def find_ffmpeg():
    """ffmpeg del sistema o, si no hay, el que trae el paquete imageio-ffmpeg."""
    found = shutil.which('ffmpeg')
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        sys.exit(
            'No encuentro ffmpeg. La forma más fácil de tenerlo:\n'
            '    pip install imageio-ffmpeg\n'
            'y vuelve a ejecutar este script.\n'
            '(Alternativa en Windows: winget install Gyan.FFmpeg y reabrir la terminal.)'
        )


def load_wallpapers():
    """Lee SHOP_ITEMS de app.py SIN importarlo (importarlo abriría la base de datos)."""
    tree = ast.parse((PROJECT_DIR / 'app.py').read_text(encoding='utf-8'))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == 'SHOP_ITEMS' for t in node.targets
        ):
            items = ast.literal_eval(node.value)
            return [i for i in items.values() if i.get('type') == 'wallpaper' and i.get('video_url')]
    sys.exit('No encontré SHOP_ITEMS en app.py. ¿Ejecutaste el script desde la carpeta del proyecto?')


def load_manifest():
    try:
        data = json.loads(MANIFEST.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_manifest(data):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')


def _fetch(url, dest, context=None):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (wallpaper-optimizer)'})
    with urllib.request.urlopen(req, timeout=60, context=context) as resp, open(dest, 'wb') as out:
        shutil.copyfileobj(resp, out)


def is_cert_error(exc):
    """True si el fallo es de verificación del certificado SSL (no de red ni enlace caído)."""
    reason = getattr(exc, 'reason', exc)
    return isinstance(reason, ssl.SSLCertVerificationError) or 'CERTIFICATE_VERIFY_FAILED' in str(exc)


def download(url, dest):
    """Descarga con verificación SSL normal. Si el certificado no se puede verificar
    con el almacén del sistema, reintenta con el paquete 'certifi' (si está instalado)."""
    try:
        return _fetch(url, dest)
    except Exception as exc:
        if not is_cert_error(exc):
            raise
        try:
            import certifi
        except ImportError:
            raise exc
        print('  (Python no pudo verificar el certificado con el sistema; reintentando con certifi...)')
        return _fetch(url, dest, ssl.create_default_context(cafile=certifi.where()))


def find_manual_source(item_id):
    """Busca un video descargado a mano en tools/source/<id>.<ext>."""
    for ext in VIDEO_EXTS:
        candidate = SOURCE_DIR / f'{item_id}{ext}'
        if candidate.exists():
            return candidate
    return None


def probe(path):
    """Devuelve (códec, ancho, alto, fps) leyendo lo que imprime `ffmpeg -i`."""
    result = subprocess.run([FFMPEG, '-hide_banner', '-i', str(path)],
                            capture_output=True, text=True)
    line = next((l for l in result.stderr.splitlines() if 'Video:' in l), None)
    if line is None:
        raise ValueError('sin stream de video')
    codec = re.search(r'Video:\s*([\w]+)', line).group(1)
    size = re.search(r',\s*(\d{2,5})x(\d{2,5})', line)
    fps_match = re.search(r'([\d.]+)\s*fps', line) or re.search(r'([\d.]+)\s*tbr', line)
    return codec, int(size.group(1)), int(size.group(2)), float(fps_match.group(1)) if fps_match else 0.0


def human(n):
    return f'{n / 1024 / 1024:.1f} MB'


def optimize(item, force, manifest):
    item_id = item['id']
    url = item['video_url']
    out_video = OUTPUT_DIR / f'{item_id}.mp4'
    out_poster = OUTPUT_DIR / f'{item_id}.jpg'

    print(f"\n=== {item['name']} ({item_id}) ===")
    up_to_date = out_video.exists() and out_poster.exists() and manifest.get(item_id) == url
    if up_to_date and not force:
        print('  Ya está optimizado.')
        return True
    if out_video.exists() and manifest.get(item_id) not in (None, url):
        print('  El video de este fondo cambió: se rehace la copia optimizada.')

    with tempfile.TemporaryDirectory() as tmp:
        source = find_manual_source(item_id)
        if source:
            print(f'  Usando el video que pusiste a mano: {source.name}')
        else:
            source = Path(tmp) / 'source'
            print(f'  Descargando {url} ...')
            try:
                download(url, source)
            except Exception as exc:
                if is_cert_error(exc):
                    print('  [X] Python no pudo verificar el certificado de seguridad del sitio.')
                    print('      Esto NO significa que el enlace esté caído (tu navegador sí lo abre).')
                    print('      Soluciones, en este orden:')
                    print('        1) Ejecuta:  pip install certifi   y vuelve a correr este script.')
                    print('        2) Si sigue igual: abre el enlace en tu navegador, guarda el video')
                    print(f'           y ponlo en tools/source/ con el nombre  {item_id}.mp4')
                    print('           (el script lo usa sin descargar nada).')
                else:  # enlace caído, sin red, bloqueado...
                    print(f'  [X] No se pudo descargar: {exc}')
                    print('      (Si el enlace está caído, ese es el motivo por el que el fondo no se ve.)')
                return False

        try:
            codec, width, height, fps = probe(source)
        except (ValueError, AttributeError):
            print('  [X] El archivo descargado no es un video válido.')
            return False

        print(f'  Original: {codec} | {width}x{height} | {fps:.0f} fps | {human(source.stat().st_size)}')
        if codec not in SAFE_CODECS:
            print(f'  [!] El códec "{codec}" NO lo reproducen todos los navegadores/PC '
                  '(probablemente por esto fallaba en otros equipos).')
        if width > MAX_WIDTH or fps > MAX_FPS + 1:
            print('  [!] Es más pesado de lo necesario (resolución/fps altos): causa lag en equipos modestos.')

        # Escala solo hacia abajo y deja dimensiones pares (requisito de H.264).
        filters = [f"scale='min({MAX_WIDTH},iw)':-2"]
        if fps > MAX_FPS + 1:
            filters.append(f'fps={MAX_FPS}')
        vf = ','.join(filters)

        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        print('  Convirtiendo (puede tardar un poco)...')
        encode = subprocess.run(
            [FFMPEG, '-y', '-v', 'error', '-i', str(source), '-an', '-vf', vf,
             '-c:v', 'libx264', '-profile:v', 'main', '-pix_fmt', 'yuv420p',
             '-crf', str(CRF), '-preset', 'slow', '-movflags', '+faststart', str(out_video)],
            capture_output=True, text=True,
        )
        if encode.returncode != 0:
            print(f'  [X] ffmpeg falló:\n{encode.stderr}')
            return False

        poster = subprocess.run(
            [FFMPEG, '-y', '-v', 'error', '-i', str(source), '-vf', vf,
             '-frames:v', '1', '-q:v', '4', str(out_poster)],
            capture_output=True, text=True,
        )
        if poster.returncode != 0:
            print(f'  [X] No se pudo crear la portada:\n{poster.stderr}')
            return False

    new_codec, new_w, new_h, new_fps = probe(out_video)
    print(f'  Listo:    {new_codec} | {new_w}x{new_h} | {new_fps:.0f} fps | {human(out_video.stat().st_size)}')
    manifest[item_id] = url
    save_manifest(manifest)   # se guarda en cada fondo, así un fallo posterior no pierde lo ya hecho
    return True


def main():
    global FFMPEG
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors='replace')
        except Exception:
            pass
    parser = argparse.ArgumentParser(description='Optimiza los fondos animados de la tienda.')
    parser.add_argument('ids', nargs='*', help='ids de los fondos a procesar (por defecto, todos)')
    parser.add_argument('--force', action='store_true', help='rehacer aunque ya estén optimizados')
    args = parser.parse_args()

    FFMPEG = find_ffmpeg()
    wallpapers = load_wallpapers()
    if args.ids:
        wallpapers = [w for w in wallpapers if w['id'] in args.ids]
        if not wallpapers:
            sys.exit('Ninguno de esos ids es un fondo de la tienda.')

    manifest = load_manifest()
    results = {w['id']: optimize(w, args.force, manifest) for w in wallpapers}

    print('\n--- Resumen ---')
    for item_id, ok in results.items():
        print(f"  {'[OK]' if ok else '[X]'} {item_id}")
    if all(results.values()):
        print('\nTodo listo. Reinicia la app: ya usa las versiones optimizadas.')
        print('Para publicarlas, sube también la carpeta static/wallpapers/ (por ejemplo con git).')
    else:
        print('\nAlgunos fondos fallaron (mira los mensajes de arriba).')
        sys.exit(1)


if __name__ == '__main__':
    main()
