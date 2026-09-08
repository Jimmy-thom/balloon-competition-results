# Balloon Competition Importer v3

This version moves the project from a one-off scrape toward a reusable competition data engine.

## What it adds
- Normalized JSON output.
- SQLite database (`competition.db`).
- Import-run history with content hashes.
- Official vs all-results (including provisional) standings.
- Cumulative ranking progression after every task.
- Separate flight occurrences using flight number + date, avoiding collisions such as two different "Flight 3" entries.
- Task/result/pilot relationships suitable for a future website/API.

## Install
```bash
py -m pip install -r requirements.txt
```

## Run
```bash
py importer.py "https://watchmefly.net/events/event.php?e=croatia2026"
```

Optional:
```bash
py importer.py "URL" --output data/my-event --delay 0.4
```

## Output
- `event.json`
- `pilots.json`
- `flights.json`
- `tasks.json`
- `results.json`
- `standings.json`
- `import_runs.json`
- `errors.json`
- `competition.db`

## Important design choice
The database keeps an `import_runs` record and stores results against that run. This is the foundation for preserving changes when provisional results become official rather than silently overwriting history.

## Next stage
Build the web/API layer on top of this stable data model:
1. event list and event page
2. overall standings
3. flight-by-flight view
4. task-by-task results
5. pilot page
6. task-range filtering
7. official/provisional selector
8. ranking progression chart
9. scheduled re-imports
10. additional competition-source adapters
