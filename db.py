10.238.23.66 - - [11/Sep/2026:10:56:23 +0000] "GET /healthz HTTP/1.1" 200 2 "-" "Render/1.0"
10.238.23.66 - - [11/Sep/2026:10:56:28 +0000] "GET /healthz HTTP/1.1" 200 2 "-" "Render/1.0"
    c.executescript('''
    ~~~~~~~~~~~~~~~^^^^
    CREATE TABLE IF NOT EXISTS competitions(id TEXT PRIMARY KEY,title TEXT,location TEXT,dates TEXT,organiser TEXT,director TEXT,source_url TEXT);
    ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
    ...<7 lines>...
    CREATE INDEX IF NOT EXISTS idx_tasks_comp_task ON tasks(competition_id,task_number);
    ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
    ''')
    ^^^^
  File "/opt/render/project/src/db.py", line 32, in executescript
    with self.raw.cursor() as cur: cur.execute(sql)
                                   ~~~~~~~~~~~^^^^^
  File "/opt/render/project/src/.venv/lib/python3.14/site-packages/psycopg/cursor.py", line 117, in execute
    raise ex.with_traceback(None)
psycopg.errors.QueryCanceled: canceling statement due to statement timeout
127.0.0.1 - - [11/Sep/2026:11:01:26 +0000] "HEAD / HTTP/1.1" 500 0 "-" "Go-http-client/1.1"
10.238.17.41 - - [11/Sep/2026:11:01:26 +0000] "GET /healthz HTTP/1.1" 200 2 "-" "Render/1.0"
10.238.17.41 - - [11/Sep/2026:11:01:26 +0000] "GET /healthz HTTP/1.1" 200 2 "-" "Render/1.0"
[2026-09-11 11:01:31,687] ERROR in app: Exception on / [HEAD]
Traceback (most recent call last):
  File "/opt/render/project/src/.venv/lib/python3.14/site-packages/flask/app.py", line 1511, in wsgi_app
    response = self.full_dispatch_request()
  File "/opt/render/project/src/.venv/lib/python3.14/site-packages/flask/app.py", line 919, in full_dispatch_request
    rv = self.handle_user_exception(e)
  File "/opt/render/project/src/.venv/lib/python3.14/site-packages/flask/app.py", line 917, in full_dispatch_request
    rv = self.dispatch_request()
  File "/opt/render/project/src/.venv/lib/python3.14/site-packages/flask/app.py", line 902, in dispatch_request
    return self.ensure_sync(self.view_functions[rule.endpoint])(**view_args)  # type: ignore[no-any-return]
           ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~^^^^^^^^^^^^^
  File "/opt/render/project/src/app.py", line 122, in index
    c=conn(); events=[dict(r) for r in c.execute("SELECT * FROM competitions ORDER BY title").fetchall()]; c.close()
  File "/opt/render/project/src/app.py", line 21, in conn
    if is_postgres(): init_postgres(c)
                      ~~~~~~~~~~~~~^^^
  File "/opt/render/project/src/db.py", line 54, in init_postgres
    c.executescript('''
    ~~~~~~~~~~~~~~~^^^^
    CREATE TABLE IF NOT EXISTS competitions(id TEXT PRIMARY KEY,title TEXT,location TEXT,dates TEXT,organiser TEXT,director TEXT,source_url TEXT);
    ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
    ...<7 lines>...
    CREATE INDEX IF NOT EXISTS idx_tasks_comp_task ON tasks(competition_id,task_number);
    ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
    ''')
    ^^^^
  File "/opt/render/project/src/db.py", line 32, in executescript
    with self.raw.cursor() as cur: cur.execute(sql)
                                   ~~~~~~~~~~~^^^^^
  File "/opt/render/project/src/.venv/lib/python3.14/site-packages/psycopg/cursor.py", line 117, in execute
    raise ex.with_traceback(None)
psycopg.errors.QueryCanceled: canceling statement due to statement timeout
127.0.0.1 - - [11/Sep/2026:11:01:31 +0000] "HEAD / HTTP/1.1" 500 0 "-" "Go-http-client/1.1"
10.238.17.41 - - [11/Sep/2026:11:01:31 +0000] "GET /healthz HTTP/1.1" 200 2 "-" "Render/1.0"
[2026-09-11 11:01:36,724] ERROR in app: Exception on / [HEAD]
Traceback (most recent call last):
  File "/opt/render/project/src/.venv/lib/python3.14/site-packages/flask/app.py", line 1511, in wsgi_app
    response = self.full_dispatch_request()
  File "/opt/render/project/src/.venv/lib/python3.14/site-packages/flask/app.py", line 919, in full_dispatch_request
    rv = self.handle_user_exception(e)
  File "/opt/render/project/src/.venv/lib/python3.14/site-packages/flask/app.py", line 917, in full_dispatch_request
    rv = self.dispatch_request()
  File "/opt/render/project/src/.venv/lib/python3.14/site-packages/flask/app.py", line 902, in dispatch_request
    return self.ensure_sync(self.view_functions[rule.endpoint])(**view_args)  # type: ignore[no-any-return]
           ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~^^^^^^^^^^^^^
  File "/opt/render/project/src/app.py", line 122, in index
    c=conn(); events=[dict(r) for r in c.execute("SELECT * FROM competitions ORDER BY title").fetchall()]; c.close()
  File "/opt/render/project/src/app.py", line 21, in conn
    if is_postgres(): init_postgres(c)
                      ~~~~~~~~~~~~~^^^
  File "/opt/render/project/src/db.py", line 54, in init_postgres
    c.executescript('''
    ~~~~~~~~~~~~~~~^^^^
    CREATE TABLE IF NOT EXISTS competitions(id TEXT PRIMARY KEY,title TEXT,location TEXT,dates TEXT,organiser TEXT,director TEXT,source_url TEXT);
    ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
    ...<7 lines>...
    CREATE INDEX IF NOT EXISTS idx_tasks_comp_task ON tasks(competition_id,task_number);
    ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
    ''')
    ^^^^
  File "/opt/render/project/src/db.py", line 32, in executescript
    with self.raw.cursor() as cur: cur.execute(sql)
                                   ~~~~~~~~~~~^^^^^
  File "/opt/render/project/src/.venv/lib/python3.14/site-packages/psycopg/cursor.py", line 117, in execute
    raise ex.with_traceback(None)
psycopg.errors.QueryCanceled: canceling statement due to statement timeout
127.0.0.1 - - [11/Sep/2026:11:01:36 +0000] "HEAD / HTTP/1.1" 500 0 "-" "Go-http-client/1.1"
10.238.17.41 - - [11/Sep/2026:11:01:36 +0000] "GET /healthz HTTP/1.1" 200 2 "-" "Render/1.0"
==> Running 'gunicorn app:app --workers 1 --threads 4 --timeout 120'
[2026-09-11 11:01:50 +0000] [39] [INFO] Starting gunicorn 23.0.0
[2026-09-11 11:01:50 +0000] [39] [INFO] Listening at: http://0.0.0.0:10000 (39)
[2026-09-11 11:01:50 +0000] [39] [INFO] Using worker: gthread
[2026-09-11 11:01:50 +0000] [40] [INFO] Booting worker with pid: 40
10.238.17.41 - - [11/Sep/2026:11:01:51 +0000] "GET /healthz HTTP/1.1" 200 2 "-" "Render/1.0"
10.238.17.41 - - [11/Sep/2026:11:01:52 +0000] "GET /healthz HTTP/1.1" 200 2 "-" "Render/1.0"
10.238.17.41 - - [11/Sep/2026:11:01:57 +0000] "GET /healthz HTTP/1.1" 200 2 "-" "Render/1.0"
10.238.17.41 - - [11/Sep/2026:11:02:02 +0000] "GET /healthz HTTP/1.1" 200 2 "-" "Render/1.0"
==> Instance srv-daftesv40ujc73ctdqhg-jxvhs restarted
10.238.17.41 - - [11/Sep/2026:11:02:07 +0000] "GET /healthz HTTP/1.1" 200 2 "-" "Render/1.0"
10.238.17.41 - - [11/Sep/2026:11:02:11 +0000] "GET /healthz HTTP/1.1" 200 2 "-" "Render/1.0"
10.238.17.41 - - [11/Sep/2026:11:02:12 +0000] "GET /healthz HTTP/1.1" 200 2 "-" "Render/1.0"
==> No open HTTP ports detected on 0.0.0.0, continuing to scan...
