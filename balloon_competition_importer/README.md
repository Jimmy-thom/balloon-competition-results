# Balloon Competition Importer

First prototype for importing Hot Air Balloon competition data from WatchMeFly.

## Requirements

Windows 10/11 with Python 3.11+.

## Install

Open Command Prompt in this folder:

```bat
py -m pip install -r requirements.txt
```

## Run

```bat
py importer.py "https://watchmefly.net/events/event.php?e=croatia2026"
```

The importer creates:

```text
data\croatia2026\
    event.json
    pilots.json
    tasks.json
    results.json
    errors.json
```

## Important

This is deliberately the **first extraction prototype**, not the finished production scraper.

WatchMeFly has different page layouts for different views and its result tables can change as tasks move from provisional to final. The next development step is to run this against Croatia 2026, inspect the extracted records, and then harden the parser against the exact result-page HTML before adding cumulative scoring and the website.

Use the data only in accordance with WatchMeFly's terms, robots rules, and any applicable competition/site permissions.
