"""Remove only Instasaver job artifacts; never touch other VPS services."""
import shutil
import time


def cleanup_jobs(store, root, active=(), now=None):
    now=time.time() if now is None else now
    downloads=root/'downloads'
    removed=0
    if downloads.exists():
        for folder in downloads.iterdir():
            if not folder.is_dir() or folder.is_symlink():
                continue
            jid=int(folder.name) if folder.name.isdecimal() else None
            if jid in active:
                continue
            job=store.db.execute('SELECT state,created FROM jobs WHERE id=?',(jid,)).fetchone() if jid else None
            if job and job['state'] in ('queued','running'):
                if now-job['created']<=86400:
                    continue
                store.execute("UPDATE jobs SET state='failed',error='Загрузка устарела после простоя сервера.' WHERE id=?",(jid,))
            if job or now-folder.stat().st_mtime>3600:
                shutil.rmtree(folder)
                removed+=1
    # Media references can remain in Telegram; local histories are short lived.
    store.execute("DELETE FROM jobs WHERE state IN ('done','failed','cancelled') AND created<?",(now-86400,))
    store.execute('DELETE FROM sent WHERE job NOT IN (SELECT id FROM jobs)')
    store.execute('DELETE FROM inbox WHERE done=1 AND id<?',(store.offset()-1000,))
    store.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    return removed
