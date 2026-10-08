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
La app los usa automáticamente en cuanto existen (ver resolve_wallpaper_media
en app.py); no hay que tocar nada más. Si borras esa carpeta, la app vuelve a
usar los enlaces originales.

Requisito: tener ffmpeg instalado (incluye ffprobe).
  Windows:  winget install Gyan.FFmpeg      (y reabrir la terminal)
  macOS:    brew install ffmpeg
  Linux:    sudo apt install ffmpeg

Uso (desde la carpeta del proyecto, la que tiene app.py):
  python tools/optimize_wallpapers.py                 # todos los fondos
  python tools/optimize_wallpapers.py wallpaper_zelda # solo uno (por su id)
  python tools/optimize_wallpapers.py --force         # rehacer aunque ya existan
"""
import argparse
import ast
import json
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_DIR / 'static' / 'wallpapers'

MAX_WIDTH = 1280      # alto/ancho máximo (720p). Más que esto no se nota detrás del panel.
MAX_FPS = 30
CRF = 26              # calidad de H.264: más bajo = mejor calidad y más peso (23-28 es lo normal)
SAFE_CODECS = {'h264'}


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


def check_ffmpeg():
    if shutil.which('ffmpeg') and shutil.which('ffprobe'):
        return
    sys.exit(
        'No encuentro ffmpeg. Instálalo y vuelve a intentar:\n'
        '  Windows:  winget install Gyan.FFmpeg   (después cierra y abre la terminal)\n'
        '  macOS:    brew install ffmpeg\n'
        '  Linux:    sudo apt install ffmpeg'
    )


def download(url, dest):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (wallpaper-optimizer)'})
    with urllib.request.urlopen(req, timeout=60) as resp, open(dest, 'wb') as out:
        shutil.copyfileobj(resp, out)


def probe(path):
    """Devuelve (códec, ancho, alto, fps) del primer stream de video."""
    result = subprocess.run(
        ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
         '-show_entries', 'stream=codec_name,width,height,r_frame_rate',
         '-of', 'json', str(path)],
        capture_output=True, text=True, check=True,
    )
    stream = json.loads(result.stdout)['streams'][0]
    num, _, den = stream.get('r_frame_rate', '0/1').partition('/')
    fps = float(num) / float(den or 1) if float(den or 1) else 0.0
    return stream['codec_name'], int(stream['width']), int(stream['height']), fps


def human(n):
    return f'{n / 1024 / 1024:.1f} MB'


def optimize(item, force):
    item_id = item['id']
    out_video = OUTPUT_DIR / f'{item_id}.mp4'
    out_poster = OUTPUT_DIR / f'{item_id}.jpg'

    print(f"\n=== {item['name']} ({item_id}) ===")
    if out_video.exists() and out_poster.exists() and not force:
        print('  Ya está optimizado (usa --force para rehacerlo).')
        return True

    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / 'source'
        print(f"  Descargando {item['video_url']} ...")
        try:
            download(item['video_url'], source)
        except Exception as exc:  # enlace caído, sin red, bloqueado...
            print(f'  ✗ No se pudo descargar: {exc}')
            print('    (Si el enlace está caído, ese es el motivo por el que el fondo no se ve.)')
            return False

        try:
            codec, width, height, fps = probe(source)
        except (subprocess.CalledProcessError, KeyError, IndexError):
            print('  ✗ El archivo descargado no es un video válido.')
            return False

        print(f'  Original: {codec} · {width}x{height} · {fps:.0f} fps · {human(source.stat().st_size)}')
        if codec not in SAFE_CODECS:
            print(f'  ⚠ El códec "{codec}" NO lo reproducen todos los navegadores/PC '
                  '(probablemente por esto fallaba en otros equipos).')
        if width > MAX_WIDTH or fps > MAX_FPS + 1:
            print('  ⚠ Es más pesado de lo necesario (resolución/fps altos): causa lag en equipos modestos.')

        # Escala solo hacia abajo y deja dimensiones pares (requisito de H.264).
        filters = [f"scale='min({MAX_WIDTH},iw)':-2"]
        if fps > MAX_FPS + 1:
            filters.append(f'fps={MAX_FPS}')
        vf = ','.join(filters)

        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        print('  Convirtiendo (puede tardar un poco)...')
        encode = subprocess.run(
            ['ffmpeg', '-y', '-v', 'error', '-i', str(source), '-an', '-vf', vf,
             '-c:v', 'libx264', '-profile:v', 'main', '-pix_fmt', 'yuv420p',
             '-crf', str(CRF), '-preset', 'slow', '-movflags', '+faststart', str(out_video)],
            capture_output=True, text=True,
        )
        if encode.returncode != 0:
            print(f'  ✗ ffmpeg falló:\n{encode.stderr}')
            return False

        poster = subprocess.run(
            ['ffmpeg', '-y', '-v', 'error', '-i', str(source), '-vf', vf,
             '-frames:v', '1', '-q:v', '4', str(out_poster)],
            capture_output=True, text=True,
        )
        if poster.returncode != 0:
            print(f'  ✗ No se pudo crear la portada:\n{poster.stderr}')
            return False

    new_codec, new_w, new_h, new_fps = probe(out_video)
    print(f'  Listo:    {new_codec} · {new_w}x{new_h} · {new_fps:.0f} fps · {human(out_video.stat().st_size)}')
    return True


def main():
    parser = argparse.ArgumentParser(description='Optimiza los fondos animados de la tienda.')
    parser.add_argument('ids', nargs='*', help='ids de los fondos a procesar (por defecto, todos)')
    parser.add_argument('--force', action='store_true', help='rehacer aunque ya estén optimizados')
    args = parser.parse_args()

    check_ffmpeg()
    wallpapers = load_wallpapers()
    if args.ids:
        wallpapers = [w for w in wallpapers if w['id'] in args.ids]
        if not wallpapers:
            sys.exit('Ninguno de esos ids es un fondo de la tienda.')

    results = {w['id']: optimize(w, args.force) for w in wallpapers}

    print('\n--- Resumen ---')
    for item_id, ok in results.items():
        print(f"  {'✓' if ok else '✗'} {item_id}")
    if all(results.values()):
        print('\nTodo listo. Reinicia la app: ya usa las versiones optimizadas.')
        print('Para publicarlas, sube también la carpeta static/wallpapers/ (por ejemplo con git).')
    else:
        print('\nAlgunos fondos fallaron (mira los mensajes de arriba).')
        sys.exit(1)


if __name__ == '__main__':
    main()
