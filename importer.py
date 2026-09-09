from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import os

from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from db import connect, is_postgres, init_postgres


UA = 'BalloonCompetitionWeb/0.5 (+https://example.invalid)'


SCHEMA = """
CREATE TABLE IF NOT EXISTS competitions(
    id TEXT PRIMARY KEY,
    title TEXT,
    location TEXT,
    dates TEXT,
    organiser TEXT,
    director TEXT,
    source_url TEXT
);

CREATE TABLE IF NOT EXISTS import_runs(
    id TEXT PRIMARY KEY,
    competition_id TEXT,
    imported_at TEXT,
    source_url TEXT,
    record_count INTEGER,
    error_count INTEGER
);

CREATE TABLE IF NOT EXISTS flights(
    id TEXT PRIMARY KEY,
    competition_id TEXT,
    flight_number TEXT,
    date_label TEXT,
    time_label TEXT,
    sort_order INTEGER,
    source_url TEXT
);

CREATE TABLE IF NOT EXISTS pilots(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    competition_id TEXT,
    competition_number INTEGER,
    name TEXT,
    country TEXT,
    UNIQUE(competition_id,competition_number)
);

CREATE TABLE IF NOT EXISTS tasks(
    id TEXT PRIMARY KEY,
    competition_id TEXT,
    task_number INTEGER,
    name TEXT,
    status TEXT,
    flight_id TEXT,
    published TEXT,
    source_url TEXT,
    UNIQUE(competition_id,task_number,published)
);

CREATE TABLE IF NOT EXISTS results(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    import_run_id TEXT,
    task_id TEXT,
    pilot_id INTEGER,
    rank INTEGER,
    result TEXT,
    points REAL,
    penalty_t REAL,
    penalty_c REAL,
    score REAL,
    notes TEXT,
    status TEXT,
    source_url TEXT
);

CREATE INDEX IF NOT EXISTS idx_results_run ON results(import_run_id);
CREATE INDEX IF NOT EXISTS idx_results_task ON results(task_id);
"""


def clean(s):
    return re.sub(r'\s+', ' ', s or '').strip()


def norm_status(s):
    s = clean(s).upper()

    if s.startswith('FINAL'):
        return 'FINAL'

    if s.startswith('OFFICIAL'):
        return 'OFFICIAL'

    if s.startswith('PROVISIONAL'):
        return 'PROVISIONAL'

    if 'CANCEL' in s:
        return 'CANCELLED'

    return s or 'UNKNOWN'


def num(s):
    s = clean(s).replace(',', '')

    if not s or s in {'-', '—', '–'}:
        return None

    try:
        return float(s)
    except Exception:
        return None


def fetch(session, url):
    r = session.get(url, timeout=30, headers={'User-Agent': UA})
    r.raise_for_status()
    return r.text


def parse_event(html, url):
    soup = BeautifulSoup(html, 'html.parser')

    title = clean(soup.title.get_text()) if soup.title else ''

    fields = {}

    for tr in soup.find_all('tr'):
        cells = [
            clean(x.get_text(' ', strip=True))
            for x in tr.find_all(['th', 'td'])
        ]

        if len(cells) >= 2:
            k = cells[0].rstrip(':')
            v = cells[1]

            if k and v and len(k) < 60:
                fields.setdefault(k, v)

    text = clean(soup.get_text(' ', strip=True))

    m = re.search(
        r'Event title:\s*([^|]+?)(?:\s+Event Location:|\s+Event Dates:)',
        text
    )

    if m:
        title = clean(m.group(1))

    location = fields.get('Event Location', '')
    dates = fields.get('Event Dates', '')
    organiser = fields.get('Organiser', '')
    director = (
        fields.get('Event Director', '')
        or fields.get('Director', '')
    )

    return {
        'title': title.replace('WatchMeFly |', '').strip() or url,
        'location': location,
        'dates': dates,
        'organiser': organiser,
        'director': director,
        'source_url': url
    }


def is_flight_heading(text):
    text = clean(text)

    return bool(
        re.match(
            r'^(?:Practice\s+)?Flight\s+\d+\s*[-–]',
            text,
            re.I
        )
    )


def parse_flight_heading(text):
    """
    Parse WatchMeFly headings such as:

        Flight 4 - 15 Aug 2026 AM
        Flight 3 - 12 Aug 2026 AM
        Practice Flight 1 - 9 Aug 2026 AM
    """

    text = clean(text)

    m = re.match(
        r'^(Practice\s+)?Flight\s+(\d+)\s*[-–]\s*(.*?)\s+(AM|PM)\s*$',
        text,
        re.I
    )

    if not m:
        return None

    practice = bool(m.group(1))

    flight_number = m.group(2)

    date_label = clean(m.group(3))
    time_label = m.group(4).upper()

    if practice:
        display_number = f'Practice {flight_number}'
    else:
        display_number = flight_number

    return {
        'flight_number': display_number,
        'date_label': date_label,
        'time_label': time_label,
        'is_practice': practice
    }


def flight_task_links(soup, base):
    """
    Read the WatchMeFly Flights & Tasks page in document order.

    Every task link is associated with the most recent Flight heading
    encountered above it.

    This is important because WatchMeFly can contain multiple occurrences
    of the same flight number, for example:

        Flight 3 - 14 Aug 2026 AM
        Flight 3 - 12 Aug 2026 AM

    and Practice Flights are also represented separately.
    """

    flights = []
    current_flight = None
    seen = set()

    tags = soup.find_all([
        'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
        'a'
    ])

    for tag in tags:

        if tag.name.startswith('h'):
            heading = clean(tag.get_text(' ', strip=True))

            parsed = parse_flight_heading(heading)

            if parsed:
                current_flight = parsed.copy()
                current_flight['heading'] = heading
                current_flight['tasks'] = []

                flights.append(current_flight)

            continue

        if tag.name != 'a':
            continue

        if current_flight is None:
            continue

        href = urljoin(base, tag.get('href', ''))

        q = parse_qs(urlparse(href).query)

        if 'tid' not in q:
            continue

        if q.get('v', [''])[0] != 'tr':
            continue

        tid = q['tid'][0]

        key = (
            current_flight['flight_number'],
            current_flight['date_label'],
            current_flight['time_label'],
            href
        )

        if key in seen:
            continue

        seen.add(key)

        current_flight['tasks'].append({
            'url': href,
            'tid': tid,
            'link_text': clean(tag.get_text(' ', strip=True))
        })

    return flights


def task_links(soup, base):
    """
    Backwards-compatible task link discovery.

    Used only as a fallback if the Flights & Tasks page does not expose
    flight headings in a way we can parse.
    """

    out = {}

    for a in soup.find_all('a', href=True):
        href = urljoin(base, a['href'])

        q = parse_qs(urlparse(href).query)

        if 'tid' not in q:
            continue

        if q.get('v', [''])[0] != 'tr':
            continue

        tid = q['tid'][0]
        out[tid] = href

    return list(out.values())


def parse_task(html, url, event_id, flight=None, link_text=''):
    soup = BeautifulSoup(html, 'html.parser')

    text = clean(soup.get_text(' ', strip=True))

    task_no = None
    name = ''
    status = 'UNKNOWN'

    # Normal competition task heading.
    m = re.search(
        r'Task\s+(\d+)\s*[—-]\s*'
        r'(.*?)\s*'
        r'(?:\(Rule:\s*([^)]+)\))?\s*'
        r'[—-]\s*'
        r'(Final|Provisional|Official[^\s]*)',
        text,
        re.I
    )

    if m:
        task_no = int(m.group(1))
        name = clean(m.group(2))
        status = norm_status(m.group(4))

    else:
        # More tolerant form.
        m = re.search(
            r'Task\s+(\d+)\s*[—-]\s*'
            r'(.*?)(?:\s*[—-]\s*'
            r'(Final|Provisional|Official[^\s]*))?'
            r'\s+Published:',
            text,
            re.I
        )

        if m:
            task_no = int(m.group(1))
            name = clean(m.group(2))
            status = norm_status(m.group(3) or '')

    # If this is a cancelled task there may be no Published field and
    # the normal task-page parser may not find the complete heading.
    # Use the task link text as a fallback.
    if task_no is None and link_text:

        m = re.search(
            r'\bTask\s+(\d+)\s*[-–]\s*(.*?)(?:\s+(FINAL|PROVISIONAL|OFFICIAL|CANCELLED))?$',
            link_text,
            re.I
        )

        if m:
            task_no = int(m.group(1))
            name = clean(m.group(2))
            status = norm_status(m.group(3) or '')

    if task_no is None:
        return None

    pm = re.search(
        r'Published:\s*([^\n]+?)(?:\s+by\s+|\s+Print\b)',
        text,
        re.I
    )

    published = clean(pm.group(1)) if pm else ''

    # Locate the result table by its headers.
    table = None

    for t in soup.find_all('table'):

        hs = [
            clean(x.get_text(' ', strip=True)).lower()
            for x in t.find_all('th')
        ]

        if 'pilot' in hs and 'score' in hs:
            table = t
            break

    rows = []

    if table:

        headers = [
            clean(x.get_text(' ', strip=True))
            for x in table.find_all('th')
        ]

        for tr in table.find_all('tr'):

            cells = [
                clean(x.get_text(' ', strip=True))
                for x in tr.find_all(['td', 'th'])
            ]

            if len(cells) < len(headers) or cells == headers:
                continue

            d = {
                headers[i].lower(): cells[i]
                for i in range(min(len(headers), len(cells)))
            }

            ptxt = d.get('pilot', '')

            pmatch = re.search(
                r'#\s*(\d+)\s*-\s*(.*?)(?:Image|$)',
                ptxt,
                re.I
            )

            if not pmatch:
                continue

            comp_no = int(pmatch.group(1))
            pname = clean(pmatch.group(2)).rstrip(',')

            country = ''

            if 'image' in ptxt.lower():
                tail = clean(ptxt.split('Image', 1)[1])

                if tail:
                    country = tail

            rank_text = d.get('rank', '').replace(',', '')

            rows.append({
                'competition_number': comp_no,
                'pilot': pname,
                'country': country,
                'rank': int(rank_text)
                    if rank_text.isdigit()
                    else None,
                'result': d.get('result', ''),
                'points': num(d.get('points', '')),
                'penalty_t': num(d.get('penalty (t)', '')),
                'penalty_c': num(d.get('penalty (c)', '')),
                'score': num(d.get('score', '')),
                'notes': d.get('notes', '')
            })

    return {
        'task_number': task_no,
        'name': name,
        'status': status,
        'published': published,
        'source_url': url,
        'rows': rows,
        'flight': flight
    }


def stable(*parts):
    return hashlib.sha1(
        '|'.join(clean(str(x)) for x in parts).encode()
    ).hexdigest()[:20]


def import_event(url, out_root):

    session = requests.Session()
    session.headers.update({'User-Agent': UA})

    event_id = (
        parse_qs(urlparse(url).query).get('e', [''])[0]
        or stable(url)
    )

    event_html = fetch(session, url)

    meta = parse_event(event_html, url)

    # ------------------------------------------------------------
    # Discover flights and tasks from the Flights & Tasks page.
    # ------------------------------------------------------------

    flights_url = url

    if '?' in url:
        flights_url = url + '&v=t'
    else:
        flights_url = url + '?v=t'

    flights = []

    try:
        flights_html = fetch(session, flights_url)

        flights_soup = BeautifulSoup(
            flights_html,
            'html.parser'
        )

        flights = flight_task_links(
            flights_soup,
            flights_url
        )

    except Exception:
        flights = []

    # ------------------------------------------------------------
    # Fallback to the old task-link discovery if no flight
    # structure was found.
    # ------------------------------------------------------------

    tasks = []
    errors = []

    if flights:

        flight_sort = 0

        for flight in flights:

            # Create a stable identifier based on the actual flight
            # rather than the publication timestamp of its tasks.
            fid = 'flight-' + stable(
                event_id,
                flight['flight_number'],
                flight['date_label'],
                flight['time_label']
            )

            flight['id'] = fid
            flight['sort_order'] = flight_sort

            flight_sort += 1

            for task_link in flight['tasks']:

                try:

                    t = parse_task(
                        fetch(session, task_link['url']),
                        task_link['url'],
                        event_id,
                        flight=flight,
                        link_text=task_link['link_text']
                    )

                    if t:
                        tasks.append(t)

                except Exception as e:

                    errors.append({
                        'url': task_link['url'],
                        'error': str(e)
                    })

    else:

        # Fallback for older/unusual WatchMeFly event pages.
        soup = BeautifulSoup(event_html, 'html.parser')

        links = task_links(soup, url)

        if not links:

            for suffix in ('&v=t', '&v=tr'):

                try:

                    h = fetch(
                        session,
                        url + suffix
                        if '?' in url
                        else url + '?v=t'
                    )

                    links = task_links(
                        BeautifulSoup(h, 'html.parser'),
                        url
                    )

                    if links:
                        break

                except Exception:
                    pass

        for link in links:

            try:

                t = parse_task(
                    fetch(session, link),
                    link,
                    event_id
                )

                if t:
                    tasks.append(t)

            except Exception as e:

                errors.append({
                    'url': link,
                    'error': str(e)
                })

    # ------------------------------------------------------------
    # Deduplicate while preserving separate task occurrences.
    # ------------------------------------------------------------

    unique = {}

    for t in tasks:

        flight = t.get('flight') or {}

        flight_key = (
            flight.get('flight_number', ''),
            flight.get('date_label', ''),
            flight.get('time_label', '')
        )

        key = stable(
            event_id,
            flight_key,
            t['task_number'],
            t['published'],
            t['source_url']
        )

        unique[key] = t

    tasks = list(unique.values())

    tasks.sort(
        key=lambda x: (
            x.get('flight', {}).get('sort_order', 999999),
            x['task_number'],
            x['published']
        )
    )

    # ------------------------------------------------------------
    # Local output directory.
    # ------------------------------------------------------------

    root = Path(out_root) / event_id
    root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------
    # Database connection.
    # ------------------------------------------------------------

    if is_postgres():

        c = connect()

        init_postgres(c)

        c.execute(
            """
            INSERT INTO competitions(
                id,title,location,dates,organiser,director,source_url
            )
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                title=EXCLUDED.title,
                location=EXCLUDED.location,
                dates=EXCLUDED.dates,
                organiser=EXCLUDED.organiser,
                director=EXCLUDED.director,
                source_url=EXCLUDED.source_url
            """,
            (
                event_id,
                meta['title'],
                meta['location'],
                meta['dates'],
                meta['organiser'],
                meta['director'],
                url
            )
        )

    else:

        dbp = root / 'competition.db'

        c = connect(dbp)

        c.executescript(SCHEMA)

        c.execute(
            'INSERT OR REPLACE INTO competitions VALUES (?,?,?,?,?,?,?)',
            (
                event_id,
                meta['title'],
                meta['location'],
                meta['dates'],
                meta['organiser'],
                meta['director'],
                url
            )
        )

    # ------------------------------------------------------------
    # Import run.
    # ------------------------------------------------------------

    run_id = stable(
        event_id,
        datetime.now(timezone.utc).isoformat(),
        len(tasks)
    )

    imported = datetime.now(timezone.utc).isoformat()

    c.execute(
        'INSERT INTO import_runs VALUES (?,?,?,?,?,?)',
        (
            run_id,
            event_id,
            imported,
            url,
            sum(len(t['rows']) for t in tasks),
            len(errors)
        )
    )

    # ------------------------------------------------------------
    # Flights and tasks.
    # ------------------------------------------------------------

    created_flights = {}

    for i, t in enumerate(tasks):

        flight = t.get('flight') or {}

        if flight:

            fid = flight['id']

            if fid not in created_flights:

                flight_number = flight.get(
                    'flight_number',
                    ''
                )

                date_label = flight.get(
                    'date_label',
                    ''
                )

                time_label = flight.get(
                    'time_label',
                    ''
                )

                sort_order = flight.get(
                    'sort_order',
                    i
                )

                if is_postgres():

                    c.execute(
                        """
                        INSERT INTO flights(
                            id,
                            competition_id,
                            flight_number,
                            date_label,
                            time_label,
                            sort_order,
                            source_url
                        )
                        VALUES (?,?,?,?,?,?,?)
                        ON CONFLICT(id) DO UPDATE SET
                            flight_number=EXCLUDED.flight_number,
                            date_label=EXCLUDED.date_label,
                            time_label=EXCLUDED.time_label,
                            sort_order=EXCLUDED.sort_order,
                            source_url=EXCLUDED.source_url
                        """,
                        (
                            fid,
                            event_id,
                            flight_number,
                            date_label,
                            time_label,
                            sort_order,
                            flights_url
                        )
                    )

                else:

                    c.execute(
                        """
                        INSERT OR IGNORE INTO flights(
                            id,
                            competition_id,
                            flight_number,
                            date_label,
                            time_label,
                            sort_order,
                            source_url
                        )
                        VALUES (?,?,?,?,?,?,?)
                        """,
                        (
                            fid,
                            event_id,
                            flight_number,
                            date_label,
                            time_label,
                            sort_order,
                            flights_url
                        )
                    )

                    c.execute(
                        """
                        UPDATE flights
                        SET flight_number=?,
                            date_label=?,
                            time_label=?,
                            sort_order=?,
                            source_url=?
                        WHERE id=?
                        """,
                        (
                            flight_number,
                            date_label,
                            time_label,
                            sort_order,
                            flights_url,
                            fid
                        )
                    )

                created_flights[fid] = True

        else:

            # Fallback for pages where no flight information exists.
            fid = 'flight-' + stable(
                event_id,
                t['published'][:20]
                if t['published']
                else t['task_number']
            )

            if is_postgres():

                c.execute(
                    """
                    INSERT INTO flights(
                        id,
                        competition_id,
                        flight_number,
                        date_label,
                        time_label,
                        sort_order,
                        source_url
                    )
                    VALUES (?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET
                        date_label=EXCLUDED.date_label,
                        sort_order=EXCLUDED.sort_order,
                        source_url=EXCLUDED.source_url
                    """,
                    (
                        fid,
                        event_id,
                        '',
                        t['published'],
                        '',
                        i,
                        t['source_url']
                    )
                )

            else:

                c.execute(
                    """
                    INSERT OR IGNORE INTO flights(
                        id,
                        competition_id,
                        flight_number,
                        date_label,
                        time_label,
                        sort_order,
                        source_url
                    )
                    VALUES (?,?,?,?,?,?,?)
                    """,
                    (
                        fid,
                        event_id,
                        '',
                        t['published'],
                        '',
                        i,
                        t['source_url']
                    )
                )

                c.execute(
                    """
                    UPDATE flights
                    SET date_label=?,
                        sort_order=?
                    WHERE id=?
                    """,
                    (
                        t['published'],
                        i,
                        fid
                    )
                )

        # --------------------------------------------------------
        # Task.
        # --------------------------------------------------------

        tid = 'task-' + stable(
            event_id,
            t['task_number'],
            t['published'],
            t['source_url']
        )

        if is_postgres():

            c.execute(
                """
                INSERT INTO tasks(
                    id,
                    competition_id,
                    task_number,
                    name,
                    status,
                    flight_id,
                    published,
                    source_url
                )
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    name=EXCLUDED.name,
                    status=EXCLUDED.status,
                    flight_id=EXCLUDED.flight_id,
                    published=EXCLUDED.published,
                    source_url=EXCLUDED.source_url
                """,
                (
                    tid,
                    event_id,
                    t['task_number'],
                    t['name'],
                    t['status'],
                    fid,
                    t['published'],
                    t['source_url']
                )
            )

        else:

            c.execute(
                """
                INSERT OR REPLACE INTO tasks(
                    id,
                    competition_id,
                    task_number,
                    name,
                    status,
                    flight_id,
                    published,
                    source_url
                )
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    tid,
                    event_id,
                    t['task_number'],
                    t['name'],
                    t['status'],
                    fid,
                    t['published'],
                    t['source_url']
                )
            )

        # --------------------------------------------------------
        # Results / pilots.
        # --------------------------------------------------------

        for r in t['rows']:

            if is_postgres():

                c.execute(
                    """
                    INSERT INTO pilots(
                        competition_id,
                        competition_number,
                        name,
                        country
                    )
                    VALUES (?,?,?,?)
                    ON CONFLICT(
                        competition_id,
                        competition_number
                    )
                    DO UPDATE SET
                        name=EXCLUDED.name,
                        country=EXCLUDED.country
                    """,
                    (
                        event_id,
                        r['competition_number'],
                        r['pilot'],
                        r['country']
                    )
                )

            else:

                c.execute(
                    """
                    INSERT OR IGNORE INTO pilots(
                        competition_id,
                        competition_number,
                        name,
                        country
                    )
                    VALUES (?,?,?,?)
                    """,
                    (
                        event_id,
                        r['competition_number'],
                        r['pilot'],
                        r['country']
                    )
                )

            p = c.execute(
                """
                SELECT id
                FROM pilots
                WHERE competition_id=?
                  AND competition_number=?
                """,
                (
                    event_id,
                    r['competition_number']
                )
            ).fetchone()[0]

            c.execute(
                """
                INSERT INTO results(
                    import_run_id,
                    task_id,
                    pilot_id,
                    rank,
                    result,
                    points,
                    penalty_t,
                    penalty_c,
                    score,
                    notes,
                    status,
                    source_url
                )
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    run_id,
                    tid,
                    p,
                    r['rank'],
                    r['result'],
                    r['points'],
                    r['penalty_t'],
                    r['penalty_c'],
                    r['score'],
                    r['notes'],
                    t['status'],
                    t['source_url']
                )
            )

    c.commit()
    c.close()

    # ------------------------------------------------------------
    # Save importer errors.
    # ------------------------------------------------------------

    (root / 'errors.json').write_text(
        json.dumps(errors, indent=2),
        encoding='utf-8'
    )

    return (
        event_id,
        run_id,
        len(tasks),
        sum(len(t['rows']) for t in tasks),
        errors
    )


if __name__ == '__main__':

    ap = argparse.ArgumentParser(
        description='Import a WatchMeFly competition into the Balloon Competition database'
    )

    ap.add_argument('url')
    ap.add_argument('--data-dir', default='data')

    a = ap.parse_args()

    print(
        json.dumps(
            dict(
                zip(
                    [
                        'event_id',
                        'run_id',
                        'tasks',
                        'results',
                        'errors'
                    ],
                    import_event(
                        a.url,
                        a.data_dir
                    )
                )
            ),
            indent=2,
            default=str
        )
    )
