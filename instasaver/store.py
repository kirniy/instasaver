import json
import sqlite3
import time


class Store:
    def __init__(self,path):
        self.db=sqlite3.connect(path)
        self.db.row_factory=sqlite3.Row
        self.db.executescript('''
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
CREATE TABLE IF NOT EXISTS inbox(id INTEGER PRIMARY KEY,body TEXT NOT NULL,done INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY AUTOINCREMENT,update_id INTEGER,chat INTEGER,user INTEGER,message INTEGER,url TEXT,mode TEXT,state TEXT DEFAULT 'queued',status_message INTEGER,created REAL,UNIQUE(update_id,url));
CREATE TABLE IF NOT EXISTS settings(user INTEGER PRIMARY KEY,mode TEXT);
CREATE TABLE IF NOT EXISTS sent(job INTEGER,item TEXT,kind TEXT,file_id TEXT,message INTEGER,PRIMARY KEY(job,item,kind));
''')
        if 'error' not in {r[1] for r in self.db.execute('PRAGMA table_info(jobs)')}:
            self.db.execute('ALTER TABLE jobs ADD COLUMN error TEXT')
        # Completed sends are checkpointed individually, so resume only remaining work.
        self.db.execute("UPDATE jobs SET state='queued' WHERE state='running'")
        self.db.commit()

    def execute(self,sql,args=()):
        cur=self.db.execute(sql,args);self.db.commit();return cur

    def offset(self):
        r=self.db.execute("SELECT value FROM meta WHERE key='offset'").fetchone()
        return int(r[0]) if r else 0

    def ingest(self,updates):
        with self.db:
            for u in updates:
                self.db.execute('INSERT OR IGNORE INTO inbox(id,body) VALUES(?,?)',(u['update_id'],json.dumps(u)))
            if updates:
                self.db.execute("INSERT OR REPLACE INTO meta VALUES('offset',?)",(str(max(u['update_id'] for u in updates)+1),))

    def pending(self):
        return self.db.execute('SELECT * FROM inbox WHERE done=0 ORDER BY id LIMIT 20').fetchall()

    def mode(self,user):
        r=self.db.execute('SELECT mode FROM settings WHERE user=?',(user,)).fetchone()
        return r[0] if r else 'video'

    def add(self,u,m,url):
        self.execute('INSERT OR IGNORE INTO jobs(update_id,chat,user,message,url,mode,created) VALUES(?,?,?,?,?,?,?)',
            (u,m['chat']['id'],m['from']['id'],m['message_id'],url,self.mode(m['from']['id']),time.time()))
        return self.db.execute('SELECT * FROM jobs WHERE update_id=? AND url=?',(u,url)).fetchone()
