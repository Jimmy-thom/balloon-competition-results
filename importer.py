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
    error_count INTEGER,
    snapshot_hash TEXT
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
    UNIQUE(competition_id,task_number,published,source_url)
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

def is_flight_heading(text):
    text = clean(text)
    return bool(re.match(r'^(?:Practice\s+)?Flight\s+\d+\s*[-–]', text, re.I))


def parse_flight_heading(text):
    """Parse WatchMeFly flight headings, including trailing COMPLETE/CANCELLED."""
    text = clean(text)
    m = re.match(r'^(Practice\s+)?Flight\s+(\d+)\s*[-–]\s*(.*?)\s+(AM|PM)(?:\s+(COMPLETE|COMPLETED|CANCELLED|CANCELLED\s+FLIGHT))?\s*$', text, re.I)
    if not m:
        return None
    practice = bool(m.group(1))
    n = m.group(2)
    return {'flight_number': f'Practice {n}' if practice else n, 'date_label': clean(m.group(3)), 'time_label': m.group(4).upper(), 'status': norm_status(m.group(5) or ''), 'is_practice': practice}


def parse_task_label(text):
    text = clean(text)
    if not text:
        return None
    m = re.match(r'^(Practice\s+)?Task\s+(\d+)\s*[-–]\s*(.*?)(?:\s+(FINAL|PROVISIONAL|OFFICIAL|CANCELLED|COMPLETE|COMPLETED))?\s*$', text, re.I)
    if not m:
        m = re.match(r'^(Practice)\s+(\d+)\s*[-–]\s*(.*?)(?:\s+(FINAL|PROVISIONAL|OFFICIAL|CANCELLED|COMPLETE|COMPLETED))?\s*$', text, re.I)
        if not m:
            return None
        return {'task_number': int(m.group(2)), 'name': clean(m.group(3)), 'status': norm_status(m.group(4) or ''), 'is_practice': True}
    return {'task_number': int(m.group(2)), 'name': clean(m.group(3)), 'status': norm_status(m.group(4) or ''), 'is_practice': bool(m.group(1))}


def flight_task_links(soup, base):
    """Discover every WatchMeFly flight and task, including empty/cancelled ones."""
    headings = []
    heading_tags = {'h1','h2','h3','h4','h5','h6'}
    for tag in soup.find_all(list(heading_tags)):
        parsed = parse_flight_heading(clean(tag.get_text(' ', strip=True)))
        if parsed:
            headings.append((tag, parsed))
    flights = []
    for idx,(heading,flight) in enumerate(headings):
        f=flight.copy(); f['heading']=clean(heading.get_text(' ',strip=True)); f['tasks']=[]; f['sort_order']=idx
        seen_links=set(); seen_numbers=set()
        for node in heading.next_elements:
            if getattr(node,'name',None) in heading_tags and parse_flight_heading(clean(node.get_text(' ',strip=True))):
                break
            if getattr(node,'name',None)=='a' and node.get('href'):
                href=urljoin(base,node['href']); q=parse_qs(urlparse(href).query)
                if 'tid' not in q or q.get('v',[''])[0].lower()!='tr':
                    continue
                key=(q['tid'][0],href)
                if key in seen_links: continue
                seen_links.add(key)
                label=parse_task_label(clean(node.get_text(' ',strip=True)))
                if label: seen_numbers.add(label['task_number'])
                f['tasks'].append({'url':href,'tid':q['tid'][0],'link_text':clean(node.get_text(' ',strip=True)),'task_number_hint':label['task_number'] if label else None,'name_hint':label['name'] if label else '','status_hint':label['status'] if label else 'UNKNOWN','linked':True})
        # Cancelled/no-result tasks are plain text on WatchMeFly.  Parse only
        # individual text nodes inside this flight section.  Do NOT call
        # get_text() on broad containers (div/span/td/etc.), because those
        # containers can wrap content from the next flight and cause task
        # leakage between flight sections.
        for node in heading.next_elements:
            if getattr(node,'name',None) in heading_tags and parse_flight_heading(clean(node.get_text(' ',strip=True))):
                break

            # BeautifulSoup exposes visible text as NavigableString objects.
            # Ignore text belonging to links because linked tasks were already
            # captured above.
            if not isinstance(node, str):
                continue
            if getattr(node, 'parent', None) is not None and node.find_parent('a') is not None:
                continue

            visible=clean(str(node))
            if not visible:
                continue

            # WatchMeFly currently renders plain cancelled tasks like:
            #   Task 7 - Judge Declared Goal CANCELLED
            # and practice labels like:
            #   Practice 1 - Pilot Declared Goal PROVISIONAL
            # Keep the parser deliberately local to the individual text node.
            task_pat = re.compile(
                r'(?i)\bTask\s+(?P<number>\d+)\s*[-–]\s*'
                r'(?P<body>.*?)(?P<status>FINAL|PROVISIONAL|OFFICIAL|CANCELLED|COMPLETE|COMPLETED)\b'
            )
            practice_pat = re.compile(
                r'(?i)\bPractice(?:\s+Task)?\s+(?P<number>\d+)\s*[-–]\s*'
                r'(?P<body>.*?)(?P<status>FINAL|PROVISIONAL|OFFICIAL|CANCELLED|COMPLETE|COMPLETED)\b'
            )

            matches=[]
            # Check practice first so "Practice 1" is never interpreted as
            # an ordinary competition task.
            for m in practice_pat.finditer(visible):
                matches.append((int(m.group('number')), clean(m.group('body')), True, m.group(0), m.group('status')))
            for m in task_pat.finditer(visible):
                matches.append((int(m.group('number')), clean(m.group('body')), False, m.group(0), m.group('status')))

            for task_no, body, is_practice, matched_text, raw_status in matches:
                status=norm_status(raw_status)
                if task_no in seen_numbers:
                    continue
                seen_numbers.add(task_no)
                synthetic=f'{base}#flight={idx}&task={task_no}'
                f['tasks'].append({
                    'url':synthetic,
                    'tid':'',
                    'link_text':clean(matched_text),
                    'task_number_hint':task_no,
                    'name_hint':body,
                    'status_hint':status,
                    'linked':False
                })
        flights.append(f)
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

            # WatchMeFly renders the pilot cell as e.g.
            #   #1 - FILUS, TomaszImage Poland
            # Some pages add punctuation/whitespace after Image. Keep the
            # nationality separate from the pilot name and normalise it.
            image_match = re.search(r'Image\s*:?[\s]*(.+)$', ptxt, re.I)
            if image_match:
                country = clean(image_match.group(1)).lstrip(':,- ').strip()

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

    # Reject generic WatchMeFly pages that are not real competition event pages.
    # This check must happen before creating any local files or database rows.
    title = clean(meta.get('title', ''))
    dates = clean(meta.get('dates', ''))
    location = clean(meta.get('location', ''))
    generic_titles = {
        'event', 'competitions', 'competition', 'results',
        'tasks', 'task data', 'noticeboard', 'pilots', 'officials'
    }
    if not title or title.lower() in generic_titles or not dates or not location:
        raise RuntimeError(
            'The supplied WatchMeFly URL does not appear to be a valid competition event page.'
        )

    # ------------------------------------------------------------
    # Discover flights and tasks from the Flights & Tasks page.
    # ------------------------------------------------------------
    flights_url = url + ('&v=t' if '?' in url else '?v=t')
    flights=[]; tasks=[]; errors=[]
    try:
        fsoup=BeautifulSoup(fetch(session,flights_url),'html.parser')
        flights=flight_task_links(fsoup,flights_url)
    except Exception as e:
        errors.append({'url':flights_url,'error':str(e)})
    for flight in flights:
        for entry in flight.get('tasks',[]):
            try:
                if entry.get('linked'):
                    t=parse_task(fetch(session,entry['url']),entry['url'],event_id,flight=flight,link_text=entry.get('link_text',''))
                    if t:
                        if t.get('status') in {'','UNKNOWN'}: t['status']=entry.get('status_hint','UNKNOWN')
                        if not t.get('name'): t['name']=entry.get('name_hint','')
                        tasks.append(t)
                else:
                    label=parse_task_label(entry.get('link_text',''))
                    if label:
                        tasks.append({'task_number':label['task_number'],'name':label['name'],'status':label['status'],'published':'','source_url':entry['url'],'rows':[],'flight':flight})
            except Exception as e:
                errors.append({'url':entry.get('url',''),'error':str(e)})
    if not flights:
        soup=BeautifulSoup(event_html,'html.parser'); links=task_links(soup,url)
        for link in links:
            try:
                t=parse_task(fetch(session,link),link,event_id)
                if t: tasks.append(t)
            except Exception as e: errors.append({'url':link,'error':str(e)})
    # ------------------------------------------------------------
    # Deduplicate while preserving separate task occurrences.
    # ------------------------------------------------------------
    unique={}
    for t in tasks:
        f=t.get('flight') or {}
        key=(event_id,f.get('flight_number',''),f.get('date_label',''),f.get('time_label',''),t['task_number'],t.get('source_url',''),t.get('published',''))
        if key not in unique or len(t.get('rows',[]))>len(unique[key].get('rows',[])): unique[key]=t
    tasks=list(unique.values())
    tasks.sort(key=lambda x:(x.get('flight',{}).get('sort_order',999999),x['task_number'],x.get('published','')))
    # ------------------------------------------------------------
    # Build a deterministic snapshot hash so repeated watcher checks do not
    # create duplicate import runs when WatchMeFly has not changed.
    # ------------------------------------------------------------
    snapshot_payload = {
        'event': meta,
        'flights': [
            {
                'flight_number': f.get('flight_number', ''),
                'date_label': f.get('date_label', ''),
                'time_label': f.get('time_label', ''),
                'sort_order': f.get('sort_order', 0),
            }
            for f in flights
        ],
        'tasks': [
            {
                'flight_number': (t.get('flight') or {}).get('flight_number', ''),
                'date_label': (t.get('flight') or {}).get('date_label', ''),
                'time_label': (t.get('flight') or {}).get('time_label', ''),
                'sort_order': (t.get('flight') or {}).get('sort_order', 0),
                'task_number': t.get('task_number'),
                'name': t.get('name', ''),
                'status': t.get('status', 'UNKNOWN'),
                'published': t.get('published', ''),
                'source_url': t.get('source_url', ''),
            }
            for t in tasks
        ],
        'results': [
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
            for t in tasks for r in t.get('rows', [])
        ],
        'errors': errors,
    }
    snapshot_hash = hashlib.sha256(
        json.dumps(snapshot_payload, sort_keys=True, separators=(',', ':')).encode('utf-8')
    ).hexdigest()

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
    # Database migration + snapshot comparison.
    # ------------------------------------------------------------
    if is_postgres():
        c.execute('ALTER TABLE import_runs ADD COLUMN IF NOT EXISTS snapshot_hash TEXT')
    else:
        columns = [row[1] for row in c.raw.execute('PRAGMA table_info(import_runs)').fetchall()]
        if 'snapshot_hash' not in columns:
            c.raw.execute('ALTER TABLE import_runs ADD COLUMN snapshot_hash TEXT')

    latest = c.execute(
        "SELECT id, snapshot_hash FROM import_runs WHERE competition_id=? ORDER BY imported_at DESC LIMIT 1",
        (event_id,)
    ).fetchone()
    if latest and latest['snapshot_hash'] == snapshot_hash:
        c.close()
        (root / 'errors.json').write_text(
            json.dumps(errors, indent=2),
            encoding='utf-8'
        )
        return (
            event_id,
            latest['id'],
            len(tasks),
            sum(len(t['rows']) for t in tasks),
            errors,
            False,
            snapshot_hash
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
        '''INSERT INTO import_runs(
            id,competition_id,imported_at,source_url,record_count,error_count,snapshot_hash
        ) VALUES (?,?,?,?,?,?,?)''',
        (
            run_id,
            event_id,
            imported,
            url,
            sum(len(t['rows']) for t in tasks),
            len(errors),
            snapshot_hash
        )
    )

    # ------------------------------------------------------------
    # Flights and tasks.
    # ------------------------------------------------------------

    # Insert every discovered flight first, including empty flights.
    created_flights={}
    for flight in flights:
        fid='flight-'+stable(event_id,flight['flight_number'],flight['date_label'],flight['time_label'])
        flight['id']=fid
        vals=(fid,event_id,flight.get('flight_number',''),flight.get('date_label',''),flight.get('time_label',''),flight.get('sort_order',0),flights_url)
        if is_postgres():
            c.execute("""INSERT INTO flights(id,competition_id,flight_number,date_label,time_label,sort_order,source_url) VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET flight_number=EXCLUDED.flight_number,date_label=EXCLUDED.date_label,time_label=EXCLUDED.time_label,sort_order=EXCLUDED.sort_order,source_url=EXCLUDED.source_url""",vals)
        else:
            c.execute("INSERT OR IGNORE INTO flights(id,competition_id,flight_number,date_label,time_label,sort_order,source_url) VALUES (?,?,?,?,?,?,?)",vals)
            c.execute("UPDATE flights SET flight_number=?,date_label=?,time_label=?,sort_order=?,source_url=? WHERE id=?",(vals[2],vals[3],vals[4],vals[5],vals[6],fid))
        created_flights[fid]=True

    for i,t in enumerate(tasks):
        flight=t.get('flight') or {}
        if flight:
            fid=flight['id']
        else:
            fid='flight-'+stable(event_id,t['task_number'],t.get('published',''),t.get('source_url',''))
            if fid not in created_flights:
                vals=(fid,event_id,'',t.get('published',''),' ',i,t.get('source_url',''))
                if is_postgres():
                    c.execute("""INSERT INTO flights(id,competition_id,flight_number,date_label,time_label,sort_order,source_url) VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET date_label=EXCLUDED.date_label,sort_order=EXCLUDED.sort_order,source_url=EXCLUDED.source_url""",vals)
                else:
                    c.execute("INSERT OR IGNORE INTO flights(id,competition_id,flight_number,date_label,time_label,sort_order,source_url) VALUES (?,?,?,?,?,?,?)",vals)
                created_flights[fid]=True

        # --------------------------------------------------------
        # Task.
        # --------------------------------------------------------
        existing=c.execute("SELECT id FROM tasks WHERE competition_id=? AND task_number=? AND published=? AND source_url=? LIMIT 1",(event_id,t['task_number'],t.get('published',''),t.get('source_url',''))).fetchone()
        if existing:
            tid=existing[0]
            c.execute("UPDATE tasks SET name=?,status=?,flight_id=?,published=?,source_url=? WHERE id=?",(t.get('name',''),t.get('status','UNKNOWN'),fid,t.get('published',''),t.get('source_url',''),tid))
        else:
            placeholder=c.execute("SELECT id FROM tasks WHERE competition_id=? AND task_number=? AND flight_id=? ORDER BY id LIMIT 1",(event_id,t['task_number'],fid)).fetchone()
            if placeholder:
                tid=placeholder[0]
                c.execute("UPDATE tasks SET name=?,status=?,published=?,source_url=? WHERE id=?",(t.get('name',''),t.get('status','UNKNOWN'),t.get('published',''),t.get('source_url',''),tid))
            else:
                tid='task-'+stable(event_id,fid,t['task_number'],t.get('published',''),t.get('source_url',''))
                c.execute("INSERT INTO tasks(id,competition_id,task_number,name,status,flight_id,published,source_url) VALUES (?,?,?,?,?,?,?,?)",(tid,event_id,t['task_number'],t.get('name',''),t.get('status','UNKNOWN'),fid,t.get('published',''),t.get('source_url','')))

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
                        country=COALESCE(NULLIF(EXCLUDED.country, ''), pilots.country)
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

                if r['country']:
                    c.execute(
                        "UPDATE pilots SET name=?, country=? WHERE competition_id=? AND competition_number=? AND (country IS NULL OR country='')",
                        (r['pilot'], r['country'], event_id, r['competition_number'])
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
        errors,
        True,
        snapshot_hash
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
