from datetime import datetime, time, timedelta, timezone

from .db import audit, dt, getmeta, iso, setmeta
from .source import KYIV


def midnight(day):
    return datetime.combine(day, time(), KYIV).astimezone(timezone.utc)


def import_events(db, events, settings):
    cutoff = midnight(settings.start_date)
    # Retain predecessor, so an interval that crosses the configured boundary is paired.
    previous = [e for e in events if e[0] < cutoff]
    selected = [e for e in events if e[0] >= cutoff]
    if previous:
        selected.insert(0, previous[-1])
    old = {(r['stamp'], r['kind']): r for r in db.execute("SELECT * FROM events WHERE origin='source' AND enabled=1")}
    new = {(iso(t), k): d for t, k, d in selected}
    changed = 0
    new_issues = [(iso(t), message, raw) for t,message,raw in getattr(events,'issues',[])]
    old_issues = [tuple(r) for r in db.execute('SELECT * FROM source_issues')]
    if new_issues != old_issues:
        audit(db,'source_time_quality',new_issues)
    db.execute('DELETE FROM source_issues')
    db.executemany('INSERT INTO source_issues VALUES (?,?,?)',new_issues)
    for key, row in old.items():
        if key not in new:
            db.execute('UPDATE events SET enabled=0 WHERE id=?', (row['id'],))
            audit(db, 'source_removed', dict(row))
            changed += 1
    for (stamp, kind), duration in new.items():
        row = old.get((stamp, kind))
        if not row or row['text_duration'] != duration:
            db.execute("INSERT INTO events(stamp,kind,origin,text_duration) VALUES (?,?,'source',?) "
                       'ON CONFLICT(stamp,kind,origin) DO UPDATE SET enabled=1,text_duration=excluded.text_duration',
                       (stamp, kind, duration))
            audit(db, 'source_upsert', [stamp, kind, duration])
            changed += 1
    setmeta(db, 'coverage_start', iso(max(cutoff, events[0][0])))
    setmeta(db, 'source_first', iso(events[0][0]))
    return changed


def rebuild(db, settings, now):
    db.execute('DELETE FROM issues')
    db.execute('INSERT INTO issues(stamp,message) SELECT stamp,message || ": " || raw FROM source_issues')
    db.execute('DELETE FROM intervals')
    previous_matches = {r['id']:r['reconciled_to'] for r in db.execute("SELECT id,reconciled_to FROM events WHERE origin='manual'")}
    db.execute("UPDATE events SET reconciled_to=NULL WHERE origin='manual'")
    source = db.execute("SELECT * FROM events WHERE origin='source' AND enabled=1 ORDER BY stamp").fetchall()
    manual = db.execute("SELECT * FROM events WHERE origin='manual' AND enabled=1 ORDER BY stamp").fetchall()
    # Match source's minute resolution. Multiple matches remain explicit conflicts.
    candidates = {m['id']: [s for s in source if s['kind'] == m['kind'] and
                             abs((dt(s['stamp']) - dt(m['stamp'])).total_seconds()) < 60] for m in manual}
    used = {}
    for matches in candidates.values():
        for s in matches:
            used[s['id']] = used.get(s['id'], 0) + 1
    for m in manual:
        matches = candidates[m['id']]
        if len(matches) == 1 and used[matches[0]['id']] == 1:
            db.execute('UPDATE events SET reconciled_to=? WHERE id=?', (matches[0]['id'], m['id']))
            if previous_matches.get(m['id']) != matches[0]['id']:
                audit(db,'manual_reconciled',{'manual_id':m['id'],'source_id':matches[0]['id']})
        elif matches:
            db.execute('INSERT INTO issues(stamp,message) VALUES (?,?)',
                       (m['stamp'], 'Неоднозначне узгодження ручного запису з джерелом'))
    rows = db.execute('SELECT * FROM events WHERE enabled=1 AND reconciled_to IS NULL ORDER BY stamp,kind').fetchall()
    active = None
    for row in rows:
        stamp = row['stamp']
        if dt(stamp) > now:
            db.execute('INSERT INTO issues(stamp,message) VALUES (?,?)', (stamp, 'Подія з майбутньою міткою часу'))
            continue
        if row['kind'] == 'start':
            if active:
                db.execute('INSERT INTO intervals VALUES (?,?,?,0)', (active['stamp'], None, active['origin']))
                db.execute('INSERT INTO issues(stamp,message) VALUES (?,?)', (stamp, 'Два початки без відбою; проміжок невідомий'))
            active = row
        elif active:
            valid = stamp > active['stamp']
            db.execute('INSERT INTO intervals VALUES (?,?,?,?)',
                       (active['stamp'], stamp if valid else None,
                        'manual' if 'manual' in (active['origin'], row['origin']) else 'source', int(valid)))
            elapsed = (dt(stamp) - dt(active['stamp'])).total_seconds() / 60
            if row['text_duration'] is not None and abs(elapsed - row['text_duration']) > 1:
                db.execute('INSERT INTO issues(stamp,message) VALUES (?,?)', (stamp, 'Текстова тривалість не збігається з мітками'))
            if not valid:
                db.execute('INSERT INTO issues(stamp,message) VALUES (?,?)', (stamp, 'Нульова або суперечлива тривалість'))
            active = None
        elif dt(stamp) >= midnight(settings.start_date):
            db.execute('INSERT INTO issues(stamp,message) VALUES (?,?)', (stamp, 'Відбій без відомого початку'))
    if active:
        db.execute('INSERT INTO intervals VALUES (?,?,?,1)', (active['stamp'], None, active['origin']))
    # A source ambiguity has no invented event, but invalidates any possibly
    # crossing interval. The issue stamp is an upper bound, not an event time.
    for gap in db.execute('SELECT stamp FROM source_issues').fetchall():
        upper = dt(gap['stamp'])
        lower = upper - timedelta(hours=2)
        db.execute('UPDATE intervals SET valid=0 WHERE start<=? AND (end IS NULL OR end>=?)',
                   (iso(upper),iso(lower)))
    aggregate(db, settings, now)


def aggregate(db, settings, now):
    db.execute('DELETE FROM daily')
    coverage = dt(getmeta(db, 'coverage_start'))
    if not coverage:
        return
    intervals = db.execute('SELECT * FROM intervals ORDER BY start').fetchall()
    anomalies = [dt(r[0]) for r in db.execute("SELECT stamp FROM issues WHERE message NOT LIKE 'Текстова%'")]
    last_success = dt(getmeta(db, 'last_success'))
    day = settings.start_date
    while day <= now.astimezone(KYIV).date():
        lo, hi = midnight(day), midnight(day + timedelta(days=1))
        starts, minutes = 0, 0
        complete = coverage <= lo and last_success is not None and last_success >= hi
        for i,row in enumerate(intervals):
            start, end = dt(row['start']), dt(row['end'])
            if lo <= start < hi:
                starts += 1
            if not row['valid']:
                # The next known start bounds uncertainty, without creating an end.
                bound = end or (dt(intervals[i+1]['start']) if i+1<len(intervals) else now)
                if start < hi and bound > lo:
                    complete = False
                continue
            finish = end or now
            minutes += max(0, (min(hi, finish, now) - max(lo, start)).total_seconds() / 60)
        if any(t and lo <= t < hi for t in anomalies):
            complete = False
        db.execute('INSERT INTO daily VALUES (?,?,?,?)', (day.isoformat(), starts, minutes, int(complete)))
        day += timedelta(days=1)


def trends(db, now):
    today = now.astimezone(KYIV).date()
    output = []
    for n in (14, 7, 3):
        periods = []
        for a, b in ((n, 0), (2*n, n)):
            rows = db.execute('SELECT * FROM daily WHERE day>=? AND day<?',
                              ((today-timedelta(days=a)).isoformat(), (today-timedelta(days=b)).isoformat())).fetchall()
            periods.append(rows)
        complete = all(len(rows) == n and all(r['complete'] for r in rows) for rows in periods)
        item = {'days': n, 'complete': complete}
        for field in ('starts', 'minutes'):
            current, previous = [sum(r[field] for r in rows) / n for rows in periods]
            delta = None if previous == 0 else round((current / previous - 1) * 100)
            item[field] = {'current': round(current, 1), 'previous': round(previous, 1), 'delta': delta,
                           'direction': '↑' if current > previous else '↓' if current < previous else '→'}
        output.append(item)
    return output
