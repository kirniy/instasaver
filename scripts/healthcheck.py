"""Read-only local process health; no Telegram calls or secret output."""
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
root=Path(os.getenv('DATA_DIR','/opt/instasaver/data'))
heartbeat=root/'healthy'
age=time.time()-heartbeat.stat().st_mtime if heartbeat.exists() else None
c=sqlite3.connect(f'file:{root}/state.sqlite3?mode=ro',uri=True)
counts=dict(c.execute('SELECT state,count(*) FROM jobs GROUP BY state'))
print(json.dumps({'poll_age_seconds':round(age,1) if age is not None else None,'jobs':counts}))
sys.exit(0 if age is not None and age<150 else 1)
