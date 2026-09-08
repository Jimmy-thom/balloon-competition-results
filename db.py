from __future__ import annotations
import os, sqlite3
from pathlib import Path

class Row(dict):
    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)

class Cursor:
    def __init__(self, cur, pg=False): self.cur=cur; self.pg=pg
    def _rows(self, rows):
        if rows is None: return None
        names=[d[0] for d in self.cur.description] if self.cur.description else []
        return [Row(zip(names,r)) for r in rows]
    def fetchone(self):
        r=self.cur.fetchone();
        if r is None: return None
        names=[d[0] for d in self.cur.description] if self.cur.description else []
        return Row(zip(names,r))
    def fetchall(self): return self._rows(self.cur.fetchall())

class Conn:
    def __init__(self, raw, pg=False): self.raw=raw; self.pg=pg
    def execute(self, sql, params=()):
        if self.pg: sql=sql.replace('?', '%s')
        cur=self.raw.cursor(); cur.execute(sql, params)
        return Cursor(cur, self.pg)
    def executescript(self, sql):
        if self.pg:
            with self.raw.cursor() as cur: cur.execute(sql)
        else: self.raw.executescript(sql)
    def commit(self): self.raw.commit()
    def rollback(self): self.raw.rollback()
    def close(self): self.raw.close()


def is_postgres(): return bool(os.getenv('DATABASE_URL'))

def connect(path=None):
    url=os.getenv('DATABASE_URL')
    if url:
        import psycopg
        # Render/Supabase URLs may use postgres://; psycopg accepts postgresql:// reliably.
        url=url.replace('postgres://','postgresql://',1)
        return Conn(psycopg.connect(url), True)
    if path is None: raise ValueError('SQLite path required when DATABASE_URL is not set')
    p=Path(path); p.parent.mkdir(parents=True, exist_ok=True)
    return Conn(sqlite3.connect(p), False)


def init_postgres(c):
    c.executescript('''
    CREATE TABLE IF NOT EXISTS competitions(id TEXT PRIMARY KEY,title TEXT,location TEXT,dates TEXT,organiser TEXT,director TEXT,source_url TEXT);
    CREATE TABLE IF NOT EXISTS import_runs(id TEXT PRIMARY KEY,competition_id TEXT,imported_at TEXT,source_url TEXT,record_count INTEGER,error_count INTEGER);
    CREATE TABLE IF NOT EXISTS flights(id TEXT PRIMARY KEY,competition_id TEXT,date_label TEXT,time_label TEXT,flight_number TEXT,sort_order INTEGER,source_url TEXT);
    CREATE TABLE IF NOT EXISTS pilots(id BIGSERIAL PRIMARY KEY,competition_id TEXT,competition_number INTEGER,name TEXT,country TEXT,UNIQUE(competition_id,competition_number));
    CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,competition_id TEXT,task_number INTEGER,name TEXT,status TEXT,flight_id TEXT,published TEXT,source_url TEXT,UNIQUE(competition_id,task_number,published,source_url));
    CREATE TABLE IF NOT EXISTS results(id BIGSERIAL PRIMARY KEY,import_run_id TEXT,task_id TEXT,pilot_id BIGINT,result TEXT,rank INTEGER,points DOUBLE PRECISION,penalty_t DOUBLE PRECISION,penalty_c DOUBLE PRECISION,score DOUBLE PRECISION,notes TEXT,status TEXT,source_url TEXT);
    CREATE INDEX IF NOT EXISTS idx_results_run ON results(import_run_id);
    CREATE INDEX IF NOT EXISTS idx_results_task ON results(task_id);
    CREATE INDEX IF NOT EXISTS idx_tasks_comp_task ON tasks(competition_id,task_number);
    ''')
    c.commit()
