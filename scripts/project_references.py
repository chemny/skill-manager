"""Bounded, resumable path-reference checks; project files are read only."""
import hashlib
import codecs
import html
import json
import os
import re
import stat
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

VERSION = 2
CHUNK = 128 * 1024
OVERLAP = 8192
READ_BUDGET = 64 * 1024 * 1024
# Quoted paths can contain spaces. Unquoted Markdown/CSS paths stop at markup.
TOKENS = re.compile(r'''"(?:[^"\\]|\\.){1,4096}"|'[^'\n]{1,4096}'|[^\s<>"'`()\[\]{},;=]{1,4096}''')
INNER = re.compile(r'''[^\s<>"'`()\[\]{},;=]{1,4096}''')


def paths_in(text, source, root, targets):
    found = set()
    for match in TOKENS.finditer(text):
        value = match.group()
        if value.startswith('"'):
            try:
                value = json.loads(value)
            except ValueError:
                value = value[1:-1]
        elif value.startswith("'"):
            value = value[1:-1]
        value = unquote(html.unescape(value)).replace('\\/', '/')
        # A JSON string may itself contain Markdown or HTML; inspect its path
        # tokens as well as the whole value (needed for paths containing spaces).
        values = {value, *INNER.findall(value)}
        for value in values:
            if value.startswith('file://'):
                uri = urlsplit(value)
                if uri.netloc not in ('', 'localhost'):
                    continue
                value = uri.path
            elif '://' in value or value.startswith('data:'):
                continue
            value = value.split('#', 1)[0].split('?', 1)[0]
            if not value or '\x00' in value:
                continue
            # Exact normalized paths only: same filenames elsewhere do not match.
            bases = ('',) if value.startswith('/') else (str(source.parent), str(root))
            for base in bases:
                path = os.path.normpath(os.path.join(base, value))
                if path in targets and path != str(source):
                    found.add(path)
    return found


def check(m, root, files, deadline, space):
    targets = {r['path'] for r in files if r['duplicate_eligible'] or r.get('cache_source')}
    signature = hashlib.sha256(json.dumps(sorted(targets)).encode()).hexdigest()
    m.db.execute('create table if not exists maintenance_reference_cache '
                 '(key text primary key, updated real, payload text)')
    references = {path: set() for path in targets}
    coverage = {'reference_files': 0, 'reference_bytes': 0, 'reference_cached_files': 0,
                'reference_total_files': 0, 'reference_pending': [], 'reference_version': VERSION}
    texts = [r for r in files if Path(r['path']).suffix.lower() in space.TEXT and r['size']]
    # Model dependency declarations and user-facing reports take priority.
    texts.sort(key=lambda r: (Path(r['path']).suffix.lower() not in {'.gltf', '.obj', '.mtl', '.html', '.md'}, r['size'], r['path']))
    coverage['reference_total_files'] = len(texts)
    for row in texts:
        path = Path(row['path'])
        key = hashlib.sha256(json.dumps([VERSION, str(path), row['fingerprint'], signature]).encode()).hexdigest()
        record = m.db.execute('select payload from maintenance_reference_cache where key=?', (key,)).fetchone()
        saved = json.loads(record[0]) if record else {'offset': 0, 'hits': [], 'complete': False}
        hits = set(saved['hits'])
        offset = saved['offset']
        reason = None
        if saved['complete']:
            coverage['reference_cached_files'] += 1
        elif row['offline']:
            reason = 'offline'
        elif deadline is not None and time.monotonic() >= deadline:
            reason = 'time_budget'
        elif coverage['reference_bytes'] >= READ_BUDGET:
            reason = 'read_budget'
        else:
            try:
                with space.directory(path.parent) as parent:
                    fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                    with os.fdopen(fd, 'rb') as stream:
                        before = os.fstat(stream.fileno())
                        if (not stat.S_ISREG(before.st_mode) or space.is_offline(before)
                                or space.fingerprint(before) != row['fingerprint']):
                            raise ValueError('changed')
                        # Re-read a bounded overlap so split UTF-8/path tokens survive resumption.
                        start = max(0, offset - OVERLAP)
                        stream.seek(start)
                        tail = b''
                        decode_error = False
                        decoder = codecs.getincrementaldecoder('utf-8')()
                        first_chunk = True
                        while start < row['size']:
                            if deadline is not None and time.monotonic() >= deadline:
                                reason = 'time_budget'
                                break
                            allowance = READ_BUDGET - coverage['reference_bytes']
                            if allowance <= 0:
                                reason = 'read_budget'
                                break
                            chunk = stream.read(min(CHUNK, allowance))
                            if not chunk:
                                raise ValueError('changed')
                            coverage['reference_bytes'] += len(chunk)
                            start += len(chunk)
                            joined = tail + chunk
                            # Replacement bytes only occur at chunk UTF-8 boundaries;
                            # overlap repeats those bytes intact on the following chunk.
                            validate = chunk
                            if first_chunk and start - len(chunk) > 0:
                                while validate and 0x80 <= validate[0] <= 0xbf:
                                    validate = validate[1:]
                            first_chunk = False
                            try:
                                decoder.decode(validate, final=start == row['size'])
                            except UnicodeError:
                                decode_error = True
                                decoder = codecs.getincrementaldecoder('utf-8')()
                            hits.update(paths_in(joined.decode('utf-8', errors='replace'), path, root, targets))
                            tail = joined[-OVERLAP:]
                            offset = start
                        if space.fingerprint(os.fstat(stream.fileno())) != row['fingerprint']:
                            raise ValueError('changed')
                        if decode_error:
                            reason = 'decode_error'
                            offset = 0
                        saved = {'offset': offset, 'hits': sorted(hits), 'complete': reason is None}
                        m.db.execute('insert or replace into maintenance_reference_cache values(?,?,?)',
                                     (key, time.time(), json.dumps(saved)))
                        m.db.commit()
            except (OSError, ValueError) as exc:
                hits = set()
                reason = 'changed' if isinstance(exc, ValueError) else 'read_error'
                m.db.execute('delete from maintenance_reference_cache where key=?', (key,))
        if reason:
            coverage['reference_pending'].append({'path': str(path), 'reason': reason, 'offset': offset, 'size': row['size']})
        else:
            coverage['reference_files'] += 1
        for target in hits:
            references[target].add(str(path))
    # Do not evict an active batch's earlier files: that would spend every
    # continuation budget re-reading them before reaching the saved cursor.
    m.db.execute('delete from maintenance_reference_cache where updated < ?',
                 (time.time() - 30 * 86400,))
    m.db.commit()
    return references, coverage
