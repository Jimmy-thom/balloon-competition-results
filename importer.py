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
    source_url TEXT,
    status TEXT,
    flight_type TEXT
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
    r = session.get(
        url,
        timeout=30,
        headers={'User-Agent': UA}
    )
    r.raise_for_status()
    return r.text


def stable(*parts):
    return hashlib.sha1(
        '|'.join(clean(str(x)) for x in parts).encode()
    ).hexdigest()[:20]


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


# ---------------------------------------------------------------------------
# FLIGHT PARSING
# ---------------------------------------------------------------------------

FLIGHT_RE = re.compile(
    r'^(?P<practice>Practice\s+)?Flight\s+'
    r'(?P<number>\d+)\s*[-–]\s*'
    r'(?P<date>\d{1,2}\s+[A-Za-z]{3}\s+\d{4})'
    r'(?:\s+(?P<time>AM|PM))?$',
    re.I
)


def parse_flight_heading(text):
    """
    Parse WatchMeFly flight headings such as:

        Flight 2 - 12 Sep 2026 PM
        Flight 1 - 12 Sep 2026 AM
        Flight 1 - 11 Sep 2026 PM
        Practice Flight 2 - 10 Sep 2026 PM
    """

    text = clean(text)

    m = FLIGHT_RE.match(text)

    if not m:
        return None

    practice = bool(m.group('practice'))

    return {
        'flight_number': m.group('number'),
        'date_label': clean(m.group('date')),
        'time_label': clean(m.group('time') or ''),
        'flight_type': 'PRACTICE' if practice else 'COMPETITION',
        'heading': text
    }


def task_links_with_flights(soup, base):
    """
    Read the WatchMeFly Flights & Tasks page as a sequence of flight
    sections.

    Each flight heading is followed by its status and task links until
    the next flight heading.

    Returns a list of flight definitions containing the task URLs that
    belong to that flight.
    """

    flights = []
    current = None

    # WatchMeFly currently renders the flight headings as h6 elements,
    # but we deliberately inspect all heading elements so small markup
    # changes do not break the importer.
    for element in soup.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6']):
        heading_text = clean(element.get_text(' ', strip=True))

        flight = parse_flight_heading(heading_text)

        if flight:
            current = {
                **flight,
                'status': 'UNKNOWN',
                'task_links': []
            }

            flights.append(current)

            # Status/task links appear after the heading, so continue
            # scanning following siblings below.

    # The heading-based pass above identifies the flights. Now walk the
    # document in source order and assign links/status to the correct
    # section.
    flights = []
    current = None

    for element in soup.find_all(
        ['h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'div', 'span', 'a']
    ):
        text = clean(element.get_text(' ', strip=True))

        flight = parse_flight_heading(text)

        if flight:
            current = {
                **flight,
                'status': 'UNKNOWN',
                'task_links': []
            }
            flights.append(current)
            continue

        if current is None:
            continue

        # Detect the explicit flight status.
        upper = text.upper()

        if upper in {
            'COMPLETE',
            'COMPLETED',
            'CANCELLED',
            'CANCELED',
            'IN PROGRESS',
            'STARTED'
        }:
            current['status'] = (
                'COMPLETE'
                if upper in {'COMPLETE', 'COMPLETED'}
                else 'CANCELLED'
                if upper in {'CANCELLED', 'CANCELED'}
                else upper
            )

        # Task links.
        if element.name == 'a' and element.get('href'):
            href = urljoin(base, element['href'])

            q = parse_qs(urlparse(href).query)

            if 'tid' in q and q.get('v', [''])[0] == 'tr':
                current['task_links'].append(href)

    # De-duplicate task links inside each flight.
    for flight in flights:
        seen = set()
        unique = []

        for link in flight['task_links']:
            if link not in seen:
                seen.add(link)
                unique.append(link)

        flight['task_links'] = unique

    return flights


def task_links(soup, base):
    """
    Backwards-compatible fallback for event pages that do not expose
    their Flights & Tasks grouping in the expected heading structure.
    """

    out = {}

    for a in soup.find_all('a', href=True):
        href = urljoin(base, a['href'])

        q = parse_qs(urlparse(href).query)

        if 'tid' not in q or q.get('v', [''])[0] != 'tr':
            continue

        tid = q['tid'][0]
        out[tid] = href

    return list(out.values())


# ---------------------------------------------------------------------------
# TASK PARSING
# ---------------------------------------------------------------------------

def parse_task(html, url, event_id):
    soup = BeautifulSoup(html, 'html.parser')
    text = clean(soup.get_text(' ', strip=True))

    m = re.search(
        r'Task\s+(\d+)\s*[—-]\s*(.*?)\s*'
        r'\(Rule:\s*([^\)]+)\)\s*[—-]\s*'
        r'(Final|Provisional|Official[^\s]*)',
        text,
        re.I
    )

    if m:
        task_no = int(m.group(1))
        name = clean(m.group(2))
        status = norm_status(m.group(4))

    else:
        m = re.search(
            r'Task\s+(\d+)\s*[—-]\s*(.*?)'
            r'(?:\s*[—-]\s*(Final|Provisional|Official[^\s]*))?'
            r'\s+Published:',
            text,
            re.I
        )

        if not m:
            # Cancelled tasks can be displayed without a result table.
            m = re.search(
                r'Task\s+(\d+)\s*[—-]\s*(.*?)(?:\s+CANCEL(?:LED|ED))',
                text,
                re.I
            )

            if not m:
                return None

            task_no = int(m.group(1))
            name = clean(m.group(2))
            status = 'CANCELLED'

        else:
            task_no = int(m.group(1))
            name = clean(m.group(2))
            status = norm_status(m.group(3) or '')

    pm = re.search(
        r'Published:\s*([^\n]+?)(?:\s+by\s+|\s+Print\b)',
        text,
        re.I
    )

    published = clean(pm.group(1)) if pm else ''

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

            rows.append({
                'competition_number': comp_no,
                'pilot': pname,
                'country': country,
                'rank': (
                    int(d.get('rank', '').replace(',', ''))
                    if d.get('rank', '').replace(',', '').isdigit()
                    else None
                ),
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
        'rows': rows
    }


# ---------------------------------------------------------------------------
# DATABASE MIGRATION
# ---------------------------------------------------------------------------

def ensure_flight_columns(c):
    """
    Safely add the new flight metadata columns to an existing database.

    PostgreSQL and SQLite both support ADD COLUMN IF NOT EXISTS in modern
    deployments, but the fallback keeps this safe for older SQLite files.
    """

    columns = {
        'status': "TEXT",
        'flight_type': "TEXT"
    }

    for column, definition in columns.items():

        try:
            if is_postgres():
                c.execute(
                    f"ALTER TABLE flights "
                    f"ADD COLUMN IF NOT EXISTS {column} {definition}"
                )
            else:
                try:
                    c.execute(
                        f"ALTER TABLE flights "
                        f"ADD COLUMN {column} {definition}"
                    )
                except Exception as e:
                    # Existing SQLite databases throw duplicate-column
                    # errors. That is safe to ignore.
                    if 'duplicate column' not in str(e).lower():
                        raise

        except Exception:
            # If PostgreSQL is using a compatibility layer or an older
            # server, inspect the table before attempting a second form.
            try:
                rows = c.execute(
                    "SELECT column_name "
                    "FROM information_schema.columns "
                    "WHERE table_name='flights'"
                ).fetchall()

                existing = {
                    row[0] if not hasattr(row, 'keys')
                    else row['column_name']
                    for row in rows
                }

                if column not in existing:
                    c.execute(
                        f"ALTER TABLE flights "
                        f"ADD COLUMN {column} {definition}"
                    )

            except Exception:
                # Do not hide a genuine migration failure.
                raise


# ---------------------------------------------------------------------------
# IMPORT
# ---------------------------------------------------------------------------

def import_event(url, out_root):

    session = requests.Session()
    session.headers.update({'User-Agent': UA})

    event_id = (
        parse_qs(urlparse(url).query).get('e', [''])[0]
        or stable(url)
    )

    event_html = fetch(session, url)
    meta = parse_event(event_html, url)

    soup = BeautifulSoup(event_html, 'html.parser')

    # First attempt: read actual WatchMeFly flight grouping.
    flight_defs = task_links_with_flights(soup, url)

    # If the base event page does not expose the task grouping, try
    # the Task Data view explicitly.
    if not any(f['task_links'] for f in flight_defs):

        for suffix in ('&v=t', '&v=tr'):

            try:
                view_url = (
                    url + suffix
                    if '?' in url
                    else url + '?v=t'
                )

                h = fetch(session, view_url)
                vsoup = BeautifulSoup(h, 'html.parser')

                flight_defs = task_links_with_flights(
                    vsoup,
                    url
                )

                if any(f['task_links'] for f in flight_defs):
                    break

            except Exception:
                pass

    # ------------------------------------------------------------------
    # Parse tasks while retaining their flight association.
    # ------------------------------------------------------------------

    parsed_flights = []
    errors = []

    if flight_defs:

        for fdef in flight_defs:

            flight_tasks = []

            for link in fdef['task_links']:

                try:
                    parsed = parse_task(
                        fetch(session, link),
                        link,
                        event_id
                    )

                    if parsed:
                        flight_tasks.append(parsed)

                except Exception as e:
                    errors.append({
                        'url': link,
                        'flight': fdef['heading'],
                        'error': str(e)
                    })

            parsed_flights.append({
                **fdef,
                'tasks': flight_tasks
            })

    else:
        # ------------------------------------------------------------------
        # Legacy fallback.
        #
        # We do not silently manufacture flight groupings from task numbers.
        # If WatchMeFly does not expose flight grouping, create a single
        # UNKNOWN competition flight so the data remains importable without
        # pretending we know the correct flight structure.
        # ------------------------------------------------------------------

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

        fallback_tasks = []

        for link in links:

            try:
                parsed = parse_task(
                    fetch(session, link),
                    link,
                    event_id
                )

                if parsed:
                    fallback_tasks.append(parsed)

            except Exception as e:
                errors.append({
                    'url': link,
                    'error': str(e)
                })

        parsed_flights.append({
            'flight_number': '',
            'date_label': '',
            'time_label': '',
            'flight_type': 'UNKNOWN',
            'status': 'UNKNOWN',
            'heading': 'UNKNOWN FLIGHT GROUP',
            'task_links': links,
            'tasks': fallback_tasks
        })

    # ------------------------------------------------------------------
    # Deduplicate tasks while preserving re-flown occurrences.
    # ------------------------------------------------------------------

    for flight in parsed_flights:

        unique = {}

        for task in flight['tasks']:

            key = stable(
                event_id,
                task['task_number'],
                task['published'],
                task['source_url']
            )

            unique[key] = task

        flight['tasks'] = list(unique.values())

    # ------------------------------------------------------------------
    # Sort flights chronologically.
    #
    # WatchMeFly displays newest first. The database needs oldest first so
    # sort_order can be used safely for progression and movement.
    # ------------------------------------------------------------------

    def flight_sort_key(flight):

        date_text = clean(flight.get('date_label', ''))
        time_text = clean(flight.get('time_label', '')).upper()

        try:
            dt = datetime.strptime(
                f'{date_text} {time_text}'.strip(),
                '%d %b %Y %p'
            )

            return (
                0 if flight.get('flight_type') == 'COMPETITION' else 1,
                dt
            )

        except Exception:
            return (
                2,
                date_text,
                time_text,
                flight.get('flight_number', '')
            )

    parsed_flights.sort(key=flight_sort_key)

    # ------------------------------------------------------------------
    # Database setup.
    # ------------------------------------------------------------------

    root = Path(out_root) / event_id
    root.mkdir(parents=True, exist_ok=True)

    if is_postgres():

        c = connect()
        init_postgres(c)

    else:

        dbp = root / 'competition.db'
        c = connect(dbp)
        c.executescript(SCHEMA)

    # Existing PostgreSQL databases need the new flight columns.
    ensure_flight_columns(c)

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

    all_tasks = [
        task
        for flight in parsed_flights
        for task in flight['tasks']
    ]

    run_id = stable(
        event_id,
        datetime.now(timezone.utc).isoformat(),
        len(all_tasks)
    )

    imported = datetime.now(timezone.utc).isoformat()

    c.execute(
        """
        INSERT INTO import_runs(
            id,competition_id,imported_at,source_url,
            record_count,error_count
        )
        VALUES (?,?,?,?,?,?)
        """,
        (
            run_id,
            event_id,
            imported,
            url,
            sum(len(t['rows']) for t in all_tasks),
            len(errors)
        )
    )

    # ------------------------------------------------------------------
    # Write FLIGHTS first.
    # ------------------------------------------------------------------

    flight_records = []

    for sort_order, flight in enumerate(parsed_flights):

        fid = 'flight-' + stable(
            event_id,
            flight.get('flight_type', ''),
            flight.get('flight_number', ''),
            flight.get('date_label', ''),
            flight.get('time_label', '')
        )

        flight_records.append((fid, flight, sort_order))

        if is_postgres():

            c.execute(
                """
                INSERT INTO flights(
                    id,competition_id,flight_number,date_label,
                    time_label,sort_order,source_url,status,flight_type
                )
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    flight_number=EXCLUDED.flight_number,
                    date_label=EXCLUDED.date_label,
                    time_label=EXCLUDED.time_label,
                    sort_order=EXCLUDED.sort_order,
                    source_url=EXCLUDED.source_url,
                    status=EXCLUDED.status,
                    flight_type=EXCLUDED.flight_type
                """,
                (
                    fid,
                    event_id,
                    flight.get('flight_number', ''),
                    flight.get('date_label', ''),
                    flight.get('time_label', ''),
                    sort_order,
                    flight.get('task_links', [''])[0]
                    if flight.get('task_links')
                    else url,
                    flight.get('status', 'UNKNOWN'),
                    flight.get('flight_type', 'UNKNOWN')
                )
            )

        else:

            c.execute(
                """
                INSERT OR IGNORE INTO flights(
                    id,competition_id,flight_number,date_label,
                    time_label,sort_order,source_url,status,flight_type
                )
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    fid,
                    event_id,
                    flight.get('flight_number', ''),
                    flight.get('date_label', ''),
                    flight.get('time_label', ''),
                    sort_order,
                    flight.get('task_links', [''])[0]
                    if flight.get('task_links')
                    else url,
                    flight.get('status', 'UNKNOWN'),
                    flight.get('flight_type', 'UNKNOWN')
                )
            )

            c.execute(
                """
                UPDATE flights
                SET flight_number=?,
                    date_label=?,
                    time_label=?,
                    sort_order=?,
                    status=?,
                    flight_type=?
                WHERE id=?
                """,
                (
                    flight.get('flight_number', ''),
                    flight.get('date_label', ''),
                    flight.get('time_label', ''),
                    sort_order,
                    flight.get('status', 'UNKNOWN'),
                    flight.get('flight_type', 'UNKNOWN'),
                    fid
                )
            )

    # ------------------------------------------------------------------
    # Write TASKS and RESULTS.
    # ------------------------------------------------------------------

    for fid, flight, sort_order in flight_records:

        for task in flight['tasks']:

            tid = 'task-' + stable(
                event_id,
                task['task_number'],
                task['published'],
                task['source_url']
            )

            if is_postgres():

                c.execute(
                    """
                    INSERT INTO tasks(
                        id,competition_id,task_number,name,status,
                        flight_id,published,source_url
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
                        task['task_number'],
                        task['name'],
                        task['status'],
                        fid,
                        task['published'],
                        task['source_url']
                    )
                )

            else:

                c.execute(
                    """
                    INSERT OR REPLACE INTO tasks(
                        id,competition_id,task_number,name,status,
                        flight_id,published,source_url
                    )
                    VALUES (?,?,?,?,?,?,?,?)
                    """,
                    (
                        tid,
                        event_id,
                        task['task_number'],
                        task['name'],
                        task['status'],
                        fid,
                        task['published'],
                        task['source_url']
                    )
                )

            # Results are only written when WatchMeFly actually provides
            # pilot result rows.
            for r in task['rows']:

                if is_postgres():

                    c.execute(
                        """
                        INSERT INTO pilots(
                            competition_id,competition_number,
                            name,country
                        )
                        VALUES (?,?,?,?)
                        ON CONFLICT(
                            competition_id,competition_number
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
                            competition_id,competition_number,
                            name,country
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
                        import_run_id,task_id,pilot_id,rank,
                        result,points,penalty_t,penalty_c,
                        score,notes,status,source_url
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
                        task['status'],
                        task['source_url']
                    )
                )

    c.commit()
    c.close()

    # Save a useful diagnostic snapshot of the imported flight structure.
    flight_debug = []

    for fid, flight, sort_order in flight_records:

        flight_debug.append({
            'id': fid,
            'sort_order': sort_order,
            'flight_number': flight.get('flight_number', ''),
            'date_label': flight.get('date_label', ''),
            'time_label': flight.get('time_label', ''),
            'status': flight.get('status', 'UNKNOWN'),
            'flight_type': flight.get('flight_type', 'UNKNOWN'),
            'tasks': [
                {
                    'task_number': t['task_number'],
                    'name': t['name'],
                    'status': t['status'],
                    'published': t['published'],
                    'results': len(t['rows'])
                }
                for t in flight['tasks']
            ]
        })

    (root / 'flight_structure.json').write_text(
        json.dumps(
            flight_debug,
            indent=2,
            ensure_ascii=False
        ),
        encoding='utf-8'
    )

    (root / 'errors.json').write_text(
        json.dumps(
            errors,
            indent=2,
            ensure_ascii=False
        ),
        encoding='utf-8'
    )

    return (
        event_id,
        run_id,
        len(all_tasks),
        sum(len(t['rows']) for t in all_tasks),
        errors
    )


if __name__ == '__main__':

    ap = argparse.ArgumentParser(
        description=(
            'Import a WatchMeFly competition into the '
            'Balloon Competition database'
        )
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
