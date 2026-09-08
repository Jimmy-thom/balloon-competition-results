#!/usr/bin/env python3
"""
WatchMeFly competition importer - first prototype.

Usage:
    python importer.py "https://watchmefly.net/events/event.php?e=croatia2026"

This version deliberately keeps the WatchMeFly-specific parsing isolated so
other competition sources can be added later.
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


UA = "BalloonCompetitionImporter/0.1 (+research tool)"
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
        r = self.session.get(url, timeout=TIMEOUT)
        r.raise_for_status()
        return r.text

    @staticmethod
    def soup(html: str) -> BeautifulSoup:
        return BeautifulSoup(html, "html.parser")

    def event_url_with_view(self, view: str) -> str:
        p = urlparse(self.event_url)
        q = parse_qs(p.query)
        q["v"] = [view]
        return p._replace(query=urlencode(q, doseq=True)).geturl()

    def parse_event(self, html: str) -> dict:
        s = self.soup(html)
        title = s.find("h3")
        title_text = title.get_text(" ", strip=True) if title else ""
        if not title_text:
            title_text = s.title.get_text(" ", strip=True) if s.title else ""

        text = s.get_text("\n", strip=True)
        dates = re.search(r"(\d{1,2})\s+September\s+2026\s*-\s*(\d{1,2})\s+September\s+2026", text, re.I)

        location = None
        if title_text:
            # Header normally contains "... \n Prelog, Croatia"
            header = s.find("h3")
            if header and header.parent:
                loc_match = re.search(r"\b([A-Z][A-Za-zÀ-ÿ' -]+,\s*[A-Z][A-Za-zÀ-ÿ' -]+)\b",
                                      header.parent.get_text(" ", strip=True))
                if loc_match:
                    location = loc_match.group(1)

        return {
            "source_url": self.event_url,
            "title": title_text,
            "location": location,
            "start_date": f"2026-09-{dates.group(1).zfill(2)}" if dates else None,
            "end_date": f"2026-09-{dates.group(2).zfill(2)}" if dates else None,
            "imported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

    def parse_pilots(self, html: str) -> list[Pilot]:
        s = self.soup(html)
        pilots = []
        # The page has "Pilot #N", followed by name/country/balloon.
        text_blocks = list(s.stripped_strings)
        for i, item in enumerate(text_blocks):
            m = re.fullmatch(r"Pilot\s+#(\d+)", item)
            if not m:
                continue
            number = int(m.group(1))
            name = text_blocks[i + 1] if i + 1 < len(text_blocks) else ""
            country = None
            balloon = None
            # Search forward only until the next pilot marker.
            j = i + 2
            while j < len(text_blocks) and not re.fullmatch(r"Pilot\s+#\d+", text_blocks[j]):
                val = text_blocks[j]
                if val.startswith("Balloon:"):
                    balloon = val.removeprefix("Balloon:").strip().strip('"')
                elif country is None and val and not val.startswith("Image"):
                    # A conservative country heuristic: countries are usually
                    # a single nearby text item after the pilot name.
                    known = {
                        "Australia","Austria","Belgium","Croatia","Czech Republic",
                        "Hungary","Italy","Lithuania","New Zealand","Switzerland",
                        "The Netherlands","United Kingdom","United States",
                    }
                    if val in known:
                        country = val
                j += 1
            pilots.append(Pilot(number, name, country, balloon))

        # Deduplicate because image/link text can expose repeated pilot blocks.
        out = {}
        for p in pilots:
            out[p.competition_number] = p
        return list(out.values())

    def parse_tasks(self, html: str) -> list[Task]:
        s = self.soup(html)
        tasks = []
        current_flight = None
        current_date = None

        for el in s.find_all(["h4", "h5", "a", "div", "span"]):
            txt = el.get_text(" ", strip=True)
            fm = re.search(r"Flight\s+(\d+)\s*-\s*(\d{1,2}\s+\w+\s+\d{4}\s+(?:AM|PM))", txt, re.I)
            if fm:
                current_flight = f"Flight {fm.group(1)}"
                current_date = fm.group(2)

            tm = re.search(
                r"Task\s+(\d+)\s*-\s*(.*?)\s*(FINAL|OFFICIAL|PROVISIONAL|CANCELLED)\s*$",
                txt, re.I
            )
            if tm:
                n = int(tm.group(1))
                name = tm.group(2).strip()
                status = tm.group(3).upper()
                href = el.get("href")
                if href:
                    href = urljoin(self.event_url, href)
                tasks.append(Task(n, name, status, current_flight, current_date, href))

        # Keep one record per task number, preferring non-cancelled current task.
        by_num = {}
        for t in tasks:
            if t.number not in by_num or t.status != "CANCELLED":
                by_num[t.number] = t
        return [by_num[n] for n in sorted(by_num)]

    def parse_result_page(self, url: str, task: Task) -> list[dict]:
        html = self.get(url)
        s = self.soup(html)
        text = list(s.stripped_strings)
        rows = []

        # WatchMeFly result tables vary slightly. Find HTML tables and use
        # headers rather than relying on a fixed column position.
        for table in s.find_all("table"):
            headers = [x.get_text(" ", strip=True) for x in table.find_all("th")]
            if not headers:
                continue
            lower = [h.lower() for h in headers]
            if not any("score" in h for h in lower):
                continue

            for tr in table.find_all("tr"):
                cells = [x.get_text(" ", strip=True) for x in tr.find_all(["td","th"])]
                if len(cells) != len(headers) or cells == headers:
                    continue
                row = dict(zip(headers, cells))
                rows.append({
                    "task_number": task.number,
                    "task_name": task.name,
                    "status": task.status,
                    "source_url": url,
                    "raw": row,
                })

        return rows

    def run(self) -> dict:
        event_html = self.get(self.event_url)
        pilots_html = self.get(self.event_url_with_view("pp"))
        tasks_html = self.get(self.event_url_with_view("t"))

        event = self.parse_event(event_html)
        pilots = self.parse_pilots(pilots_html)
        tasks = self.parse_tasks(tasks_html)

        results = []
        errors = []
        for task in tasks:
            if not task.url or task.status == "CANCELLED":
                continue
            try:
                time.sleep(self.delay)
                results.extend(self.parse_result_page(task.url, task))
            except Exception as exc:
                errors.append({
                    "task_number": task.number,
                    "url": task.url,
                    "error": repr(exc),
                })

        return {
            "event": event,
            "pilots": [asdict(x) for x in pilots],
            "tasks": [asdict(x) for x in tasks],
            "results": results,
            "errors": errors,
        }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url", help="WatchMeFly event URL")
    ap.add_argument("--output", default=None, help="Output directory")
    args = ap.parse_args()

    parsed = urlparse(args.url)
    if "watchmefly.net" not in parsed.netloc:
        raise SystemExit("This prototype currently supports watchmefly.net URLs only.")

    event_id = parse_qs(parsed.query).get("e", ["event"])[0]
    output = Path(args.output or Path("data") / event_id)
    output.mkdir(parents=True, exist_ok=True)

    data = WatchMeFlyImporter(args.url).run()

    for name, value in data.items():
        with open(output / f"{name}.json", "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)

    print(f"Imported: {data['event'].get('title')}")
    print(f"Pilots:   {len(data['pilots'])}")
    print(f"Tasks:    {len(data['tasks'])}")
    print(f"Results:  {len(data['results'])}")
    print(f"Errors:   {len(data['errors'])}")
    print(f"Saved to: {output.resolve()}")


if __name__ == "__main__":
    main()
