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
    """Extract competition metadata from a WatchMeFly event page.

    WatchMeFly currently uses a generic HTML page title ("WatchMeFly | Event")
    and presents the real competition title/location/dates in the page body.
    Older pages exposed labelled table fields, so keep those as fallbacks.
    """
    soup = BeautifulSoup(html, 'html.parser')

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

    # Older WatchMeFly pages sometimes expose explicit metadata labels.
    title = ''
    m = re.search(
        r'Event title:\s*([^|]+?)(?:\s+Event Location:|\s+Event Dates:)',
        text,
        re.I
    )
    if m:
        title = clean(m.group(1))

    # Current WatchMeFly pages use a generic <title> but put the real event
    # name in a heading near the top of the page.
    if not title:
        ignored = {
            'event details', 'results', 'task data', 'noticeboard',
            'pilots', 'officials', 'details', 'tasks', 'enb'
        }
        for tag in soup.find_all(['h1', 'h2', 'h3']):
            candidate = clean(tag.get_text(' ', strip=True))
            if not candidate:
                continue
            if candidate.lower() in ignored:
                continue
            if re.match(r'^(?:practice\s+)?flight\s+\d+', candidate, re.I):
                continue
            if re.match(r'^task\s+\d+', candidate, re.I):
                continue
            title = candidate
            break

    if not title:
        title = clean(soup.title.get_text()) if soup.title else ''

    title = title.replace('WatchMeFly |', '').strip()
    if title.lower() == 'event':
        title = ''

    location = fields.get('Event Location', '')
    dates = fields.get('Event Dates', '')

    # Current WatchMeFly pages show the location in the DOM immediately after
    # the event heading. Do not search the flattened page text here because
    # navigation text can contain "Home Competitions <event title>".
    if title and not location:
        for tag in soup.find_all(['h1', 'h2', 'h3']):
            if clean(tag.get_text(' ', strip=True)) != title:
                continue
            for node in tag.next_elements:
                if getattr(node, 'name', None) in {'h1', 'h2', 'h3'}:
                    break
                if getattr(node, 'name', None) == 'a':
                    candidate = clean(node.get_text(' ', strip=True))
                    if candidate.lower() == 'image':
                        continue
                elif isinstance(node, str):
                    candidate = clean(str(node))
                else:
                    continue
                if not candidate:
                    continue
                if candidate.lower().startswith('local time:'):
                    break
                if candidate.lower() == 'image':
                    continue
                if candidate.lower() not in {'event details', 'results', 'task data', 'noticeboard', 'pilots', 'officials', 'details', 'tasks', 'enb'}:
                    location = candidate.split('Local Time:', 1)[0].strip()
                    break
            if location:
                break

    if not dates:
        range_patterns = [
            r'(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}\s*[-–]\s*\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})',
            r'(\d{1,2}[./-]\d{1,2}[./-]\d{4}\s*[-–]\s*\d{1,2}[./-]\d{1,2}[./-]\d{4})',
        ]
        for pattern in range_patterns:
            m = re.search(pattern, text)
            if m:
                dates = clean(m.group(1))
                break

    organiser = fields.get('Organiser', '')
    director = (
        fields.get('Event Director', '')
        or fields.get('Director', '')
    )

    if not director:
        m = re.search(r'\bDirector:\s*([^|]+?)(?=\s+Combined Logger/Marker Event|\s+Contact Details|$)', text, re.I)
        if m:
            director = clean(m.group(1))

    return {
        'title': title or url,
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

            # WatchMeFly's result-table HTML is not completely consistent:
            # in some rows the country is separated from the pilot by the
            # image element, while in others BeautifulSoup's flattened text
            # becomes e.g. "FUJITA, YudaiImage Japan" or
            # "FUJITA, Yudai Japan".  Do not rely solely on the literal
            # "Image" text being present.
            #
            # First try the text following the image marker.
            if 'image' in ptxt.lower():
                tail = clean(ptxt.split('Image', 1)[1])
                if tail:
                    country = tail

            # If the flattened pilot cell contains a country suffix, split
            # the country from the pilot name.  Longest names are checked
            # first so "United States" and "South Africa" are handled before
            # shorter suffixes.
            COUNTRY_NAMES = [
                'United States', 'South Africa', 'New Zealand',
                'Czech Republic', 'United Kingdom', 'The Netherlands',
                'Netherlands', 'Saudi Arabia', 'United Arab Emirates',
                'South Korea', 'North Macedonia', 'Costa Rica',
                'Dominican Republic', 'Hong Kong', 'Chinese Taipei',
                'Armenia', 'Australia', 'Austria', 'Belgium', 'Brazil',
                'Canada', 'China', 'Colombia', 'Croatia', 'Denmark',
                'Estonia', 'Finland', 'France', 'Georgia', 'Germany',
                'Greece', 'Hungary', 'India', 'Ireland', 'Israel', 'Italy',
                'Japan', 'Latvia', 'Lithuania', 'Luxembourg', 'Mexico',
                'Moldova', 'Norway', 'Poland', 'Portugal', 'Romania',
                'Serbia', 'Singapore', 'Slovakia', 'Slovenia', 'Spain',
                'Sweden', 'Switzerland', 'Turkey', 'Ukraine'
            ]

            # The parser above may already have extracted the country.  If
            # not, recover it from a country suffix in the pilot text.
            if not country:
                pilot_text = clean(pname)
                for cname in sorted(COUNTRY_NAMES, key=len, reverse=True):
                    if re.search(r'\s+' + re.escape(cname) + r'$', pilot_text, re.I):
                        pname = clean(pilot_text[:-len(cname)])
                        country = cname
                        break

            # If the country was extracted but the pilot name still contains
            # the same country suffix, remove the duplicate suffix.
            if country:
                pname = re.sub(
                    r'\s+' + re.escape(country) + r'$',
                    '',
                    pname,
                    flags=re.I
                ).strip()

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
    # Parse only task publications that are not already in the database.
    #
    # WatchMeFly exposes each published version as a task-result URL.  The
    # importer stores that exact source_url, so comparing URLs lets us skip
    # old publications while still importing a newly published provisional,
    # official, or final version of the same task.
    #
    # This is deliberately conservative: if the database lookup fails, do
    # not skip anything.  The importer falls back to the previous full-fetch
    # behaviour rather than risk missing a result publication.
    # ------------------------------------------------------------------

    existing_task_urls = set()

    try:
        check_root = Path(out_root) / event_id

        if is_postgres():
            check_conn = connect()
            init_postgres(check_conn)
        else:
            check_db = check_root / 'competition.db'
            check_conn = connect(check_db) if check_db.exists() else None

        if check_conn is not None:
            rows = check_conn.execute(
                """
                SELECT source_url
                FROM tasks
                WHERE competition_id=?
                  AND source_url IS NOT NULL
                  AND source_url <> ''
                """,
                (event_id,)
            ).fetchall()

            existing_task_urls = {
                row['source_url']
                for row in rows
                if row['source_url']
            }

            check_conn.close()

    except Exception as e:
        print(
            f"  Incremental task check failed for {event_id}; "
            f"falling back to full task fetch: {e}"
        )
        existing_task_urls = set()

    total_task_links = sum(
        len(f.get('task_links', []))
        for f in flight_defs
    )

    new_task_links = sum(
        1
        for f in flight_defs
        for link in f.get('task_links', [])
        if link not in existing_task_urls
    )

    skipped_task_links = total_task_links - new_task_links

    print(
        f"  {event_id}: {total_task_links} task publications found; "
        f"{skipped_task_links} already imported; "
        f"{new_task_links} to fetch"
    )

    parsed_flights = []
    errors = []

    if flight_defs:

        for fdef in flight_defs:

            flight_tasks = []

            for link in fdef['task_links']:

                # An exact source URL means this publication has already
                # been imported.  Do not download the old result page again.
                if link in existing_task_urls:
                    continue

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

            # Apply the same exact-publication check to the legacy fallback.
            if link in existing_task_urls:
                continue

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

    # PostgreSQL already has the flight metadata columns in the deployed
    # database, so do not run ALTER TABLE during every automatic import.
    # Keep the migration for SQLite/local databases.
    if not is_postgres():
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

    # Build a deterministic snapshot hash before writing an import run.
    # The watcher expects import_event() to return (.., changed, snapshot_hash)
    # and uses this to avoid creating duplicate runs when WatchMeFly has not
    # changed.
    snapshot_payload = {
        'event': meta,
        'flights': [
            {
                'flight_number': f.get('flight_number', ''),
                'date_label': f.get('date_label', ''),
                'time_label': f.get('time_label', ''),
                'sort_order': i,
                'status': f.get('status', 'UNKNOWN'),
                'flight_type': f.get('flight_type', 'UNKNOWN'),
            }
            for i, f in enumerate(parsed_flights)
        ],
        'tasks': sorted([
            {
                'flight_number': next((f.get('flight_number', '') for f in parsed_flights if t in f.get('tasks', [])), ''),
                'task_number': t.get('task_number'),
                'name': t.get('name', ''),
                'status': t.get('status', 'UNKNOWN'),
                'published': t.get('published', ''),
                'source_url': t.get('source_url', ''),
            }
            for t in all_tasks
        ], key=lambda x: json.dumps(x, sort_keys=True)),
        'results': sorted([
            {
                'task_number': t.get('task_number'),
                'task_source_url': t.get('source_url', ''),
                'competition_number': r.get('competition_number'),
                'pilot': r.get('pilot', ''),
                'country': r.get('country', ''),
                'rank': r.get('rank'),
                'result': r.get('result', ''),
                'points': r.get('points'),
                'penalty_t': r.get('penalty_t'),
                'penalty_c': r.get('penalty_c'),
                'score': r.get('score'),
                'notes': r.get('notes', ''),
                'status': t.get('status', 'UNKNOWN'),
            }
            for t in all_tasks for r in t.get('rows', [])
        ], key=lambda x: json.dumps(x, sort_keys=True)),
        'errors': sorted(errors, key=lambda x: json.dumps(x, sort_keys=True)),
    }
    snapshot_hash = hashlib.sha256(
        json.dumps(snapshot_payload, sort_keys=True, separators=(',', ':')).encode('utf-8')
    ).hexdigest()

    # PostgreSQL already has snapshot_hash in the deployed database.
    # Do not run ALTER TABLE during every automatic import.
    # Keep the migration for older/local SQLite databases.
    if not is_postgres():
        columns = [row[1] for row in c.raw.execute('PRAGMA table_info(import_runs)').fetchall()]
        if 'snapshot_hash' not in columns:
            c.raw.execute('ALTER TABLE import_runs ADD COLUMN snapshot_hash TEXT')

    latest = c.execute(
        "SELECT id, snapshot_hash FROM import_runs WHERE competition_id=? ORDER BY imported_at DESC LIMIT 1",
        (event_id,)
    ).fetchone()

    if latest and latest['snapshot_hash'] == snapshot_hash:
        (root / 'errors.json').write_text(
            json.dumps(errors, indent=2, ensure_ascii=False),
            encoding='utf-8'
        )
        c.close()
        return (
            event_id,
            latest['id'],
            len(all_tasks),
            sum(len(t['rows']) for t in all_tasks),
            errors,
            False,
            snapshot_hash
        )

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
            record_count,error_count,snapshot_hash
        )
        VALUES (?,?,?,?,?,?,?)
        """,
        (
            run_id,
            event_id,
            imported,
            url,
            sum(len(t['rows']) for t in all_tasks),
            len(errors),
            snapshot_hash
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
                    source_url=?,
                    status=?,
                    flight_type=?
                WHERE id=?
                """,
                (
                    flight.get('flight_number', ''),
                    flight.get('date_label', ''),
                    flight.get('time_label', ''),
                    sort_order,
                    flight.get('task_links', [''])[0]
                    if flight.get('task_links')
                    else url,
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
                    ON CONFLICT(
                        competition_id,
                        task_number,
                        published,
                        source_url
                    )
                    DO UPDATE SET
                        name=EXCLUDED.name,
                        status=EXCLUDED.status,
                        flight_id=EXCLUDED.flight_id
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

                existing_task = c.execute(
                    """
                    SELECT id
                    FROM tasks
                    WHERE competition_id=?
                      AND task_number=?
                      AND published=?
                      AND source_url=?
                    LIMIT 1
                    """,
                    (
                        event_id,
                        task['task_number'],
                        task['published'],
                        task['source_url']
                    )
                ).fetchone()

                if existing_task:
                    tid = existing_task[0]
                    c.execute(
                        """
                        UPDATE tasks
                        SET name=?,
                            status=?,
                            flight_id=?
                        WHERE id=?
                        """,
                        (
                            task['name'],
                            task['status'],
                            fid,
                            tid
                        )
                    )
                else:
                    c.execute(
                        """
                        INSERT INTO tasks(
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
        errors,
        True,
        snapshot_hash
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
                        'errors',
                        'changed',
                        'snapshot_hash'
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
