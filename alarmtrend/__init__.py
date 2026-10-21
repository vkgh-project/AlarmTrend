import hmac
import json
import secrets
import time
from datetime import timedelta
from functools import wraps

from filelock import FileLock, Timeout
from flask import Flask, abort, flash, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

from .config import Settings
from .db import audit, connect, dt, iso, utcnow
from .source import KYIV, local_time


def create_app(settings=None):
    settings = settings or Settings.load()
    app = Flask(__name__)
    app.config.update(SECRET_KEY=settings.secret_key or secrets.token_hex(32),
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Strict',
                      SESSION_COOKIE_SECURE=settings.secure_cookie,
                      PERMANENT_SESSION_LIFETIME=timedelta(hours=2), MAX_CONTENT_LENGTH=16384)
    app.config['SETTINGS'] = settings
    with connect(settings):
        pass

    @app.template_filter('kyiv')
    def kyiv(value):
        return dt(value).astimezone(KYIV).strftime('%d.%m %H:%M') if value else 'ще не було'

    @app.context_processor
    def csrf_context():
        if 'csrf' not in session:
            session['csrf'] = secrets.token_urlsafe(32)
        return {'csrf': session['csrf']}

    @app.before_request
    def csrf_check():
        if request.method == 'POST' and not hmac.compare_digest(session.get('csrf','!'), request.form.get('csrf','')):
            abort(400, 'Невірний токен форми; оновіть сторінку')

    @app.after_request
    def headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Content-Security-Policy'] = "default-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'; form-action 'self'"
        if request.path != '/static/style.css':
            response.headers['Cache-Control'] = 'no-store'
        return response

    def read_state():
        with connect(settings) as db:
            row = db.execute('SELECT payload FROM forecasts ORDER BY id DESC LIMIT 1').fetchone()
            if row:
                data = json.loads(row[0])
            else:
                from .model import snapshot
                data = snapshot(db,settings,utcnow())
            last_run = db.execute('SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1').fetchone()
            data['stale'] = not data['last_success'] or (utcnow()-dt(data['last_success'])).total_seconds() > settings.interval*2
            data['sync_error'] = bool(last_run and not last_run['ok'])
            data['forecast_stale'] = (utcnow()-dt(data['at'])).total_seconds() > settings.interval*2
            return data

    @app.get('/')
    def index():
        return render_template('index.html', d=read_state())

    @app.get('/api/status')
    def status():
        return jsonify(read_state())

    def admin_required(fn):
        @wraps(fn)
        def wrapped(*args,**kwargs):
            if not settings.secret_key or not settings.admin_password_hash:
                abort(503, 'Адміністрування потребує SECRET_KEY і ADMIN_PASSWORD_HASH у конфігурації')
            if not session.get('admin'):
                return redirect(url_for('login'))
            return fn(*args,**kwargs)
        return wrapped

    @app.route('/login',methods=['GET','POST'])
    def login():
        if not settings.secret_key or not settings.admin_password_hash:
            abort(503,'Задайте секрет і хеш пароля адміністратора')
        if request.method=='POST':
            address = request.remote_addr or 'unknown'
            with connect(settings) as db:
                db.execute('DELETE FROM login_attempts WHERE at<?',(time.time()-900,))
                count = db.execute('SELECT count(*) FROM login_attempts WHERE address=?',(address,)).fetchone()[0]
                if count >= 5:
                    abort(429,'Забагато спроб. Спробуйте за 15 хвилин.')
                valid = check_password_hash(settings.admin_password_hash,request.form.get('password',''))
                if not valid:
                    db.execute('INSERT INTO login_attempts VALUES (?,?)',(time.time(),address))
            if valid:
                session.clear()
                session.update(admin=True,csrf=secrets.token_urlsafe(32))
                session.permanent=True
                return redirect(url_for('manual'))
            flash('Невірний пароль')
        return render_template('login.html')

    @app.post('/logout')
    def logout():
        session.clear()
        return redirect(url_for('index'))

    @app.route('/manual',methods=['GET','POST'])
    @admin_required
    def manual():
        if request.method=='POST':
            from .data import rebuild
            from .sync import save_forecast
            try:
                with FileLock(str(settings.data_dir/'sync.lock'),timeout=3):
                    with connect(settings) as db:
                        now=utcnow()
                        action=request.form.get('action')
                        if action=='cancel':
                            row=db.execute("SELECT * FROM events WHERE id=? AND origin='manual'",(request.form.get('id'),)).fetchone()
                            if not row:
                                raise ValueError('Ручний запис не знайдено')
                            db.execute('UPDATE events SET enabled=0 WHERE id=?',(row['id'],))
                            audit(db,'manual_cancel',dict(row))
                        else:
                            kind=request.form.get('kind','start')
                            if kind not in ('start','end'):
                                raise ValueError('Невідомий тип')
                            stamp=now if action=='now' else local_time(request.form.get('stamp',''))
                            if stamp>now:
                                raise ValueError('Не можна додати подію з майбутнього')
                            if stamp.astimezone(KYIV).date()<settings.start_date:
                                raise ValueError('Дата перед початком періоду')
                            old_id=request.form.get('id')
                            if old_id:
                                old=db.execute("SELECT * FROM events WHERE id=? AND origin='manual'",(old_id,)).fetchone()
                                if not old:
                                    raise ValueError('Виправляти можна лише ручні записи')
                                db.execute('UPDATE events SET enabled=0 WHERE id=?',(old_id,))
                                audit(db,'manual_replaced',dict(old))
                            db.execute("INSERT INTO events(stamp,kind,origin,precision) VALUES (?,?,'manual','second') "
                                       'ON CONFLICT(stamp,kind,origin) DO UPDATE SET enabled=1', (iso(stamp),kind))
                            audit(db,'manual_save',{'stamp':iso(stamp),'kind':kind})
                        rebuild(db,settings,now)
                        save_forecast(db,settings,now)
                flash('Збережено. Статус і оцінки перераховано.')
                return redirect(url_for('manual'))
            except (ValueError,Timeout) as exc:
                flash(str(exc) if not isinstance(exc,Timeout) else 'Триває синхронізація. Повторіть за хвилину.')
        with connect(settings) as db:
            records=db.execute("SELECT * FROM events WHERE origin='manual' ORDER BY id DESC LIMIT 30").fetchall()
        return render_template('manual.html',records=records)

    return app
