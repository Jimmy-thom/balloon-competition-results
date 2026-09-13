from __future__ import annotations
from pathlib import Path
from flask import Flask, abort, jsonify, render_template, request
import os
from db import connect, is_postgres, init_postgres

BASE = Path(__file__).resolve().parent
DATA_DIR = BASE / "data"
app = Flask(__name__)

def db_for(eid):
    if is_postgres(): return None
    p = DATA_DIR / eid / "competition.db"
    if not p.exists(): abort(404)
    return p
def conn(eid=None):
    c=connect(db_for(eid) if not is_postgres() else None)
    if is_postgres(): init_postgres(c)
    return c
def latest_run(c, eid):
    return c.execute("SELECT * FROM import_runs WHERE competition_id=? ORDER BY imported_at DESC LIMIT 1", (eid,)).fetchone()
def event(eid):
    c=conn(eid); r=c.execute("SELECT * FROM competitions WHERE id=?",(eid,)).fetchone(); c.close()
    if not r: abort(404)
    return dict(r)
def mode_statuses(mode):
    if mode == "official": return ("FINAL","OFFICIAL")
    if mode == "all": return ("FINAL","OFFICIAL","PROVISIONAL")
    abort(400, description="mode must be official or all")


def _flight_columns_available(c):
    """Return whether the current flights table has the importer flight metadata."""
    try:
        c.execute("SELECT status, flight_type FROM flights LIMIT 1").fetchone()
        return True
    except Exception:
        return False


def _flight_chronology_key(f):
    """Return a sortable real-world date/time key for a flight row."""
    from datetime import datetime
    date_text=str(f["date_label"] or "").strip()
    time_text=str(f["time_label"] or "").strip().upper()
    for fmt in ("%d %b %Y", "%d %B %Y"):
        try:
            dt=datetime.strptime(date_text,fmt)
            if time_text in ("AM","PM"):
                hour=0 if time_text=="AM" else 12
                dt=dt.replace(hour=hour)
            return (0,dt)
        except ValueError:
            pass
    return (1,int(f["sort_order"] or 0),str(f["id"]))


def _completed_competition_flights(c, eid):
    """Return unique flown competition flights in chronological order.

    WatchMeFly lists newest flights first, so importer sort_order is not a
    chronological ordering.  A flight identity is flight number + date + time;
    duplicate database rows for the same real flight are collapsed, preferring
    the row that has the greatest number of scored tasks.
    """
    columns = _flight_columns_available(c)
    if columns:
        rows = c.execute(
            """SELECT f.*
                 FROM flights f
                WHERE f.competition_id=?
                  AND UPPER(COALESCE(f.flight_type,'')) NOT IN ('PRACTICE','TRAINING')
                  AND LEFT(UPPER(COALESCE(f.flight_number,'')), 8) <> 'PRACTICE'
                  AND UPPER(COALESCE(f.status,'')) NOT IN ('CANCELLED','CANCELED')
                  AND EXISTS (
                      SELECT 1 FROM tasks t JOIN results r ON r.task_id=t.id
                       WHERE t.flight_id=f.id AND r.score IS NOT NULL
                  )
                """,
            (eid,)
        ).fetchall()
    else:
        rows = c.execute(
            """SELECT f.*
                 FROM flights f
                WHERE f.competition_id=?
                  AND LEFT(UPPER(COALESCE(f.flight_number,'')), 8) <> 'PRACTICE'
                  AND EXISTS (
                      SELECT 1 FROM tasks t JOIN results r ON r.task_id=t.id
                       WHERE t.flight_id=f.id AND r.score IS NOT NULL
                  )
                """,
            (eid,)
        ).fetchall()

    grouped={}
    for f in rows:
        key=(str(f["flight_number"] or "").strip(),
             str(f["date_label"] or "").strip(),
             str(f["time_label"] or "").strip().upper())
        scored=c.execute(
            """SELECT COUNT(*) AS n FROM tasks t JOIN results r ON r.task_id=t.id
               WHERE t.flight_id=? AND r.score IS NOT NULL""",
            (f["id"],)
        ).fetchone()["n"]
        candidate=(scored,_flight_chronology_key(f),str(f["id"]))
        if key not in grouped or candidate>grouped[key][0]:
            grouped[key]=(candidate,f)

    out=[item[1] for item in grouped.values()]
    out.sort(key=_flight_chronology_key)
    return out


def _effective_task_ids_through_flight(c, eid, flight_row, run_id, mode):
    """Select one effective scored task occurrence through a real flight.

    Chronology is based on the flight's actual date/time, not WatchMeFly's
    reverse-page sort_order.  This is what lets Flight 3 provisional results be
    compared with the completed Flight 2 checkpoint.
    """
    statuses=mode_statuses(mode)
    qs=','.join('?'*len(statuses))

    # Build the set of real competition flights up to and including the
    # requested checkpoint.  Practice/cancelled flights are already excluded.
    flights=_completed_competition_flights(c,eid)
    cutoff=_flight_chronology_key(flight_row)
    allowed=[f["id"] for f in flights if _flight_chronology_key(f)<=cutoff]
    if not allowed:
        return []

    qf=','.join('?'*len(allowed))
    rows=c.execute(
        f"""SELECT t.id,t.task_number,t.status,t.published,
                  f.date_label,f.time_label,f.sort_order,f.id AS flight_id
             FROM tasks t JOIN flights f ON f.id=t.flight_id
            WHERE t.competition_id=? AND f.id IN ({qf})
              AND EXISTS (
                  SELECT 1 FROM results r
                   WHERE r.task_id=t.id AND r.status IN ({qs})
              )
            ORDER BY f.date_label, f.time_label, f.id,
                     CASE t.status WHEN 'FINAL' THEN 0 WHEN 'OFFICIAL' THEN 1
                                   WHEN 'PROVISIONAL' THEN 2 ELSE 3 END,
                     t.published DESC, t.id DESC""",
        (eid,*allowed,*statuses)
    ).fetchall()

    # Process newest flight first so a later re-flight/version wins.
    rows=sorted(rows,key=lambda r:(
        _flight_chronology_key({"date_label":r["date_label"],"time_label":r["time_label"],"sort_order":r["sort_order"],"id":r["flight_id"]}),
        0 if r["status"]=="FINAL" else 1 if r["status"]=="OFFICIAL" else 2,
        r["published"] or "", str(r["id"])
    ),reverse=True)
    selected={}
    for row in rows:
        if row["task_number"] not in selected:
            selected[row["task_number"]]=row["id"]
    return list(selected.values())


def _standings_for_task_ids(c, eid, run_id, task_ids, mode):
    """Build cumulative standings from effective task IDs using stored results.

    For movement, results can span multiple watcher import runs. Pick the newest
    eligible result for each pilot/task so an unchanged earlier task remains part
    of the cumulative checkpoint when later tasks arrive.
    """
    if not task_ids:
        return []
    statuses=mode_statuses(mode)
    qids=','.join('?'*len(task_ids)); qs=','.join('?'*len(statuses))
    rows=c.execute(
        f"""SELECT r.score,p.competition_number,p.name,p.country,r.task_id,r.id
             FROM results r JOIN pilots p ON p.id=r.pilot_id
            WHERE r.task_id IN ({qids})
              AND r.status IN ({qs})
              AND r.id=(
                  SELECT r2.id
                    FROM results r2
                   WHERE r2.task_id=r.task_id
                     AND r2.pilot_id=r.pilot_id
                     AND r2.status IN ({qs})
                   ORDER BY r2.id DESC
                   LIMIT 1
              )""",
        (*task_ids,*statuses,*statuses)
    ).fetchall()
    totals={}
    for r in rows:
        n=r["competition_number"]
        totals[n]=totals.get(n,0)+(r["score"] or 0)
    pilots={r["competition_number"]:dict(r) for r in c.execute(
        "SELECT competition_number,name,country FROM pilots WHERE competition_id=?",(eid,)
    ).fetchall()}
    ordered=sorted(totals,key=lambda n:(-totals[n],pilots[n]["name"]))
    return [{"position":i,"competition_number":n,"pilot":pilots[n]["name"],
             "country":pilots[n]["country"],"total":totals[n]}
            for i,n in enumerate(ordered,1)]


def _movement_for_latest_completed_flight(c, eid, mode, end=None):
    """Compare the latest scored competition flight with the prior completed one.

    The current checkpoint may be IN PROGRESS when provisional results are
    appearing.  The previous checkpoint must be an earlier completed flight.
    """
    run=latest_run(c,eid)
    if not run:
        return {}

    flights=_completed_competition_flights(c,eid)
    if not flights:
        return {}

    # The latest scored flight is the current checkpoint.  If Flight 3 is in
    # progress with provisional results, it is included here even though its
    # status is not COMPLETE.  The helper already requires scored results and
    # excludes cancelled/practice flights.
    current=flights[-1]

    # Optional task-number filter should not make us jump to an older flight
    # merely because the current flight has only newer task numbers.
    if end is not None:
        eligible=[f for f in flights if c.execute(
            """SELECT 1 FROM tasks WHERE competition_id=? AND flight_id=?
               AND task_number<=? LIMIT 1""",(eid,f["id"],end)
        ).fetchone()]
        if eligible:
            current=eligible[-1]
        else:
            return {}

    idx=next((i for i,f in enumerate(flights) if str(f["id"])==str(current["id"])),None)
    if idx is None or idx==0:
        return {}

    # Find the most recent earlier flight that is actually COMPLETE/FINAL when
    # metadata is available.  This deliberately skips cancelled/in-progress
    # flights while allowing the current checkpoint to be in progress.
    previous=None
    if _flight_columns_available(c):
        for f in reversed(flights[:idx]):
            if str(f["status"] or "").upper() in ("COMPLETE","COMPLETED","FINAL"):
                previous=f
                break
    else:
        previous=flights[idx-1]
    if previous is None:
        return {}

    current_ids=_effective_task_ids_through_flight(c,eid,current,run["id"],mode)
    previous_ids=_effective_task_ids_through_flight(c,eid,previous,run["id"],mode)
    current_rows=_standings_for_task_ids(c,eid,run["id"],current_ids,mode)
    previous_rows=_standings_for_task_ids(c,eid,run["id"],previous_ids,mode)
    current_pos={r["competition_number"]:r["position"] for r in current_rows}
    previous_pos={r["competition_number"]:r["position"] for r in previous_rows}
    return {n:(previous_pos[n]-pos) if n in previous_pos else None for n,pos in current_pos.items()}


def all_tasks(c,eid):
    return [dict(r) for r in c.execute("""SELECT t.*, f.flight_number, f.date_label AS flight_date
      FROM tasks t LEFT JOIN flights f ON f.id=t.flight_id
      WHERE t.competition_id=? ORDER BY t.task_number, f.date_label""",(eid,)).fetchall()]
def active_task(c,eid,num):
    return c.execute("""SELECT t.*,f.flight_number,f.date_label AS flight_date
      FROM tasks t LEFT JOIN flights f ON f.id=t.flight_id
      WHERE t.competition_id=? AND t.task_number=?
      ORDER BY CASE WHEN EXISTS (SELECT 1 FROM results r WHERE r.task_id=t.id) THEN 0 ELSE 1 END,
               CASE t.status WHEN 'FINAL' THEN 0 WHEN 'OFFICIAL' THEN 1 WHEN 'PROVISIONAL' THEN 2 ELSE 3 END,
               f.date_label DESC LIMIT 1""",(eid,num)).fetchone()
def standings_data(eid,mode="official",start=None,end=None):
    """Build cumulative standings while retaining task scores from prior imports.

    A watcher run is a snapshot of what WatchMeFly exposed at that moment, so a
    later run may contain T6-T8 without repeating the older T1-T5 result rows.
    Select the effective task occurrence first, then take the newest stored
    result for each pilot/task from any import run, restricted to the selected
    result statuses.
    """
    statuses=mode_statuses(mode)
    c=conn(eid)

    # Work out the task numbers in the requested range.
    task_params=[eid]
    task_where="competition_id=?"
    if start is not None:
        task_where += " AND task_number>=?"
        task_params.append(start)
    if end is not None:
        task_where += " AND task_number<=?"
        task_params.append(end)
    task_numbers=[r["task_number"] for r in c.execute(
        f"SELECT DISTINCT task_number FROM tasks WHERE {task_where} ORDER BY task_number",
        task_params
    ).fetchall()]

    if not task_numbers:
        c.close()
        return []

    # Pick one effective task occurrence per task number.  Prefer an occurrence
    # that actually has results in the selected mode, then prefer FINAL/OFFICIAL
    # over PROVISIONAL, then the newest published/id.  This preserves the
    # existing re-flight behaviour without allowing an empty task placeholder
    # to hide a scored occurrence.
    task_ids=[]
    for num in task_numbers:
        qs=','.join('?'*len(statuses))
        t=c.execute(f"""
            SELECT t.id,t.task_number,t.status,t.published
            FROM tasks t
            WHERE t.competition_id=? AND t.task_number=?
            ORDER BY
              CASE WHEN EXISTS (
                SELECT 1 FROM results r
                WHERE r.task_id=t.id AND r.status IN ({qs})
              ) THEN 0 ELSE 1 END,
              CASE t.status
                WHEN 'FINAL' THEN 0
                WHEN 'OFFICIAL' THEN 1
                WHEN 'PROVISIONAL' THEN 2
                ELSE 3
              END,
              t.published DESC,
              t.id DESC
            LIMIT 1
        """,(eid,num,*statuses)).fetchone()
        if t:
            task_ids.append(t["id"])

    if not task_ids:
        c.close()
        return []

    # Pull the newest eligible result for each pilot/task.  The result may have
    # been written by an earlier import run; that is intentional and fixes the
    # case where T1-T5 were unchanged while a later run added T6-T8.
    qids=','.join('?'*len(task_ids))
    qs=','.join('?'*len(statuses))
    rows=c.execute(f"""
        SELECT p.competition_number,p.name,p.country,
               r.task_id,r.score,r.status,r.id
        FROM results r
        JOIN pilots p ON p.id=r.pilot_id
        WHERE r.task_id IN ({qids})
          AND r.status IN ({qs})
          AND r.id=(
            SELECT r2.id
            FROM results r2
            WHERE r2.task_id=r.task_id
              AND r2.pilot_id=r.pilot_id
              AND r2.status IN ({qs})
            ORDER BY r2.id DESC
            LIMIT 1
          )
    """,(*task_ids,*statuses,*statuses)).fetchall()

    # Map task ids back to task numbers.
    id_to_num={}
    for tid in task_ids:
        tr=c.execute("SELECT task_number FROM tasks WHERE id=?",(tid,)).fetchone()
        if tr:
            id_to_num[tid]=tr["task_number"]

    totals={}
    scores={}
    statuses_by={}
    pilots={}
    for r in rows:
        n=r["competition_number"]
        num=id_to_num.get(r["task_id"])
        if num is None:
            continue
        score=r["score"] or 0
        totals[n]=totals.get(n,0)+score
        scores.setdefault(n,{})[num]=r["score"]
        statuses_by.setdefault(n,{})[num]=r["status"]
        pilots[n]={"competition_number":n,"name":r["name"],"country":r["country"]}

    ordered=sorted(totals,key=lambda n:(-totals[n],pilots[n]["name"]))
    out=[{
        "position":i,
        "competition_number":n,
        "pilot":pilots[n]["name"],
        "country":pilots[n]["country"],
        "total":totals[n],
        "tasks":scores.get(n,{}),
        "statuses":statuses_by.get(n,{})
    } for i,n in enumerate(ordered,1)]
    c.close()
    return out

def progression_data(eid,mode="official",start=None,end=None):
    c=conn(eid); nums=[r["task_number"] for r in c.execute("SELECT DISTINCT task_number FROM tasks WHERE competition_id=? ORDER BY task_number",(eid,)).fetchall()]; c.close()
    if start is not None: nums=[n for n in nums if n>=start]
    if end is not None: nums=[n for n in nums if n<=end]
    return [{"through_task":n,"rows":standings_data(eid,mode,start if start is not None else None,n)} for n in nums]
def common_filters():
    mode=request.args.get("mode","official"); start=request.args.get("from",type=int); end=request.args.get("to",type=int)
    if start is not None and end is not None and start>end: abort(400,description="from must be <= to")
    return mode,start,end
def task_results(eid,num,mode="all"):
    c=conn(eid); t=active_task(c,eid,num); run=latest_run(c,eid)
    if not t or not run: c.close(); abort(404)
    statuses=mode_statuses(mode); qs=','.join('?'*len(statuses))
    rs=c.execute(f"""SELECT r.*,p.competition_number,p.name,p.country FROM results r JOIN pilots p ON p.id=r.pilot_id
      WHERE r.import_run_id=? AND r.task_id=? AND r.status IN ({qs})
      ORDER BY CASE WHEN r.rank IS NULL THEN 999999 ELSE r.rank END""",(run["id"],t["id"],*statuses)).fetchall()
    c.close(); return dict(t),[dict(r) for r in rs]
def flight_standings(eid,flight_id,mode="all",cumulative=False):
    statuses=mode_statuses(mode); c=conn(eid); run=latest_run(c,eid)
    if not run: c.close(); return []
    flight=c.execute("SELECT * FROM flights WHERE competition_id=? AND id=?",(eid,flight_id)).fetchone()
    if not flight: c.close(); abort(404)
    if cumulative:
        tasks=c.execute("""SELECT t.id,t.task_number FROM tasks t JOIN flights f ON f.id=t.flight_id
          WHERE t.competition_id=? AND f.sort_order<=? ORDER BY f.sort_order,t.task_number""",(eid,flight["sort_order"])).fetchall()
    else:
        tasks=c.execute("SELECT id,task_number FROM tasks WHERE competition_id=? AND flight_id=? ORDER BY task_number",(eid,flight_id)).fetchall()
    task_ids=[r["id"] for r in tasks]
    if not task_ids: c.close(); return []
    qids=','.join('?'*len(task_ids)); qs=','.join('?'*len(statuses))
    rows=c.execute(f"""SELECT r.score,p.competition_number,p.name,p.country
      FROM results r JOIN pilots p ON p.id=r.pilot_id
      WHERE r.import_run_id=? AND r.task_id IN ({qids}) AND r.status IN ({qs})""",
      (run["id"],*task_ids,*statuses)).fetchall()
    totals={}
    for r in rows:
        n=r["competition_number"]; totals[n]=totals.get(n,0)+(r["score"] or 0)
    pilots={r["competition_number"]:dict(r) for r in c.execute("SELECT competition_number,name,country FROM pilots WHERE competition_id=?",(eid,)).fetchall()}
    ordered=sorted(totals,key=lambda n:(-totals[n],pilots[n]["name"]))
    out=[{"position":i,"competition_number":n,"pilot":pilots[n]["name"],"country":pilots[n]["country"],"total":totals[n]} for i,n in enumerate(ordered,1)]
    c.close(); return out
def task_navigation(c,eid,num):
    rows=c.execute("SELECT task_number,name,status FROM tasks WHERE competition_id=? ORDER BY task_number",(eid,)).fetchall()
    unique={}
    for r in rows: unique.setdefault(r["task_number"],dict(r))
    nums=sorted(unique); idx=nums.index(num) if num in nums else -1
    prev=unique[nums[idx-1]] if idx>0 else None
    nxt=unique[nums[idx+1]] if idx>=0 and idx+1<len(nums) else None
    return prev,nxt

@app.get("/")
def index():
    if is_postgres():
        c=conn(); events=[dict(r) for r in c.execute("SELECT * FROM competitions ORDER BY title").fetchall()]; c.close()
    else:
        events=[]
        if DATA_DIR.exists():
            for d in sorted(p for p in DATA_DIR.iterdir() if p.is_dir() and (p/"competition.db").exists()): events.append(event(d.name))
    return render_template("index.html",events=events)
@app.get("/event/<eid>")
def event_page(eid):
    e=event(eid); c=conn(eid); ts=all_tasks(c,eid); fs=[dict(r) for r in c.execute("SELECT * FROM flights WHERE competition_id=? ORDER BY sort_order",(eid,)).fetchall()]; pc=c.execute("SELECT COUNT(*) FROM pilots WHERE competition_id=?",(eid,)).fetchone()[0]; run=latest_run(c,eid); task_max=max([r["task_number"] for r in c.execute("SELECT task_number FROM tasks WHERE competition_id=?",(eid,)).fetchall()] or [1]); c.close()
    return render_template("event.html",event=e,tasks=ts,flights=fs,pilot_count=pc,latest_import=dict(run) if run else None,tasks_max=task_max)
@app.get("/event/<eid>/task/<int:num>")
def task_page(eid,num):
    e=event(eid); mode=request.args.get("mode","all"); t,rs=task_results(eid,num,mode); c=conn(eid); prev,nxt=task_navigation(c,eid,num); c.close()
    return render_template("task.html",event=e,task=t,results=rs,mode=mode,prev_task=prev,next_task=nxt)
@app.get("/event/<eid>/pilot/<int:number>")
def pilot_page(eid,number):
    e=event(eid); c=conn(eid); p=c.execute("SELECT * FROM pilots WHERE competition_id=? AND competition_number=?",(eid,number)).fetchone(); run=latest_run(c,eid)
    if not p: abort(404)

    # Load flights in their competition order so the pilot page can display
    # Flight 1, Flight 2, etc. directly from the server (no JavaScript lookup).
    flights=[dict(r) for r in c.execute(
        "SELECT * FROM flights WHERE competition_id=? ORDER BY sort_order",(eid,)
    ).fetchall()]
    flight_ordinals={str(f["id"]):i for i,f in enumerate(flights,1)}

    rs=c.execute("""SELECT r.*,t.task_number,t.name,t.status AS task_status,
             f.id AS flight_id,f.flight_number,f.date_label AS flight_date
      FROM results r JOIN tasks t ON t.id=r.task_id LEFT JOIN flights f ON f.id=t.flight_id
      WHERE r.import_run_id=? AND r.pilot_id=? ORDER BY t.task_number,f.date_label""",
      (run["id"],p["id"])).fetchall()
    results=[]
    for row in rs:
        item=dict(row)
        item["flight_ordinal"]=flight_ordinals.get(str(item.get("flight_id")))
        results.append(item)

    pilot=dict(p)

    # Clean imported country prefixes/suffixes for this display page only.
    country_raw=(pilot.get("country") or "").strip()
    country_map={
        "AU":"Australia","AUS":"Australia","GB":"United Kingdom","GBR":"United Kingdom",
        "AT":"Austria","AUT":"Austria","HR":"Croatia","HRV":"Croatia",
        "CZ":"Czech Republic","CZE":"Czech Republic","DE":"Germany","DEU":"Germany",
        "HU":"Hungary","HUN":"Hungary","LT":"Lithuania","LTU":"Lithuania",
        "NL":"Netherlands","NLD":"Netherlands","PL":"Poland","POL":"Poland",
        "SK":"Slovakia","SVK":"Slovakia","SI":"Slovenia","SVN":"Slovenia",
        "NZ":"New Zealand","NZL":"New Zealand","FR":"France","FRA":"France",
        "IT":"Italy","ITA":"Italy","ES":"Spain","ESP":"Spain",
        "CH":"Switzerland","CHE":"Switzerland","US":"United States","USA":"United States",
        "CA":"Canada","CAN":"Canada"
    }
    parts=country_raw.split()
    if parts and parts[0].upper() in country_map:
        pilot["country"]=country_map[parts[0].upper()]
    else:
        pilot["country"]=country_raw

    # Some imported names have the country appended. Remove a recognised
    # country suffix from the display name without changing the database.
    name=(pilot.get("name") or "").strip()
    for country_name in sorted(set(country_map.values()), key=len, reverse=True):
        if name.lower().endswith(" " + country_name.lower()):
            name=name[:-(len(country_name)+1)].rstrip()
            break
    pilot["name"]=name

    c.close()
    return render_template("pilot.html",event=e,pilot=pilot,results=results,flights=flights)
@app.get("/event/<eid>/compare")
def compare_page(eid):
    e=event(eid); c=conn(eid); pilots=[dict(r) for r in c.execute("SELECT competition_number,name,country FROM pilots WHERE competition_id=? ORDER BY name",(eid,)).fetchall()]; task_max=max([r["task_number"] for r in c.execute("SELECT task_number FROM tasks WHERE competition_id=?",(eid,)).fetchall()] or [1]); c.close()
    return render_template("compare.html",event=e,pilots=pilots,tasks_max=task_max)
@app.get("/event/<eid>/search")
def search_page(eid):
    e=event(eid); q=request.args.get("q","").strip(); c=conn(eid); pilots=[]
    if q:
        like="%"+q+"%"; pilots=[dict(r) for r in c.execute("SELECT competition_number,name,country FROM pilots WHERE competition_id=? AND (name LIKE ? OR CAST(competition_number AS TEXT) LIKE ?)",(eid,like,like)).fetchall()]
    c.close(); return render_template("search.html",event=e,q=q,pilots=pilots)
@app.get("/event/<eid>/history")
def history_page(eid):
    e=event(eid); c=conn(eid); runs=[dict(r) for r in c.execute("SELECT * FROM import_runs WHERE competition_id=? ORDER BY imported_at DESC",(eid,)).fetchall()]; c.close(); return render_template("history.html",event=e,runs=runs)
@app.get("/event/<eid>/flight/<flight_id>")
def flight_page(eid,flight_id):
    e=event(eid); c=conn(eid); f=c.execute("SELECT * FROM flights WHERE competition_id=? AND id=?",(eid,flight_id)).fetchone()
    if not f: abort(404)
    ts=c.execute("SELECT * FROM tasks WHERE competition_id=? AND flight_id=? ORDER BY task_number",(eid,flight_id)).fetchall(); flights=c.execute("SELECT * FROM flights WHERE competition_id=? ORDER BY sort_order",(eid,)).fetchall(); c.close()
    return render_template("flight.html",event=e,flight=dict(f),tasks=[dict(t) for t in ts],flights=[dict(x) for x in flights])
@app.get("/api/events")
def api_events():
    if is_postgres():
        c=conn(); out=[dict(r) for r in c.execute("SELECT * FROM competitions ORDER BY title").fetchall()]; c.close(); return jsonify(out)
    return jsonify([event(d.name) for d in sorted(DATA_DIR.iterdir()) if d.is_dir() and (d/"competition.db").exists()] if DATA_DIR.exists() else [])
@app.get("/api/event/<eid>")
def api_event(eid):
    e=event(eid); c=conn(eid); run=latest_run(c,eid); out=e|{"latest_import":dict(run) if run else None,"tasks":all_tasks(c,eid),"flights":[dict(r) for r in c.execute("SELECT * FROM flights WHERE competition_id=? ORDER BY sort_order",(eid,)).fetchall()]}; c.close(); return jsonify(out)
@app.get("/api/event/<eid>/standings")
def api_standings(eid):
    mode,start,end=common_filters()
    rows=standings_data(eid,mode,start,end)
    c=conn(eid)
    movement=_movement_for_latest_completed_flight(c,eid,mode,end)
    c.close()
    return jsonify({"mode":mode,"from":start,"to":end,"rows":rows,"movement":movement})
@app.get("/api/event/<eid>/tasks")
def api_tasks(eid):
    c=conn(eid); out=all_tasks(c,eid); c.close(); return jsonify(out)
@app.get("/api/event/<eid>/task/<int:num>")
def api_task(eid,num):
    mode=request.args.get("mode","all"); t,rs=task_results(eid,num,mode); return jsonify({"task":t,"results":rs,"mode":mode})
@app.get("/api/event/<eid>/pilot/<int:number>")
def api_pilot(eid,number):
    event(eid); c=conn(eid); p=c.execute("SELECT * FROM pilots WHERE competition_id=? AND competition_number=?",(eid,number)).fetchone(); run=latest_run(c,eid)
    if not p: abort(404)
    rs=c.execute("""SELECT r.*,t.task_number,t.name,t.status AS task_status,f.flight_number,f.date_label AS flight_date FROM results r JOIN tasks t ON t.id=r.task_id LEFT JOIN flights f ON f.id=t.flight_id WHERE r.import_run_id=? AND r.pilot_id=? ORDER BY t.task_number,f.date_label""",(run["id"],p["id"])).fetchall(); c.close(); return jsonify({"pilot":dict(p),"results":[dict(r) for r in rs]})
@app.get("/api/event/<eid>/progression")
def api_progression(eid):
    mode,start,end=common_filters(); return jsonify({"mode":mode,"from":start,"to":end,"progression":progression_data(eid,mode,start,end)})
@app.get("/api/event/<eid>/flight/<flight_id>/standings")
def api_flight_standings(eid,flight_id):
    mode=request.args.get("mode","all"); cumulative=request.args.get("view","flight")=="cumulative"
    current=flight_standings(eid,flight_id,mode,cumulative); movement={}
    if cumulative:
        c=conn(eid)
        f=c.execute("SELECT * FROM flights WHERE competition_id=? AND id=?",(eid,flight_id)).fetchone()
        completed=_completed_competition_flights(c,eid)
        ids=[str(x["id"]) for x in completed]
        if f and str(f["id"]) in ids:
            idx=ids.index(str(f["id"]))
            if idx>0:
                run=latest_run(c,eid)
                if run:
                    current_ids=_effective_task_ids_through_flight(c,eid,f,run["id"],mode)
                    previous_flight=completed[idx-1]
                    previous_ids=_effective_task_ids_through_flight(c,eid,previous_flight,run["id"],mode)
                    current_rows=_standings_for_task_ids(c,eid,run["id"],current_ids,mode)
                    previous_rows=_standings_for_task_ids(c,eid,run["id"],previous_ids,mode)
                    old={r["competition_number"]:r["position"] for r in previous_rows}
                    movement={r["competition_number"]:(old[r["competition_number"]]-r["position"]) if r["competition_number"] in old else None for r in current_rows}
        c.close()
    return jsonify({"mode":mode,"view":"cumulative" if cumulative else "flight","rows":current,"movement":movement})
@app.get("/api/event/<eid>/compare")
def api_compare(eid):
    event(eid); nums=[]
    for raw in request.args.get("pilots","").split(","):
        try: nums.append(int(raw))
        except ValueError: pass
    nums=list(dict.fromkeys(nums))[:8]; mode=request.args.get("mode","official"); start=request.args.get("from",type=int); end=request.args.get("to",type=int); prog=progression_data(eid,mode,start,end)
    series={n:[] for n in nums}
    for point in prog:
        ranks={r["competition_number"]:r["position"] for r in point["rows"]}
        for n in nums: series[n].append({"task":point["through_task"],"position":ranks.get(n)})
    c=conn(eid); names={r["competition_number"]:r["name"] for r in c.execute("SELECT competition_number,name FROM pilots WHERE competition_id=?",(eid,)).fetchall()}; c.close()
    return jsonify({"mode":mode,"pilots":[{"competition_number":n,"name":names.get(n)} for n in nums],"series":series})
@app.get("/healthz")
def healthz(): return "ok",200
if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.getenv("PORT","8000")))
