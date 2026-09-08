import sqlite3
import tempfile
import unittest
from pathlib import Path

from bs4 import BeautifulSoup

import importer

TASK_HTML = '''
<html><head><title>WatchMeFly</title></head><body>
<h1>Task 8 — Minimum Distance (Rule: JDG) — Provisional</h1>
<div>Published: 05/09/2026 08:15 by Director</div>
<table><tr><th>Rank</th><th>Pilot</th><th>Result</th><th>Points</th><th>Penalty (T)</th><th>Penalty (C)</th><th>Score</th><th>Notes</th></tr>
<tr><td>1</td><td># 29 - BAREFORD, Dominic ImageUnited Kingdom</td><td>12.4 m</td><td>1000</td><td>0</td><td>0</td><td>1000</td><td></td></tr>
<tr><td>2</td><td># 7 - DELEERSNYDER, Maarten ImageBelgium</td><td>10.2 m</td><td>900</td><td>0</td><td>10</td><td>890</td><td>Example</td></tr>
</table></body></html>
'''

class ImporterTests(unittest.TestCase):
    def test_status_and_rows_are_parsed(self):
        got = importer.parse_task(TASK_HTML, 'https://watchmefly.net/events/event.php?v=tr&tid=x', 'croatia2026')
        self.assertEqual(got['task_number'], 8)
        self.assertEqual(got['status'], 'PROVISIONAL')
        self.assertEqual(got['name'], 'Minimum Distance')
        self.assertEqual(len(got['rows']), 2)
        self.assertEqual(got['rows'][0]['competition_number'], 29)
        self.assertEqual(got['rows'][0]['score'], 1000)
        self.assertEqual(got['rows'][1]['penalty_c'], 10)

    def test_status_normalization(self):
        self.assertEqual(importer.norm_status('Final Official'), 'FINAL')
        self.assertEqual(importer.norm_status('Provisional'), 'PROVISIONAL')
        self.assertEqual(importer.norm_status('Cancelled'), 'CANCELLED')

    def test_snapshot_runs_can_coexist(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / 'competition.db'
            c = sqlite3.connect(db)
            c.executescript(importer.SCHEMA)
            c.execute('INSERT INTO competitions VALUES (?,?,?,?,?,?,?)', ('x','X','','','','',''))
            c.execute('INSERT INTO import_runs VALUES (?,?,?,?,?,?)', ('r1','x','2026-09-06T10:00:00Z','u',1,0))
            c.execute('INSERT INTO import_runs VALUES (?,?,?,?,?,?)', ('r2','x','2026-09-06T11:00:00Z','u',1,0))
            self.assertEqual(c.execute('select count(*) from import_runs').fetchone()[0], 2)
            c.close()

if __name__ == '__main__':
    unittest.main()
