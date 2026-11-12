"""Point-in-time features and purged labels, on an elapsed-time UTC grid."""
from datetime import timedelta

import numpy as np

from .data import midnight
from .db import dt
from .source import KYIV


def known_rows(rows, at):
    return [{**r, 'end': r['end'] if r['end'] and dt(r['end']) <= at else None}
            for r in rows if dt(r['start']) <= at]


class Timeline:
    def __init__(self, rows, coverage, complete, observed_until):
        self.rows = rows
        self.coverage = coverage
        self.complete = set(complete)
        self.observed_until = observed_until
        valid = [r for r in rows if r['valid']]
        self.starts = np.array(sorted(dt(r['start']).timestamp() for r in valid))
        self.ends = np.array(sorted(dt(r['end']).timestamp() for r in valid if r['end']))
        self.bad = [r for r in rows if not r['valid']]

    def known_span(self, lo, hi):
        if lo < self.coverage or hi > self.observed_until:
            return False
        day = lo.astimezone(KYIV).date()
        # End is exclusive: an interval ending at midnight needs no next day.
        while midnight(day) < hi:
            if day.isoformat() not in self.complete:
                return False
            day += timedelta(days=1)
        return True

    def features(self, anchors):
        ts = np.array([a.timestamp() for a in anchors])
        local = [a.astimezone(KYIV) for a in anchors]
        phase = np.array([2*np.pi*(a.hour+a.minute/60)/24 for a in local])
        weekend = np.array([a.weekday() >= 5 for a in local], dtype=float)
        before = np.searchsorted(self.starts, ts, side='right')
        n6 = before-np.searchsorted(self.starts, ts-6*3600, side='right')
        n24 = before-np.searchsorted(self.starts, ts-24*3600, side='right')
        ended = np.searchsorted(self.ends, ts, side='right')
        last_end = np.where(ended > 0, self.ends[np.maximum(ended-1, 0)], ts-7*86400) if len(self.ends) else ts-7*86400

        def occupancy(t):
            # Integral of starts minus ends. Future end times contribute zero;
            # an ongoing alarm contributes only its elapsed duration.
            def integral(events):
                n = np.searchsorted(events, t, side='right')
                prefix = np.r_[0., np.cumsum(events)]
                return n*t-prefix[n]
            return integral(self.starts)-integral(self.ends)

        minutes24 = (occupancy(ts)-occupancy(ts-86400))/60
        remaining = np.array([(midnight(a.date()+timedelta(days=1)).timestamp()-t)/3600
                              for a, t in zip(local, ts)])
        return np.column_stack([np.sin(phase), np.cos(phase), weekend,
                                np.log1p(n24), np.log1p(n6),
                                np.log1p(np.maximum(0, (ts-last_end)/3600)),
                                np.log1p(np.maximum(0, minutes24)), remaining])

    def active(self, anchor):
        return any(r['valid'] and dt(r['start']) <= anchor and
                   (not r['end'] or anchor < dt(r['end'])) for r in self.rows)

    def dataset(self, step):
        anchors, start_y, count_y, inactive, days, slots, ends = [], [], [], [], [], [], []
        day = self.coverage.astimezone(KYIV).date()
        while midnight(day) < self.observed_until:
            lo, hi = midnight(day), midnight(day+timedelta(days=1))
            anchor = lo
            while anchor < hi and anchor < self.observed_until:
                horizon = anchor+timedelta(minutes=60)
                # All features require a known 24-hour lookback. The complete
                # label window includes the next hour and the rest of the day.
                if self.known_span(anchor-timedelta(hours=24), max(hi, horizon)):
                    t = anchor.timestamp()
                    left = np.searchsorted(self.starts, t, side='right')
                    start_y.append([int(np.searchsorted(self.starts, t+h*60, side='right') > left)
                                    for h in (15, 30, 60)])
                    count_y.append(int(np.searchsorted(self.starts, hi.timestamp(), side='left')-left))
                    anchors.append(anchor)
                    inactive.append(not self.active(anchor))
                    days.append(day.isoformat())
                    local = anchor.astimezone(KYIV)
                    slots.append(local.hour*3600+local.minute*60+local.second)
                    ends.append(max(hi, horizon).timestamp())
                anchor += timedelta(seconds=step)
            day += timedelta(days=1)
        return {'anchors': anchors, 'x': self.features(anchors) if anchors else np.empty((0, 8)),
                'onset': np.array(start_y).reshape(-1, 3), 'count': np.array(count_y),
                'inactive': np.array(inactive, dtype=bool), 'days': np.array(days),
                'slots': np.array(slots), 'label_end': np.array(ends)}

    def survival_data(self, cutoff):
        records = []
        for r in self.rows:
            start = dt(r['start'])
            if not r['valid'] or start < self.coverage or start >= cutoff:
                continue
            end = dt(r['end']) if r['end'] else None
            finish = min(end or cutoff, cutoff, self.observed_until)
            # Truncate at the first gap; never treat an unknown period as survival.
            day = start.astimezone(KYIV).date()
            while midnight(day) < finish:
                if day.isoformat() not in self.complete:
                    finish = min(finish, max(start, midnight(day)))
                    break
                day += timedelta(days=1)
            if finish > start:
                records.append({'start': start, 'time': (finish-start).total_seconds()/60,
                                'event': bool(end and end <= finish), 'id': r['start']})
        return records


def day_weights(days):
    _, inv, counts = np.unique(days, return_inverse=True, return_counts=True)
    weights = 1/counts[inv]
    return weights/weights.sum()
