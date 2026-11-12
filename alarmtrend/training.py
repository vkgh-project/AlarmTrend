"""Persistent training queue and atomic, versioned model artifacts."""
import hashlib
import json
import logging
import os
import tempfile
import time
import uuid

from filelock import FileLock, Timeout

from .db import connect, dt, getmeta, iso, setmeta, utcnow
from .source import KYIV

VERSION = 'alarmtrend-components-v2'
log = logging.getLogger(__name__)


def signature(db, settings, cutoff=None):
    rows = []
    for r in db.execute('SELECT start,end,valid,origin FROM intervals ORDER BY start'):
        if cutoff and dt(r['start']) >= cutoff:
            continue
        end = r['end'] if r['end'] and (not cutoff or dt(r['end']) <= cutoff) else None
        rows.append([r['start'], end, r['valid'], r['origin']])
    complete = [r[0] for r in db.execute('SELECT day FROM daily WHERE complete=1 ORDER BY day')
                if not cutoff or r[0] < cutoff.astimezone(KYIV).date().isoformat()]
    value = [str(settings.start_date), settings.interval, settings.source_url,
             getmeta(db, 'coverage_start'), rows, complete]
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def request_training(db, settings, now):
    fingerprint = signature(db, settings)
    # A new completed day also changes the signature, even on HTTP 304.
    row = db.execute('SELECT fingerprint FROM training_jobs WHERE id=1').fetchone()
    if not row or row[0] != fingerprint:
        db.execute("INSERT INTO training_jobs VALUES (1,?,?,'queued','') "
                   "ON CONFLICT(id) DO UPDATE SET requested_at=excluded.requested_at, "
                   "fingerprint=excluded.fingerprint,state='queued',message=''",
                   (iso(now), fingerprint))


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.model-', suffix='.tmp', delete=False) as f:
            name = f.name
            json.dump(value, f, ensure_ascii=False, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def read_artifact(settings, db=None, at=None):
    root = settings.data_dir/'models'
    for manifest in ('current.json', 'previous.json'):
        try:
            pointer = json.loads((root/manifest).read_text(encoding='utf-8'))
            name = pointer['artifact']
            if name != os.path.basename(name) or not name.endswith('.json'):
                continue
            raw = (root/name).read_bytes()
            if hashlib.sha256(raw).hexdigest() != pointer['sha256']:
                continue
            model = json.loads(raw)
            if model['version'] != VERSION or model['step_seconds'] != settings.interval:
                continue
            cutoff = dt(model['training_end'])
            if at and cutoff > at:
                continue
            if db is not None and model['history_signature'] != signature(db, settings, cutoff):
                continue
            return model
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return None


def train(settings, force=True):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    try:
        with FileLock(str(settings.data_dir/'training.lock'), timeout=0):
            return _train(settings, force)
    except Timeout:
        return {'skipped': True, 'reason': 'training.lock is busy'}


def _train(settings, force):
    from .evaluation import fit_candidates, run_evaluation
    from .model import history
    from .learning_data import Timeline
    from .data import midnight

    now = utcnow()
    with connect(settings) as db:
        job = db.execute('SELECT * FROM training_jobs WHERE id=1').fetchone()
        last = db.execute('SELECT * FROM model_runs ORDER BY trained_at DESC LIMIT 1').fetchone()
        if not force:
            if not job or job['state'] != 'queued':
                return {'skipped': True, 'reason': 'queue empty'}
            if last and (now-dt(last['trained_at'])).total_seconds() < 86400:
                old = read_artifact(settings, db, now)
                # Corrections inside the fitted history bypass the daily throttle.
                if old is not None:
                    return {'skipped': True, 'reason': 'daily training throttle'}
        coverage, until = dt(getmeta(db, 'coverage_start')), dt(getmeta(db, 'last_success'))
        if not coverage or not until:
            return {'skipped': True, 'reason': 'no source history'}
        cutoff = midnight(min(now, until).astimezone(KYIV).date())
        complete = {r[0] for r in db.execute('SELECT day FROM daily WHERE complete=1')}
        if len(complete) < 21:
            return {'skipped': True, 'reason': 'need 21 complete days'}
        timeline = Timeline(history(db), max(coverage, midnight(settings.start_date)), complete, min(now, until))
        fingerprint = signature(db, settings)
        frozen_signature = signature(db, settings, cutoff)
    # No database transaction or synchronization lock is held while fitting.
    began = time.monotonic()
    cpu_began = time.process_time()
    data = timeline.dataset(settings.interval)
    evaluation = run_evaluation(settings, timeline, data)
    fitted = fit_candidates(timeline, data, cutoff)
    selected = evaluation['selected'].copy()
    for task in selected:
        if selected[task] not in fitted[task]:
            selected[task] = 'historical'
    artifact_id = now.strftime('%Y%m%dT%H%M%S')+'-'+uuid.uuid4().hex[:8]
    payload = {'id': artifact_id, 'version': VERSION, 'trained_at': iso(now),
               'training_end': iso(cutoff), 'history_signature': frozen_signature,
               'fingerprint': fingerprint, 'step_seconds': settings.interval,
               'selected': selected, 'fitted': fitted, 'evaluation': evaluation,
               'elapsed_seconds': round(time.monotonic()-began, 3),
               'cpu_seconds': round(time.process_time()-cpu_began, 3),
               'dataset_array_bytes': sum(getattr(v, 'nbytes', 0) for v in data.values())}
    root = settings.data_dir/'models'
    file = root/(artifact_id+'.json')
    atomic_json(file, payload)
    pointer = {'artifact': file.name, 'sha256': hashlib.sha256(file.read_bytes()).hexdigest()}
    with connect(settings) as db:
        db.execute('BEGIN IMMEDIATE')
        if signature(db, settings, cutoff) != frozen_signature:
            return {'skipped': True, 'reason': 'history corrected during training; retry queued'}
        current = root/'current.json'
        # Activate before committing queue completion. A failed replacement
        # rolls back model_runs and leaves the job queued for a retry.
        old = read_artifact(settings)
        if old:
            old_file = root/(old['id']+'.json')
            atomic_json(root/'previous.json', {'artifact': old_file.name,
                        'sha256': hashlib.sha256(old_file.read_bytes()).hexdigest()})
        atomic_json(current, pointer)
        db.execute('INSERT INTO model_runs VALUES (?,?,?,?,?,?)',
                   (artifact_id, iso(now), iso(cutoff), fingerprint, file.name,
                    json.dumps({'training': fitted['training'], 'selected': selected,
                                'evaluation': evaluation, 'elapsed_seconds': payload['elapsed_seconds'],
                                'cpu_seconds': payload['cpu_seconds'], 'dataset_array_bytes': payload['dataset_array_bytes']}, ensure_ascii=False)))
        setmeta(db, 'evaluation', json.dumps(evaluation, ensure_ascii=False, allow_nan=False))
        db.execute("UPDATE training_jobs SET state='done',message='' WHERE id=1 AND fingerprint=?", (fingerprint,))
    log.info('Trained %s in %.1fs: %s', artifact_id, payload['elapsed_seconds'], selected)
    return {'ok': True, 'model_id': artifact_id, 'selected': selected,
            'training': fitted['training'], 'elapsed_seconds': payload['elapsed_seconds']}


def run_trainer(settings, stop):
    while not stop.is_set():
        try:
            train(settings, force=False)
        except Exception as exc:
            log.exception('Training failed; last working artifact preserved')
            with connect(settings) as db:
                db.execute("UPDATE training_jobs SET message=? WHERE id=1", (f'{type(exc).__name__}: {exc}',))
        stop.wait(60)
