import argparse
import getpass
import json
import logging
import signal
import sqlite3
import threading
import time
from pathlib import Path

from werkzeug.security import generate_password_hash

from .config import Settings
from .db import connect


def main():
    parser=argparse.ArgumentParser(description='AlarmTrend — вебзастосунок і незалежна синхронізація')
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('init-db')
    sub.add_parser('sync')
    sub.add_parser('worker')
    sub.add_parser('evaluate')
    sub.add_parser('password-hash')
    backup=sub.add_parser('backup')
    backup.add_argument('destination',type=Path)
    serve=sub.add_parser('serve')
    serve.add_argument('--port',type=int,default=5000)
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
        from .sync import synchronize
        stop=threading.Event()
        for sig in (signal.SIGINT,signal.SIGTERM):
            signal.signal(sig,lambda *_:stop.set())
        # Separate CLI process, never started or imported by WSGI.
        while not stop.is_set():
            began=time.monotonic()
            try:
                synchronize(settings)
            except Exception:
                logging.exception('Unexpected worker failure; next attempt follows')
            stop.wait(max(1,settings.interval-(time.monotonic()-began)))
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
        create_app(settings).run(host='127.0.0.1',port=args.port,debug=False)


if __name__=='__main__':
    main()
