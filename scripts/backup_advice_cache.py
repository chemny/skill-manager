"""Disposable advice cache; never used to authorize recovery or deletion."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

TTL = 3600


def context(m):
    installed=[]
    for raw in m.db.execute("select c.*,m.source,m.baseline,m.last_check from capabilities c left join maintenance_items m on m.id=c.id where c.kind='skill'"):
        r=dict(raw)
        installed.append((r,str(Path(r['path']).resolve())))
    return {'installed':installed,'stamps':{},'roots':[(platform,str(root),state) for platform,root,state in m.roots()]}


def signature(m, row, rows, ctx=None):
    from backup_policy import lookup_names
    names = lookup_names(row)
    related = [r for r in rows if r['id'] == row['id'] or names.intersection(lookup_names(r))]
    paths = {str(Path(r['path']) if r.get('external') else Path(r['path'])/'content') for r in related}
    resolved_paths=[str(Path(path).resolve()) for path in paths]
    ctx=ctx if ctx is not None else context(m)
    installed = []
    for r,real in ctx['installed']:
        p = Path(r['path'])
        if r['name'] in names or p.name in names or str(p) == row.get('original_path') or any(real.startswith(path+os.sep) or real==path for path in resolved_paths):
            installed.append(r)
            paths.add(str(p))
    # Stat only: cache hits never hash or compare file contents. Track nested edits too.
    stamps = []
    for value in sorted(paths):
        if value in ctx['stamps']:
            stamps.extend(ctx['stamps'][value]);continue
        start=len(stamps)
        p = Path(value)
        if not p.exists():
            stamps.append((value,None));continue
        for root, dirs, files in os.walk(p, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d != '.git')
            for name in [''] + dirs + sorted(files):
                target=Path(root)/name if name else Path(root)
                s=target.lstat()
                stamps.append((str(target),s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns,s.st_mode))
                if len(stamps)>100000:raise ValueError('Advice cache dependency limit')
        ctx['stamps'][value]=stamps[start:]
    data=json.dumps([related,installed,stamps,ctx['roots']],sort_keys=True,default=str)
    return hashlib.sha256(data.encode()).hexdigest()


class Cache:
    def __init__(self, m):
        self.db=None
        # Explicit read-only tests and callers must remain read-only.
        if m.db.execute('pragma query_only').fetchone()[0]:return
        try:
            self.db=sqlite3.connect(m.home/'backup-advice-cache.sqlite3',timeout=.2)
            os.chmod(m.home/'backup-advice-cache.sqlite3',0o600)
            self.db.execute('create table if not exists advice(id text primary key, signature text, saved real, payload text)')
        except (sqlite3.Error, OSError):
            self.close()

    def get(self, bid, signature):
        if self.db is None:return None
        try:
            row=self.db.execute('select saved,payload from advice where id=? and signature=?',(bid,signature)).fetchone()
            if row and 0<=time.time()-row[0]<TTL:return json.loads(row[1])
        except (sqlite3.Error,ValueError):pass
        return None

    def put(self, bid, signature, decision):
        if self.db is None:return
        try:
            self.db.execute('insert or replace into advice values(?,?,?,?)',(bid,signature,time.time(),json.dumps(decision)))
            self.db.execute('delete from advice where saved<?',(time.time()-TTL,))
            self.db.commit()
        except sqlite3.Error:self.db.rollback()

    def close(self):
        if self.db is not None:self.db.close()
        self.db=None
