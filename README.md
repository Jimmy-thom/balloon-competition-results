# Balloon Competition Website — v8

Production-beta build for hot-air balloon competition results.

## Architecture

WatchMeFly → importer → PostgreSQL/SQLite → Flask website/API

- PostgreSQL is selected automatically when `DATABASE_URL` is set.
- SQLite remains available for local development and seeded Croatia 2026 testing.
- Historical `import_runs` are retained.
- Official/provisional views are supported.
- Cancelled/re-flown task occurrences are preserved.
- `/healthz` is available for deployment monitoring.

## Local run

```bash
pip install -r requirements.txt
python seed_croatia.py
python -m pytest -q
python app.py
```

## Import

Local SQLite:

```bash
python importer.py 'https://watchmefly.net/events/event.php?e=croatia2026'
```

Production Postgres:

```bash
DATABASE_URL='postgresql://...' python importer.py 'https://watchmefly.net/events/event.php?e=croatia2026'
```

## Important

The free Render web filesystem is ephemeral. Production data must live in Postgres, not `data/*/competition.db`.
