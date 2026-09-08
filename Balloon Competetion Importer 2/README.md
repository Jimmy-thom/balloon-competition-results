# Balloon Competition Importer v2

This is the next-stage importer for the competition website project.

It currently imports **WatchMeFly** competition pages and creates a clean dataset suitable for a future database/API/frontend.

## Run

Python 3.11+ is recommended.

```text
py -m pip install -r requirements.txt
py importer.py "https://watchmefly.net/events/event.php?e=croatia2026"
```

Optional output directory:

```text
py importer.py "https://watchmefly.net/events/event.php?e=croatia2026" --output data/croatia2026
```

## Files produced

- `event.json` — competition metadata
- `pilots.json` — authoritative pilot list assembled from results
- `flights.json` — flight/task grouping
- `tasks.json` — task number, name, status, flight and source URL
- `results.json` — normalized individual task results
- `standings.json` — calculated cumulative standings
- `errors.json` — task-level import errors

## Important design choices

### 1. WatchMeFly totals are not treated as authoritative

The importer calculates cumulative standings by summing the individual task `score` values.

This lets the eventual website offer different views without depending on a pre-calculated overall table.

### 2. Official and provisional results are kept separate

`standings.json` contains:

- `official` — FINAL/OFFICIAL task results only
- `all_results` — FINAL/OFFICIAL plus PROVISIONAL results

This is important while a competition is still running.

### 3. Competition number is the pilot identifier

Pilot numbers such as `#29`, `#7`, etc. are identifiers. They are not assumed to be sequential.

### 4. Task numbers are identifiers

Task 8 does not become array item 7 simply because an earlier task was cancelled. The actual task number is retained.

### 5. Penalties and notes are retained

The normalized result contains:

- points
- time/competition penalties
- score
- raw result
- notes
- status
- source URL

### 6. No Result / No Flight are retained

A missing flight is not silently removed. The WatchMeFly score is preserved, including zero where supplied.

## First validation target

For the Croatia 2026 test event, the importer should find:

- 26 pilots
- 13 competition tasks
- 338 individual task-result rows
- 0 import errors

The first seven tasks are final in the supplied test dataset and the remaining six are provisional.

The official cumulative totals should reproduce the WatchMeFly task-score sums for Tasks 1–7.

## Next development stage

Once this importer is validated against several competitions, the next stage is:

1. add a database model
2. add an API
3. store historical imports/snapshots
4. build the website views:
   - Overall
   - By Flight
   - By Task
   - By Pilot
   - ranking progression
   - official/provisional filters
   - task selection
5. add additional competition sources if required
