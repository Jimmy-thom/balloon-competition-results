from __future__ import annotations
from pathlib import Path
from flask import Flask, abort, jsonify, render_template, request, redirect, url_for, render_template_string
import os
import hmac
from urllib.parse import urlparse, parse_qs
from db import connect, is_postgres, init_postgres
from watcher import add_event

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
    statuses=mode_statuses(mode); c=conn(eid); run=latest_run(c,eid)
    if not run: c.close(); return []
    qs=','.join('?'*len(statuses)); params=[run["id"],eid,*statuses]
    where=f"r.import_run_id=? AND p.competition_id=? AND r.status IN ({qs})"
    if start is not None: where += " AND t.task_number>=?"; params.append(start)
    if end is not None: where += " AND t.task_number<=?"; params.append(end)
    rows=c.execute(f"""SELECT p.competition_number,p.name,p.country,t.task_number,r.score,r.status
      FROM results r JOIN tasks t ON t.id=r.task_id JOIN pilots p ON p.id=r.pilot_id
      WHERE {where}
        AND t.id = (
          SELECT t2.id FROM tasks t2 WHERE t2.competition_id=t.competition_id AND t2.task_number=t.task_number
          ORDER BY CASE WHEN EXISTS (SELECT 1 FROM results r2 WHERE r2.task_id=t2.id AND r2.import_run_id=r.import_run_id) THEN 0 ELSE 1 END,
                   CASE t2.status WHEN 'FINAL' THEN 0 WHEN 'OFFICIAL' THEN 1 WHEN 'PROVISIONAL' THEN 2 ELSE 3 END,
                   t2.published DESC, t2.id DESC LIMIT 1
        )""",params).fetchall()
    totals={}; scores={}; statuses_by={}
    for r in rows:
        n=r["competition_number"]; totals[n]=totals.get(n,0)+(r["score"] or 0)
        scores.setdefault(n,{})[r["task_number"]]=r["score"]
        statuses_by.setdefault(n,{})[r["task_number"]]=r["status"]
    pilots={r["competition_number"]:dict(r) for r in c.execute("SELECT competition_number,name,country FROM pilots WHERE competition_id=?",(eid,)).fetchall()}
    ordered=sorted(totals,key=lambda n:(-totals[n],pilots[n]["name"]))
    out=[{"position":i,"competition_number":n,"pilot":pilots[n]["name"],"country":pilots[n]["country"],"total":totals[n],"tasks":scores.get(n,{}),"statuses":statuses_by.get(n,{})} for i,n in enumerate(ordered,1)]
    c.close(); return out
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
@app.get("/event/<eid>/progression")
def progression_page(eid):
    e=event(eid)
    c=conn(eid)
    task_max=max([r["task_number"] for r in c.execute(
        "SELECT task_number FROM tasks WHERE competition_id=?",(eid,)
    ).fetchall()] or [1])
    c.close()
    return render_template(
        "progression.html",
        event=e,
        tasks_max=task_max
    )
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
        like="%"+q+"%"
        if is_postgres():
            sql="""SELECT competition_number,name,country
                     FROM pilots
                     WHERE competition_id=?
                       AND (name ILIKE ? OR CAST(competition_number AS TEXT) ILIKE ?)
                     ORDER BY CASE WHEN CAST(competition_number AS TEXT)=? THEN 0 ELSE 1 END, name
                     LIMIT 100"""
        else:
            sql="""SELECT competition_number,name,country
                     FROM pilots
                     WHERE competition_id=?
                       AND (name LIKE ? OR CAST(competition_number AS TEXT) LIKE ?)
                     ORDER BY CASE WHEN CAST(competition_number AS TEXT)=? THEN 0 ELSE 1 END, name
                     LIMIT 100"""
        pilots=[dict(r) for r in c.execute(sql,(eid,like,like,q)).fetchall()]

    # Match the clean country/name presentation used on the pilot page.
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
    for pilot in pilots:
        raw=(pilot.get("country") or "").strip()
        parts=raw.split()
        pilot["country"]=country_map.get(parts[0].upper(),raw) if parts else ""
        name=(pilot.get("name") or "").strip()
        for country_name in sorted(set(country_map.values()), key=len, reverse=True):
            if name.lower().endswith(" " + country_name.lower()):
                name=name[:-(len(country_name)+1)].rstrip()
                break
        pilot["name"]=name

    # Add the current official standing to each result when available.
    standings={}
    if pilots:
        for row in standings_data(eid,"official"):
            standings[row["competition_number"]]=row
    for pilot in pilots:
        row=standings.get(pilot["competition_number"],{})
        pilot["position"]=row.get("position")
        pilot["total"]=row.get("total")

    c.close(); return render_template("search.html",event=e,q=q,pilots=pilots)
@app.get("/event/<eid>/history")
def history_page(eid):
    e=event(eid); c=conn(eid)
    raw_runs=c.execute("SELECT * FROM import_runs WHERE competition_id=? ORDER BY imported_at DESC",(eid,)).fetchall()
    runs=[]
    for i,row in enumerate(raw_runs):
        run=dict(row)
        rid=run.get("id")
        stats=c.execute("""SELECT COUNT(*) AS results,
                                COUNT(DISTINCT pilot_id) AS pilots,
                                COUNT(DISTINCT task_id) AS tasks
                         FROM results WHERE import_run_id=?""",(rid,)).fetchone()
        run["results_count"]=stats["results"] if stats else 0
        run["pilots_count"]=stats["pilots"] if stats else 0
        run["tasks_count"]=stats["tasks"] if stats else 0
        run["is_latest"]=(i==0)
        runs.append(run)
    c.close()
    return render_template("history.html",event=e,runs=runs)
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
    mode,start,end=common_filters(); return jsonify({"mode":mode,"from":start,"to":end,"rows":standings_data(eid,mode,start,end)})
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
        c=conn(eid); f=c.execute("SELECT sort_order FROM flights WHERE competition_id=? AND id=?",(eid,flight_id)).fetchone()
        prev=c.execute("SELECT id FROM flights WHERE competition_id=? AND sort_order<? ORDER BY sort_order DESC LIMIT 1",(eid,f["sort_order"])).fetchone() if f else None; c.close()
        if prev:
            prior=flight_standings(eid,prev["id"],mode,True); old={r["competition_number"]:r["position"] for r in prior}
            movement={r["competition_number"]:(old[r["competition_number"]]-r["position"]) if r["competition_number"] in old else None for r in current}
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
        lookup={r["competition_number"]:r for r in point["rows"]}
        for n in nums:
            r=lookup.get(n)
            series[n].append({
                "task":point["through_task"],
                "position":r["position"] if r else None,
                "total":r["total"] if r else None,
                "score":(r.get("tasks") or {}).get(point["through_task"]) if r else None,
                "status":(r.get("statuses") or {}).get(point["through_task"]) if r else None
            })
    c=conn(eid); names={r["competition_number"]:{"name":r["name"],"country":r["country"]} for r in c.execute("SELECT competition_number,name,country FROM pilots WHERE competition_id=?",(eid,)).fetchall()}; c.close()
    return jsonify({"mode":mode,"pilots":[{"competition_number":n,"name":names.get(n,{}).get("name",str(n)),"country":names.get(n,{}).get("country",""),"series":series[n]} for n in nums]})


ADMIN_TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Admin — Balloon Competition Results</title>
  <style>
    body{font-family:Arial,sans-serif;max-width:760px;margin:40px auto;padding:0 20px;color:#222}
    .card{border:1px solid #ddd;border-radius:12px;padding:24px;background:#fff;box-shadow:0 2px 10px rgba(0,0,0,.05)}
    h1{margin-top:0}
    label{display:block;font-weight:600;margin:18px 0 7px}
    input{box-sizing:border-box;width:100%;padding:12px;border:1px solid #bbb;border-radius:8px;font-size:16px}
    button{margin-top:20px;padding:12px 18px;border:0;border-radius:8px;font-size:16px;cursor:pointer}
    .message{padding:12px 14px;border-radius:8px;margin-bottom:18px}
    .error{background:#fde8e8;color:#8a1c1c}
    .success{background:#e8f7e8;color:#1d6b2b}
    .muted{color:#666}
    a{color:#175ea8}
  </style>
</head>
<body>
  <div class="card">
    <h1>Add Competition</h1>
    <p class="muted">Import a WatchMeFly competition and automatically enable monitoring.</p>

    {% if error %}
      <div class="message error">{{ error }}</div>
    {% endif %}

    {% if success %}
      <div class="message success">
        Competition <strong>{{ event_id }}</strong> was imported successfully and monitoring is enabled.
        <br><br>
        <a href="{{ event_url }}">Open competition</a>
      </div>
    {% endif %}

    <form method="post" action="{{ url_for('admin_import') }}">
      <label for="token">Import token</label>
      <input id="token" name="token" type="password" autocomplete="off" required>

      <label for="url">WatchMeFly competition URL</label>
      <input id="url" name="url" type="url"
             placeholder="https://watchmefly.net/events/event.php?e=croatia2026"
             required>

      <button type="submit">Import Competition</button>
    </form>
  </div>
</body>
</html>
"""


@app.get("/admin")
def admin_page():
    if not os.getenv("IMPORT_TOKEN"):
        abort(503, description="IMPORT_TOKEN is not configured.")
    return render_template_string(
        ADMIN_TEMPLATE,
        error=None,
        success=False,
        event_id=None,
        event_url=None,
    )


@app.post("/admin/import")
def admin_import():
    configured_token = os.getenv("IMPORT_TOKEN", "")
    if not configured_token:
        abort(503, description="IMPORT_TOKEN is not configured.")

    supplied_token = request.form.get("token", "")
    if not hmac.compare_digest(supplied_token, configured_token):
        return render_template_string(
            ADMIN_TEMPLATE,
            error="Invalid import token.",
            success=False,
            event_id=None,
            event_url=None,
        ), 403

    source_url = request.form.get("url", "").strip()

    parsed = urlparse(source_url)
    event_id = parse_qs(parsed.query).get("e", [""])[0].strip()

    if (
        parsed.scheme not in ("http", "https")
        or parsed.netloc.lower() not in ("watchmefly.net", "www.watchmefly.net")
        or not parsed.path.startswith("/events/")
        or not event_id
    ):
        return render_template_string(
            ADMIN_TEMPLATE,
            error="Please enter a valid WatchMeFly competition URL.",
            success=False,
            event_id=None,
            event_url=None,
        ), 400

    try:
        # Reuse the existing importer + monitoring workflow.
        # This keeps the admin page from having a second import implementation.
        add_event(source_url)
    except Exception as exc:
        return render_template_string(
            ADMIN_TEMPLATE,
            error=f"Import failed: {exc}",
            success=False,
            event_id=None,
            event_url=None,
        ), 500

    return render_template_string(
        ADMIN_TEMPLATE,
        error=None,
        success=True,
        event_id=event_id,
        event_url=url_for("event_page", eid=event_id),
    )

@app.get("/healthz")
def healthz(): return "ok",200
if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.getenv("PORT","8000")))
