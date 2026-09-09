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
    # Only use the active occurrence for each task number. WatchMeFly can retain
    # cancelled/re-flown occurrences (e.g. Task 8 on two Flight 3 dates).
    rows=c.execute(f"""SELECT p.competition_number,p.name,p.country,t.task_number,r.score,r.status
      FROM results r
      JOIN tasks t ON t.id=r.task_id
      JOIN pilots p ON p.id=r.pilot_id
      WHERE {where}
        AND t.id = (
          SELECT t2.id FROM tasks t2
          WHERE t2.competition_id=t.competition_id AND t2.task_number=t.task_number
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
    # Progression is cumulative from task 1 by default; an explicit range starts at `start`.
    base_start = start if start is not None else None
    return [{"through_task":n,"rows":standings_data(eid,mode,base_start,n)} for n in nums]

def common_filters():
    mode=request.args.get("mode","official"); start=request.args.get("from",type=int); end=request.args.get("to",type=int)
    if start is not None and end is not None and start>end: abort(400,description="from must be <= to")
    return mode,start,end

def flight_standings(eid, flight_id, mode="all", cumulative=False):
    statuses=mode_statuses(mode); c=conn(eid); run=latest_run(c,eid)
    if not run:
        c.close(); return []
    flight=c.execute("SELECT * FROM flights WHERE competition_id=? AND id=?",(eid,flight_id)).fetchone()
    if not flight:
        c.close(); abort(404)
    # Use the selected flight's task occurrences for standalone view. For
    # cumulative view, include all flights up to this flight and prefer the
    # active/result-bearing occurrence for each task number, matching the main
    # standings logic when WatchMeFly retains a re-flight/cancelled occurrence.
    if cumulative:
        flight_order=flight["sort_order"]
        task_rows=c.execute("""SELECT t.id,t.task_number,t.status,t.published,t.flight_id,f.sort_order
          FROM tasks t LEFT JOIN flights f ON f.id=t.flight_id
          WHERE t.competition_id=? AND (f.sort_order IS NULL OR f.sort_order<=?)
          ORDER BY t.task_number, f.sort_order, t.id""",(eid,flight_order)).fetchall()
        chosen={}
        for t in task_rows:
            existing=chosen.get(t["task_number"])
            has_results=c.execute("SELECT 1 FROM results WHERE task_id=? AND import_run_id=? LIMIT 1",(t["id"],run["id"])).fetchone() is not None
            if existing is None:
                chosen[t["task_number"]]=(dict(t),has_results)
            else:
                old,old_has=existing
                rank=lambda x:(0 if x[1] else 1, 0 if x[0]["status"]=="FINAL" else 1 if x[0]["status"]=="OFFICIAL" else 2, -(x[0]["published"] or 0) if isinstance(x[0]["published"],(int,float)) else 0, -x[0]["id"])
                if rank((dict(t),has_results)) < rank(existing): chosen[t["task_number"]]=(dict(t),has_results)
        task_ids=[v[0]["id"] for v in chosen.values()]
    else:
        task_ids=[r["id"] for r in c.execute("SELECT t.id FROM tasks t WHERE t.competition_id=? AND t.flight_id=? ORDER BY t.task_number",(eid,flight_id)).fetchall()]
    if not task_ids:
        c.close(); return []
    qs_tasks=','.join('?'*len(task_ids)); qs_status=','.join('?'*len(statuses))
    rows=c.execute(f"""SELECT p.competition_number,p.name,p.country,t.task_number,r.score,r.status
      FROM results r JOIN tasks t ON t.id=r.task_id JOIN pilots p ON p.id=r.pilot_id
      WHERE r.import_run_id=? AND r.task_id IN ({qs_tasks}) AND r.status IN ({qs_status})""",[run["id"],*task_ids,*statuses]).fetchall()
    totals={}; scores={}; status_map={}; pilots={}
    for r in rows:
        n=r["competition_number"]; pilots[n]={"name":r["name"],"country":r["country"]}; totals[n]=totals.get(n,0)+(r["score"] or 0)
        scores.setdefault(n,{})[r["task_number"]]=r["score"]; status_map.setdefault(n,{})[r["task_number"]]=r["status"]
    ordered=sorted(totals,key=lambda n:(-totals[n],pilots[n]["name"]))
    out=[{"position":i,"competition_number":n,"pilot":pilots[n]["name"],"country":pilots[n]["country"],"total":totals[n],"tasks":scores.get(n,{}),"statuses":status_map.get(n,{})} for i,n in enumerate(ordered,1)]
    c.close(); return out

def task_navigation(c,eid,num):
    rows=c.execute("""SELECT t.task_number,t.name,t.status,f.flight_number,f.date_label
      FROM tasks t LEFT JOIN flights f ON f.id=t.flight_id
      WHERE t.competition_id=? ORDER BY t.task_number,f.date_label,t.id""",(eid,)).fetchall()
    unique={}
    for r in rows: unique.setdefault(r["task_number"],dict(r))
    nums=sorted(unique)
    if num not in unique: return None,None
    i=nums.index(num)
    return (unique[nums[i-1]] if i else None),(unique[nums[i+1]] if i+1<len(nums) else None)

def task_results(eid,num,mode="all"):
    c=conn(eid); t=active_task(c,eid,num); run=latest_run(c,eid)
    if not t or not run: c.close(); abort(404)
    statuses=mode_statuses(mode); qs=','.join('?'*len(statuses))
    rs=c.execute(f"""SELECT r.*,p.competition_number,p.name,p.country FROM results r JOIN pilots p ON p.id=r.pilot_id
      WHERE r.import_run_id=? AND r.task_id=? AND r.status IN ({qs})
      ORDER BY CASE WHEN r.rank IS NULL THEN 999999 ELSE r.rank END""",(run["id"],t["id"],*statuses)).fetchall()
    c.close(); return dict(t),[dict(r) for r in rs]

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
    e=event(eid); mode=request.args.get("mode","all"); t,rs=task_results(eid,num,mode); c=conn(eid); prev_task,next_task=task_navigation(c,eid,num); c.close(); return render_template("task.html",event=e,task=t,results=rs,mode=mode,prev_task=prev_task,next_task=next_task)

@app.get("/event/<eid>/pilot/<int:number>")
def pilot_page(eid,number):
    e=event(eid); c=conn(eid); p=c.execute("SELECT * FROM pilots WHERE competition_id=? AND competition_number=?",(eid,number)).fetchone(); run=latest_run(c,eid)
    if not p: abort(404)
    rs=c.execute("""SELECT r.*,t.task_number,t.name,t.status AS task_status,f.flight_number,f.date_label AS flight_date
      FROM results r JOIN tasks t ON t.id=r.task_id LEFT JOIN flights f ON f.id=t.flight_id
      WHERE r.import_run_id=? AND r.pilot_id=? ORDER BY t.task_number,f.date_label""",(run["id"],p["id"])).fetchall(); c.close()
    return render_template("pilot.html",event=e,pilot=dict(p),results=[dict(r) for r in rs])

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
    ts=c.execute("SELECT * FROM tasks WHERE competition_id=? AND flight_id=? ORDER BY task_number",(eid,flight_id)).fetchall(); fs=c.execute("SELECT * FROM flights WHERE competition_id=? ORDER BY sort_order",(eid,)).fetchall(); c.close(); return render_template("flight.html",event=e,flight=dict(f),tasks=[dict(t) for t in ts],flights=[dict(x) for x in fs])

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

@app.get("/api/event/<eid>/flight/<flight_id>/standings")
def api_flight_standings(eid,flight_id):
    mode=request.args.get("mode","all")
    view=request.args.get("view","standalone")
    if view not in ("standalone","cumulative"):
        abort(400,description="view must be standalone or cumulative")
    rows=flight_standings(eid,flight_id,mode,cumulative=(view=="cumulative"))
    movement={}
    if view=="cumulative":
        c=conn(eid); f=c.execute("SELECT * FROM flights WHERE competition_id=? AND id=?",(eid,flight_id)).fetchone()
        prev=c.execute("SELECT * FROM flights WHERE competition_id=? AND sort_order<? ORDER BY sort_order DESC LIMIT 1",(eid,f["sort_order"])).fetchone() if f else None; c.close()
        if prev:
            before=flight_standings(eid,prev["id"],mode,True)
            old={r["competition_number"]:r["position"] for r in before}
            for r in rows:
                if r["competition_number"] in old: movement[r["competition_number"]]=old[r["competition_number"]]-r["position"]
    for r in rows: r["movement"]=movement.get(r["competition_number"])
    return jsonify({"mode":mode,"view":view,"rows":rows})

@app.get("/api/event/<eid>/progression")
def api_progression(eid):
    mode,start,end=common_filters(); return jsonify({"mode":mode,"from":start,"to":end,"progression":progression_data(eid,mode,start,end)})

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
    return jsonify({"mode":mode,"pilots":[{"competition_number":n,"name":names.get(n,str(n)),"series":series[n]} for n in nums]})

@app.get("/api/search")
def api_search():
    q=request.args.get("q","").strip(); out=[]
    if not q: return jsonify(out)
    like="%"+q+"%"
    if not DATA_DIR.exists(): return jsonify(out)
    for d in DATA_DIR.iterdir():
        if not d.is_dir() or not (d/"competition.db").exists(): continue
        c=conn(d.name)
        out += [{"type":"event","event_id":r["id"],"title":r["title"]} for r in c.execute("SELECT id,title FROM competitions WHERE id LIKE ? OR title LIKE ?",(like,like)).fetchall()]
        out += [{"type":"pilot","event_id":d.name,"competition_number":r["competition_number"],"name":r["name"],"country":r["country"]} for r in c.execute("SELECT competition_number,name,country FROM pilots WHERE name LIKE ? OR CAST(competition_number AS TEXT) LIKE ?",(like,like)).fetchall()]
        c.close()
    return jsonify(out)

@app.post("/api/admin/import")
def api_admin_import():
    token=os.environ.get("IMPORT_TOKEN")
    supplied=request.headers.get("X-Import-Token")
    if not token or supplied != token: abort(401,description="Import endpoint is not configured or token is invalid")
    payload=request.get_json(silent=True) or {}
    url=(payload.get("url") or "").strip()
    if not url: abort(400,description="JSON body must contain url")
    try:
        from importer import import_event
        eid,run_id,tasks_count,result_count,errors=import_event(url,DATA_DIR)
        return jsonify({"ok":True,"event_id":eid,"run_id":run_id,"tasks":tasks_count,"results":result_count,"errors":errors})
    except Exception as exc:
        return jsonify({"ok":False,"error":str(exc)}),500

@app.get("/api/event/<eid>/changes")
def api_changes(eid):
    event(eid); c=conn(eid)
    runs=c.execute("SELECT * FROM import_runs WHERE competition_id=? ORDER BY imported_at DESC LIMIT 2",(eid,)).fetchall()
    if len(runs)<2: c.close(); return jsonify({"available":False,"changes":[]})
    latest,previous=runs[0],runs[1]
    def snap(run_id):
        rows=c.execute("""SELECT t.task_number,p.competition_number,r.score,r.status,r.result,r.penalty_t,r.penalty_c,r.notes
          FROM results r JOIN tasks t ON t.id=r.task_id JOIN pilots p ON p.id=r.pilot_id WHERE r.import_run_id=?
          ORDER BY t.task_number,p.competition_number""",(run_id,)).fetchall()
        return {(r[0],r[1]):dict(r) for r in rows}
    a,b=snap(previous['id']),snap(latest['id']); changes=[]
    for key in sorted(set(a)|set(b)):
        if a.get(key)!=b.get(key): changes.append({"task":key[0],"competition_number":key[1],"previous":a.get(key),"latest":b.get(key)})
    c.close(); return jsonify({"available":True,"previous_run":dict(previous),"latest_run":dict(latest),"changes":changes})

if __name__ == "__main__": app.run(host="127.0.0.1",port=5000,debug=True)

@app.get('/healthz')
def healthz():
    checks=[]
    if DATA_DIR.exists():
        for d in DATA_DIR.iterdir():
            if d.is_dir() and (d/'competition.db').exists():
                try:
                    c=conn(d.name); c.execute('SELECT 1').fetchone(); c.close(); checks.append(d.name)
                except Exception:
                    return jsonify({'ok':False,'events':checks}),503
    return jsonify({'ok':True,'events':checks})
