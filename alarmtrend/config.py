import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from dotenv import load_dotenv


@dataclass
class Settings:
    data_dir: Path
    start_date: date = date(2026, 9, 1)
    source_url: str = 'https://kyiv.digital/storage/air-alert/stats.html'
    secret_key: str = ''
    admin_password_hash: str = ''
    secure_cookie: bool = True
    interval: int = 300
    timeout: int = 30

    @property
    def database(self):
        return self.data_dir / 'alarmtrend.sqlite3'

    @classmethod
    def load(cls):
        # Both WSGI and CLI read exactly the same explicit configuration file.
        env_file = os.environ.get('ALARMTREND_ENV_FILE')
        if env_file:
            load_dotenv(env_file, override=False)
        get = lambda name, default='': os.environ.get('ALARMTREND_' + name, default)
        root = Path(__file__).resolve().parent.parent
        data = Path(get('DATA_DIR', str(root / 'instance'))).expanduser()
        if not data.is_absolute():
            raise ValueError('ALARMTREND_DATA_DIR must be an absolute path')
        if data == root / 'alarmtrend' / 'static' or root / 'alarmtrend' / 'static' in data.parents:
            raise ValueError('Database must be outside static files')
        result = cls(data, date.fromisoformat(get('START_DATE', '2026-09-01')),
                     get('SOURCE_URL', cls.source_url), get('SECRET_KEY'),
                     get('ADMIN_PASSWORD_HASH'), get('SECURE_COOKIE', 'true').lower() == 'true',
                     int(get('INTERVAL', '300')), int(get('TIMEOUT', '30')))
        if result.interval < 60 or not 1 <= result.timeout <= 120:
            raise ValueError('Invalid interval or timeout')
        return result
