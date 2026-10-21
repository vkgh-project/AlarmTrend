import math
from datetime import datetime, timedelta
from statistics import median

from .data import midnight, trends
from .db import dt, getmeta, iso
from .source import KYIV

VERSION = 'historical-analogs-v1'


def quantile(values, q):
    ordered = sorted(values)
    pos = (len(ordered)-1)*q
    a, b = math.floor(pos), math.ceil(pos)
    return ordered[a] + (ordered[b]-ordered[a])*(pos-a)


def history(db):
    return [dict(r) for r in db.execute('SELECT * FROM intervals ORDER BY start')]


def predict(rows, coverage, now, complete_days=None):
    """Only completed historical calendar days are analogs. No future outcomes enter training."""
    local = now.astimezone(KYIV)
    today = midnight(local.date())
    starts = [dt(r['start']) for r in rows if coverage <= dt(r['start']) < today]
    closed = [(dt(r['start']), dt(r['end'])) for r in rows
              if r['valid'] and r['end'] and coverage <= dt(r['start']) < dt(r['end']) <= today]
    result = {'probabilities': None, 'count': None, 'duration': None, 'remaining': None,
              'survive60': None, 'observations': 0, 'duration_observations': 0}
    counts, outcomes = [], []
    day = coverage.astimezone(KYIV).date()
    while day < local.date():
        if midnight(day) < coverage or (complete_days is not None and day.isoformat() not in complete_days):
            day += timedelta(days=1)
            continue
        anchor = datetime.combine(day, local.timetz().replace(tzinfo=None), KYIV)
        anchor_utc = anchor.astimezone(now.tzinfo)
        end = midnight(day + timedelta(days=1))
        counts.append(sum(anchor_utc < s < end for s in starts))
        # Inactive analogs only; use full 60 minute outcomes, even near midnight.
        horizon = anchor_utc + timedelta(minutes=60)
        horizon_day = horizon.astimezone(KYIV).date()
        horizon_known = complete_days is None or horizon_day.isoformat() in complete_days
        if horizon <= today and horizon_known and not any(s <= anchor_utc < e for s,e in closed):
            outcomes.append([int(any(anchor_utc < s <= anchor_utc+timedelta(minutes=h) for s in starts)) for h in (15,30,60)])
        day += timedelta(days=1)
    result['observations'] = len(outcomes)
    if len(outcomes) >= 14:
        # Shared Jeffreys smoothing preserves horizon ordering by construction.
        result['probabilities'] = [round(100*(sum(o[i] for o in outcomes)+0.5)/(len(outcomes)+1), 1) for i in range(3)]
    if len(counts) >= 14:
        result['count'] = {'estimate': int(math.floor(sum(counts)/len(counts)+0.5)),
                           'low': math.floor(quantile(counts, .1)), 'high': math.ceil(quantile(counts, .9)), 'n': len(counts)}
    lengths = [(e-s).total_seconds()/60 for s,e in closed]
    result['duration_observations'] = len(lengths)
    if len(lengths) >= 10:
        result['duration'] = {'median': round(median(lengths)), 'low': round(quantile(lengths,.25)),
                              'high': round(quantile(lengths,.75)), 'n': len(lengths)}
    active = [r for r in rows if r['valid'] and dt(r['start']) <= now and (not r['end'] or dt(r['end']) > now)]
    if active:
        elapsed = (now-dt(active[-1]['start'])).total_seconds()/60
        residuals = [length-elapsed for length in lengths if length > elapsed]
        result['probabilities'] = None
        if len(residuals) >= 10:
            result['remaining'] = {'median': round(median(residuals)), 'low': round(quantile(residuals,.25)),
                                   'high': round(quantile(residuals,.75)), 'n': len(residuals)}
            result['survive60'] = round(100*(sum(r>60 for r in residuals)+.5)/(len(residuals)+1),1)
    return result


def snapshot(db, settings, now):
    coverage = dt(getmeta(db, 'coverage_start'))
    rows = history(db)
    complete = {r[0] for r in db.execute('SELECT day FROM daily WHERE complete=1')}
    result = predict(rows, coverage, now, complete) if coverage else {
        'probabilities': None, 'count': None, 'duration': None, 'remaining': None, 'survive60': None,
        'observations': 0, 'duration_observations': 0}
    last = db.execute('SELECT * FROM events WHERE enabled=1 AND reconciled_to IS NULL AND stamp<=? ORDER BY stamp DESC,id DESC LIMIT 1', (iso(now),)).fetchone()
    issues = [dict(r) for r in db.execute('SELECT * FROM issues ORDER BY id DESC')]
    severe = [r for r in issues if not r['message'].startswith('Текстова')]
    result.update(status='unknown', active_since=None, elapsed=None, manual=False)
    if last:
        latest_interval = rows[-1] if rows else None
        uncertain = severe and (not latest_interval or any(r['stamp'] >= latest_interval['start'] for r in severe))
        if not uncertain:
            result['status'] = 'active' if last['kind']=='start' else 'clear'
        result['manual'] = last['origin']=='manual'
        if result['status']=='active':
            result['active_since'] = last['stamp']
            result['elapsed'] = round((now-dt(last['stamp'])).total_seconds()/60)
    if result['status'] != 'clear':
        result['probabilities'] = None
    if result['status'] == 'unknown':
        result['remaining'] = result['survive60'] = None
    result.update(at=iso(now), model=VERSION, last_success=getmeta(db,'last_success'),
                  last_event=last['stamp'] if last else None, trends=trends(db,now), issues=issues,
                  source=settings.source_url)
    return result
