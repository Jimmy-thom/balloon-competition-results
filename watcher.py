from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from db import connect, is_postgres
from importer import import_event


SCHEMA = """
CREATE TABLE IF NOT EXISTS competition_monitoring(
    competition_id TEXT PRIMARY KEY,
    source_url TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_checked_at TEXT,
    last_import_at TEXT,
    last_changed_at TEXT,
    last_error TEXT
)
"""

RETENTION_DAYS_DEFAULT = 7


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def _table_columns(c, table):
    # The project DB adapter returns a lightweight Cursor wrapper on
    # PostgreSQL, so it does not expose DB-API ``description`` directly.
    # Use the database catalog/PRAGMA instead of relying on cursor internals.
    if is_postgres():
        rows = c.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=?",
            (table,)
        ).fetchall()
        return {r["column_name"] for r in rows}
    rows = c.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


def ensure_schema(c):
    c.execute(SCHEMA)
    columns = _table_columns(c, 'competition_monitoring')
    migrations = {
        'lifecycle_status': "TEXT NOT NULL DEFAULT 'ACTIVE'",
        'event_end_date': 'TEXT',
        'finished_at': 'TEXT',
        'purge_after': 'TEXT',
    }
    for name, definition in migrations.items():
        if name not in columns:
            c.execute(f"ALTER TABLE competition_monitoring ADD COLUMN {name} {definition}")
    c.commit()


def event_id_from_url(url):
    return parse_qs(urlparse(url).query).get('e', [''])[0]


def get_monitored(c):
    return c.execute(
        "SELECT competition_id, source_url, enabled, lifecycle_status, event_end_date, finished_at, purge_after FROM competition_monitoring WHERE enabled=1 ORDER BY competition_id"
    ).fetchall()


def upsert_monitor(c, event_id, url, enabled=1):
    existing = c.execute(
        "SELECT competition_id FROM competition_monitoring WHERE competition_id=?",
        (event_id,)
    ).fetchone()
    if existing:
        c.execute(
            "UPDATE competition_monitoring SET source_url=?, enabled=?, lifecycle_status=CASE WHEN lifecycle_status='PURGED' THEN 'ACTIVE' ELSE lifecycle_status END WHERE competition_id=?",
            (url, enabled, event_id)
        )
    else:
        c.execute(
            "INSERT INTO competition_monitoring(competition_id,source_url,enabled,lifecycle_status) VALUES(?,?,?,'ACTIVE')",
            (event_id, url, enabled)
        )
    c.commit()


def record_check(c, event_id, changed=False, error=None):
    checked = now_iso()
    if error:
        c.execute(
            "UPDATE competition_monitoring SET last_checked_at=?, last_error=? WHERE competition_id=?",
            (checked, str(error), event_id)
        )
    elif changed:
        c.execute(
            "UPDATE competition_monitoring SET last_checked_at=?, last_import_at=?, last_changed_at=?, last_error=NULL WHERE competition_id=?",
            (checked, checked, checked, event_id)
        )
    else:
        c.execute(
            "UPDATE competition_monitoring SET last_checked_at=?, last_error=NULL WHERE competition_id=?",
            (checked, event_id)
        )
    c.commit()


def _parse_date_candidates(text):
    if not text:
        return []
    text = str(text)
    out = []
    patterns = [
        (r"(\d{4})-(\d{1,2})-(\d{1,2})", '%Y-%m-%d'),
        (r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})", None),
        (r"(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{4})", None),
        (r"([A-Za-z]{3,9})\s+(\d{1,2}),?\s+(\d{4})", None),
        (r"([A-Za-z]{3,9})\s+(\d{1,2})\s*[-–]\s*(\d{1,2}),?\s+(\d{4})", None),
        (r"(\d{1,2})\s*[-–]\s*(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{4})", None),
    ]
    for pattern, fmt in patterns:
        for m in re.finditer(pattern, text):
            try:
                if fmt:
                    out.append(datetime.strptime(m.group(0), fmt).date())
                elif len(m.groups()) == 3 and m.group(1).isdigit() and m.group(2).isdigit() and m.group(3).isdigit():
                    # dd/mm/yyyy style
                    out.append(date(int(m.group(3)), int(m.group(2)), int(m.group(1))))
                elif len(m.groups()) == 3 and m.group(1).isdigit():
                    out.append(datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", '%d %B %Y').date() if len(m.group(2)) > 3 else datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", '%d %b %Y').date())
                elif len(m.groups()) == 3:
                    out.append(datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", '%B %d %Y').date() if len(m.group(1)) > 3 else datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", '%b %d %Y').date())
                elif len(m.groups()) == 4:
                    out.append(datetime.strptime(f"{m.group(3)} {m.group(2)} {m.group(4)}", '%B %d %Y').date() if len(m.group(3)) > 3 else datetime.strptime(f"{m.group(3)} {m.group(2)} {m.group(4)}", '%b %d %Y').date())
            except ValueError:
                pass
    return out


def _watchmefly_end_date(url):
    """Read the official competition date range from the WatchMeFly page.

    The imported ``competitions.dates`` value is not always a full range; for
    some events it contains only the start date. Flight labels are also not a
    reliable substitute for the official event end date because the latest
    flown flight may occur before the competition's scheduled end.
    """
    if not url:
        return None
    try:
        req = urllib.request.Request(
            url,
            headers={'User-Agent': 'balloon-competition-web/1.0'}
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            html = response.read().decode('utf-8', errors='ignore')
        text = re.sub(r'<[^>]+>', ' ', html)
        text = re.sub(r'\s+', ' ', text)

        # Prefer an explicit full event range, e.g.
        # "9 September 2026 - 13 September 2026".
        range_patterns = [
            r'(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{4})\s*[-–]\s*(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{4})',
            r'(\d{1,2})[./-](\d{1,2})[./-](\d{4})\s*[-–]\s*(\d{1,2})[./-](\d{1,2})[./-](\d{4})',
        ]
        for pattern in range_patterns:
            m = re.search(pattern, text)
            if not m:
                continue
            try:
                if m.group(2).isdigit():
                    return date(int(m.group(6)), int(m.group(5)), int(m.group(4)))
                month = m.group(5)
                fmt = '%b' if len(month) <= 3 else '%B'
                return datetime.strptime(
                    f'{m.group(4)} {month} {m.group(6)}', f'%d {fmt} %Y'
                ).date()
            except ValueError:
                pass
    except Exception:
        return None
    return None


def competition_end_date(c, event_id):
    sources = []
    source_url = None
    try:
        row = c.execute("SELECT * FROM competitions WHERE id=?", (event_id,)).fetchone()
        if row:
            d = dict(row)
            for key in ('end_date', 'dates'):
                if d.get(key):
                    sources.append(d[key])
    except Exception:
        pass

    # If the stored competition dates contain a real range, trust that first.
    candidates = []
    for source in sources:
        candidates.extend(_parse_date_candidates(source))
    if len(candidates) >= 2:
        return max(candidates)

    # WatchMeFly is the source of record. Its event page contains the official
    # scheduled range even when the imported database value only contains the
    # start date.
    try:
        row = c.execute(
            "SELECT source_url FROM competition_monitoring WHERE competition_id=?",
            (event_id,)
        ).fetchone()
        source_url = row['source_url'] if row else None
    except Exception:
        pass
    official_end = _watchmefly_end_date(source_url)
    if official_end:
        return official_end

    # Final fallback: use the latest flown flight date, but only when the
    # official event page did not provide a scheduled end date.
    try:
        rows = c.execute("SELECT date_label FROM flights WHERE competition_id=?", (event_id,)).fetchall()
        flight_candidates = []
        for r in rows:
            if r['date_label']:
                flight_candidates.extend(_parse_date_candidates(r['date_label']))
        if flight_candidates:
            return max(flight_candidates)
    except Exception:
        pass
    return max(candidates) if candidates else None


def retention_days():
    try:
        return max(0, int(os.getenv('COMPETITION_RETENTION_DAYS', str(RETENTION_DAYS_DEFAULT))))
    except ValueError:
        return RETENTION_DAYS_DEFAULT


def auto_purge_enabled():
    return os.getenv('AUTO_PURGE_ENABLED', 'false').strip().lower() in {'1', 'true', 'yes', 'on'}


def refresh_lifecycle(c, event_id, now=None):
    """Update lifecycle from the competition end date. Returns a status dict."""
    now = now or datetime.now(timezone.utc)
    row = c.execute(
        "SELECT lifecycle_status,event_end_date,finished_at,purge_after FROM competition_monitoring WHERE competition_id=?",
        (event_id,)
    ).fetchone()
    if not row:
        return {'event_id': event_id, 'status': 'UNKNOWN'}

    end = competition_end_date(c, event_id)
    end_text = end.isoformat() if end else row['event_end_date']
    status = row['lifecycle_status'] or 'ACTIVE'
    finished_at = row['finished_at']
    purge_after = row['purge_after']

    # Reconcile the stored lifecycle status with the authoritative end date.
    # This also repairs a previously incorrect FINISHED status if a later run
    # discovers that the official event actually ends in the future.
    if end and now.date() <= end:
        status = 'ACTIVE'
        finished_at = None
        purge_after = None
    elif end and now.date() > end and status == 'ACTIVE':
        status = 'FINISHED'
        finished_at = finished_at or now.isoformat()
        purge_after = (end + timedelta(days=retention_days())).isoformat()

    if end_text and not purge_after and status == 'FINISHED':
        try:
            purge_after = (date.fromisoformat(end_text) + timedelta(days=retention_days())).isoformat()
        except ValueError:
            pass

    c.execute(
        "UPDATE competition_monitoring SET lifecycle_status=?, event_end_date=?, finished_at=?, purge_after=? WHERE competition_id=?",
        (status, end_text, finished_at, purge_after, event_id)
    )
    c.commit()
    due = bool(status == 'FINISHED' and purge_after and now.date() >= date.fromisoformat(purge_after))
    return {
        'event_id': event_id,
        'status': status,
        'event_end_date': end_text,
        'finished_at': finished_at,
        'purge_after': purge_after,
        'purge_due': due,
    }


def purge_event(c, event_id, out_root='data'):
    """Permanently remove one competition and all of its dependent data."""
    row = c.execute("SELECT lifecycle_status FROM competition_monitoring WHERE competition_id=?", (event_id,)).fetchone()
    if not row:
        raise RuntimeError(f'Competition {event_id} is not registered for monitoring.')
    if row['lifecycle_status'] != 'FINISHED':
        raise RuntimeError(f'Competition {event_id} is not marked FINISHED.')

    run_rows = c.execute("SELECT id FROM import_runs WHERE competition_id=?", (event_id,)).fetchall()
    run_ids = [r['id'] for r in run_rows]
    if run_ids:
        qs = ','.join('?' * len(run_ids))
        c.execute(f"DELETE FROM results WHERE import_run_id IN ({qs})", run_ids)
    c.execute("DELETE FROM tasks WHERE competition_id=?", (event_id,))
    c.execute("DELETE FROM flights WHERE competition_id=?", (event_id,))
    c.execute("DELETE FROM pilots WHERE competition_id=?", (event_id,))
    c.execute("DELETE FROM import_runs WHERE competition_id=?", (event_id,))
    c.execute("DELETE FROM competition_monitoring WHERE competition_id=?", (event_id,))
    c.execute("DELETE FROM competitions WHERE id=?", (event_id,))
    c.commit()

    folder = Path(out_root) / event_id
    if folder.exists():
        shutil.rmtree(folder, ignore_errors=True)

    return {'event_id': event_id, 'purged': True}


def run_import(url, out_root='data'):
    result = import_event(url, out_root)
    event_id, run_id, tasks, results, errors, changed, snapshot_hash = result
    return {
        'event_id': event_id,
        'run_id': run_id,
        'tasks': tasks,
        'results': results,
        'errors': errors,
        'changed': changed,
        'snapshot_hash': snapshot_hash,
    }


def add_event(url, out_root='data'):
    c = connect(None if os.getenv('DATABASE_URL') else 'data/app.db')
    try:
        ensure_schema(c)
        print(f'Importing {url} ...')
        result = run_import(url, out_root)
        if result['errors']:
            print(json.dumps(result, indent=2, default=str))
            raise RuntimeError('Initial import reported errors; event was not enabled for monitoring.')
        event_id = result['event_id']
        upsert_monitor(c, event_id, url, 1)
        if result['changed']:
            record_check(c, event_id, changed=True)
        else:
            record_check(c, event_id, changed=False)
        lifecycle = refresh_lifecycle(c, event_id)
        print(json.dumps({**result, 'monitoring_enabled': True, 'lifecycle': lifecycle}, indent=2, default=str))
    finally:
        c.close()


def run_once(out_root='data'):
    c = connect(None if os.getenv('DATABASE_URL') else 'data/app.db')
    try:
        ensure_schema(c)
        rows = get_monitored(c)
    finally:
        c.close()

    summary = []
    for row in rows:
        event_id = row['competition_id']
        url = row['source_url']
        try:
            print(f'Checking {event_id} ...', flush=True)
            result = run_import(url, out_root)
            c = connect(None if os.getenv('DATABASE_URL') else 'data/app.db')
            try:
                ensure_schema(c)
                record_check(c, event_id, changed=result['changed'], error=(result['errors'] or None))
                lifecycle = refresh_lifecycle(c, event_id)
                if lifecycle.get('purge_due') and auto_purge_enabled():
                    result['purged'] = purge_event(c, event_id, out_root)
                    lifecycle['purged'] = True
            finally:
                c.close()
            result['lifecycle'] = lifecycle
            summary.append(result)
            print(json.dumps(result, indent=2, default=str), flush=True)
        except Exception as exc:
            c = connect(None if os.getenv('DATABASE_URL') else 'data/app.db')
            try:
                ensure_schema(c)
                record_check(c, event_id, error=exc)
            finally:
                c.close()
            summary.append({'event_id': event_id, 'error': str(exc)})
            print(f'ERROR {event_id}: {exc}', file=sys.stderr, flush=True)

    print(json.dumps({'checked': len(rows), 'results': summary}, indent=2, default=str))


def list_events():
    c = connect(None if os.getenv('DATABASE_URL') else 'data/app.db')
    try:
        ensure_schema(c)
        rows = c.execute(
            "SELECT competition_id, source_url, enabled, lifecycle_status, event_end_date, finished_at, purge_after, last_checked_at, last_import_at, last_changed_at, last_error FROM competition_monitoring ORDER BY competition_id"
        ).fetchall()
        print(json.dumps([dict(r) for r in rows], indent=2, default=str))
    finally:
        c.close()


def disable_event(event_id):
    c = connect(None if os.getenv('DATABASE_URL') else 'data/app.db')
    try:
        ensure_schema(c)
        c.execute("UPDATE competition_monitoring SET enabled=0 WHERE competition_id=?", (event_id,))
        c.commit()
        print(f'Monitoring disabled: {event_id}')
    finally:
        c.close()


def main():
    ap = argparse.ArgumentParser(description='Automatic WatchMeFly competition watcher')
    sub = ap.add_subparsers(dest='command', required=True)

    p_add = sub.add_parser('add', help='Import and enable monitoring for a competition URL')
    p_add.add_argument('url')
    p_add.add_argument('--data-dir', default='data')

    p_run = sub.add_parser('run-once', help='Check every enabled competition once')
    p_run.add_argument('--data-dir', default='data')

    sub.add_parser('list', help='List monitored competitions')

    p_disable = sub.add_parser('disable', help='Disable monitoring for an event')
    p_disable.add_argument('event_id')

    args = ap.parse_args()
    if args.command == 'add':
        add_event(args.url, args.data_dir)
    elif args.command == 'run-once':
        run_once(args.data_dir)
    elif args.command == 'list':
        list_events()
    elif args.command == 'disable':
        disable_event(args.event_id)


if __name__ == '__main__':
    main()
