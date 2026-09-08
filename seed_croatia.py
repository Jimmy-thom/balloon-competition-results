import json, sqlite3, hashlib
from pathlib import Path
from importer import SCHEMA

ROOT=Path(__file__).resolve().parent
D=ROOT/'data'/'croatia2026'
DB=D/'competition.db'
for f in [DB]:
    if f.exists(): f.unlink()
c=sqlite3.connect(DB); c.executescript(SCHEMA)
e=json.load(open(D/'event.json')); pilots=json.load(open(D/'pilots.json')); flights=json.load(open(D/'flights.json')); tasks=json.load(open(D/'tasks.json')); results=json.load(open(D/'results.json'))
eid='croatia2026'; run='seed-2026-09-06'
c.execute('INSERT INTO competitions VALUES (?,?,?,?,?,?,?)',(eid,e['title'],e.get('location') or 'Prelog, Croatia',f"{e['start_date']} - {e['end_date']}",'Balloon Club Zagreb','Goran Grgić',e['source_url']))
c.execute('INSERT INTO import_runs VALUES (?,?,?,?,?,?)',(run,eid,e['imported_at'],e['source_url'],len(results),0))
for i,f in enumerate(flights):
    c.execute('INSERT INTO flights VALUES (?,?,?,?,?,?,?)',(f['id'],eid,f.get('flight',''),f.get('date',''),' ',i,''))
for p in pilots:
    c.execute('INSERT INTO pilots(competition_id,competition_number,name,country) VALUES (?,?,?,?)',(eid,p['competition_number'],p['name'],p.get('country','')))
for t in tasks:
    fid=next((f['id'] for f in flights if f.get('flight')==t.get('flight') and f.get('date')==t.get('date')),None)
    tid='task-'+hashlib.sha1('|'.join(map(str,[eid,t['number'],t['date'],t['url']])).encode()).hexdigest()[:20]
    c.execute('INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?)',(tid,eid,t['number'],t['name'],t['status'],fid,t['date'],t['url']))
for r in results:
    raw=r['raw']; pilot=raw['Pilot']; import re
    m=re.search(r'#\s*(\d+)\s*-\s*(.*?)(?:\s+[A-Z][A-Za-z .-]+)?$',pilot)
    no=int(m.group(1)) if m else int(pilot.split('#')[1].split()[0])
    pid=c.execute('SELECT id FROM pilots WHERE competition_id=? AND competition_number=?',(eid,no)).fetchone()[0]
    task=c.execute('SELECT id FROM tasks WHERE competition_id=? AND task_number=? AND source_url=?',(eid,r['task_number'],r['source_url'])).fetchone()[0]
    def n(v):
        try:return float(str(v).replace(',',''))
        except:return None
    def i(v):
        try:return int(v)
        except:return None
    c.execute('INSERT INTO results(import_run_id,task_id,pilot_id,rank,result,points,penalty_t,penalty_c,score,notes,status,source_url) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',(run,task,pid,i(raw.get('Rank')),raw.get('Result',''),n(raw.get('Points')),n(raw.get('Penalty (T)')),n(raw.get('Penalty (C)')),n(raw.get('Score')),raw.get('Notes',''),r['status'],r['source_url']))
c.commit(); c.close()
print('seeded',DB)
