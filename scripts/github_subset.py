"""Pinned, bounded Skill snapshots; unchanged blobs are copied locally, never downloaded."""
import base64
import collections
import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import signal
import subprocess
import ssl
import tempfile
import threading
import time
import urllib.error

MAX_FILES = 10000
MAX_BYTES = 256_000_000
MAX_TRANSFER = 64_000_000
_cache = collections.OrderedDict()
_cache_lock = threading.Lock()


class SourcePathError(ValueError):
    def __init__(self, path, candidates=(), kind=None):
        super().__init__('Confirmed source path does not contain SKILL.md; bind its exact subdirectory')
        self.source_issue = {'kind':kind or ('missing_subdirectory' if path else 'repository_root_not_skill'),
                             'declared_path':path, 'candidate_paths':list(candidates)[:20],
                             'candidates_verified':False}


def selected_entries(entries, path, local_name=''):
    prefix = path+'/' if path else ''
    if not any(e['path']==prefix+'SKILL.md' and e['type']=='blob' for e in entries):
        names={Path(path).name,local_name}-{''}
        candidates=[e['path'][:-9].rstrip('/') for e in entries if e['type']=='blob'
                    and e['path'].endswith('/SKILL.md') and Path(e['path']).parent.name in names]
        has_skills=any(e['type']=='blob' and (e['path']=='SKILL.md' or e['path'].endswith('/SKILL.md')) for e in entries)
        raise SourcePathError(path,candidates,kind=None if has_skills else 'repository_has_no_skills')
    return [{**e,'path':e['path'][len(prefix):]} for e in entries
            if e['path'].startswith(prefix) and e['type']!='tree']


def shared(key, loader):
    from update_checks import path_lock
    with path_lock('github-snapshot:'+key):
        with _cache_lock:
            value = _cache.get(key)
            if value and time.monotonic()-value[0] < 900:
                _cache.move_to_end(key)
                return copy.deepcopy(value[1])
        result = loader()
        size = len(result) if isinstance(result,bytes) else len(json.dumps(result))
        if size <= (1_000_000 if isinstance(result,bytes) else 4_000_000):
            with _cache_lock:
                _cache[key] = (time.monotonic(),copy.deepcopy(result),size)
                while len(_cache)>64 or sum(v[2] for v in _cache.values())>16_000_000:
                    _cache.popitem(last=False)
        return result


def pinned_entries(key, loader):
    """A missing Skill path is immutable at this commit, unlike a transport failure."""
    def load():
        try:
            return loader()
        except SourcePathError as error:
            return {'missing_skill_path':error.source_issue}
    value = shared('entries:'+key, load)
    if isinstance(value, dict) and 'missing_skill_path' in value:
        issue = value['missing_skill_path']
        raise SourcePathError(issue['declared_path'], issue['candidate_paths'],issue['kind'])
    return value


def sha(value):
    if not re.fullmatch(r'[0-9a-fA-F]{40}', str(value)): raise ValueError('Invalid pinned Git object')
    return value.lower()


def relative(value):
    p = PurePosixPath(value)
    if (p.is_absolute() or '..' in p.parts or '\\' in value or ':' in value
            or any(ord(c)<32 for c in value)
            or any(part.endswith((' ','.')) or re.fullmatch(r'(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?',part) for part in p.parts)):
        raise ValueError('Unsafe remote Skill path')
    return '' if str(p)=='.' else str(p)


def blob_hash(body):
    return hashlib.sha1(b'blob '+str(len(body)).encode()+b'\0'+body).hexdigest()


def git_env():
    env = {**os.environ,'GIT_TERMINAL_PROMPT':'0','GCM_INTERACTIVE':'never',
           'GIT_LFS_SKIP_SMUDGE':'1','GIT_CONFIG_NOSYSTEM':'1'}
    env.pop('GIT_SSL_NO_VERIFY',None)
    # No credentials in argv, on disk, or forwarded to another host.
    if env.get('GITHUB_TOKEN'):
        n = int(env.get('GIT_CONFIG_COUNT','0'))
        env.update(GIT_CONFIG_COUNT=str(n+1))
        env['GIT_CONFIG_KEY_'+str(n)] = 'http.https://github.com/.extraHeader'
        env['GIT_CONFIG_VALUE_'+str(n)] = 'Authorization: Basic '+base64.b64encode(('x-access-token:'+env['GITHUB_TOKEN']).encode()).decode()
    return env


def repository_size(repo):
    total=0;pending=[Path(repo)]
    while pending:
        directory=pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink(): raise ValueError('Unsafe Git object store link')
                        if entry.is_dir(follow_symlinks=False):pending.append(Path(entry.path))
                        else:total+=entry.stat(follow_symlinks=False).st_size
                    except FileNotFoundError:
                        # Git atomically renames packs and removes maintenance locks.
                        continue
                    if total>MAX_TRANSFER:return total
        except FileNotFoundError:continue
    return total


def git_run(repo, args, limit=MAX_TRANSFER, timeout=35, input_data=None):
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err, tempfile.TemporaryFile() as stdin:
        if input_data is not None:stdin.write(input_data);stdin.seek(0)
        command = ['git','-c','http.sslVerify=true','-c','core.hooksPath='+os.devnull,
                   '-c','protocol.allow=never','-c','protocol.https.allow=always',
                   '-c','protocol.file.allow=never','-C',str(repo),*args]
        process = subprocess.Popen(command,stdin=stdin,stdout=out,stderr=err,env=git_env(),start_new_session=os.name=='posix')
        start = time.monotonic()
        next_size_check = start
        try:
            while process.poll() is None:
                now=time.monotonic()
                if now-start>timeout: raise TimeoutError('GitHub selected-directory request timed out')
                if os.fstat(out.fileno()).st_size>limit or os.fstat(err.fileno()).st_size>1_000_000:
                    raise ValueError('Remote response exceeds size limit')
                if now>=next_size_check:
                    if repository_size(repo)>MAX_TRANSFER:raise ValueError('Remote response exceeds size limit')
                    next_size_check=now+.1
                time.sleep(.01)
            if os.fstat(out.fileno()).st_size>limit or repository_size(repo)>MAX_TRANSFER:
                raise ValueError('Remote response exceeds size limit')
            if process.returncode:
                err.seek(0); message=err.read(4000).decode(errors='replace')
                verification_errors=('ssl certificate problem','certificate verify failed',
                    'certificate_verify_failed','server certificate verification failed',
                    'unable to get local issuer certificate','certificate has expired',
                    'cert_trust_is_untrusted_root','sec_e_untrusted_root','sec_e_cert_expired')
                if any(text in message.lower() for text in verification_errors):
                    raise urllib.error.URLError(ssl.SSLCertVerificationError('CERTIFICATE_VERIFY_FAILED: '+message[:500]))
                if re.search(r'(?:requested URL returned error:|HTTP(?:/\d(?:\.\d)?)?)\s+429\b', message, re.I):
                    error = urllib.error.HTTPError('https://github.com',429,'Git HTTPS transport rate limited',{},None)
                    error.rate_limit_scope = 'github-git'
                    raise error
                if 'repository not found' in message.lower():
                    raise urllib.error.HTTPError('https://github.com',404,'Repository unavailable or private',{},None)
                if 'authentication failed' in message.lower() or 'could not read username' in message.lower():
                    raise urllib.error.HTTPError('https://github.com',403,'Repository access denied',{},None)
                if 'not a valid object name' in message.lower():
                    raise ValueError('Confirmed source path does not contain SKILL.md; bind its exact subdirectory')
                if process.returncode==2 and args[0]=='ls-remote':
                    raise ValueError('Confirmed Git ref not found; no alternative branch was selected')
                raise ConnectionError('GitHub selected-directory Git request failed: '+message[:500])
            out.seek(0); return out.read(limit+1)
        finally:
            if process.poll() is None:
                if os.name=='posix':
                    try:os.killpg(process.pid,signal.SIGKILL)
                    except ProcessLookupError:pass
                elif os.name=='nt':
                    subprocess.run(['taskkill','/F','/T','/PID',str(process.pid)],capture_output=True,timeout=5,check=False)
                else:process.kill()
                if process.poll() is None:process.kill()
            process.wait()


def git_request(repo, args, budget=35, **kwargs):
    """Retry only a transient read, with both attempts inside the original 35s budget."""
    from network_io import transient
    deadline=time.monotonic()+budget
    for attempt in range(2):
        remaining=deadline-time.monotonic()
        if remaining<=0:raise TimeoutError('GitHub selected-directory request timed out')
        try:return git_run(repo,args,timeout=min(12,budget/2,remaining) if attempt==0 else remaining,**kwargs)
        except Exception as exc:
            if attempt or not transient(exc) or deadline-time.monotonic()<=.2:raise
            time.sleep(.1)


def git_repository(manager, source, commit, folder):
    from update_checks import path_lock
    key=source['url']+'@'+sha(commit)
    with path_lock('git-repository:'+key):
        return _git_repository(manager,source,commit,folder)


def _git_repository(manager, source, commit, folder):
    key = (source['url'],commit)
    cache = getattr(manager,'_selected_git_repos',None)
    if cache is None: manager._selected_git_repos = cache = {}
    if key in cache and isinstance(cache[key],Path) and not cache[key].exists(): cache.pop(key)
    if key not in cache:
        home = Path(getattr(manager,'_repository_archive_home',folder))
        # A bounded retry must not reuse a half-initialized or interrupted object store.
        repo = Path(tempfile.mkdtemp(prefix='git-'+hashlib.sha256(repr(key).encode()).hexdigest()[:24]+'-',dir=home))
        try:
            git_run(repo,['init','--bare'])
            info=manager.a.parse_github_url(source['url'])
            git_run(repo,['remote','add','origin',f"https://github.com/{info['owner']}/{info['repo']}.git"])
            git_request(repo,['fetch','--depth=1','--filter=blob:none','--no-tags','origin',sha(commit)])
            actual = git_run(repo,['rev-parse','FETCH_HEAD']).decode().strip()
            if sha(actual)!=sha(commit): raise ValueError('Pinned Git commit did not match')
            cache[key] = repo
        except Exception as exc:
            cache[key] = exc
    if isinstance(cache[key],Exception): raise cache[key]
    return cache[key]


def git_entries(manager, source, commit, folder):
    repo = git_repository(manager,source,commit,folder)
    path = relative(source.get('path') or '')
    def load():
        records = git_run(repo,['ls-tree','-r','-z',commit],limit=4_000_000)
        entries=[]
        for record in records.split(b'\0'):
            if not record:continue
            head,name=record.split(b'\t',1)
            mode,kind,object_id=head.decode().split(' ')
            entries.append({'path':name.decode('utf-8'),'mode':mode,'type':kind,'sha':sha(object_id)})
        return entries
    try:
        entries=shared('git-index:'+source['url']+'@'+commit,load)
    except ValueError as exc:
        if 'Remote response exceeds size limit' not in str(exc):raise
        tree=commit+(':'+path if path else '')
        if not git_run(repo,['ls-tree','-z',tree,'--','SKILL.md'],limit=4000):raise SourcePathError(path)
        records=git_run(repo,['ls-tree','-r','-z',tree],limit=4_000_000)
        entries=[]
        for record in records.split(b'\0'):
            if not record:continue
            head,name=record.split(b'\t',1);mode,kind,object_id=head.decode().split(' ')
            entries.append({'path':(path+'/' if path else '')+name.decode('utf-8'),'mode':mode,'type':kind,'sha':sha(object_id)})
    return selected_entries(entries,path,source.get('_local_name',''))


def git_blobs(manager, source, commit, folder, object_ids):
    """Fetch the selected missing objects together; parse and verify every batch frame."""
    from update_checks import path_lock
    ids=list(dict.fromkeys(sha(i) for i in object_ids))
    if not ids:return {}
    repo=git_repository(manager,source,commit,folder)
    with path_lock('git-objects:'+str(repo)):
        loaded=getattr(manager,'_loaded_git_blobs',None)
        if loaded is None:manager._loaded_git_blobs=loaded={}
        known=loaded.setdefault(str(repo),set())
        missing=[i for i in ids if i not in known]
        if missing:
            git_request(repo,['fetch','--no-tags','--no-write-fetch-head','--no-auto-maintenance','origin','--stdin'],
                    input_data=('\n'.join(missing)+'\n').encode())
        data=git_run(repo,['cat-file','--batch'],input_data=('\n'.join(ids)+'\n').encode(),limit=MAX_TRANSFER+len(ids)*100)
        bodies={};offset=0;total=0
        for expected in ids:
            end=data.find(b'\n',offset)
            if end<0:raise ValueError('Remote Git blob integrity mismatch')
            parts=data[offset:end].split();offset=end+1
            if len(parts)!=3 or parts[0].decode()!=expected or parts[1]!=b'blob':raise ValueError('Remote Git blob integrity mismatch')
            try:size=int(parts[2])
            except ValueError:raise ValueError('Remote Git blob integrity mismatch') from None
            if size<0 or size>MAX_TRANSFER or total+size>MAX_TRANSFER:raise ValueError('Remote response exceeds size limit')
            body=data[offset:offset+size];offset+=size
            if data[offset:offset+1]!=b'\n' or blob_hash(body)!=expected:raise ValueError('Remote Git blob integrity mismatch')
            offset+=1;total+=size;bodies[expected]=body
        if offset!=len(data):raise ValueError('Remote Git blob integrity mismatch')
        known.update(ids)
        return bodies


def api_entries(manager, source, commit):
    info = manager.a.parse_github_url(source['url'])
    api = f"https://api.github.com/repos/{info['owner']}/{info['repo']}"
    def tree(object_id):
        value = shared(api+'/git/trees/'+object_id,lambda:manager.remote_json(api+'/git/trees/'+object_id))
        if value.get('truncated'): raise ValueError('Remote tree was truncated; comparison is incomplete')
        return value['tree']
    obj = shared(api+'/git/commits/'+commit,lambda:manager.remote_json(api+'/git/commits/'+commit))
    tree_id = sha(obj['tree']['sha'])
    selected = relative(source.get('path') or '')
    # Share one bounded metadata index for a monorepo instead of a request per Skill.
    index_url=api+'/git/trees/'+tree_id+'?recursive=1'
    try: index=shared(index_url,lambda:manager.remote_json(index_url))
    except ValueError as exc:
        if 'Remote response exceeds size limit' not in str(exc): raise
        index=None
    if index and not index.get('truncated'):
        entries=index['tree']
        return selected_entries(entries,selected,source.get('_local_name',''))
    for part in PurePosixPath(selected).parts:
        entry = next((e for e in tree(tree_id) if e['path']==part and e['type']=='tree'),None)
        if not entry: raise SourcePathError(selected)
        tree_id=sha(entry['sha'])
    if not any(e['path']=='SKILL.md' and e['type']=='blob' for e in tree(tree_id)):
        raise SourcePathError(selected)
    result=[]; visited=0
    def walk(object_id,prefix='',depth=0):
        nonlocal visited
        visited+=1
        if visited>MAX_FILES: raise ValueError('Archive exceeds expanded size or file count limit')
        if depth>30: raise ValueError('Remote Skill directory nesting exceeds limit')
        for entry in tree(object_id):
            name=relative(entry['path'])
            if not name or '/' in name: raise ValueError('Unsafe remote tree entry')
            path=prefix+name
            if entry['type']=='tree': walk(sha(entry['sha']),path+'/',depth+1)
            else: result.append({**entry,'path':path})
            if len(result)>MAX_FILES: raise ValueError('Archive exceeds expanded size or file count limit')
    walk(tree_id)
    return result


def validate_entries(entries):
    if len(entries)>MAX_FILES or sum(e.get('size',0) for e in entries)>MAX_BYTES:
        raise ValueError('Archive exceeds expanded size or file count limit')
    if not any(e['path']=='SKILL.md' for e in entries):
        raise ValueError('Confirmed source path does not contain SKILL.md; bind its exact subdirectory')
    seen=set()
    for entry in entries:
        path=relative(entry['path']);sha(entry['sha'])
        if not path or path.casefold() in seen:raise ValueError('Unsafe or duplicate remote Skill path')
        seen.add(path.casefold())
        if entry['type']!='blob' or entry['mode'] not in ('100644','100755'):
            raise ValueError('Remote Skill contains symlinks or submodules; manual review required')


def local_blob(local, entry):
    current=local/relative(entry['path'])
    if current.is_file() and not current.is_symlink() and not any(p.is_symlink() for p in current.parents if p!=local.parent):
        if current.stat().st_size<=MAX_TRANSFER:
            body=current.read_bytes()
            if blob_hash(body)==sha(entry['sha']):return body
    return None


def prefetch(manager, rows):
    """Optional chunk-local downloads; failures fall back to normal per-item checks.

    Isolated stores prevent an oversized/failed bundle from poisoning the normal
    repository cache. Only validated files from exact pinned Skill paths are fetched.
    """
    from github_transport import resolve
    from types import SimpleNamespace
    manager._prefetched_git_blobs={}
    stats={'bundles':0,'objects':0,'fallbacks':0,'bytes':0}
    if len(rows)<2 or not getattr(manager,'_github_api_state',{}).get('limited'):
        return stats
    groups={}
    for row in rows:
        source=manager.source(row)
        if not source or source.get('provider')!='github' or not manager.r.channel_enabled('github'):continue
        try:
            commit,_=resolve(manager,source)
            with tempfile.TemporaryDirectory(prefix='asm-prefetch-index-') as folder:
                entries=git_entries(manager,source,commit,folder)
            validate_entries(entries)
            ids=[e['sha'] for e in entries if local_blob(Path(row['path']).resolve(),e) is None]
            if ids:
                group=groups.setdefault((source['url'],commit),{'source':source,'ids':set()})
                group['ids'].update(ids)
        except (OSError,ValueError):
            # The ordinary comparison records its precise failure, never a guessed success.
            continue
    for key,group in groups.items():
        try:
            with tempfile.TemporaryDirectory(prefix='asm-prefetch-blobs-') as folder:
                repo=Path(folder)
                git_run(repo,['init','--bare'])
                info=manager.a.parse_github_url(group['source']['url'])
                git_run(repo,['remote','add','origin',f"https://github.com/{info['owner']}/{info['repo']}.git"])
                isolated=SimpleNamespace(a=manager.a,_selected_git_repos={key:repo})
                bodies=git_blobs(isolated,group['source'],key[1],repo,sorted(group['ids']))
            size=sum(map(len,bodies.values()))
            if stats['bytes']+size>16_000_000:
                stats['fallbacks']+=1
                continue
            manager._prefetched_git_blobs[key]=bodies
            stats['bundles']+=1;stats['objects']+=len(bodies);stats['bytes']+=size
        except (OSError,ValueError):
            stats['fallbacks']+=1
    return stats


def snapshot(manager, source, commit, folder, local, use_git=False):
    commit=sha(commit);folder=Path(folder);local=Path(local)
    info=manager.a.parse_github_url(source['url'])
    api=f"https://api.github.com/repos/{info['owner']}/{info['repo']}"
    from github_transport import can_fallback
    source={**source,'_local_name':local.name}
    key=source['url']+'@'+commit+':'+relative(source.get('path') or '')+':'+local.name
    transport='git-selected' if use_git else 'github-tree'
    def fallback(exc):
        from diagnostics import classify
        if classify(exc)[0]=='remote_rate_limit':
            from update_checks import rate_limit_deadline
            state=getattr(manager,'_github_api_state',None)
            if state is not None: state.update(limited=True,retry_at=rate_limit_deadline(exc))
    try:
        entries=pinned_entries(key,lambda:git_entries(manager,source,commit,folder) if use_git else api_entries(manager,source,commit))
    except Exception as exc:
        if use_git or not can_fallback(exc): raise
        fallback(exc)
        transport='git-selected';use_git=True
        entries=pinned_entries(key,lambda:git_entries(manager,source,commit,folder))
    validate_entries(entries)
    target=folder/'selected-skill';target.mkdir()
    downloaded=reused=total=0
    pending=[]
    for entry in entries:
        path=relative(entry['path']);object_id=sha(entry['sha'])
        body=local_blob(local,entry)
        if body is not None:reused+=1
        if body is None:
            pending.append(entry)
            continue
        total+=len(body)
        if total>MAX_BYTES:raise ValueError('Remote response exceeds size limit')
        destination=target/path;destination.parent.mkdir(parents=True,exist_ok=True)
        destination.write_bytes(body);destination.chmod(0o755 if entry['mode']=='100755' else 0o644)
    if any(e.get('size',0)>2_000_000 for e in pending):use_git=True;transport='git-selected'
    prefetched=getattr(manager,'_prefetched_git_blobs',{}).get((source['url'],commit),{})
    batched={e['sha']:prefetched[e['sha']] for e in pending if e['sha'] in prefetched} if use_git else {}
    if use_git:
        batched.update(git_blobs(manager,source,commit,folder,[e['sha'] for e in pending if e['sha'] not in batched]))
    for entry in pending:
        path=relative(entry['path']);object_id=sha(entry['sha'])
        def load_blob():
            if use_git:
                data=batched[object_id]
            else:
                value=manager.remote_json(api+'/git/blobs/'+object_id)
                if value.get('encoding')!='base64': raise ValueError('Unsupported remote blob encoding')
                data=base64.b64decode(''.join(value['content'].split()),validate=True)
            if blob_hash(data)!=object_id: raise ValueError('Remote Git blob integrity mismatch')
            return data
        try: body=shared('blob:'+source['url']+':'+object_id,load_blob)
        except Exception as exc:
            if use_git or not can_fallback(exc): raise
            fallback(exc)
            use_git=True;transport='git-selected'
            batched=git_blobs(manager,source,commit,folder,[e['sha'] for e in pending if e['sha'] not in batched])
            body=shared('blob:'+source['url']+':'+object_id,load_blob)
        downloaded+=len(body)
        if blob_hash(body)!=object_id: raise ValueError('Remote Git blob integrity mismatch')
        total+=len(body)
        if total>MAX_BYTES or downloaded>MAX_TRANSFER: raise ValueError('Remote response exceeds size limit')
        destination=target/path;destination.parent.mkdir(parents=True,exist_ok=True)
        destination.write_bytes(body);destination.chmod(0o755 if entry['mode']=='100755' else 0o644)
    return target,{'comparison_transport':transport,'file_count':len(entries),
                   'reused_local_files':reused,'selected_file_bytes':downloaded,'snapshot_bytes':total}
