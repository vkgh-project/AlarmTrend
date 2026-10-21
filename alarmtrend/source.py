import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

KYIV = ZoneInfo('Europe/Kyiv')


class ParsedEvents(list):
    def __init__(self, values, issues):
        super().__init__(values)
        self.issues = issues


def local_time(value):
    """Reject ambiguous/nonexistent manual times rather than silently choose a DST fold."""
    naive = datetime.fromisoformat(value)
    if naive.tzinfo:
        return naive.astimezone(timezone.utc)
    a, b = (naive.replace(tzinfo=KYIV, fold=f) for f in (0, 1))
    if a.utcoffset() != b.utcoffset():
        raise ValueError('Неоднозначний або неіснуючий час. Вкажіть ISO-час зі зміщенням +02:00 або +03:00.')
    return a.astimezone(timezone.utc)


def parse(html, start_date=None):
    soup = BeautifulSoup(html, 'html.parser')
    events, warnings, issues = [], [], []
    for tr in soup.select('table tr'):
        cells = [c.get_text(' ', strip=True) for c in tr.find_all('td')]
        if len(cells) < 2:
            continue
        try:
            naive = datetime.strptime(cells[0], '%H:%M %d.%m.%y')
            # Old DST ambiguities outside the configured analysis period must not
            # invalidate current data. Keep ordinary earlier rows for the predecessor.
            if start_date and naive.date() < start_date:
                a, b = (naive.replace(tzinfo=KYIV, fold=f) for f in (0, 1))
                if a.utcoffset() != b.utcoffset():
                    continue
            try:
                stamp = local_time(naive.isoformat())
            except ValueError:
                # HTML omits the offset during the repeated/skipped local hour.
                # Keep the raw record as a gap, never invent a UTC event.
                upper = max(naive.replace(tzinfo=KYIV,fold=f).astimezone(timezone.utc) for f in (0,1))
                issues.append((upper, 'Неоднозначна або неіснуюча мітка джерела; інтервал невідомий', ' | '.join(cells)))
                continue
            kind = 'end' if 'Відбій' in cells[1] else 'start' if 'Повітряна тривога' in cells[1] else None
            if kind is None:
                raise ValueError('Невідомий тип події')
            duration = None
            if len(cells) > 2 and cells[2]:
                hours = re.search(r'(\d+)\s*год', cells[2])
                minutes = re.search(r'(\d+)\s*хв', cells[2])
                duration = (int(hours[1]) * 60 if hours else 0) + (int(minutes[1]) if minutes else 0)
            events.append((stamp, kind, duration))
        except ValueError as exc:
            warnings.append(f'{cells[0]}: {exc}')
    if not events:
        raise ValueError('HTML не містить розпізнаних подій; попередні дані збережено')
    # Full history starts in 2022. Reject a partial/block page before replacing a snapshot.
    if warnings:
        raise ValueError('Некоректні рядки джерела: ' + '; '.join(warnings[:5]))
    return ParsedEvents(sorted(set(events), key=lambda e: (e[0], e[1])), issues)


def fetch(settings, etag=None, modified=None):
    session = requests.Session()
    retry = Retry(total=2, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=['GET'], respect_retry_after_header=False)
    session.mount('https://', HTTPAdapter(max_retries=retry))
    headers = {'User-Agent': 'AlarmTrend/1.0 (Kyiv historical statistics)'}
    if etag:
        headers['If-None-Match'] = etag
    if modified:
        headers['If-Modified-Since'] = modified
    with session:
        response = session.get(settings.source_url, headers=headers, timeout=(10, settings.timeout))
        if response.status_code != 304:
            response.raise_for_status()
        response.encoding = 'utf-8'
        return response
