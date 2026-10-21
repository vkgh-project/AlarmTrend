import json
import logging

from filelock import FileLock, Timeout

from . import source
from .data import import_events, midnight, rebuild
from .db import connect, getmeta, iso, setmeta, utcnow
from .model import VERSION, snapshot

log = logging.getLogger(__name__)


def save_forecast(db, settings, now):
    payload = snapshot(db, settings, now)
    db.execute('INSERT INTO forecasts(at,model,payload) VALUES (?,?,?)',
               (iso(now), VERSION, json.dumps(payload, ensure_ascii=False)))
    # Bound the five-minute archive to 90 days. Audit and sync history are retained.
    db.execute('DELETE FROM forecasts WHERE id < (SELECT max(id)-25920 FROM forecasts)')
    return payload


def synchronize(settings, fetcher=None, now=None):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(settings.data_dir/'sync.lock'), timeout=0)
    try:
        with lock:
            return _synchronize(settings, fetcher or source.fetch, now or utcnow())
    except Timeout:
        log.info('Synchronization skipped: another process owns sync.lock')
        return {'skipped': True}


def _synchronize(settings, fetcher, now):
    with connect(settings) as db:
        etag, modified = getmeta(db,'etag'), getmeta(db,'last_modified')
        source_config = f'{settings.source_url}|{settings.start_date}'
        if getmeta(db,'source_config') != source_config:
            etag = modified = None
    status, changed, error = None, 0, None
    try:
        response = fetcher(settings, etag, modified)
        status = response.status_code
        if status not in (200,304):
            raise ValueError(f'Unexpected HTTP {status}')
        events = source.parse(response.text, settings.start_date) if status == 200 else None
        with connect(settings) as db:
            if events is not None:
                # Protect against truncated full-history responses.
                old_count = db.execute("SELECT count(*) FROM events WHERE origin='source' AND enabled=1").fetchone()[0]
                relevant = sum(t >= midnight(settings.start_date) for t,_,_ in events)
                if old_count > 20 and relevant < old_count*.8:
                    raise ValueError('Різке скорочення історії; потрібна перевірка джерела')
                changed = import_events(db, events, settings)
                setmeta(db,'etag',response.headers.get('ETag',''))
                setmeta(db,'last_modified',response.headers.get('Last-Modified',''))
                setmeta(db,'source_config',source_config)
            elif not getmeta(db,'coverage_start'):
                raise ValueError('HTTP 304 без локальної історії')
            setmeta(db,'last_success',iso(now))
    except Exception as exc:
        error = f'{type(exc).__name__}: {exc}'
        log.warning('Source sync failed: %s', error)
    with connect(settings) as db:
        db.execute('INSERT INTO sync_runs(at,ok,http_status,changed,message) VALUES (?,?,?,?,?)',
                   (iso(now),int(error is None),status,changed,error or 'OK'))
        # Also advances the horizon on 304 and on network failure; freshness remains explicit.
        rebuild(db,settings,now)
        payload = save_forecast(db,settings,now)
    log.info('Sync HTTP=%s changed=%s success=%s',status,changed,error is None)
    return {'ok': error is None, 'changed': changed, 'error': error, 'forecast': payload}
