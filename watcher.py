from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

from db import connect
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


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def ensure_schema(c):
    c.execute(SCHEMA)
    c.commit()


def event_id_from_url(url):
    return parse_qs(urlparse(url).query).get('e', [''])[0]


def get_monitored(c):
    return c.execute(
        "SELECT competition_id, source_url, enabled FROM competition_monitoring WHERE enabled=1 ORDER BY competition_id"
    ).fetchall()


def upsert_monitor(c, event_id, url, enabled=1):
    existing = c.execute(
        "SELECT competition_id FROM competition_monitoring WHERE competition_id=?",
        (event_id,)
    ).fetchone()
    if existing:
        c.execute(
            "UPDATE competition_monitoring SET source_url=?, enabled=? WHERE competition_id=?",
            (url, enabled, event_id)
        )
    else:
        c.execute(
            "INSERT INTO competition_monitoring(competition_id,source_url,enabled) VALUES(?,?,?)",
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
    c = connect(None if __import__('os').getenv('DATABASE_URL') else 'data/app.db')
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
        print(json.dumps({**result, 'monitoring_enabled': True}, indent=2, default=str))
    finally:
        c.close()


def run_once(out_root='data'):
    c = connect(None if __import__('os').getenv('DATABASE_URL') else 'data/app.db')
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
            c = connect(None if __import__('os').getenv('DATABASE_URL') else 'data/app.db')
            try:
                ensure_schema(c)
                record_check(c, event_id, changed=result['changed'], error=(result['errors'] or None))
            finally:
                c.close()
            summary.append(result)
            print(json.dumps(result, indent=2, default=str), flush=True)
        except Exception as exc:
            c = connect(None if __import__('os').getenv('DATABASE_URL') else 'data/app.db')
            try:
                ensure_schema(c)
                record_check(c, event_id, error=exc)
            finally:
                c.close()
            summary.append({'event_id': event_id, 'error': str(exc)})
            print(f'ERROR {event_id}: {exc}', file=sys.stderr, flush=True)

    print(json.dumps({'checked': len(rows), 'results': summary}, indent=2, default=str))


def list_events():
    c = connect(None if __import__('os').getenv('DATABASE_URL') else 'data/app.db')
    try:
        ensure_schema(c)
        rows = c.execute(
            "SELECT competition_id, source_url, enabled, last_checked_at, last_import_at, last_changed_at, last_error FROM competition_monitoring ORDER BY competition_id"
        ).fetchall()
        print(json.dumps([dict(r) for r in rows], indent=2, default=str))
    finally:
        c.close()


def disable_event(event_id):
    c = connect(None if __import__('os').getenv('DATABASE_URL') else 'data/app.db')
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
