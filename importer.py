from __future__ import annotations
import argparse, hashlib, json, re, sqlite3, os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from db import connect, is_postgres, init_postgres

UA = 'BalloonCompetitionWeb/0.5 (+https://example.invalid)'

SCHEMA = """
CREATE TABLE IF NOT EXISTS competitions(id TEXT PRIMARY KEY,title TEXT,location TEXT,dates TEXT,organiser TEXT,director TEXT,source_url TEXT);
CREATE TABLE IF NOT EXISTS import_runs(id TEXT PRIMARY KEY,competition_id TEXT,imported_at TEXT,source_url TEXT,record_count INTEGER,error_count INTEGER);
CREATE TABLE IF NOT EXISTS flights(id TEXT PRIMARY KEY,competition_id TEXT,flight_number TEXT,date_label TEXT,time_label TEXT,sort_order INTEGER,source_url TEXT);
CREATE TABLE IF NOT EXISTS pilots(id INTEGER PRIMARY KEY AUTOINCREMENT,competition_id TEXT,competition_number INTEGER,name TEXT,country TEXT,UNIQUE(competition_id,competition_number));
CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,competition_id TEXT,task_number INTEGER,name TEXT,status TEXT,flight_id TEXT,published TEXT,source_url TEXT,UNIQUE(competition_id,task_number,published));
CREATE TABLE IF NOT EXISTS results(id INTEGER PRIMARY KEY AUTOINCREMENT,import_run_id TEXT,task_id TEXT,pilot_id INTEGER,rank INTEGER,result TEXT,points REAL,penalty_t REAL,penalty_c REAL,score REAL,notes TEXT,status TEXT,source_url TEXT);
CREATE INDEX IF NOT EXISTS idx_results_run ON results(import_run_id);
CREATE INDEX IF NOT EXISTS idx_results_task ON results(task_id);
"""

def clean(s): return re.sub(r'\s+', ' ', s or '').strip()
def norm_status(s):
    s=clean(s).upper()
    if s.startswith('FINAL'): return 'FINAL'
    if s.startswith('OFFICIAL'): return 'OFFICIAL'
    if s.startswith('PROVISIONAL'): return 'PROVISIONAL'
    if 'CANCEL' in s: return 'CANCELLED'
    return s or 'UNKNOWN'
def num(s):
    s=clean(s).replace(',','')
    if not s or s in {'-','—','–'}: return None
    try: return float(s)
    except: return None

def fetch(session,url):
    r=session.get(url,timeout=30,headers={'User-Agent':UA})
    r.raise_for_status(); return r.text

def parse_event(html,url):
    soup=BeautifulSoup(html,'html.parser'); title=clean(soup.title.get_text()) if soup.title else ''
    # WatchMeFly exposes event fields as label/value rows. Keep this intentionally tolerant.
    fields={}
    for tr in soup.find_all('tr'):
        cells=[clean(x.get_text(' ',strip=True)) for x in tr.find_all(['th','td'])]
        if len(cells)>=2:
            k=cells[0].rstrip(':'); v=cells[1]
            if k and v and len(k)<60: fields.setdefault(k,v)
    text=clean(soup.get_text(' ',strip=True))
    m=re.search(r'Event title:\s*([^|]+?)(?:\s+Event Location:|\s+Event Dates:)',text)
    if m: title=clean(m.group(1))
    location=fields.get('Event Location','')
    dates=fields.get('Event Dates','')
    organiser=fields.get('Organiser','')
    director=fields.get('Event Director','') or fields.get('Director','')
    return {'title':title.replace('WatchMeFly |','').strip() or url,'location':location,'dates':dates,'organiser':organiser,'director':director,'source_url':url}

def task_links(soup,base):
    out={}
    for a in soup.find_all('a',href=True):
        href=urljoin(base,a['href'])
        q=parse_qs(urlparse(href).query)
        if 'tid' not in q or q.get('v',[''])[0] != 'tr': continue
        tid=q['tid'][0]
        out[tid]=href
    return list(out.values())

def parse_task(html,url,event_id):
    soup=BeautifulSoup(html,'html.parser'); text=clean(soup.get_text(' ',strip=True))
    m=re.search(r'Task\s+(\d+)\s*[—-]\s*(.*?)\s*\(Rule:\s*([^\)]+)\)\s*[—-]\s*(Final|Provisional|Official[^\s]*)',text,re.I)
    if m:
        task_no=int(m.group(1)); name=clean(m.group(2)); status=norm_status(m.group(4))
    else:
        m=re.search(r'Task\s+(\d+)\s*[—-]\s*(.*?)(?:\s*[—-]\s*(Final|Provisional|Official[^\s]*))?\s+Published:',text,re.I)
        if not m: return None
        task_no=int(m.group(1)); name=clean(m.group(2)); status=norm_status(m.group(3) or '')
    pm=re.search(r'Published:\s*([^\n]+?)(?:\s+by\s+|\s+Print\b)',text,re.I)
    published=clean(pm.group(1)) if pm else ''
    # Locate the result table by its headers.
    table=None
    for t in soup.find_all('table'):
        hs=[clean(x.get_text(' ',strip=True)).lower() for x in t.find_all('th')]
        if 'pilot' in hs and 'score' in hs:
            table=t; break
    rows=[]
    if table:
        headers=[clean(x.get_text(' ',strip=True)) for x in table.find_all('th')]
        for tr in table.find_all('tr'):
            cells=[clean(x.get_text(' ',strip=True)) for x in tr.find_all(['td','th'])]
            if len(cells)<len(headers) or cells==headers: continue
            d={headers[i].lower():cells[i] for i in range(min(len(headers),len(cells)))}
            ptxt=d.get('pilot','')
            pmatch=re.search(r'#\s*(\d+)\s*-\s*(.*?)(?:Image|$)',ptxt,re.I)
            if not pmatch: continue
            comp_no=int(pmatch.group(1)); pname=clean(pmatch.group(2)).rstrip(',')
            country=''
            # Country is commonly the text following the pilot name; strip image/markers first.
            if 'image' in ptxt.lower():
                tail=clean(ptxt.split('Image',1)[1])
                if tail: country=tail
            rows.append({'competition_number':comp_no,'pilot':pname,'country':country,
                         'rank':int(d.get('rank','').replace(',','')) if d.get('rank','').replace(',','').isdigit() else None,
                         'result':d.get('result',''),'points':num(d.get('points','')),'penalty_t':num(d.get('penalty (t)','')),
                         'penalty_c':num(d.get('penalty (c)','')),'score':num(d.get('score','')),'notes':d.get('notes','')})
    return {'task_number':task_no,'name':name,'status':status,'published':published,'source_url':url,'rows':rows}

def stable(*parts): return hashlib.sha1('|'.join(clean(str(x)) for x in parts).encode()).hexdigest()[:20]

def import_event(url,out_root):
    session=requests.Session(); session.headers.update({'User-Agent':UA})
    event_id=parse_qs(urlparse(url).query).get('e',[''])[0] or stable(url)
    event_html=fetch(session,url); meta=parse_event(event_html,url)
    soup=BeautifulSoup(event_html,'html.parser'); links=task_links(soup,url)
    # Some event pages expose task links only through Results/Task Data tabs. Follow the same event URL with common views.
    if not links:
        for suffix in ('&v=t','&v=tr'):
            try:
                h=fetch(session,url+suffix if '?' in url else url+'?v=t'); links=task_links(BeautifulSoup(h,'html.parser'),url); 
                if links: break
            except Exception: pass
    tasks=[]; errors=[]
    for link in links:
        try:
            t=parse_task(fetch(session,link),link,event_id)
            if t and t['rows']: tasks.append(t)
        except Exception as e: errors.append({'url':link,'error':str(e)})
    # Deduplicate by task number + published timestamp, preserving re-flown occurrences.
    unique={stable(event_id,t['task_number'],t['published'],t['source_url']):t for t in tasks}
    tasks=list(unique.values()); tasks.sort(key=lambda x:(x['task_number'],x['published']))
    root=Path(out_root)/event_id; root.mkdir(parents=True,exist_ok=True)
    if is_postgres():
        c=connect(); init_postgres(c)
        c.execute("INSERT INTO competitions(id,title,location,dates,organiser,director,source_url) VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=EXCLUDED.title,location=EXCLUDED.location,dates=EXCLUDED.dates,organiser=EXCLUDED.organiser,director=EXCLUDED.director,source_url=EXCLUDED.source_url",(event_id,meta['title'],meta['location'],meta['dates'],meta['organiser'],meta['director'],url))
    else:
        dbp=root/'competition.db'; c=connect(dbp); c.executescript(SCHEMA)
        c.execute('INSERT OR REPLACE INTO competitions VALUES (?,?,?,?,?,?,?)',(event_id,meta['title'],meta['location'],meta['dates'],meta['organiser'],meta['director'],url))
    run_id=stable(event_id,datetime.now(timezone.utc).isoformat(),len(tasks)); imported=datetime.now(timezone.utc).isoformat()
    c.execute('INSERT INTO import_runs VALUES (?,?,?,?,?,?)',(run_id,event_id,imported,url,sum(len(t['rows']) for t in tasks),len(errors)))
    for i,t in enumerate(tasks):
        fid='flight-'+stable(event_id,t['published'][:20] or t['task_number'])
        if is_postgres():
            c.execute("INSERT INTO flights(id,competition_id,flight_number,date_label,time_label,sort_order,source_url) VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET date_label=EXCLUDED.date_label,sort_order=EXCLUDED.sort_order,source_url=EXCLUDED.source_url",(fid,event_id,'',t['published'],'',i,t['source_url']))
            tid='task-'+stable(event_id,t['task_number'],t['published'],t['source_url'])
            c.execute("INSERT INTO tasks(id,competition_id,task_number,name,status,flight_id,published,source_url) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=EXCLUDED.name,status=EXCLUDED.status,source_url=EXCLUDED.source_url",(tid,event_id,t['task_number'],t['name'],t['status'],fid,t['published'],t['source_url']))
        else:
            c.execute('INSERT OR IGNORE INTO flights(id,competition_id,flight_number,date_label,time_label,sort_order,source_url) VALUES (?,?,?,?,?,?,?)',(fid,event_id,'',t['published'],'',i,t['source_url']))
            c.execute('UPDATE flights SET date_label=?,sort_order=? WHERE id=?',(t['published'],i,fid))
            tid='task-'+stable(event_id,t['task_number'],t['published'],t['source_url'])
            c.execute('INSERT OR REPLACE INTO tasks(id,competition_id,task_number,name,status,flight_id,published,source_url) VALUES (?,?,?,?,?,?,?,?)',(tid,event_id,t['task_number'],t['name'],t['status'],fid,t['published'],t['source_url']))
        for r in t['rows']:
            if is_postgres():
                c.execute("INSERT INTO pilots(competition_id,competition_number,name,country) VALUES (?,?,?,?) ON CONFLICT(competition_id,competition_number) DO UPDATE SET name=EXCLUDED.name,country=EXCLUDED.country",(event_id,r['competition_number'],r['pilot'],r['country']))
            else:
                c.execute('INSERT OR IGNORE INTO pilots(competition_id,competition_number,name,country) VALUES (?,?,?,?)',(event_id,r['competition_number'],r['pilot'],r['country']))
            p=c.execute('SELECT id FROM pilots WHERE competition_id=? AND competition_number=?',(event_id,r['competition_number'])).fetchone()[0]
            c.execute('INSERT INTO results(import_run_id,task_id,pilot_id,rank,result,points,penalty_t,penalty_c,score,notes,status,source_url) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',(run_id,tid,p,r['rank'],r['result'],r['points'],r['penalty_t'],r['penalty_c'],r['score'],r['notes'],t['status'],t['source_url']))
    c.commit(); c.close()
    (root/'errors.json').write_text(json.dumps(errors,indent=2),encoding='utf-8')
    return event_id,run_id,len(tasks),sum(len(t['rows']) for t in tasks),errors

if __name__=='__main__':
    ap=argparse.ArgumentParser(description='Import a WatchMeFly competition into the Balloon Competition database')
    ap.add_argument('url'); ap.add_argument('--data-dir',default='data')
    a=ap.parse_args(); print(json.dumps(dict(zip(['event_id','run_id','tasks','results','errors'],import_event(a.url,a.data_dir))),indent=2,default=str))
