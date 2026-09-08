#!/usr/bin/env python3
"""
Balloon Competition Importer v2
Imports a WatchMeFly competition and produces clean, reusable JSON data.

Usage:
    python importer.py "https://watchmefly.net/events/event.php?e=croatia2026"

Outputs:
    event.json
    pilots.json
    flights.json
    tasks.json
    results.json
    standings.json
    errors.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import requests
from bs4 import BeautifulSoup


UA = "BalloonCompetitionImporter/0.2"
TIMEOUT = 30


@dataclass
class Pilot:
    competition_number: int
    name: str
    country: str | None = None
    balloon: str | None = None


@dataclass
class Task:
    number: int
    name: str
    status: str
    flight: str | None = None
    date: str | None = None
    url: str | None = None


class WatchMeFlyImporter:
    def __init__(self, event_url: str, delay: float = 0.25):
        self.event_url = event_url
        self.delay = delay
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": UA})

    def get(self, url: str) -> str:
        response = self.session.get(url, timeout=TIMEOUT)
        response.raise_for_status()
        return response.text

    @staticmethod
    def soup(html: str) -> BeautifulSoup:
        return BeautifulSoup(html, "html.parser")

    def event_url_with_view(self, view: str) -> str:
        p = urlparse(self.event_url)
        q = parse_qs(p.query)
        q["v"] = [view]
        return p._replace(query=urlencode(q, doseq=True)).geturl()

    @staticmethod
    def clean_spaces(value: str) -> str:
        return re.sub(r"\s+", " ", value or "").strip()

    def parse_event(self, html: str) -> dict:
        s = self.soup(html)

        # Prefer the page heading/title, then fall back to <title>.
        heading = s.find(["h1", "h2", "h3"])
        title_text = self.clean_spaces(heading.get_text(" ", strip=True)) if heading else ""
        if not title_text and s.title:
            title_text = self.clean_spaces(s.title.get_text(" ", strip=True))

        full_text = self.clean_spaces(s.get_text(" ", strip=True))

        # Generic date extraction; do not hard-code a competition year.
        date_patterns = [
            r"(\d{1,2}\s+[A-Za-z]+\s+\d{4})\s*[-–]\s*(\d{1,2}\s+[A-Za-z]+\s+\d{4})",
            r"(\d{1,2}\s+[A-Za-z]+\s+\d{4})\s+to\s+(\d{1,2}\s+[A-Za-z]+\s+\d{4})",
        ]
        start_date = end_date = None
        for pattern in date_patterns:
            m = re.search(pattern, full_text, re.I)
            if m:
                start_date, end_date = m.group(1), m.group(2)
                break

        # Try labelled fields first.
        location = None
        for label in ("Location", "Venue", "Place"):
            m = re.search(rf"{label}\s*[:\-]\s*([^|]+?)(?=\s+(?:Director|Organizer|Date|Dates|FAI|Category)\b|$)", full_text, re.I)
            if m:
                location = self.clean_spaces(m.group(1))
                break

        # WatchMeFly commonly exposes a city/country near the event heading.
        if not location:
            loc_matches = re.findall(
                r"\b([A-Z][A-Za-zÀ-ÿ'’.\- ]{1,40},\s*[A-Z][A-Za-zÀ-ÿ'’.\- ]{1,40})\b",
                full_text,
            )
            if loc_matches:
                # Prefer a match containing a common country/city pattern.
                location = loc_matches[0]

        return {
            "source_url": self.event_url,
            "title": title_text,
            "location": location,
            "start_date": start_date,
            "end_date": end_date,
            "imported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

    def parse_pilots(self, html: str) -> list[Pilot]:
        s = self.soup(html)
        pilots: dict[int, Pilot] = {}

        # Primary source: explicit "Pilot #N" blocks.
        strings = list(s.stripped_strings)
        for i, item in enumerate(strings):
            m = re.fullmatch(r"Pilot\s+#(\d+)", item, re.I)
            if not m:
                continue

            number = int(m.group(1))
            name = ""
            country = None
            balloon = None

            if i + 1 < len(strings):
                name = strings[i + 1].strip()

            for value in strings[i + 2:i + 12]:
                value = self.clean_spaces(value)
                if value.startswith("Balloon:"):
                    balloon = value.split(":", 1)[1].strip().strip('"')
                if value in COUNTRY_NAMES:
                    country = value

            pilots[number] = Pilot(number, name, country, balloon)

        return list(pilots.values())

    def parse_tasks(self, html: str) -> list[Task]:
        s = self.soup(html)
        tasks: list[Task] = []
        current_flight = None
        current_date = None

        # Search elements individually, but avoid processing the same visible
        # text repeatedly where nested elements occur.
        seen = set()
        for el in s.find_all(["h4", "h5", "a", "div", "span"]):
            txt = self.clean_spaces(el.get_text(" ", strip=True))
            if not txt or txt in seen:
                continue
            seen.add(txt)

            fm = re.search(
                r"Flight\s+(\d+)\s*[-–]\s*(\d{1,2}\s+\w+\s+\d{4}\s+(?:AM|PM))",
                txt,
                re.I,
            )
            if fm:
                current_flight = f"Flight {fm.group(1)}"
                current_date = fm.group(2)

            tm = re.search(
                r"Task\s+(\d+)\s*[-–]\s*(.*?)\s*(FINAL|OFFICIAL|PROVISIONAL|CANCELLED)\s*$",
                txt,
                re.I,
            )
            if tm:
                number = int(tm.group(1))
                name = self.clean_spaces(tm.group(2))
                status = tm.group(3).upper()
                href = el.get("href")
                if href:
                    href = urljoin(self.event_url, href)
                tasks.append(Task(number, name, status, current_flight, current_date, href))

        # Deduplicate task numbers. Prefer a non-cancelled task if both an
        # alternate/cancelled listing and a live competition task exist.
        by_number: dict[int, Task] = {}
        for task in tasks:
            old = by_number.get(task.number)
            if old is None or (old.status == "CANCELLED" and task.status != "CANCELLED"):
                by_number[task.number] = task

        return [by_number[n] for n in sorted(by_number)]

    def parse_result_page(self, url: str, task: Task) -> list[dict]:
        html = self.get(url)
        s = self.soup(html)
        rows = []

        for table in s.find_all("table"):
            # Header cells can be th or td depending on the page version.
            header_row = None
            headers = []
            for tr in table.find_all("tr"):
                cells = [self.clean_spaces(x.get_text(" ", strip=True))
                         for x in tr.find_all(["th", "td"])]
                if not cells:
                    continue
                lowered = [c.lower() for c in cells]
                if "pilot" in lowered and "score" in lowered:
                    header_row = tr
                    headers = cells
                    break

            if not headers:
                continue

            for tr in table.find_all("tr"):
                if tr is header_row:
                    continue
                cells = [self.clean_spaces(x.get_text(" ", strip=True))
                         for x in tr.find_all(["td", "th"])]
                if len(cells) != len(headers):
                    continue
                if not cells or cells == headers:
                    continue

                raw = dict(zip(headers, cells))
                if not any(k.lower() == "pilot" for k in raw):
                    continue

                rows.append({
                    "task_number": task.number,
                    "task_name": task.name,
                    "status": task.status,
                    "source_url": url,
                    "raw": raw,
                })

        return rows

    @staticmethod
    def parse_pilot_field(value: str) -> tuple[int | None, str | None, str | None]:
        """
        Convert:
            '#29 - BAREFORD, Dominic United Kingdom'
        into:
            29, 'Dominic Bareford', 'United Kingdom'
        """
        value = re.sub(r"\s+", " ", value or "").strip()
        m = re.match(r"#(\d+)\s*-\s*(.*)", value)
        if not m:
            return None, value or None, None

        number = int(m.group(1))
        remainder = m.group(2).strip()

        # The WatchMeFly result format is SURNAME, Given(s) Country.
        country = None
        for country_name in sorted(COUNTRY_NAMES, key=len, reverse=True):
            suffix = " " + country_name
            if remainder.endswith(suffix):
                country = country_name
                remainder = remainder[:-len(suffix)].strip()
                break

        if "," in remainder:
            surname, given = remainder.split(",", 1)
            name = f"{given.strip()} {surname.strip()}".strip()
        else:
            name = remainder

        return number, name, country

    def normalize_results(self, raw_results: list[dict]) -> list[dict]:
        clean = []

        for item in raw_results:
            raw = item["raw"]
            pilot_value = raw.get("Pilot", "")
            number, name, country = self.parse_pilot_field(pilot_value)

            def number_value(key: str):
                value = raw.get(key)
                if value is None:
                    return None
                value = value.replace(",", "").strip()
                if value in {"", "-", "—"}:
                    return None
                try:
                    return float(value)
                except ValueError:
                    return None

            rank = number_value("Rank")
            points = number_value("Points")
            penalty_t = number_value("Penalty (T)")
            penalty_c = number_value("Penalty (C)")
            score = number_value("Score")

            clean.append({
                "task_number": item["task_number"],
                "task_name": item["task_name"],
                "status": item["status"],
                "competition_number": number,
                "pilot": name,
                "country": country,
                "rank": int(rank) if rank is not None and rank.is_integer() else rank,
                "result": raw.get("Result"),
                "points": points,
                "penalty_t": penalty_t,
                "penalty_c": penalty_c,
                "score": score,
                "notes": raw.get("Notes", ""),
                "source_url": item["source_url"],
            })

        return clean

    def run(self) -> dict:
        event_html = self.get(self.event_url)
        pilots_html = self.get(self.event_url_with_view("pp"))
        tasks_html = self.get(self.event_url_with_view("t"))

        event = self.parse_event(event_html)
        page_pilots = self.parse_pilots(pilots_html)
        tasks = self.parse_tasks(tasks_html)

        raw_results = []
        errors = []

        for task in tasks:
            if not task.url or task.status == "CANCELLED":
                continue
            try:
                time.sleep(self.delay)
                raw_results.extend(self.parse_result_page(task.url, task))
            except Exception as exc:
                errors.append({
                    "task_number": task.number,
                    "url": task.url,
                    "error": repr(exc),
                })

        results = self.normalize_results(raw_results)

        # Build the authoritative pilot list from task results. This avoids
        # depending on presentation quirks in the WatchMeFly pilot page.
        pilot_map: dict[int, Pilot] = {p.competition_number: p for p in page_pilots}
        for r in results:
            n = r["competition_number"]
            if n is None:
                continue
            if n not in pilot_map:
                pilot_map[n] = Pilot(n, r["pilot"] or "", r["country"], None)
            else:
                p = pilot_map[n]
                if not p.name and r["pilot"]:
                    p.name = r["pilot"]
                if not p.country and r["country"]:
                    p.country = r["country"]

        pilots = [asdict(pilot_map[n]) for n in sorted(pilot_map)]

        flights = []
        seen_flights = set()
        for task in tasks:
            if task.flight and task.flight not in seen_flights:
                flights.append({
                    "flight": task.flight,
                    "date": task.date,
                    "tasks": [],
                })
                seen_flights.add(task.flight)
            if task.flight:
                next(x for x in flights if x["flight"] == task.flight)["tasks"].append(task.number)

        standings = calculate_standings(pilots, results)

        return {
            "event": event,
            "pilots": pilots,
            "flights": flights,
            "tasks": [asdict(t) for t in tasks],
            "results": results,
            "standings": standings,
            "errors": errors,
        }


COUNTRY_NAMES = {
    "Australia", "Austria", "Belgium", "Croatia", "Czech Republic",
    "Hungary", "Italy", "Lithuania", "New Zealand", "Switzerland",
    "The Netherlands", "United Kingdom", "United States", "France",
    "Germany", "Poland", "Slovakia", "Slovenia", "Spain", "Portugal",
    "Canada", "Japan", "Brazil", "Denmark", "Finland", "Ireland",
    "Norway", "Sweden", "Serbia", "Romania", "Bulgaria", "Greece",
}


def calculate_standings(pilots: list[dict], results: list[dict]) -> dict:
    """
    Calculate cumulative standings ourselves.

    Two views are supplied:
      official: FINAL/OFFICIAL results only
      all_results: FINAL/OFFICIAL + PROVISIONAL results

    A task contributes its recorded score. Missing/No Flight rows generally
    have a score of 0 on WatchMeFly and therefore contribute zero.
    """
    task_numbers = sorted({r["task_number"] for r in results})
    pilot_lookup = {p["competition_number"]: p for p in pilots}

    def make_view(allowed_statuses: set[str]):
        totals = {n: 0.0 for n in pilot_lookup}
        by_task = {n: {} for n in pilot_lookup}

        for r in results:
            if r["status"] not in allowed_statuses:
                continue
            n = r["competition_number"]
            if n is None:
                continue
            score = r["score"] if r["score"] is not None else 0.0
            totals[n] += score
            by_task[n][r["task_number"]] = score

        ordered = sorted(
            totals.items(),
            key=lambda x: (-x[1], pilot_lookup[x[0]]["name"]),
        )

        table = []
        for position, (number, total) in enumerate(ordered, 1):
            row = {
                "position": position,
                "competition_number": number,
                "pilot": pilot_lookup[number]["name"],
                "country": pilot_lookup[number]["country"],
                "total": int(total) if float(total).is_integer() else total,
                "tasks": {
                    str(t): (
                        int(by_task[number][t])
                        if t in by_task[number] and float(by_task[number][t]).is_integer()
                        else by_task[number].get(t)
                    )
                    for t in task_numbers
                },
            }
            table.append(row)

        return {
            "task_numbers": task_numbers,
            "rows": table,
        }

    return {
        "official": make_view({"FINAL", "OFFICIAL"}),
        "all_results": make_view({"FINAL", "OFFICIAL", "PROVISIONAL"}),
    }


def main():
    parser = argparse.ArgumentParser(description="Import a WatchMeFly competition.")
    parser.add_argument("url", help="WatchMeFly event URL")
    parser.add_argument("--output", default=None, help="Output directory")
    parser.add_argument("--delay", type=float, default=0.25,
                        help="Delay between task requests (default: 0.25 seconds)")
    args = parser.parse_args()

    parsed = urlparse(args.url)
    if "watchmefly.net" not in parsed.netloc:
        raise SystemExit("This importer currently supports watchmefly.net URLs only.")

    event_id = parse_qs(parsed.query).get("e", ["event"])[0]
    output = Path(args.output or Path("data") / event_id)
    output.mkdir(parents=True, exist_ok=True)

    try:
        data = WatchMeFlyImporter(args.url, delay=args.delay).run()
    except Exception as exc:
        print(f"IMPORT FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)

    for name, value in data.items():
        with open(output / f"{name}.json", "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)

    print()
    print("=== Balloon Competition Importer v2 ===")
    print(f"Event:    {data['event'].get('title')}")
    print(f"Location: {data['event'].get('location')}")
    print(f"Pilots:   {len(data['pilots'])}")
    print(f"Flights:  {len(data['flights'])}")
    print(f"Tasks:    {len(data['tasks'])}")
    print(f"Results:  {len(data['results'])}")
    print(f"Errors:   {len(data['errors'])}")
    print(f"Output:   {output.resolve()}")

    official = data["standings"]["official"]["rows"]
    all_results = data["standings"]["all_results"]["rows"]

    if official:
        print()
        print("Official cumulative leader:")
        print(f"  1. {official[0]['pilot']} — {official[0]['total']} points")

    if all_results:
        print("Including provisional:")
        print(f"  1. {all_results[0]['pilot']} — {all_results[0]['total']} points")


if __name__ == "__main__":
    main()
