import os
import uuid
from datetime import datetime, timedelta
from collections import defaultdict
from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event
from sqlalchemy.engine import Engine
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'clave_secreta_para_sesiones')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///pomodoro.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# --- Configuración para subir fotos de perfil ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'static', 'uploads')
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024  # límite de 5 MB por imagen

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# --- Sistema de rangos según cantidad de pomodoros (tomates), con temática de materiales ---
# 1 pomodoro = 1 hora completa de estudio acumulada (ver User.pomodoros).
RANKS = [
    {'min': 100, 'key': 'diamante', 'name': 'Dios',       'material': 'Diamante', 'icon': '💎'},
    {'min': 40,  'key': 'oro',      'name': 'Experto',    'material': 'Oro',      'icon': '🥇'},
    {'min': 20,  'key': 'hierro',   'name': 'Avanzado',   'material': 'Hierro',   'icon': '⚙️'},
    {'min': 10,  'key': 'piedra',   'name': 'Intermedio', 'material': 'Piedra',   'icon': '🪨'},
    {'min': 0,   'key': 'madera',   'name': 'Novato',     'material': 'Madera',   'icon': '🪵'},
]

def get_rank_info(pomodoros):
    for rank in RANKS:
        if pomodoros >= rank['min']:
            return rank
    return RANKS[-1]

# --- Logros ---
# Catálogo de logros canjeables por puntos. Cada logro se puede canjear una
# sola vez por usuario (se controla con el modelo AchievementClaim, que tiene
# una restricción única de user_id + achievement_id). 'check' recibe al
_PIEDRA_MIN_POMODOROS = next(r['min'] for r in RANKS if r['key'] == 'piedra')


def _weekly_minutes_map(user, offset):
    """Minutos estudiados por semana calendario (lunes a domingo, hora local
    del usuario), a partir de TODO su historial de sesiones. La llave de
    cada semana es la fecha de su lunes. Se usa para logros basados en
    semanas (ej. 'estudia 10 horas en una semana')."""
    sessions = StudySession.query.filter(StudySession.user_id == user.id).all()
    weekly_minutes = defaultdict(int)
    for s in sessions:
        local_dt = s.completed_at - timedelta(minutes=offset)
        week_start = (local_dt - timedelta(days=local_dt.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        weekly_minutes[week_start] += s.minutes
    return weekly_minutes


def _has_10h_week(user):
    offset = get_tz_offset()
    weekly_minutes = _weekly_minutes_map(user, offset)
    return any(minutes >= 600 for minutes in weekly_minutes.values())


def _has_zero_hour_week(user):
    """Se cumple si hubo una semana calendario completa y ya terminada en la
    que el usuario estudió 0 minutos. Solo cuenta a partir de la semana
    siguiente a su primera sesión registrada, para que una cuenta recién
    creada (que nunca ha estudiado) no la cumpla gratis con solo entrar."""
    offset = get_tz_offset()
    weekly_minutes = _weekly_minutes_map(user, offset)
    if not weekly_minutes:
        return False

    now_local = datetime.utcnow() - timedelta(minutes=offset)
    this_week_start = (now_local - timedelta(days=now_local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)

    cursor = min(weekly_minutes.keys()) + timedelta(weeks=1)
    while cursor < this_week_start:
        if weekly_minutes.get(cursor, 0) == 0:
            return True
        cursor += timedelta(weeks=1)
    return False


def _has_5h_in_a_folder(user):
    top_folder = user.folders.order_by(Folder.points.desc()).first()
    return top_folder is not None and top_folder.points >= 300


# A partir de esta fecha (UTC) el bug de zona horaria que afectaba al logro
# 'Búho nocturno' ya está corregido. Las sesiones de estudio de antes de
# esta fecha no cuentan para ese logro (ver _has_madrugada_block).
MADRUGADA_FIX_CUTOFF = datetime(2026, 8, 22, 12, 0, 0)


def _has_madrugada_block(user):
    """True si algún bloque de estudio de 1 hora o más terminó entre las
    00:00 y las 06:00, hora local del usuario.

    Solo cuentan sesiones completadas después de MADRUGADA_FIX_CUTOFF: antes
    de esa fecha hubo un bug de zona horaria que en algunos casos marcaba
    sesiones de tarde/noche como si fueran de madrugada. Las sesiones
    anteriores a la corrección quedan afuera a propósito, para que el logro
    solo se pueda ganar de verdad de ahora en adelante."""
    offset = get_tz_offset()
    sessions = StudySession.query.filter(
        StudySession.user_id == user.id,
        StudySession.completed_at >= MADRUGADA_FIX_CUTOFF,
        StudySession.minutes >= 60
    ).all()
    for s in sessions:
        local_dt = s.completed_at - timedelta(minutes=offset)
        if 0 <= local_dt.hour < 6:
            return True
    return False


def _has_7_day_streak(user):
    offset = get_tz_offset()
    return compute_streaks_for_users([user.id], offset)[user.id] >= 7


ACHIEVEMENTS = [
    {
        'id': 'rango_piedra',
        'name': 'Rango de Piedra',
        'description': f'Alcanza el rango de Piedra ({_PIEDRA_MIN_POMODOROS} pomotates acumulados).',
        'points': 50,
        'icon': '🪨',
        'check': lambda user: user.pomodoros >= _PIEDRA_MIN_POMODOROS,
    },
    {
        'id': 'semana_10_horas',
        'name': 'Maratón semanal',
        'description': 'Estudia 10 horas o más dentro de una misma semana.',
        'points': 100,
        'icon': '📚',
        'check': _has_10h_week,
    },
    {
        'id': 'compra_dos_objetos',
        'name': 'Comprador frecuente',
        'description': 'Compra dos objetos distintos en la tienda.',
        'points': 30,
        'icon': '🛍️',
        'check': lambda user: user.purchases.count() >= 2,
    },
    {
        'id': 'carpeta_5_horas',
        'name': 'Especialista',
        'description': 'Alcanza 5 horas acumuladas en una carpeta específica.',
        'points': 50,
        'icon': '🎯',
        'check': _has_5h_in_a_folder,
    },
    {
        'id': 'semana_0_horas',
        'name': 'Semana de descanso',
        'description': 'Ten una semana completa sin registrar estudio.',
        'points': 20,
        'icon': '🌴',
        'check': _has_zero_hour_week,
    },
    {
        'id': 'bloque_madrugada',
        'name': 'Búho nocturno',
        'description': 'Termina un bloque de estudio de 1 hora o más entre las 12:00 y las 6:00 am.',
        'points': 300,
        'icon': '🌙',
        'check': _has_madrugada_block,
    },
    {
        'id': 'racha_7_dias',
        'name': 'Constancia de hierro',
        'description': 'Logra una racha de 7 días seguidos estudiando.',
        'points': 100,
        'icon': '🔥',
        'check': _has_7_day_streak,
    },
]

# --- Tienda de puntos ---
# Catálogo de objetos comprables. Por ahora solo hay un banner animado,
# pero está pensado para poder agregar más objetos (y tipos) más adelante.
SHOP_ITEMS = {
    'banner_aurora': {
        'id': 'banner_aurora',
        'name': 'Banner Aurora',
        'description': 'Un fondo animado que aparece detrás de tu perfil.',
        'price': 100,
        'type': 'banner',
        'image_url': 'https://cdn.pixabay.com/animation/2023/04/16/16/18/16-18-04-472_512.gif'
    },
    'banner_nova': {
        'id': 'banner_nova',
        'name': 'Banner Nova',
        'description': 'Otro fondo animado para personalizar tu perfil.',
        'price': 150,
        'type': 'banner',
        'image_url': 'https://cdn.pixabay.com/animation/2026/05/09/09/34/09-34-48-912_256.gif'
    },
    'banner_galaxia': {
        'id': 'banner_galaxia',
        'name': 'Banner Galaxia',
        'description': 'Fondo animado de galaxia y estrellas para tu perfil.',
        'price': 300,
        'type': 'banner',
        'image_url': '/static/uploads/galaxy.gif'
    },
    'banner_matrix': {
        'id': 'banner_matrix',
        'name': 'Banner Matrix',
        'description': 'Fondo animado de Matrix para tu perfil.',
        'price':500,
        'type': 'banner',
        'image_url': '/static/uploads/matrix.gif'
    },
    'banner_ponyo': {
        'id': 'banner_ponyo',
        'name': 'Banner Ponyo',
        'description': 'Fondo animado de Studio Ghibli para tu perfil.',
        'price':650,
        'type': 'banner',
        'image_url': '/static/uploads/ponyo.gif'
    },
    'banner_chihiro': {
        'id': 'banner_chihiro',
        'name': 'Banner Chihiro 8 bit',
        'description': 'Fondo animado de Studio Ghibli para tu perfil.',
        'price':1000,
        'type': 'banner',
        'image_url': '/static/uploads/chihiro8bit.gif'
    },
    'font_orbitron': {
        'id': 'font_orbitron',
        'name': 'Tipografía Orbitron',
        'description': 'Una tipografía futurista y geométrica para los números de tu temporizador.',
        'price': 30,
        'type': 'font',
        'font_family': 'Orbitron'
    },
    'sound_lofi_nocturno': {
        'id': 'sound_lofi_nocturno',
        'name': 'Lofi Nocturno',
        'description': 'Una pista lofi distinta a la de siempre, ideal para sesiones de estudio nocturnas.',
        'price': 15,
        'type': 'sound',
        'category': 'lofi',
        'audio_url': 'https://cdn.pixabay.com/audio/2023/07/30/audio_e0908e8569.mp3'
    },
    'sound_undertale_relax': {
        'id': 'sound_undertale_relax',
        'name': 'Undertale hogareño',
        'description': 'Un mix de canciones relajantes de undertale con una fogata de fondo.',
        'price': 60,
        'type': 'sound',
        'category': 'piano',
        'audio_url': 'https://files.catbox.moe/y7k2fr.mp3'
    },
    'sound_ghibli_relax': {
        'id': 'sound_ghibli_relax',
        'name': 'Ghibli Mix',
        'description': 'Un mix de canciones de Studio Ghibli en piano.',
        'price': 60,
        'type': 'sound',
        'category': 'piano',
        'audio_url': 'https://files.catbox.moe/1bcaij.mp3'
    },
    'sound_cp': {
        'id': 'sound_cp',
        'name': 'Pizza Parlor Theme',
        'description': 'La cancion que sonaba cuando visitabas la pizzeria de Club Penguin.',
        'price': 30,
        'type': 'sound',
        'category': 'piano',
        'audio_url': 'https://files.catbox.moe/s899is.mp3'
    }
}

# Categorías de canciones (coinciden con las 3 pistas del reproductor:
# lofi/piano/rain), usadas solo para mostrar una etiqueta linda en la tienda.
SOUND_CATEGORY_LABELS = {
    'lofi': 'Lofi 🎧',
    'piano': 'Música relajada 🎹',
    'rain': 'Ambiente 🌧️'
}

# Pistas gratuitas de siempre (las que ya traía el reproductor). Si un
# usuario no tiene ninguna canción comprada equipada en una categoría, esta
# es la que suena.
DEFAULT_TRACKS = {
    'lofi': 'https://cdn.pixabay.com/download/audio/2022/05/27/audio_1808fbf07a.mp3?filename=lofi-study-112191.mp3',
    'piano': 'https://cdn.pixabay.com/audio/2026/07/07/audio_e36696ae9b.mp3',
    'rain': 'https://cdn.pixabay.com/audio/2022/05/11/audio_567640eb63.mp3'
}


def get_user_tracks(user):
    """Arma las 3 pistas del reproductor de un usuario: la canción comprada y
    equipada en cada categoría si tiene una, o si no la gratuita de siempre."""
    tracks = dict(DEFAULT_TRACKS)
    for category in DEFAULT_TRACKS:
        equipped_id = getattr(user, f'equipped_sound_{category}', None)
        item = SHOP_ITEMS.get(equipped_id)
        if item and item['type'] == 'sound' and item['category'] == category:
            tracks[category] = item['audio_url']
    return tracks

# Fuentes desbloqueables: id del objeto de la tienda -> clase CSS que la aplica
# en el temporizador. Se usa para no tener que tocar el HTML cada vez que se
# agregue una tipografía nueva a la tienda.
FONT_CSS_CLASSES = {
    'font_orbitron': 'font-orbitron'
}

db = SQLAlchemy(app)


@event.listens_for(Engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, connection_record):
    """SQLite en modo por defecto se bloquea fácil cuando dos conexiones lo
    tocan casi al mismo tiempo (típico con el recargador de Flask en
    debug=True, o con la carpeta del proyecto sincronizada por OneDrive),
    y eso se ve como 'disk I/O error' o 'database is locked'. WAL permite
    leer y escribir a la vez con menos choques, y busy_timeout hace que
    espere unos segundos antes de fallar en vez de tirar el error altiro."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()

class Follow(db.Model):
    """Representa la relación 'A sigue a B'."""
    id = db.Column(db.Integer, primary_key=True)
    follower_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    followed_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('follower_id', 'followed_id', name='uix_follower_followed'),
    )


class Folder(db.Model):
    """Carpeta/ramo del usuario (ej. 'MAT071') para agrupar los puntos por materia.
    La suma de los puntos de todas las carpetas de un usuario siempre es igual
    a su total de puntos (user.points)."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    name = db.Column(db.String(30), nullable=False)
    points = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('user_id', 'name', name='uix_user_folder_name'),
    )


class ExamDate(db.Model):
    """Fecha de prueba/examen que el usuario quiere tener presente."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    title = db.Column(db.String(100), nullable=False)
    exam_date = db.Column(db.Date, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class StudySession(db.Model):
    """Registro de cada bloque de estudio completado (con su fecha y carpeta),
    usado para armar el gráfico de productividad por día/semana/mes."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    folder_id = db.Column(db.Integer, db.ForeignKey('folder.id'), nullable=True)
    minutes = db.Column(db.Integer, nullable=False)
    completed_at = db.Column(db.DateTime, default=datetime.utcnow)


class Purchase(db.Model):
    """Objeto de la tienda que un usuario ya compró (su 'inventario')."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    item_id = db.Column(db.String(50), nullable=False)
    purchased_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('user_id', 'item_id', name='uix_user_item_purchase'),
    )


class AchievementClaim(db.Model):
    """Logro que un usuario ya canjeó por sus puntos. La restricción única
    de abajo es lo que garantiza que un mismo logro no se pueda canjear
    dos veces para el mismo usuario."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    achievement_id = db.Column(db.String(50), nullable=False)
    claimed_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('user_id', 'achievement_id', name='uix_user_achievement_claim'),
    )


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    points = db.Column(db.Integer, default=0)
    profile_pic = db.Column(db.String(300), default='https://i.imgur.com/6VBx3io.png') # Avatar por defecto
    balance = db.Column(db.Integer, default=0)  # Puntos gastables en la tienda (no baja el total/rango)
    equipped_banner = db.Column(db.String(50), nullable=True)  # id del banner activo en el perfil
    equipped_font = db.Column(db.String(50), nullable=True)  # id de la tipografía activa en el temporizador
    # Canción comprada equipada en cada categoría del reproductor (una por
    # categoría). None = se usa la pista gratuita de siempre para esa
    # categoría (ver DEFAULT_TRACKS / get_user_tracks).
    equipped_sound_lofi = db.Column(db.String(50), nullable=True)
    equipped_sound_piano = db.Column(db.String(50), nullable=True)
    equipped_sound_rain = db.Column(db.String(50), nullable=True)

    # --- Relaciones de seguidores (sistema tipo red social) ---
    following = db.relationship(
        'Follow',
        foreign_keys=[Follow.follower_id],
        backref=db.backref('follower', lazy='joined'),
        lazy='dynamic',
        cascade='all, delete-orphan'
    )
    followers = db.relationship(
        'Follow',
        foreign_keys=[Follow.followed_id],
        backref=db.backref('followed', lazy='joined'),
        lazy='dynamic',
        cascade='all, delete-orphan'
    )

    # --- Carpetas de estudio (puntos por ramo) ---
    folders = db.relationship(
        'Folder',
        backref='user',
        lazy='dynamic',
        cascade='all, delete-orphan'
    )

    # --- Objetos comprados en la tienda ---
    purchases = db.relationship(
        'Purchase',
        backref='user',
        lazy='dynamic',
        cascade='all, delete-orphan'
    )

    # --- Logros ya canjeados ---
    achievement_claims = db.relationship(
        'AchievementClaim',
        backref='user',
        lazy='dynamic',
        cascade='all, delete-orphan'
    )

    @property
    def pomodoros(self):
        """Cantidad de pomodoros ganados. 1 pomodoro = 1 hora completa de
        estudio acumulada (user.points guarda el total de minutos estudiados
        históricamente). Esto es lo que se muestra en el perfil y define el
        rango; los 'points' en sí quedan solo como saldo interno para la
        tienda (ver user.balance)."""
        return self.points // 60

    def owns_item(self, item_id):
        return self.purchases.filter_by(item_id=item_id).first() is not None

    def has_claimed_achievement(self, achievement_id):
        return self.achievement_claims.filter_by(achievement_id=achievement_id).first() is not None

    def is_following(self, other_user):
        if other_user is None:
            return False
        return self.following.filter_by(followed_id=other_user.id).first() is not None

    def follow(self, other_user):
        if other_user.id == self.id or self.is_following(other_user):
            return False
        db.session.add(Follow(follower_id=self.id, followed_id=other_user.id))
        return True

    def unfollow(self, other_user):
        existing = self.following.filter_by(followed_id=other_user.id).first()
        if existing:
            db.session.delete(existing)
            return True
        return False

    @property
    def followers_count(self):
        return self.followers.count()

    @property
    def following_count(self):
        return self.following.count()

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        user_exists = User.query.filter_by(username=username).first()
        if user_exists:
            return render_template('register.html', error="Ese usuario ya existe. Intenta con otro nombre.")

        hashed_password = generate_password_hash(password)
        new_user = User(username=username, password=hashed_password)
        db.session.add(new_user)
        db.session.commit()
        return redirect(url_for('login'))

    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        user = User.query.filter_by(username=username).first()

        if user and check_password_hash(user.password, password):
            session['user_id'] = user.id
            session['username'] = user.username
            return redirect(url_for('index'))
        else:
            return render_template('login.html', error="Usuario o contraseña incorrectos.")

    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('user_id', None)
    session.pop('username', None)
    return redirect(url_for('login'))

@app.route('/')
def index():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    user = User.query.get(session['user_id'])
    if user is None:
        session.clear()
        return redirect(url_for('login'))

    rank = get_rank_info(user.pomodoros)
    timer_font_class = FONT_CSS_CLASSES.get(user.equipped_font, '')
    tracks = get_user_tracks(user)
    return render_template('index.html', user=user, rank=rank, ranks=RANKS, timer_font_class=timer_font_class, tracks=tracks)

def render_profile_page(viewer, profile_user, error=None):
    """Arma el contexto compartido por /profile y /profile/<username>."""
    is_own = viewer.id == profile_user.id

    followers = [f.follower for f in profile_user.followers.order_by(Follow.created_at.desc()).all()]
    following = [f.followed for f in profile_user.following.order_by(Follow.created_at.desc()).all()]

    # IDs que el usuario que está mirando la página (viewer) ya sigue,
    # para poder pintar el botón correcto (Seguir / Siguiendo) en cada fila de las listas.
    viewer_following_ids = {f.followed_id for f in viewer.following.all()}

    banner_item = SHOP_ITEMS.get(profile_user.equipped_banner)
    banner_url = banner_item['image_url'] if banner_item else None

    return render_template(
        'profile.html',
        user=viewer,
        profile_user=profile_user,
        is_own=is_own,
        is_following_profile=viewer.is_following(profile_user),
        followers=followers,
        following=following,
        following_ids=viewer_following_ids,
        rank=get_rank_info(profile_user.pomodoros),
        banner_url=banner_url,
        error=error
    )


@app.route('/profile', methods=['GET', 'POST'])
def profile():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    user = User.query.get(session['user_id'])
    if user is None:
        session.clear()
        return redirect(url_for('login'))

    error = None

    if request.method == 'POST':
        file = request.files.get('profile_pic')

        if file and file.filename != '':
            if allowed_file(file.filename):
                # Borra la foto anterior si fue subida por el usuario (no la de por defecto)
                if user.profile_pic and user.profile_pic.startswith('/static/uploads/'):
                    old_filename = user.profile_pic.rsplit('/', 1)[-1]
                    old_path = os.path.join(app.config['UPLOAD_FOLDER'], old_filename)
                    if os.path.exists(old_path):
                        os.remove(old_path)

                ext = file.filename.rsplit('.', 1)[1].lower()
                filename = secure_filename(f"user_{user.id}_{uuid.uuid4().hex}.{ext}")
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                file.save(filepath)

                user.profile_pic = '/static/uploads/' + filename
                db.session.commit()
                return redirect(url_for('profile'))
            else:
                error = "Formato no permitido. Usa PNG, JPG, JPEG, GIF o WEBP."
        else:
            error = "No seleccionaste ninguna imagen."

    return render_profile_page(user, user, error=error)


@app.route('/profile/<username>')
def view_profile(username):
    if 'user_id' not in session:
        return redirect(url_for('login'))

    viewer = User.query.get(session['user_id'])
    if viewer is None:
        session.clear()
        return redirect(url_for('login'))

    profile_user = User.query.filter_by(username=username).first()
    if profile_user is None:
        return render_template('profile_not_found.html', username=username), 404

    # Si el usuario busca su propio perfil por username, lo mandamos a /profile
    if profile_user.id == viewer.id:
        return redirect(url_for('profile'))

    return render_profile_page(viewer, profile_user)


@app.route('/follow/<username>', methods=['POST'])
def follow_user(username):
    if 'user_id' not in session:
        return jsonify({"error": "No autorizado"}), 401

    current_user = User.query.get(session['user_id'])
    if current_user is None:
        session.clear()
        return jsonify({"error": "Sesión inválida. Vuelve a iniciar sesión."}), 401

    target = User.query.filter_by(username=username).first()
    if target is None:
        return jsonify({"error": "Usuario no encontrado"}), 404

    if target.id == current_user.id:
        return jsonify({"error": "No puedes seguirte a ti mismo"}), 400

    current_user.follow(target)
    db.session.commit()

    return jsonify({
        "success": True,
        "is_following": True,
        "followers_count": target.followers_count,
        "following_count": current_user.following_count
    })


@app.route('/unfollow/<username>', methods=['POST'])
def unfollow_user(username):
    if 'user_id' not in session:
        return jsonify({"error": "No autorizado"}), 401

    current_user = User.query.get(session['user_id'])
    if current_user is None:
        session.clear()
        return jsonify({"error": "Sesión inválida. Vuelve a iniciar sesión."}), 401

    target = User.query.filter_by(username=username).first()
    if target is None:
        return jsonify({"error": "Usuario no encontrado"}), 404

    current_user.unfollow(target)
    db.session.commit()

    return jsonify({
        "success": True,
        "is_following": False,
        "followers_count": target.followers_count,
        "following_count": current_user.following_count
    })

@app.errorhandler(413)
def file_too_large(e):
    return "La imagen es demasiado grande. El límite es 5MB.", 413

def ensure_default_folder(user):
    """Si el usuario todavía no tiene ninguna carpeta, crea una llamada
    'General' heredando los puntos totales que ya tenía acumulados.
    Así ningún usuario existente pierde sus puntos al migrar al sistema
    de carpetas, y el total sigue siendo la suma de las carpetas."""
    folder = user.folders.order_by(Folder.id.asc()).first()
    if folder is None:
        folder = Folder(user_id=user.id, name='General', points=user.points)
        db.session.add(folder)
        db.session.commit()
    return folder


@app.route('/folders', methods=['GET'])
def get_folders():
    if 'user_id' not in session:
        return jsonify({"error": "No autorizado"}), 401

    user = User.query.get(session['user_id'])
    if user is None:
        session.clear()
        return jsonify({"error": "Sesión inválida. Vuelve a iniciar sesión."}), 401

    ensure_default_folder(user)

    folders = user.folders.order_by(Folder.points.desc(), Folder.created_at.asc()).all()
    return jsonify({
        "folders": [{"id": f.id, "name": f.name, "points": f.points} for f in folders]
    })


@app.route('/folders', methods=['POST'])
def create_folder():
    if 'user_id' not in session:
        return jsonify({"error": "No autorizado"}), 401

    user = User.query.get(session['user_id'])
    if user is None:
        session.clear()
        return jsonify({"error": "Sesión inválida. Vuelve a iniciar sesión."}), 401

    data = request.get_json() or {}
    name = (data.get('name') or '').strip()

    if not name:
        return jsonify({"error": "El nombre de la carpeta no puede estar vacío."}), 400
    if len(name) > 30:
        return jsonify({"error": "El nombre es demasiado largo (máx. 30 caracteres)."}), 400

    exists = user.folders.filter(db.func.lower(Folder.name) == name.lower()).first()
    if exists:
        return jsonify({"error": f"Ya tienes una carpeta llamada '{name}'."}), 400

    folder = Folder(user_id=user.id, name=name, points=0)
    db.session.add(folder)
    db.session.commit()

    return jsonify({
        "success": True,
        "folder": {"id": folder.id, "name": folder.name, "points": folder.points}
    })


@app.route('/folders/<int:folder_id>', methods=['DELETE'])
def delete_folder(folder_id):
    if 'user_id' not in session:
        return jsonify({"error": "No autorizado"}), 401

    user = User.query.get(session['user_id'])
    if user is None:
        session.clear()
        return jsonify({"error": "Sesión inválida. Vuelve a iniciar sesión."}), 401

    folder = Folder.query.get(folder_id)
    if folder is None or folder.user_id != user.id:
        return jsonify({"error": "Carpeta no encontrada"}), 404

    # Como el total siempre es la suma de las carpetas, al borrar una carpeta
    # también se descuentan sus puntos del total del usuario.
    user.points = max(0, user.points - folder.points)
    db.session.delete(folder)
    db.session.commit()

    return jsonify({"success": True, "new_total": user.points, "new_pomodoros": user.pomodoros})


@app.route('/add_points', methods=['POST'])
def add_points():
    if 'user_id' not in session:
        return jsonify({"error": "No autorizado"}), 401

    user = User.query.get(session['user_id'])
    if user is None:
        session.clear()
        return jsonify({"error": "Sesión inválida. Vuelve a iniciar sesión."}), 401

    data = request.get_json()
    points_earned = data.get('points', 0)
    folder_id = data.get('folder_id')

    folder = None
    if folder_id:
        candidate = Folder.query.get(folder_id)
        if candidate is not None and candidate.user_id == user.id:
            folder = candidate
    if folder is None:
        # Si no llegó folder_id o la carpeta ya no existe (ej. se borró en otra
        # pestaña), los puntos igual se guardan en la carpeta por defecto en
        # vez de perderse.
        folder = ensure_default_folder(user)

    # Los puntos se suman al total Y a la carpeta seleccionada, para que
    # la suma de las carpetas siempre sea igual al total. También se suman
    # al saldo gastable de la tienda (ese sí baja al comprar algo).
    user.points += points_earned
    user.balance += points_earned
    folder.points += points_earned

    # Registro histórico del bloque, para el gráfico de productividad
    if points_earned > 0:
        db.session.add(StudySession(
            user_id=user.id,
            folder_id=folder.id,
            minutes=points_earned,
            completed_at=datetime.utcnow()
        ))

    db.session.commit()

    return jsonify({
        "success": True,
        "new_total": user.points,
        "new_pomodoros": user.pomodoros,
        "folder": {"id": folder.id, "name": folder.name, "points": folder.points}
    })


@app.route('/shop')
def shop():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    user = User.query.get(session['user_id'])
    if user is None:
        session.clear()
        return redirect(url_for('login'))

    owned_ids = {p.item_id for p in user.purchases.all()}
    items = list(SHOP_ITEMS.values())
    equipped_sounds = {
        'lofi': user.equipped_sound_lofi,
        'piano': user.equipped_sound_piano,
        'rain': user.equipped_sound_rain
    }

    return render_template(
        'shop.html', user=user, items=items, owned_ids=owned_ids,
        equipped_sounds=equipped_sounds, sound_labels=SOUND_CATEGORY_LABELS
    )


@app.route('/shop/buy/<item_id>', methods=['POST'])
def buy_item(item_id):
    if 'user_id' not in session:
        return jsonify({"error": "No autorizado"}), 401

    user = User.query.get(session['user_id'])
    if user is None:
        session.clear()
        return jsonify({"error": "Sesión inválida. Vuelve a iniciar sesión."}), 401

    item = SHOP_ITEMS.get(item_id)
    if item is None:
        return jsonify({"error": "Objeto no encontrado"}), 404

    if user.owns_item(item_id):
        return jsonify({"error": "Ya tienes este objeto"}), 400

    if user.balance < item['price']:
        return jsonify({"error": "No tienes suficientes puntos para este objeto"}), 400

    user.balance -= item['price']
    db.session.add(Purchase(user_id=user.id, item_id=item_id))

    # Si es un banner o una tipografía y el usuario todavía no tiene ninguno
    # equipado de ese tipo, se lo equipa automáticamente como cortesía (para
    # que la primera compra se note altiro). Si ya tenía uno puesto, se
    # respeta su elección: puede cambiarlo cuando quiera con el botón
    # "Equipar" del objeto que ya compró. Con las canciones pasa lo mismo,
    # pero por categoría (lofi/piano/rain) en vez de un solo slot global.
    if item['type'] == 'banner' and user.equipped_banner is None:
        user.equipped_banner = item_id
    elif item['type'] == 'font' and user.equipped_font is None:
        user.equipped_font = item_id
    elif item['type'] == 'sound':
        field = f"equipped_sound_{item['category']}"
        if getattr(user, field, None) is None:
            setattr(user, field, item_id)

    db.session.commit()

    return jsonify({
        "success": True,
        "new_balance": user.balance,
        "equipped_banner": user.equipped_banner,
        "equipped_font": user.equipped_font,
        "equipped_sounds": {
            "lofi": user.equipped_sound_lofi,
            "piano": user.equipped_sound_piano,
            "rain": user.equipped_sound_rain
        }
    })


@app.route('/shop/equip/<item_id>', methods=['POST'])
def equip_item(item_id):
    if 'user_id' not in session:
        return jsonify({"error": "No autorizado"}), 401

    user = User.query.get(session['user_id'])
    if user is None:
        session.clear()
        return jsonify({"error": "Sesión inválida. Vuelve a iniciar sesión."}), 401

    item = SHOP_ITEMS.get(item_id)
    if item is None:
        return jsonify({"error": "Objeto no encontrado"}), 404

    if item['type'] not in ('banner', 'font', 'sound'):
        return jsonify({"error": "Este objeto no se puede equipar"}), 400

    if not user.owns_item(item_id):
        return jsonify({"error": "No tienes este objeto"}), 403

    if item['type'] == 'banner':
        user.equipped_banner = item_id
    elif item['type'] == 'font':
        user.equipped_font = item_id
    else:
        setattr(user, f"equipped_sound_{item['category']}", item_id)
    db.session.commit()

    return jsonify({
        "success": True,
        "equipped_banner": user.equipped_banner,
        "equipped_font": user.equipped_font,
        "equipped_sounds": {
            "lofi": user.equipped_sound_lofi,
            "piano": user.equipped_sound_piano,
            "rain": user.equipped_sound_rain
        }
    })


@app.route('/shop/unequip', methods=['POST'])
def unequip_banner():
    if 'user_id' not in session:
        return jsonify({"error": "No autorizado"}), 401

    user = User.query.get(session['user_id'])
    if user is None:
        session.clear()
        return jsonify({"error": "Sesión inválida. Vuelve a iniciar sesión."}), 401

    user.equipped_banner = None
    db.session.commit()

    return jsonify({"success": True, "equipped_banner": None})


@app.route('/shop/unequip_font', methods=['POST'])
def unequip_font():
    if 'user_id' not in session:
        return jsonify({"error": "No autorizado"}), 401

    user = User.query.get(session['user_id'])
    if user is None:
        session.clear()
        return jsonify({"error": "Sesión inválida. Vuelve a iniciar sesión."}), 401

    user.equipped_font = None
    db.session.commit()

    return jsonify({"success": True, "equipped_font": None})


@app.route('/shop/unequip_sound/<category>', methods=['POST'])
def unequip_sound(category):
    """Quita la canción comprada equipada en una categoría (lofi/piano/rain)
    y vuelve a la pista gratuita de siempre para esa categoría."""
    if 'user_id' not in session:
        return jsonify({"error": "No autorizado"}), 401

    user = User.query.get(session['user_id'])
    if user is None:
        session.clear()
        return jsonify({"error": "Sesión inválida. Vuelve a iniciar sesión."}), 401

    if category not in DEFAULT_TRACKS:
        return jsonify({"error": "Categoría inválida"}), 400

    setattr(user, f'equipped_sound_{category}', None)
    db.session.commit()

    return jsonify({"success": True, "category": category})


@app.route('/logros')
def logros():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    user = User.query.get(session['user_id'])
    if user is None:
        session.clear()
        return redirect(url_for('login'))

    claimed_ids = {c.achievement_id for c in user.achievement_claims.all()}
    # Se arma la lista con el estado de cada logro ya resuelto (desbloqueado
    # y/o canjeado) para que la plantilla no tenga que llamar funciones.
    achievements = [
        {
            'id': ach['id'],
            'name': ach['name'],
            'description': ach['description'],
            'points': ach['points'],
            'icon': ach['icon'],
            'unlocked': ach['check'](user),
            'claimed': ach['id'] in claimed_ids
        }
        for ach in ACHIEVEMENTS
    ]

    return render_template('logros.html', user=user, achievements=achievements)


@app.route('/logros/canjear/<achievement_id>', methods=['POST'])
def canjear_logro(achievement_id):
    if 'user_id' not in session:
        return jsonify({"error": "No autorizado"}), 401

    user = User.query.get(session['user_id'])
    if user is None:
        session.clear()
        return jsonify({"error": "Sesión inválida. Vuelve a iniciar sesión."}), 401

    achievement = next((a for a in ACHIEVEMENTS if a['id'] == achievement_id), None)
    if achievement is None:
        return jsonify({"error": "Logro no encontrado"}), 404

    if user.has_claimed_achievement(achievement_id):
        return jsonify({"error": "Ya canjeaste este logro"}), 400

    if not achievement['check'](user):
        return jsonify({"error": "Todavía no cumples los requisitos de este logro"}), 403

    # Solo se acreditan al saldo gastable de la tienda, igual que los puntos
    # ganados estudiando (ver /add_points); el total/rango no se ve afectado.
    user.balance += achievement['points']
    db.session.add(AchievementClaim(user_id=user.id, achievement_id=achievement_id))
    db.session.commit()

    return jsonify({
        "success": True,
        "new_balance": user.balance,
        "points_awarded": achievement['points']
    })


@app.route('/exam_dates', methods=['GET'])
def get_exam_dates():
    if 'user_id' not in session:
        return jsonify({"error": "No autorizado"}), 401

    user = User.query.get(session['user_id'])
    if user is None:
        session.clear()
        return jsonify({"error": "Sesión inválida. Vuelve a iniciar sesión."}), 401

    exams = ExamDate.query.filter_by(user_id=user.id).order_by(ExamDate.exam_date.asc()).all()
    return jsonify({
        "exam_dates": [
            {"id": e.id, "title": e.title, "date": e.exam_date.isoformat()}
            for e in exams
        ]
    })


@app.route('/exam_dates', methods=['POST'])
def create_exam_date():
    if 'user_id' not in session:
        return jsonify({"error": "No autorizado"}), 401

    user = User.query.get(session['user_id'])
    if user is None:
        session.clear()
        return jsonify({"error": "Sesión inválida. Vuelve a iniciar sesión."}), 401

    data = request.get_json() or {}
    title = (data.get('title') or '').strip()
    date_str = (data.get('date') or '').strip()

    if not title:
        return jsonify({"error": "El nombre de la prueba no puede estar vacío."}), 400
    if len(title) > 100:
        return jsonify({"error": "El nombre es demasiado largo (máx. 100 caracteres)."}), 400

    try:
        parsed_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return jsonify({"error": "Fecha inválida."}), 400

    exam = ExamDate(user_id=user.id, title=title, exam_date=parsed_date)
    db.session.add(exam)
    db.session.commit()

    return jsonify({
        "success": True,
        "exam_date": {"id": exam.id, "title": exam.title, "date": exam.exam_date.isoformat()}
    })


@app.route('/exam_dates/<int:exam_id>', methods=['DELETE'])
def delete_exam_date(exam_id):
    if 'user_id' not in session:
        return jsonify({"error": "No autorizado"}), 401

    user = User.query.get(session['user_id'])
    if user is None:
        session.clear()
        return jsonify({"error": "Sesión inválida. Vuelve a iniciar sesión."}), 401

    exam = ExamDate.query.get(exam_id)
    if exam is None or exam.user_id != user.id:
        return jsonify({"error": "Fecha no encontrada"}), 404

    db.session.delete(exam)
    db.session.commit()

    return jsonify({"success": True})


MONTH_NAMES_ES = ['Ene', 'Feb', 'Mar', 'Abr', 'May', 'Jun', 'Jul', 'Ago', 'Sep', 'Oct', 'Nov', 'Dic']


def _months_back(base, n):
    """Devuelve (año, mes) que quedan 'n' meses atrás de la fecha base."""
    total = base.year * 12 + (base.month - 1) - n
    y, m = divmod(total, 12)
    return y, m + 1


def compute_streaks_for_users(user_ids, offset):
    """Calcula la racha de días consecutivos (60+ min de estudio) para varios
    usuarios a la vez, con UNA sola consulta a la base de datos en vez de
    una por usuario (se usa en el top de usuarios, que puede mostrar hasta
    50 a la vez). Misma lógica que streak_info(), pero en lote."""
    if not user_ids:
        return {}

    today_local = (datetime.utcnow() - timedelta(minutes=offset)).date()
    window_start_local = datetime(today_local.year, today_local.month, today_local.day) - timedelta(days=400)
    window_start_utc = window_start_local + timedelta(minutes=offset)

    sessions = StudySession.query.filter(
        StudySession.user_id.in_(user_ids),
        StudySession.completed_at >= window_start_utc
    ).all()

    daily_minutes_by_user = defaultdict(lambda: defaultdict(int))
    for s in sessions:
        local_dt = s.completed_at - timedelta(minutes=offset)
        daily_minutes_by_user[s.user_id][local_dt.date()] += s.minutes

    streaks = {}
    for uid in user_ids:
        daily_minutes = daily_minutes_by_user.get(uid, {})
        cursor = today_local
        if daily_minutes.get(cursor, 0) < 60:
            cursor -= timedelta(days=1)
        streak = 0
        while daily_minutes.get(cursor, 0) >= 60:
            streak += 1
            cursor -= timedelta(days=1)
        streaks[uid] = streak

    return streaks


def get_tz_offset():
    """Minutos de diferencia entre UTC y la hora local del navegador,
    tal cual los entrega JS con Date.getTimezoneOffset() (ej. Chile
    continental es +180 o +240 según horario de verano). El servidor
    guarda todo en UTC, así que sin este ajuste "hoy" queda calculado en
    UTC y puede ir un día adelantado respecto al usuario (ej. de noche
    en Chile, en UTC ya es el día siguiente)."""
    try:
        return int(request.args.get('tz_offset', 0))
    except (TypeError, ValueError):
        return 0


@app.route('/productivity_stats')
def productivity_stats():
    if 'user_id' not in session:
        return jsonify({"error": "No autorizado"}), 401

    user = User.query.get(session['user_id'])
    if user is None:
        session.clear()
        return jsonify({"error": "Sesión inválida. Vuelve a iniciar sesión."}), 401

    offset = get_tz_offset()
    now = datetime.utcnow() - timedelta(minutes=offset)  # "ahora" en la hora local del usuario

    # --- Serie diaria: últimos 14 días ---
    daily_start_local = (now - timedelta(days=13)).replace(hour=0, minute=0, second=0, microsecond=0)
    daily_start_utc = daily_start_local + timedelta(minutes=offset)

    daily_minutes = defaultdict(int)
    for s in StudySession.query.filter(StudySession.user_id == user.id, StudySession.completed_at >= daily_start_utc).all():
        local_dt = s.completed_at - timedelta(minutes=offset)
        daily_minutes[local_dt.date().isoformat()] += s.minutes

    daily_series = []
    for i in range(13, -1, -1):
        day = (now - timedelta(days=i)).date()
        daily_series.append({"label": day.strftime('%d/%m'), "minutes": daily_minutes.get(day.isoformat(), 0)})

    # --- Serie semanal: últimas 8 semanas ---
    weekly_start_local = now - timedelta(weeks=8)
    weekly_start_utc = weekly_start_local + timedelta(minutes=offset)
    weekly_minutes = defaultdict(int)
    for s in StudySession.query.filter(StudySession.user_id == user.id, StudySession.completed_at >= weekly_start_utc).all():
        local_dt = s.completed_at - timedelta(minutes=offset)
        y, w, _ = local_dt.isocalendar()
        weekly_minutes[f"{y}-W{w:02d}"] += s.minutes

    weekly_series = []
    for i in range(7, -1, -1):
        wdate = now - timedelta(weeks=i)
        y, w, _ = wdate.isocalendar()
        weekly_series.append({"label": f"Sem {w}", "minutes": weekly_minutes.get(f"{y}-W{w:02d}", 0)})

    # --- Serie mensual: últimos 6 meses ---
    earliest_year, earliest_month = _months_back(now, 5)
    monthly_start_utc = datetime(earliest_year, earliest_month, 1) + timedelta(minutes=offset)
    monthly_minutes = defaultdict(int)
    for s in StudySession.query.filter(StudySession.user_id == user.id, StudySession.completed_at >= monthly_start_utc).all():
        local_dt = s.completed_at - timedelta(minutes=offset)
        monthly_minutes[f"{local_dt.year}-{local_dt.month:02d}"] += s.minutes

    monthly_series = []
    for i in range(5, -1, -1):
        y, m = _months_back(now, i)
        monthly_series.append({"label": MONTH_NAMES_ES[m - 1], "minutes": monthly_minutes.get(f"{y}-{m:02d}", 0)})

    # --- Por carpeta (categoría): tiempo total invertido, de siempre ---
    folders = user.folders.order_by(Folder.points.desc()).limit(10).all()
    by_folder = [{"name": f.name, "minutes": f.points} for f in folders if f.points > 0]

    # --- Total de esta semana (lunes a hoy, en hora local) ---
    week_start_local = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    week_start_utc = week_start_local + timedelta(minutes=offset)
    week_total_minutes = db.session.query(db.func.coalesce(db.func.sum(StudySession.minutes), 0)).filter(
        StudySession.user_id == user.id,
        StudySession.completed_at >= week_start_utc
    ).scalar()

    return jsonify({
        "daily": daily_series,
        "weekly": weekly_series,
        "monthly": monthly_series,
        "by_folder": by_folder,
        "week_total_minutes": week_total_minutes
    })


@app.route('/calendar_stats')
def calendar_stats():
    """Devuelve, para un mes dado (en la hora local del usuario), qué días
    completó al menos un bloque de estudio, con cuántos bloques y minutos
    totales ese día."""
    if 'user_id' not in session:
        return jsonify({"error": "No autorizado"}), 401

    user = User.query.get(session['user_id'])
    if user is None:
        session.clear()
        return jsonify({"error": "Sesión inválida. Vuelve a iniciar sesión."}), 401

    offset = get_tz_offset()
    now = datetime.utcnow() - timedelta(minutes=offset)

    try:
        year = int(request.args.get('year', now.year))
        month = int(request.args.get('month', now.month))
    except (TypeError, ValueError):
        return jsonify({"error": "Parámetros inválidos"}), 400

    if month < 1 or month > 12:
        return jsonify({"error": "Mes inválido"}), 400

    start_local = datetime(year, month, 1)
    end_local = datetime(year + 1, 1, 1) if month == 12 else datetime(year, month + 1, 1)
    start_utc = start_local + timedelta(minutes=offset)
    end_utc = end_local + timedelta(minutes=offset)

    days = defaultdict(lambda: {"blocks": 0, "minutes": 0, "by_folder": defaultdict(int)})
    sessions = StudySession.query.filter(
        StudySession.user_id == user.id,
        StudySession.completed_at >= start_utc,
        StudySession.completed_at < end_utc
    ).all()

    folder_ids = {s.folder_id for s in sessions if s.folder_id is not None}
    folder_names = {
        f.id: f.name for f in Folder.query.filter(Folder.id.in_(folder_ids)).all()
    } if folder_ids else {}

    for s in sessions:
        local_dt = s.completed_at - timedelta(minutes=offset)
        key = local_dt.date().isoformat()
        days[key]["blocks"] += 1
        days[key]["minutes"] += s.minutes
        folder_name = folder_names.get(s.folder_id, "Sin carpeta")
        days[key]["by_folder"][folder_name] += s.minutes

    # Se aplana el desglose por carpeta a una lista (ordenada de mayor a
    # menor tiempo) para que el frontend no tenga que lidiar con dicts
    # anidados al armar las píldoras "MAT = 2h".
    days_out = {}
    for key, info in days.items():
        by_folder = sorted(
            ({"name": name, "minutes": minutes} for name, minutes in info["by_folder"].items()),
            key=lambda f: f["minutes"],
            reverse=True
        )
        days_out[key] = {"blocks": info["blocks"], "minutes": info["minutes"], "by_folder": by_folder}

    return jsonify({"year": year, "month": month, "days": days_out})


@app.route('/streak_info')
def streak_info():
    """Racha de días consecutivos con 60+ min de estudio, en la hora local
    de quien mira la página (ver get_tz_offset). Si el día de hoy todavía
    no llega a 60 min no se rompe la racha -el día no ha terminado-, se
    sigue contando desde ayer hacia atrás.

    Se puede pedir la racha de cualquier usuario con ?username=, para
    poder mostrarla también al visitar el perfil de otra persona."""
    if 'user_id' not in session:
        return jsonify({"error": "No autorizado"}), 401

    viewer = User.query.get(session['user_id'])
    if viewer is None:
        session.clear()
        return jsonify({"error": "Sesión inválida. Vuelve a iniciar sesión."}), 401

    username = request.args.get('username')
    if username:
        target = User.query.filter_by(username=username).first()
        if target is None:
            return jsonify({"error": "Usuario no encontrado"}), 404
    else:
        target = viewer

    offset = get_tz_offset()
    streak = compute_streaks_for_users([target.id], offset)[target.id]

    return jsonify({"streak": streak})


@app.route('/search_users')
def search_users():
    if 'user_id' not in session:
        return jsonify({"error": "No autorizado"}), 401

    current_user = User.query.get(session['user_id'])
    if current_user is None:
        session.clear()
        return jsonify({"error": "Sesión inválida. Vuelve a iniciar sesión."}), 401

    query = request.args.get('q', '').strip()
    if not query:
        return jsonify({"results": []})

    users = User.query.filter(
        User.username.ilike(f'%{query}%'),
        User.id != session['user_id']
    ).order_by(User.points.desc()).limit(20).all()

    my_following_ids = {f.followed_id for f in current_user.following.all()}

    results = [
        {
            "username": u.username,
            "pomodoros": u.pomodoros,
            "profile_pic": u.profile_pic,
            "is_following": u.id in my_following_ids
        }
        for u in users
    ]
    return jsonify({"results": results})


@app.route('/leaderboard')
def leaderboard():
    """Top de usuarios según sus pomotates (1 pomotate = 1 hora de estudio
    acumulada). Para entrar a la lista hace falta al menos 1 pomotate."""
    if 'user_id' not in session:
        return jsonify({"error": "No autorizado"}), 401

    current_user = User.query.get(session['user_id'])
    if current_user is None:
        session.clear()
        return jsonify({"error": "Sesión inválida. Vuelve a iniciar sesión."}), 401

    top_users = User.query.filter(User.points >= 60).order_by(User.points.desc()).limit(50).all()

    offset = get_tz_offset()
    streaks = compute_streaks_for_users([u.id for u in top_users], offset)

    return jsonify({
        "leaderboard": [
            {
                "username": u.username,
                "profile_pic": u.profile_pic,
                "pomodoros": u.pomodoros,
                "streak": streaks.get(u.id, 0),
                "is_you": u.id == current_user.id
            }
            for u in top_users
        ]
    })

with app.app_context():
    db.create_all()

    # --- Migración ligera para bases de datos que ya existían antes de la tienda ---
    # db.create_all() solo crea TABLAS nuevas (como Purchase), no agrega columnas
    # nuevas a una tabla que ya existe (como 'balance' en 'user'). Esto se
    # encarga de eso de forma segura y solo una vez.
    inspector = db.inspect(db.engine)
    if 'user' in inspector.get_table_names():
        existing_columns = [col['name'] for col in inspector.get_columns('user')]

        if 'balance' not in existing_columns:
            db.session.execute(db.text('ALTER TABLE user ADD COLUMN balance INTEGER DEFAULT 0'))
            db.session.commit()
            # Los puntos que ya tenían ganados quedan disponibles para gastar en la tienda
            db.session.execute(db.text('UPDATE user SET balance = points'))
            db.session.commit()

        if 'equipped_banner' not in existing_columns:
            db.session.execute(db.text('ALTER TABLE user ADD COLUMN equipped_banner VARCHAR(50)'))
            db.session.commit()

        if 'equipped_font' not in existing_columns:
            db.session.execute(db.text('ALTER TABLE user ADD COLUMN equipped_font VARCHAR(50)'))
            db.session.commit()

        for sound_column in ('equipped_sound_lofi', 'equipped_sound_piano', 'equipped_sound_rain'):
            if sound_column not in existing_columns:
                db.session.execute(db.text(f'ALTER TABLE user ADD COLUMN {sound_column} VARCHAR(50)'))
                db.session.commit()

    # --- Reset único del logro 'Búho nocturno' (bloque_madrugada) ---
    # Por el bug de zona horaria ya corregido, algunos usuarios pudieron
    # haber canjeado este logro sin merecerlo de verdad. Se revierte
    # cualquier canje ya hecho (devolviendo el saldo otorgado) y se borra el
    # registro del canje, para que el logro quede disponible de nuevo y solo
    # se pueda ganar con sesiones de estudio reales a partir de ahora. Esto
    # es seguro de ejecutar en cada arranque: después de la primera vez ya
    # no queda ningún canje viejo que revertir, así que no vuelve a pasar
    # nada.
    old_madrugada_claims = AchievementClaim.query.filter_by(achievement_id='bloque_madrugada').all()
    if old_madrugada_claims:
        madrugada_points = next(a['points'] for a in ACHIEVEMENTS if a['id'] == 'bloque_madrugada')
        for claim in old_madrugada_claims:
            claim_user = User.query.get(claim.user_id)
            if claim_user is not None:
                claim_user.balance = max(0, claim_user.balance - madrugada_points)
            db.session.delete(claim)
        db.session.commit()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)