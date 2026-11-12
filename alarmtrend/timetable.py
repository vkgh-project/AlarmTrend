from datetime import datetime, time, timedelta, timezone

from .db import dt, getmeta, iso
from .source import KYIV


DAY_NAMES = ('Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Нд')
DAY_COUNT = 8
MINUTES_PER_DAY = 24 * 60


def _minute_of_day(value):
    local = value.astimezone(KYIV)
    return local.hour * 60 + local.minute


def build_timetable(db, now):
    """Build seven Kyiv-local days of confirmed alarm, clear, and unknown periods."""
    today = now.astimezone(KYIV).date()
    first_day = today - timedelta(days=DAY_COUNT - 1)
    range_start = datetime.combine(first_day, time.min, KYIV).astimezone(timezone.utc)
    range_end = datetime.combine(today + timedelta(days=1), time.min, KYIV).astimezone(timezone.utc)

    coverage_start = dt(getmeta(db, 'coverage_start'))
    last_success = dt(getmeta(db, 'last_success'))
    observed_until = min(last_success, now) if last_success else None

    interval_rows = db.execute(
        'SELECT * FROM intervals WHERE start<? AND (end IS NULL OR end>=?) ORDER BY start',
        (iso(range_end), iso(range_start)),
    ).fetchall()
    intervals = []
    for index, row in enumerate(interval_rows):
        start = dt(row['start'])
        end = dt(row['end'])
        if end is None:
            if not row['valid'] and index + 1 < len(interval_rows):
                end = dt(interval_rows[index + 1]['start'])
            else:
                end = observed_until or now
        if end > range_start and start < range_end:
            intervals.append((start, end, bool(row['valid'])))

    issue_ranges = []
    issue_floor = iso(range_start - timedelta(hours=3))
    issue_ceiling = iso(range_end)
    for row in db.execute(
            'SELECT stamp,message FROM issues WHERE stamp>=? AND stamp<=?',
            (issue_floor, issue_ceiling)):
        if row['stamp'] and not row['message'].startswith('Текстова'):
            stamp = dt(row['stamp'])
            issue_ranges.append((stamp - timedelta(hours=2), stamp + timedelta(minutes=1)))
    for row in db.execute(
            'SELECT stamp FROM source_issues WHERE stamp>=? AND stamp<=?',
            (issue_floor, issue_ceiling)):
        stamp = dt(row['stamp'])
        issue_ranges.append((stamp - timedelta(hours=2), stamp))

    days = []
    for offset in range(DAY_COUNT):
        day = first_day + timedelta(days=offset)
        states = []
        for minute in range(MINUTES_PER_DAY):
            local_time = datetime.combine(day, time(minute // 60, minute % 60), KYIV)
            instant = local_time.astimezone(timezone.utc)
            if coverage_start and observed_until and coverage_start <= instant < observed_until:
                state = 'clear'
            else:
                state = 'unknown'

            for start, end, valid in intervals:
                if start <= instant < end:
                    state = 'active' if valid else 'unknown'
                    break
            if any(start <= instant < end for start, end in issue_ranges):
                state = 'unknown'
            states.append(state)

        segments = []
        segment_start = 0
        for minute in range(1, MINUTES_PER_DAY + 1):
            if minute == MINUTES_PER_DAY or states[minute] != states[segment_start]:
                length = minute - segment_start
                segments.append({
                    'status': states[segment_start],
                    'top': round(segment_start / MINUTES_PER_DAY * 100, 4),
                    'height': round(length / MINUTES_PER_DAY * 100, 4),
                    'from': f'{segment_start // 60:02d}:{segment_start % 60:02d}',
                    'to': f'{minute // 60:02d}:{minute % 60:02d}',
                })
                segment_start = minute

        days.append({
            'date': day.strftime('%d.%m'),
            'weekday': DAY_NAMES[day.weekday()],
            'today': day == today,
            'segments': segments,
            'alarm_periods': [
                {'from': segment['from'], 'to': segment['to']}
                for segment in segments if segment['status'] == 'active'
            ],
        })
    return days
