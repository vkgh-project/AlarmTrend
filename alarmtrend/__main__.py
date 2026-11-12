import argparse
import getpass
import json
import logging
import signal
import sqlite3
import threading
import time
import webbrowser
from pathlib import Path

from werkzeug.security import generate_password_hash

from .config import Settings
from .db import connect


def run_worker(settings, stop, wait_first=False):
    from .sync import synchronize

    if wait_first and stop.wait(settings.interval):
        return
    while not stop.is_set():
        began=time.monotonic()
        try:
            synchronize(settings)
        except Exception:
            logging.exception('Unexpected worker failure; next attempt follows')
        stop.wait(max(1,settings.interval-(time.monotonic()-began)))


def main():
    parser=argparse.ArgumentParser(description='AlarmTrend — вебзастосунок і незалежна синхронізація')
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('init-db')
    sub.add_parser('sync')
    worker_parser=sub.add_parser('worker')
    worker_parser.add_argument('--train',action='store_true',help='Обробляти чергу навчання в окремому потоці')
    sub.add_parser('train')
    sub.add_parser('trainer')
    sub.add_parser('predict')
    report_parser=sub.add_parser('report')
    report_parser.add_argument('--directory',type=Path,default=Path('docs'))
    sub.add_parser('evaluate')
    sub.add_parser('password-hash')
    backup=sub.add_parser('backup')
    backup.add_argument('destination',type=Path)
    serve=sub.add_parser('serve')
    serve.add_argument('--port',type=int,default=5000)
    serve.add_argument('--no-sync',action='store_true',
                       help='Не оновлювати джерело (якщо worker запущений окремо)')
    desktop=sub.add_parser('desktop',help='Запустити локальну панель і відкрити її у браузері')
    desktop.add_argument('--port',type=int,default=5000)
    desktop.add_argument('--no-sync',action='store_true')
    args=parser.parse_args()
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    if args.command=='password-hash':
        password=getpass.getpass('Пароль адміністратора: ')
        if len(password)<12:
            parser.error('Використайте пароль від 12 символів')
        print(generate_password_hash(password))
        return
    settings=Settings.load()
    if args.command=='init-db':
        with connect(settings):
            pass
        print(settings.database)
    elif args.command=='sync':
        from .sync import synchronize
        result=synchronize(settings)
        print(json.dumps({k:v for k,v in result.items() if k!='forecast'},ensure_ascii=False))
        if result.get('ok') is False:
            raise SystemExit(1)
    elif args.command=='worker':
        from .sync import run_forecaster
        stop=threading.Event()
        for sig in (signal.SIGINT,signal.SIGTERM):
            signal.signal(sig,lambda *_:stop.set())
        # Separate CLI process, never started or imported by WSGI.
        trainer=None
        forecaster=threading.Thread(target=run_forecaster,args=(settings,stop),daemon=True,name='alarmtrend-forecast')
        forecaster.start()
        if args.train:
            from .training import run_trainer
            trainer=threading.Thread(target=run_trainer,args=(settings,stop),daemon=True,name='alarmtrend-train')
            trainer.start()
        try:
            run_worker(settings,stop)
        finally:
            stop.set()
            if trainer:
                trainer.join(timeout=1)
            forecaster.join(timeout=1)
    elif args.command=='trainer':
        from .training import run_trainer
        stop=threading.Event()
        for sig in (signal.SIGINT,signal.SIGTERM):
            signal.signal(sig,lambda *_:stop.set())
        run_trainer(settings,stop)
    elif args.command=='train':
        from .training import train
        print(json.dumps(train(settings),ensure_ascii=False,allow_nan=False))
    elif args.command=='report':
        from .reporting import export_report
        print(export_report(settings,args.directory))
    elif args.command=='predict':
        from filelock import FileLock
        from .data import rebuild
        from .db import utcnow
        from .sync import save_forecast
        settings.data_dir.mkdir(parents=True,exist_ok=True)
        with FileLock(str(settings.data_dir/'sync.lock'),timeout=0):
            with connect(settings) as db:
                now=utcnow()
                rebuild(db,settings,now)
                payload=save_forecast(db,settings,now)
                from .db import setmeta
                setmeta(db,'forecast_requested','')
        print(json.dumps(payload,ensure_ascii=False,allow_nan=False))
    elif args.command=='backup':
        if not settings.database.exists():
            parser.error('Базу ще не створено')
        destination=args.destination.expanduser().resolve()
        if destination==settings.database.resolve() or destination.exists():
            parser.error('Вкажіть новий файл резервної копії')
        destination.parent.mkdir(parents=True,exist_ok=True)
        # SQLite online backup creates a consistent snapshot during live updates.
        with sqlite3.connect(settings.database) as source, sqlite3.connect(destination) as target:
            source.backup(target)
            if target.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
                raise RuntimeError('Backup integrity check failed')
        print(destination)
    elif args.command=='evaluate':
        from .evaluation import evaluate
        print(json.dumps(evaluate(settings),ensure_ascii=False,indent=2))
    else:
        from . import create_app
        app=create_app(settings)
        stop=threading.Event()
        worker=None
        forecaster=None
        if not args.no_sync:
            from .sync import synchronize, run_forecaster
            # Complete the first attempt before serving the dashboard. A source
            # failure preserves cached data and is retried by the worker.
            synchronize(settings)
            worker=threading.Thread(target=run_worker,args=(settings,stop),
                                    kwargs={'wait_first':True},daemon=True,
                                    name='alarmtrend-sync')
            worker.start()
            forecaster=threading.Thread(target=run_forecaster,args=(settings,stop),daemon=True,name='alarmtrend-forecast')
            forecaster.start()
        try:
            if args.command=='desktop':
                threading.Timer(1.0,webbrowser.open,(f'http://127.0.0.1:{args.port}',)).start()
            app.run(host='127.0.0.1',port=args.port,debug=False,use_reloader=False)
        finally:
            stop.set()
            if worker is not None:
                worker.join(timeout=1)
            if forecaster is not None:
                forecaster.join(timeout=1)


if __name__=='__main__':
    main()
