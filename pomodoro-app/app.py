import os
import uuid
from datetime import datetime, timedelta
from collections import defaultdict
from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from flask_sqlalchemy import SQLAlchemy
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

# --- Sistema de rangos según puntaje, con temática de materiales ---
RANKS = [
    {'min': 1200, 'key': 'diamante', 'name': 'Dios',       'material': 'Diamante', 'icon': '💎'},
    {'min': 600,  'key': 'oro',      'name': 'Experto',    'material': 'Oro',      'icon': '🥇'},
    {'min': 360,  'key': 'hierro',   'name': 'Avanzado',   'material': 'Hierro',   'icon': '⚙️'},
    {'min': 120,  'key': 'piedra',   'name': 'Intermedio', 'material': 'Piedra',   'icon': '🪨'},
    {'min': 0,    'key': 'madera',   'name': 'Novato',     'material': 'Madera',   'icon': '🪵'},
]

def get_rank_info(points):
    for rank in RANKS:
        if points >= rank['min']:
            return rank
    return RANKS[-1]

db = SQLAlchemy(app)

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


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    points = db.Column(db.Integer, default=0)
    profile_pic = db.Column(db.String(300), default='https://i.imgur.com/6VBx3io.png') # Avatar por defecto

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

    rank = get_rank_info(user.points)
    return render_template('index.html', user=user, rank=rank)

def render_profile_page(viewer, profile_user, error=None):
    """Arma el contexto compartido por /profile y /profile/<username>."""
    is_own = viewer.id == profile_user.id

    followers = [f.follower for f in profile_user.followers.order_by(Follow.created_at.desc()).all()]
    following = [f.followed for f in profile_user.following.order_by(Follow.created_at.desc()).all()]

    # IDs que el usuario que está mirando la página (viewer) ya sigue,
    # para poder pintar el botón correcto (Seguir / Siguiendo) en cada fila de las listas.
    viewer_following_ids = {f.followed_id for f in viewer.following.all()}

    return render_template(
        'profile.html',
        user=viewer,
        profile_user=profile_user,
        is_own=is_own,
        is_following_profile=viewer.is_following(profile_user),
        followers=followers,
        following=following,
        following_ids=viewer_following_ids,
        rank=get_rank_info(profile_user.points),
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

    return jsonify({"success": True, "new_total": user.points})


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
    # la suma de las carpetas siempre sea igual al total.
    user.points += points_earned
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
        "folder": {"id": folder.id, "name": folder.name, "points": folder.points}
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


@app.route('/productivity_stats')
def productivity_stats():
    if 'user_id' not in session:
        return jsonify({"error": "No autorizado"}), 401

    user = User.query.get(session['user_id'])
    if user is None:
        session.clear()
        return jsonify({"error": "Sesión inválida. Vuelve a iniciar sesión."}), 401

    now = datetime.utcnow()

    # --- Serie diaria: últimos 14 días ---
    daily_start = (now - timedelta(days=13)).replace(hour=0, minute=0, second=0, microsecond=0)
    daily_minutes = defaultdict(int)
    for s in StudySession.query.filter(StudySession.user_id == user.id, StudySession.completed_at >= daily_start).all():
        daily_minutes[s.completed_at.date().isoformat()] += s.minutes

    daily_series = []
    for i in range(13, -1, -1):
        day = (now - timedelta(days=i)).date()
        daily_series.append({"label": day.strftime('%d/%m'), "minutes": daily_minutes.get(day.isoformat(), 0)})

    # --- Serie semanal: últimas 8 semanas ---
    weekly_start = now - timedelta(weeks=8)
    weekly_minutes = defaultdict(int)
    for s in StudySession.query.filter(StudySession.user_id == user.id, StudySession.completed_at >= weekly_start).all():
        y, w, _ = s.completed_at.isocalendar()
        weekly_minutes[f"{y}-W{w:02d}"] += s.minutes

    weekly_series = []
    for i in range(7, -1, -1):
        wdate = now - timedelta(weeks=i)
        y, w, _ = wdate.isocalendar()
        weekly_series.append({"label": f"Sem {w}", "minutes": weekly_minutes.get(f"{y}-W{w:02d}", 0)})

    # --- Serie mensual: últimos 6 meses ---
    earliest_year, earliest_month = _months_back(now, 5)
    monthly_minutes = defaultdict(int)
    for s in StudySession.query.filter(StudySession.user_id == user.id, StudySession.completed_at >= datetime(earliest_year, earliest_month, 1)).all():
        monthly_minutes[f"{s.completed_at.year}-{s.completed_at.month:02d}"] += s.minutes

    monthly_series = []
    for i in range(5, -1, -1):
        y, m = _months_back(now, i)
        monthly_series.append({"label": MONTH_NAMES_ES[m - 1], "minutes": monthly_minutes.get(f"{y}-{m:02d}", 0)})

    # --- Por carpeta (categoría): tiempo total invertido, de siempre ---
    folders = user.folders.order_by(Folder.points.desc()).limit(10).all()
    by_folder = [{"name": f.name, "minutes": f.points} for f in folders if f.points > 0]

    return jsonify({
        "daily": daily_series,
        "weekly": weekly_series,
        "monthly": monthly_series,
        "by_folder": by_folder
    })


@app.route('/calendar_stats')
def calendar_stats():
    """Devuelve, para un mes dado, qué días el usuario completó al menos
    un bloque de estudio, con cuántos bloques y minutos totales ese día."""
    if 'user_id' not in session:
        return jsonify({"error": "No autorizado"}), 401

    user = User.query.get(session['user_id'])
    if user is None:
        session.clear()
        return jsonify({"error": "Sesión inválida. Vuelve a iniciar sesión."}), 401

    now = datetime.utcnow()
    try:
        year = int(request.args.get('year', now.year))
        month = int(request.args.get('month', now.month))
    except (TypeError, ValueError):
        return jsonify({"error": "Parámetros inválidos"}), 400

    if month < 1 or month > 12:
        return jsonify({"error": "Mes inválido"}), 400

    start = datetime(year, month, 1)
    end = datetime(year + 1, 1, 1) if month == 12 else datetime(year, month + 1, 1)

    days = defaultdict(lambda: {"blocks": 0, "minutes": 0})
    sessions = StudySession.query.filter(
        StudySession.user_id == user.id,
        StudySession.completed_at >= start,
        StudySession.completed_at < end
    ).all()
    for s in sessions:
        key = s.completed_at.date().isoformat()
        days[key]["blocks"] += 1
        days[key]["minutes"] += s.minutes

    return jsonify({"year": year, "month": month, "days": days})


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
            "points": u.points,
            "profile_pic": u.profile_pic,
            "is_following": u.id in my_following_ids
        }
        for u in users
    ]
    return jsonify({"results": results})

with app.app_context():
    db.create_all()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)