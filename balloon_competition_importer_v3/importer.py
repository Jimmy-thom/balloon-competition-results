#!/usr/bin/env python3
"""
Balloon Competition Importer v3

WatchMeFly -> normalized JSON + SQLite database.

Usage:
    python importer.py "https://watchmefly.net/events/event.php?e=croatia2026"

Outputs:
    event.json
    pilots.json
    flights.json
    tasks.json
    results.json
    standings.json
    import_runs.json
    competition.db
    errors.json
"""
from __future__ import annotations

import argparse, hashlib, json, re, sqlite3, sys, time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

UA = "BalloonCompetitionImporter/0.3"
TIMEOUT = 30

COUNTRY_NAMES = {
    "Australia","Austria","Belgium","Brazil","Bulgaria","Canada","Croatia",
    "Czech Republic","Denmark","Finland","France","Germany","Greece",
    "Hungary","Ireland","Italy","Japan","Lithuania","New Zealand","Norway",
    "Poland","Portugal","Romania","Serbia","Slovakia","Slovenia","Spain",
    "Sweden","Switzerland","The Netherlands","United Kingdom","United States",
}

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

def now_utc():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00","Z")

def clean_spaces(v):
    return re.sub(r"\s+", " ", v or "").strip()

def to_number(v):
    if v is None: return None
    v = str(v).replace(",", "").strip()
    if v in {"", "-", "—", "N/A", "n/a"}: return None
    try:
        x = float(v)
        return int(x) if x.is_integer() else x
    except ValueError:
        return None

class WatchMeFlyImporter:
    def __init__(self, event_url, delay=0.25):
        self.event_url = event_url
        self.delay = delay
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": UA})

    def get(self, url):
        r = self.session.get(url, timeout=TIMEOUT)
        r.raise_for_status()
        return r.text

    def soup(self, html):
        return BeautifulSoup(html, "html.parser")

    def event_url_with_view(self, view):
        p = urlparse(self.event_url)
        q = parse_qs(p.query)
        q["v"] = [view]
        return p._replace(query=urlencode(q, doseq=True)).geturl()

    def parse_event(self, html):
        s = self.soup(html)
        heading = s.find(["h1","h2","h3"])
        title = clean_spaces(heading.get_text(" ",strip=True)) if heading else ""
        if not title and s.title: title = clean_spaces(s.title.get_text(" ",strip=True))
        text = clean_spaces(s.get_text(" ",strip=True))

        dates = re.search(
            r"(\d{1,2}\s+[A-Za-z]+\s+\d{4})\s*[-–]\s*(\d{1,2}\s+[A-Za-z]+\s+\d{4})",
            text, re.I)
        location = None
        for label in ("Location","Venue","Place"):
            m = re.search(rf"{label}\s*[:\-]\s*(.*?)(?=\s+(?:Director|Organizer|Date|Dates|FAI|Category|Pilots?)\b|$)",
                          text, re.I)
            if m:
                location = clean_spaces(m.group(1)); break
        if not location:
            m = re.search(r"\b([A-Z][A-Za-zÀ-ÿ'’.\- ]{1,50},\s*[A-Z][A-Za-zÀ-ÿ'’.\- ]{1,50})\b", text)
            if m: location = clean_spaces(m.group(1))

        return {
            "source_url": self.event_url,
            "title": title,
            "location": location,
            "start_date": dates.group(1) if dates else None,
            "end_date": dates.group(2) if dates else None,
            "imported_at": now_utc(),
        }

    def parse_pilots(self, html):
        s = self.soup(html)
        strings = list(s.stripped_strings)
        pilots = {}
        for i, item in enumerate(strings):
            m = re.fullmatch(r"Pilot\s+#(\d+)", item, re.I)
            if not m: continue
            n = int(m.group(1))
            name = strings[i+1].strip() if i+1 < len(strings) else ""
            country = balloon = None
            for value in strings[i+2:i+14]:
                value = clean_spaces(value)
                if value.startswith("Balloon:"):
                    balloon = value.split(":",1)[1].strip().strip('"')
                elif value in COUNTRY_NAMES:
                    country = value
            pilots[n] = Pilot(n,name,country,balloon)
        return [asdict(pilots[n]) for n in sorted(pilots)]

    def parse_tasks(self, html):
        s = self.soup(html)
        tasks = []
        current_flight = None
        current_date = None
        # Treat a flight occurrence as date/time + number, avoiding Flight 3 collisions.
        for el in s.find_all(["h4","h5","a","div","span"]):
            txt = clean_spaces(el.get_text(" ",strip=True))
            fm = re.search(r"Flight\s+(\d+)\s*[-–]\s*(\d{1,2}\s+\w+\s+\d{4}\s+(?:AM|PM))", txt, re.I)
            if fm:
                current_flight = f"Flight {fm.group(1)}"
                current_date = fm.group(2)
            tm = re.search(r"Task\s+(\d+)\s*[-–]\s*(.*?)\s*(FINAL|OFFICIAL|PROVISIONAL|CANCELLED)\s*$", txt, re.I)
            if tm:
                href = el.get("href")
                tasks.append(asdict(Task(
                    int(tm.group(1)), clean_spaces(tm.group(2)), tm.group(3).upper(),
                    current_flight, current_date, urljoin(self.event_url,href) if href else None)))
        # Deduplicate repeated DOM representations. Prefer non-cancelled task occurrence.
        unique = {}
        for t in tasks:
            key = (t["number"], t["flight"], t["date"])
            if key not in unique or unique[key]["status"] == "CANCELLED":
                unique[key] = t
        return list(unique.values())

    def parse_result_page(self, url, task):
        html = self.get(url)
        s = self.soup(html)
        rows = []
        for table in s.find_all("table"):
            header_row = None; headers = []
            for tr in table.find_all("tr"):
                cells = [clean_spaces(x.get_text(" ",strip=True)) for x in tr.find_all(["th","td"])]
                low = [c.lower() for c in cells]
                if "pilot" in low and "score" in low:
                    header_row, headers = tr, cells
                    break
            if not headers: continue
            for tr in table.find_all("tr"):
                if tr is header_row: continue
                cells = [clean_spaces(x.get_text(" ",strip=True)) for x in tr.find_all(["td","th"])]
                if len(cells) != len(headers) or cells == headers: continue
                raw = dict(zip(headers,cells))
                if "Pilot" not in raw: continue
                rows.append({"task_number":task["number"],"task_name":task["name"],
                             "status":task["status"],"source_url":url,"raw":raw})
        return rows

    @staticmethod
    def parse_pilot_field(value):
        value = clean_spaces(value)
        m = re.match(r"#(\d+)\s*-\s*(.*)", value)
        if not m: return None, value or None, None
        n, rem = int(m.group(1)), m.group(2).strip()
        country = None
        for c in sorted(COUNTRY_NAMES,key=len,reverse=True):
            if rem.endswith(" "+c):
                country=c; rem=rem[:-(len(c)+1)].strip(); break
        if "," in rem:
            surname,given=rem.split(",",1); name=f"{given.strip()} {surname.strip()}"
        else: name=rem
        return n,name,country

    def normalize_results(self, raw):
        out=[]
        for item in raw:
            r=item["raw"]
            n,name,country=self.parse_pilot_field(r.get("Pilot",""))
            out.append({
                "task_number":item["task_number"],"task_name":item["task_name"],
                "status":item["status"],"competition_number":n,"pilot":name,
                "country":country,"rank":to_number(r.get("Rank")),"result":r.get("Result"),
                "points":to_number(r.get("Points")),"penalty_t":to_number(r.get("Penalty (T)")),
                "penalty_c":to_number(r.get("Penalty (C)")),"score":to_number(r.get("Score")),
                "notes":r.get("Notes",""),"source_url":item["source_url"],
            })
        return out

    def run(self):
        event=self.parse_event(self.get(self.event_url))
        pilots=self.parse_pilots(self.get(self.event_url_with_view("pp")))
        tasks=self.parse_tasks(self.get(self.event_url_with_view("t")))
        raw=[]; errors=[]
        for task in tasks:
            if not task["url"] or task["status"]=="CANCELLED": continue
            try:
                time.sleep(self.delay)
                raw.extend(self.parse_result_page(task["url"],task))
            except Exception as exc:
                errors.append({"task_number":task["number"],"url":task["url"],"error":repr(exc)})

        results=self.normalize_results(raw)
        pmap={p["competition_number"]:p for p in pilots}
        for r in results:
            n=r["competition_number"]
            if n is not None and n not in pmap:
                pmap[n]={"competition_number":n,"name":r["pilot"] or "",
                         "country":r["country"],"balloon":None}
        pilots=[pmap[n] for n in sorted(pmap)]
        # Flights are unique occurrences, not merely flight numbers.
        flights=[]; fids={}
        for t in sorted(tasks,key=lambda x:(x["date"] or "",x["number"])):
            key=(t["flight"],t["date"])
            if key not in fids and t["flight"]:
                fid=f"flight-{len(flights)+1}"
                fids[key]=fid
                flights.append({"id":fid,"flight":t["flight"],"date":t["date"],"tasks":[]})
            if key in fids: flights[-1]["tasks"].append(t["number"])
        standings=calculate_standings(pilots,results)
        return {"event":event,"pilots":pilots,"flights":flights,"tasks":tasks,
                "results":results,"standings":standings,
                "import_runs":[],"errors":errors}

def calculate_standings(pilots, results):
    task_numbers=sorted({r["task_number"] for r in results})
    lookup={p["competition_number"]:p for p in pilots}
    def view(statuses):
        totals={n:0 for n in lookup}; by_task={n:{} for n in lookup}
        for r in results:
            if r["status"] not in statuses or r["competition_number"] is None: continue
            n=r["competition_number"]; score=r["score"] or 0
            totals[n]+=score; by_task[n][r["task_number"]]=score
        ordered=sorted(totals.items(),key=lambda x:(-x[1],lookup[x[0]]["name"]))
        rows=[]
        for pos,(n,total) in enumerate(ordered,1):
            rows.append({"position":pos,"competition_number":n,"pilot":lookup[n]["name"],
                         "country":lookup[n]["country"],"total":total,
                         "tasks":{str(t):by_task[n].get(t) for t in task_numbers}})
        return {"task_numbers":task_numbers,"rows":rows}
    official=view({"FINAL","OFFICIAL"})
    all_results=view({"FINAL","OFFICIAL","PROVISIONAL"})
    # Ranking progression: cumulative total after each task.
    progression={}
    for mode,statuses in [("official",{"FINAL","OFFICIAL"}),("all_results",{"FINAL","OFFICIAL","PROVISIONAL"})]:
        progression[mode]=[]
        for through in task_numbers:
            rows=[]
            totals={n:0 for n in lookup}
            for r in results:
                if r["task_number"]<=through and r["status"] in statuses and r["competition_number"] is not None:
                    totals[r["competition_number"]]+=r["score"] or 0
            ordered=sorted(totals.items(),key=lambda x:(-x[1],lookup[x[0]]["name"]))
            for pos,(n,total) in enumerate(ordered,1):
                rows.append({"position":pos,"competition_number":n,"pilot":lookup[n]["name"],"total":total})
            progression[mode].append({"through_task":through,"rows":rows})
    return {"official":official,"all_results":all_results,"progression":progression}

def init_db(db):
    con=sqlite3.connect(db)
    con.executescript("""
    PRAGMA foreign_keys=ON;
    CREATE TABLE IF NOT EXISTS competitions(
      id TEXT PRIMARY KEY, source TEXT NOT NULL, source_url TEXT NOT NULL,
      title TEXT, location TEXT, start_date TEXT, end_date TEXT, imported_at TEXT);
    CREATE TABLE IF NOT EXISTS import_runs(
      id TEXT PRIMARY KEY, competition_id TEXT NOT NULL, imported_at TEXT NOT NULL,
      result_count INTEGER, task_count INTEGER, pilot_count INTEGER, error_count INTEGER,
      content_hash TEXT, FOREIGN KEY(competition_id) REFERENCES competitions(id));
    CREATE TABLE IF NOT EXISTS flights(
      id TEXT PRIMARY KEY, competition_id TEXT NOT NULL, flight_number TEXT,
      date_label TEXT, sort_order INTEGER, UNIQUE(competition_id,flight_number,date_label),
      FOREIGN KEY(competition_id) REFERENCES competitions(id));
    CREATE TABLE IF NOT EXISTS pilots(
      id INTEGER PRIMARY KEY AUTOINCREMENT, competition_id TEXT NOT NULL,
      competition_number INTEGER NOT NULL, name TEXT, country TEXT, balloon TEXT,
      UNIQUE(competition_id,competition_number),
      FOREIGN KEY(competition_id) REFERENCES competitions(id));
    CREATE TABLE IF NOT EXISTS tasks(
      id INTEGER PRIMARY KEY AUTOINCREMENT, competition_id TEXT NOT NULL,
      task_number INTEGER NOT NULL, name TEXT, status TEXT, flight_id TEXT,
      date_label TEXT, source_url TEXT, UNIQUE(competition_id,task_number,flight_id),
      FOREIGN KEY(competition_id) REFERENCES competitions(id),
      FOREIGN KEY(flight_id) REFERENCES flights(id));
    CREATE TABLE IF NOT EXISTS results(
      id INTEGER PRIMARY KEY AUTOINCREMENT, import_run_id TEXT NOT NULL,
      task_id INTEGER NOT NULL, pilot_id INTEGER NOT NULL, status TEXT,
      rank REAL, result_text TEXT, points REAL, penalty_t REAL, penalty_c REAL,
      score REAL, notes TEXT, source_url TEXT,
      FOREIGN KEY(import_run_id) REFERENCES import_runs(id),
      FOREIGN KEY(task_id) REFERENCES tasks(id),
      FOREIGN KEY(pilot_id) REFERENCES pilots(id));
    CREATE INDEX IF NOT EXISTS idx_results_task ON results(task_id);
    CREATE INDEX IF NOT EXISTS idx_results_pilot ON results(pilot_id);
    """)
    return con

def save_db(output,data,event_id):
    db=output/"competition.db"; con=init_db(db)
    run_id=hashlib.sha1((now_utc()+event_id).encode()).hexdigest()[:16]
    content_hash=hashlib.sha256(json.dumps(data["results"],sort_keys=True).encode()).hexdigest()
    con.execute("""INSERT OR REPLACE INTO competitions VALUES(?,?,?,?,?,?,?,?)""",
                (event_id,"watchmefly",data["event"]["source_url"],data["event"]["title"],
                 data["event"]["location"],data["event"]["start_date"],data["event"]["end_date"],data["event"]["imported_at"]))
    con.execute("INSERT INTO import_runs VALUES(?,?,?,?,?,?,?,?)",
                (run_id,event_id,now_utc(),len(data["results"]),len(data["tasks"]),len(data["pilots"]),
                 len(data["errors"]),content_hash))
    con.execute("DELETE FROM tasks WHERE competition_id=?",(event_id,))
    con.execute("DELETE FROM flights WHERE competition_id=?",(event_id,))
    # pilots are retained by number; update them.
    for p in data["pilots"]:
        con.execute("""INSERT INTO pilots(competition_id,competition_number,name,country,balloon)
                       VALUES(?,?,?,?,?) ON CONFLICT(competition_id,competition_number)
                       DO UPDATE SET name=excluded.name,country=excluded.country,balloon=excluded.balloon""",
                    (event_id,p["competition_number"],p["name"],p["country"],p["balloon"]))
    for i,f in enumerate(data["flights"],1):
        con.execute("INSERT INTO flights VALUES(?,?,?,?,?)",
                    (f["id"],event_id,f["flight"],f["date"],i))
    task_id={}
    for t in data["tasks"]:
        fid=next((f["id"] for f in data["flights"] if t["flight"]==f["flight"] and t["date"]==f["date"]),None)
        cur=con.execute("""INSERT INTO tasks(competition_id,task_number,name,status,flight_id,date_label,source_url)
                           VALUES(?,?,?,?,?,?,?)""",
                        (event_id,t["number"],t["name"],t["status"],fid,t["date"],t["url"]))
        task_id[(t["number"],t["flight"],t["date"])]=cur.lastrowid
    for r in data["results"]:
        p=con.execute("SELECT id FROM pilots WHERE competition_id=? AND competition_number=?",
                      (event_id,r["competition_number"])).fetchone()
        tid=task_id.get((r["task_number"],r["task_name"] if False else next((t["flight"] for t in data["tasks"] if t["number"]==r["task_number"]),None),
                        next((t["date"] for t in data["tasks"] if t["number"]==r["task_number"]),None)))
        if p and tid:
            con.execute("""INSERT INTO results(import_run_id,task_id,pilot_id,status,rank,result_text,points,penalty_t,penalty_c,score,notes,source_url)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (run_id,tid,p[0],r["status"],r["rank"],r["result"],r["points"],r["penalty_t"],
                         r["penalty_c"],r["score"],r["notes"],r["source_url"]))
    con.commit(); con.close()
    return run_id

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("url"); ap.add_argument("--output",default=None); ap.add_argument("--delay",type=float,default=.25)
    args=ap.parse_args()
    if "watchmefly.net" not in urlparse(args.url).netloc:
        raise SystemExit("This importer currently supports watchmefly.net URLs only.")
    eid=parse_qs(urlparse(args.url).query).get("e",["event"])[0]
    out=Path(args.output or Path("data")/eid); out.mkdir(parents=True,exist_ok=True)
    data=WatchMeFlyImporter(args.url,args.delay).run()
    run_id=save_db(out,data,eid)
    data["import_runs"]=[{"id":run_id,"imported_at":now_utc(),"result_count":len(data["results"])}]
    for name,value in data.items():
        (out/f"{name}.json").write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding="utf-8")
    print(f"Event: {data['event']['title']}")
    print(f"Pilots: {len(data['pilots'])} | Flights: {len(data['flights'])} | Tasks: {len(data['tasks'])} | Results: {len(data['results'])} | Errors: {len(data['errors'])}")
    print(f"Database: {out/'competition.db'}")

if __name__=="__main__":
    main()
