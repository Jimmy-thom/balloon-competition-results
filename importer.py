from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
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


# ---------------------------------------------------------------------------
# Basic helpers
# ---------------------------------------------------------------------------

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


def stable(*parts):
    return hashlib.sha1(
        '|'.join(clean(str(x)) for x in parts).encode()
    ).hexdigest()[:20]


def fetch(session, url):
    r = session.get(
        url,
        timeout=30,
        headers={'User-Agent': UA}
    )
    r.raise_for_status()
    return r.text


# ---------------------------------------------------------------------------
# Event parsing
# ---------------------------------------------------------------------------

def parse_event(html, url):
    soup = BeautifulSoup(html, 'html.parser')

    title = clean(
        soup.title.get_text()
    ) if soup.title else ''

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

    text = clean(
        soup.get_text(' ', strip=True)
    )

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
# Flight parsing
# ---------------------------------------------------------------------------

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

        Flight 4 - 15 Aug 2026 AM COMPLETE
        Flight 3 - 12 Aug 2026 AM CANCELLED
        Practice Flight 1 - 9 Aug 2026 AM COMPLETE
    """

    text = clean(text)

    # The status at the end of the heading is deliberately optional.
    # We preserve it separately if present.
    m = re.match(
        r'^(Practice\s+)?Flight\s+(\d+)\s*[-–]\s*'
        r'(.*?)\s+(AM|PM)'
        r'(?:\s+(COMPLETE|CANCELLED|CANCELLED\s+.*))?$',
        text,
        re.I
    )

    if not m:
        return None

    practice = bool(m.group(1))

    flight_number = m.group(2)
    date_label = clean(m.group(3))
    time_label = m.group(4).upper()

    heading_status = norm_status(m.group(5) or '')

    if practice:
        display_number = f'Practice {flight_number}'
    else:
        display_number = flight_number

    return {
        'flight_number': display_number,
        'date_label': date_label,
        'time_label': time_label,
        'is_practice': practice,
        'status': heading_status,
        'heading': text,
        'tasks': []
    }


def flight_id(event_id, flight):
    return 'flight-' + stable(
        event_id,
        flight.get('flight_number', ''),
        flight.get('date_label', ''),
        flight.get('time_label', '')
    )


# ---------------------------------------------------------------------------
# Task-card parsing
# ---------------------------------------------------------------------------

TASK_PATTERN = re.compile(
    r'^(?:Task|Practice)\s+(\d+)\s*[-–]\s*(.*)$',
    re.I
)


def parse_task_label(text):
    """
    Parse visible task-card text.

    Examples:

        Task 17 - Pilot Declared Goal
        Task 18 - Gordon Bennett Memorial - FINAL
        Task 12 - Hesitation Waltz - CANCELLED
        Practice 1 - Fly In - OFFICIAL
    """

    text = clean(text)

    m = TASK_PATTERN.match(text)

    if not m:
        return None

    number = int(m.group(1))
    remainder = clean(m.group(2))

    status = 'UNKNOWN'

    # Status is normally the final word/phrase.
    status_match = re.search(
        r'\s*[-–]\s*(FINAL|PROVISIONAL|OFFICIAL|CANCELLED)\s*$',
        remainder,
        re.I
    )

    if status_match:
        status = norm_status(status_match.group(1))
        remainder = clean(
            remainder[:status_match.start()]
        )

    # A visible "(Rule: ...)" is not part of the task name.
    remainder = re.sub(
        r'\s*\(Rule:\s*[^)]*\)',
        '',
        remainder,
        flags=re.I
    )

    name = clean(remainder)

    return {
        'task_number': number,
        'name': name,
        'status': status
    }


def task_source_for_flight(flight, task_number, flight_url):
    """
    Create a stable URL-like identifier for a task that has no results page.

    We deliberately include the flight information so that, for example,
    Task 12 on Flight 3 on one date cannot collide with Task 12 on another
    Flight 3 occurrence.
    """

    fragment = stable(
        flight.get('flight_number', ''),
        flight.get('date_label', ''),
        flight.get('time_label', ''),
        task_number
    )

    return f'{flight_url}#task-{task_number}-{fragment}'


def task_entries_from_card(card):
    """
    Find task entries inside a WatchMeFly flight card.

    We inspect links as well as visible text elements because cancelled
    tasks and unpublished tasks may not have result links.
    """

    entries = []
    seen = set()

    # ---------------------------------------------------------------
    # First: result links.
    # ---------------------------------------------------------------

    for a in card.find_all('a', href=True):

        href = a.get('href', '')
        text = clean(a.get_text(' ', strip=True))

        parsed = parse_task_label(text)

        if not parsed:
            continue

        absolute = urljoin(
            str(card.get('data-base-url', '') or ''),
            href
        )

        q = parse_qs(
            urlparse(absolute).query
        )

        if 'tid' not in q:
            continue

        if q.get('v', [''])[0] != 'tr':
            continue

        key = (
            parsed['task_number'],
            absolute
        )

        if key in seen:
            continue

        seen.add(key)

        entries.append({
            **parsed,
            'url': absolute,
            'tid': q['tid'][0],
            'has_result_page': True,
            'link_text': text
        })

    # ---------------------------------------------------------------
    # Second: visible task text.
    #
    # This catches tasks without result links, including cancelled
    # tasks and tasks that have not yet been published.
    # ---------------------------------------------------------------

    for tag in card.find_all(
        ['a', 'span', 'div', 'p', 'li', 'strong', 'b', 'td']
    ):

        text = clean(
            tag.get_text(' ', strip=True)
        )

        parsed = parse_task_label(text)

        if not parsed:
            continue

        # If this is an ancestor containing several task entries,
        # don't treat the entire block as one task.
        if len(
            re.findall(
                r'(?:Task|Practice)\s+\d+\s*[-–]',
                text,
                re.I
            )
        ) > 1:
            continue

        key = (
            parsed['task_number'],
            parsed['name'],
            parsed['status']
        )

        if key in seen:
            continue

        seen.add(key)

        entries.append({
            **parsed,
            'url': None,
            'tid': None,
            'has_result_page': False,
            'link_text': text
        })

    return entries


def flight_task_links(soup, base):
    """
    Discover ALL flights and ALL tasks from the WatchMeFly Flights & Tasks
    page.

    The important rule here is:

        Flight cards tell us what exists.
        Result links tell us whether detailed results are available.

    Therefore a task without a result link is still imported.
    """

    flights = []

    # ---------------------------------------------------------------
    # Identify flight headings.
    # ---------------------------------------------------------------

    headings = []

    for tag in soup.find_all([
        'h1', 'h2', 'h3', 'h4', 'h5', 'h6'
    ]):

        text = clean(
            tag.get_text(' ', strip=True)
        )

        parsed = parse_flight_heading(text)

        if parsed:
            headings.append(
                (tag, parsed)
            )

    # ---------------------------------------------------------------
    # Build each flight card.
    #
    # We first try the heading's parent containers. This allows the
    # parser to capture the complete card rather than relying on
    # document-wide link ordering.
    # ---------------------------------------------------------------

    for heading_tag, flight in headings:

        card = None

        # Walk upwards looking for a sensible containing block.
        parent = heading_tag.parent

        for _ in range(6):

            if parent is None:
                break

            text = clean(
                parent.get_text(' ', strip=True)
            )

            task_count = len(
                re.findall(
                    r'(?:Task|Practice)\s+\d+\s*[-–]',
                    text,
                    re.I
                )
            )

            if task_count > 0:
                card = parent
                break

            parent = parent.parent

        # If no task-containing parent was found, use the heading
        # itself as a fallback. The flight will still be preserved.
        if card is None:
            card = heading_tag

        # Make the base URL available to task_entries_from_card().
        card['data-base-url'] = base

        entries = task_entries_from_card(card)

        # Remove helper attribute.
        try:
            del card['data-base-url']
        except Exception:
            pass

        flight['tasks'] = entries

        flights.append(flight)

    # ---------------------------------------------------------------
    # Fallback document-order parser.
    #
    # If the page structure is unusual and the cards above failed to
    # find tasks, use the older heading/link association method.
    # ---------------------------------------------------------------

    if flights and sum(
        len(f.get('tasks', []))
        for f in flights
    ) == 0:

        current = None

        for tag in soup.find_all([
            'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'a'
        ]):

            if tag.name.startswith('h'):

                parsed = parse_flight_heading(
                    tag.get_text(' ', strip=True)
                )

                if parsed:
                    current = parsed
                    current['tasks'] = []
                    flights.append(current)

                continue

            if tag.name != 'a' or current is None:
                continue

            text = clean(
                tag.get_text(' ', strip=True)
            )

            parsed = parse_task_label(text)

            if not parsed:
                continue

            href = urljoin(
                base,
                tag.get('href', '')
            )

            q = parse_qs(
                urlparse(href).query
            )

            if 'tid' not in q:
                continue

            if q.get('v', [''])[0] != 'tr':
                continue

            current['tasks'].append({
                **parsed,
                'url': href,
                'tid': q['tid'][0],
                'has_result_page': True,
                'link_text': text
            })

    return flights


# ---------------------------------------------------------------------------
# Legacy task-link discovery
# ---------------------------------------------------------------------------

def task_links(soup, base):
    """
    Backwards-compatible task link discovery.

    Used only if the Flights & Tasks page itself cannot be parsed.
    """

    out = {}

    for a in soup.find_all('a', href=True):

        href = urljoin(
            base,
            a['href']
        )

        q = parse_qs(
            urlparse(href).query
        )

        if 'tid' not in q:
            continue

        if q.get('v', [''])[0] != 'tr':
            continue

        tid = q['tid'][0]

        out[tid] = href

    return list(out.values())


# ---------------------------------------------------------------------------
# Detailed result-page parser
# ---------------------------------------------------------------------------

def parse_task(
    html,
    url,
    event_id,
    flight=None,
    link_text=''
):
    soup = BeautifulSoup(
        html,
        'html.parser'
    )

    text = clean(
        soup.get_text(' ', strip=True)
    )

    task_no = None
    name = ''
    status = 'UNKNOWN'

    # ---------------------------------------------------------------
    # Normal task heading.
    # ---------------------------------------------------------------

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
            status = norm_status(
                m.group(3) or ''
            )

    # ---------------------------------------------------------------
    # Link text fallback.
    # ---------------------------------------------------------------

    if task_no is None and link_text:

        parsed = parse_task_label(
            link_text
        )

        if parsed:

            task_no = parsed['task_number']
            name = parsed['name']
            status = parsed['status']

    if task_no is None:
        return None

    # ---------------------------------------------------------------
    # Published date.
    # ---------------------------------------------------------------

    pm = re.search(
        r'Published:\s*([^\n]+?)(?:\s+by\s+|\s+Print\b)',
        text,
        re.I
    )

    published = clean(
        pm.group(1)
    ) if pm else ''

    # ---------------------------------------------------------------
    # Result table.
    # ---------------------------------------------------------------

    table = None

    for t in soup.find_all('table'):

        hs = [
            clean(
                x.get_text(' ', strip=True)
            ).lower()
            for x in t.find_all('th')
        ]

        if 'pilot' in hs and 'score' in hs:
            table = t
            break

    rows = []

    if table:

        headers = [
            clean(
                x.get_text(' ', strip=True)
            )
            for x in table.find_all('th')
        ]

        for tr in table.find_all('tr'):

            cells = [
                clean(
                    x.get_text(' ', strip=True)
                )
                for x in tr.find_all(
                    ['td', 'th']
                )
            ]

            if len(cells) < len(headers):
                continue

            if cells == headers:
                continue

            d = {
                headers[i].lower(): cells[i]
                for i in range(
                    min(len(headers), len(cells))
                )
            }

            ptxt = d.get('pilot', '')

            pmatch = re.search(
                r'#\s*(\d+)\s*-\s*(.*?)(?:Image|$)',
                ptxt,
                re.I
            )

            if not pmatch:
                continue

            comp_no = int(
                pmatch.group(1)
            )

            pname = clean(
                pmatch.group(2)
            ).rstrip(',')

            country = ''

            if 'image' in ptxt.lower():

                tail = clean(
                    ptxt.split(
                        'Image',
                        1
                    )[1]
                )

                if tail:
                    country = tail

            rank_text = d.get(
                'rank',
                ''
            ).replace(',', '')

            rows.append({
                'competition_number': comp_no,
                'pilot': pname,
                'country': country,
                'rank': int(rank_text)
                if rank_text.isdigit()
                else None,
                'result': d.get(
                    'result',
                    ''
                ),
                'points': num(
                    d.get('points', '')
                ),
                'penalty_t': num(
                    d.get('penalty (t)', '')
                ),
                'penalty_c': num(
                    d.get('penalty (c)', '')
                ),
                'score': num(
                    d.get('score', '')
                ),
                'notes': d.get(
                    'notes',
                    ''
                )
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


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def import_event(url, out_root):

    session = requests.Session()

    session.headers.update({
        'User-Agent': UA
    })

    event_id = (
        parse_qs(
            urlparse(url).query
        ).get('e', [''])[0]
        or stable(url)
    )

    event_html = fetch(
        session,
        url
    )

    meta = parse_event(
        event_html,
        url
    )

    # ---------------------------------------------------------------
    # Flights & Tasks page.
    # ---------------------------------------------------------------

    if '?' in url:
        flights_url = url + '&v=t'
    else:
        flights_url = url + '?v=t'

    flights = []
    errors = []

    try:

        flights_html = fetch(
            session,
            flights_url
        )

        flights_soup = BeautifulSoup(
            flights_html,
            'html.parser'
        )

        flights = flight_task_links(
            flights_soup,
            flights_url
        )

    except Exception as e:

        errors.append({
            'url': flights_url,
            'error': str(e)
        })

        flights = []

    # ---------------------------------------------------------------
    # If no flight structure was found, use legacy result-link
    # discovery.
    # ---------------------------------------------------------------

    tasks = []

    if flights:

        for sort_order, flight in enumerate(
            flights
        ):

            fid = flight_id(
                event_id,
                flight
            )

            flight['id'] = fid
            flight['sort_order'] = sort_order

            for entry in flight.get(
                'tasks',
                []
            ):

                # ---------------------------------------------------
                # Task WITH result page.
                # ---------------------------------------------------

                if entry.get(
                    'has_result_page'
                ) and entry.get('url'):

                    try:

                        t = parse_task(
                            fetch(
                                session,
                                entry['url']
                            ),
                            entry['url'],
                            event_id,
                            flight=flight,
                            link_text=entry.get(
                                'link_text',
                                ''
                            )
                        )

                        if t:

                            tasks.append(t)

                        else:

                            # Even if the detailed parser fails,
                            # preserve the task from the flight card.
                            source_url = entry['url']

                            tasks.append({
                                'task_number':
                                    entry['task_number'],
                                'name':
                                    entry['name'],
                                'status':
                                    entry['status'],
                                'published': '',
                                'source_url':
                                    source_url,
                                'rows': [],
                                'flight': flight
                            })

                    except Exception as e:

                        errors.append({
                            'url': entry['url'],
                            'error': str(e)
                        })

                        # Do NOT lose the task simply because its
                        # result page failed to load.
                        tasks.append({
                            'task_number':
                                entry['task_number'],
                            'name':
                                entry['name'],
                            'status':
                                entry['status'],
                            'published': '',
                            'source_url':
                                entry['url'],
                            'rows': [],
                            'flight': flight
                        })

                # ---------------------------------------------------
                # Task WITHOUT result page.
                #
                # This is the important new behaviour.
                # ---------------------------------------------------

                else:

                    source_url = task_source_for_flight(
                        flight,
                        entry['task_number'],
                        flights_url
                    )

                    tasks.append({
                        'task_number':
                            entry['task_number'],
                        'name':
                            entry['name'],
                        'status':
                            entry['status'],
                        'published': '',
                        'source_url':
                            source_url,
                        'rows': [],
                        'flight': flight
                    })

    else:

        # -----------------------------------------------------------
        # Legacy fallback.
        # -----------------------------------------------------------

        soup = BeautifulSoup(
            event_html,
            'html.parser'
        )

        links = task_links(
            soup,
            url
        )

        if not links:

            for suffix in (
                '&v=t',
                '&v=tr'
            ):

                try:

                    h = fetch(
                        session,
                        url + suffix
                        if '?' in url
                        else url + '?v=t'
                    )

                    links = task_links(
                        BeautifulSoup(
                            h,
                            'html.parser'
                        ),
                        url
                    )

                    if links:
                        break

                except Exception as e:

                    errors.append({
                        'url': url + suffix,
                        'error': str(e)
                    })

        for link in links:

            try:

                t = parse_task(
                    fetch(
                        session,
                        link
                    ),
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

    # ---------------------------------------------------------------
    # Deduplicate.
    #
    # IMPORTANT:
    # The flight is part of the identity.
    #
    # This means:
    #
    #   Flight 3 / 12 Aug / Task 12
    #
    # and
    #
    #   Flight 3 / 14 Aug / Task 12
    #
    # remain two separate task occurrences.
    # ---------------------------------------------------------------

    unique = {}

    for t in tasks:

        flight = t.get(
            'flight'
        ) or {}

        flight_key = (
            flight.get(
                'flight_number',
                ''
            ),
            flight.get(
                'date_label',
                ''
            ),
            flight.get(
                'time_label',
                ''
            )
        )

        key = stable(
            event_id,
            flight_key,
            t['task_number'],
            t.get(
                'published',
                ''
            ),
            t.get(
                'source_url',
                ''
            )
        )

        unique[key] = t

    tasks = list(
        unique.values()
    )

    tasks.sort(
        key=lambda x: (
            x.get(
                'flight',
                {}
            ).get(
                'sort_order',
                999999
            ),
            x['task_number'],
            x.get(
                'published',
                ''
            )
        )
    )

    # ---------------------------------------------------------------
    # Local output directory.
    # ---------------------------------------------------------------

    root = (
        Path(out_root)
        / event_id
    )

    root.mkdir(
        parents=True,
        exist_ok=True
    )

    # ---------------------------------------------------------------
    # Database.
    # ---------------------------------------------------------------

    if is_postgres():

        c = connect()

        init_postgres(c)

        c.execute(
            """
            INSERT INTO competitions(
                id,
                title,
                location,
                dates,
                organiser,
                director,
                source_url
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

        dbp = (
            root
            / 'competition.db'
        )

        c = connect(dbp)

        c.executescript(
            SCHEMA
        )

        c.execute(
            """
            INSERT OR REPLACE INTO competitions
            VALUES (?,?,?,?,?,?,?)
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

    # ---------------------------------------------------------------
    # Import run.
    # ---------------------------------------------------------------

    run_id = stable(
        event_id,
        datetime.now(
            timezone.utc
        ).isoformat(),
        len(tasks)
    )

    imported = datetime.now(
        timezone.utc
    ).isoformat()

    c.execute(
        """
        INSERT INTO import_runs
        VALUES (?,?,?,?,?,?)
        """,
        (
            run_id,
            event_id,
            imported,
            url,
            sum(
                len(t.get('rows', []))
                for t in tasks
            ),
            len(errors)
        )
    )

    # ---------------------------------------------------------------
    # Create ALL flights first.
    #
    # This is important because a flight may contain zero tasks.
    # ---------------------------------------------------------------

    created_flights = {}

    for flight in flights:

        fid = flight['id']

        if fid in created_flights:
            continue

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
            999999
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

    # ---------------------------------------------------------------
    # Create tasks and results.
    # ---------------------------------------------------------------

    for i, t in enumerate(tasks):

        flight = t.get(
            'flight'
        ) or {}

        if flight:

            fid = flight['id']

        else:

            # Legacy fallback only.
            fid = 'flight-' + stable(
                event_id,
                t.get(
                    'published',
                    ''
                )[:20]
                if t.get('published')
                else t['task_number']
            )

            if fid not in created_flights:

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
                            t.get(
                                'published',
                                ''
                            ),
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
                            t.get(
                                'published',
                                ''
                            ),
                            '',
                            i,
                            t['source_url']
                        )
                    )

                created_flights[fid] = True

        # -----------------------------------------------------------
        # Stable task ID.
        #
        # Flight identity is included so re-flown task numbers remain
        # separate.
        # -----------------------------------------------------------

        tid = 'task-' + stable(
            event_id,
            fid,
            t['task_number'],
            t.get(
                'published',
                ''
            ),
            t.get(
                'source_url',
                ''
            )
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
                    t.get(
                        'published',
                        ''
                    ),
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
                    t.get(
                        'published',
                        ''
                    ),
                    t['source_url']
                )
            )

        # -----------------------------------------------------------
        # Results / pilots.
        # -----------------------------------------------------------

        for r in t.get(
            'rows',
            []
        ):

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

    # ---------------------------------------------------------------
    # Commit.
    # ---------------------------------------------------------------

    c.commit()
    c.close()

    # ---------------------------------------------------------------
    # Save importer errors.
    # ---------------------------------------------------------------

    (root / 'errors.json').write_text(
        json.dumps(
            errors,
            indent=2
        ),
        encoding='utf-8'
    )

    return (
        event_id,
        run_id,
        len(tasks),
        sum(
            len(t.get('rows', []))
            for t in tasks
        ),
        errors
    )


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

if __name__ == '__main__':

    ap = argparse.ArgumentParser(
        description=(
            'Import a WatchMeFly competition '
            'into the Balloon Competition database'
        )
    )

    ap.add_argument(
        'url'
    )

    ap.add_argument(
        '--data-dir',
        default='data'
    )

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
