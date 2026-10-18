"""Resolve the same public GitHub ref without the REST quota; never guess another source."""
import re
import tempfile

from network_io import transient


def can_fallback(error):
    from diagnostics import classify
    return classify(error)[0] == 'remote_rate_limit' or transient(error)


def lookup(url, patterns):
    from github_subset import git_request
    with tempfile.TemporaryDirectory(prefix='asm-ref-') as folder:
        return git_request(folder,['ls-remote','--symref','--exit-code',url,*patterns],budget=30,limit=1_000_000).decode('utf-8')


def resolve(manager, source):
    from update_checks import path_lock
    with path_lock('github-ref:'+source['url']+':'+(source.get('ref') or '')):
        return _resolve(manager,source)


def _resolve(manager, source):
    info = manager.a.parse_github_url(source['url'])
    if not info: raise ValueError('Invalid GitHub source')
    ref = source.get('ref') or ''
    if re.fullmatch(r'[0-9a-fA-F]{40}', ref): return ref.lower(), ref
    if ref.startswith('-') or any(c in ref for c in ('*', '?', '[', '\\')) or any(c.isspace() or ord(c)<32 for c in ref):
        raise ValueError('Cannot resolve this exact Git ref safely')
    cache = getattr(manager, '_github_refs', None)
    if cache is None: manager._github_refs = cache = {}
    key = (source['url'], ref)
    errors = getattr(manager, '_github_ref_errors', None)
    if errors is None: manager._github_ref_errors = errors = {}
    if key in errors: raise errors[key]
    if key not in cache:
        patterns = [ref] if ref.startswith('refs/') else ['refs/heads/'+ref, 'refs/tags/'+ref, 'refs/tags/'+ref+'^{}'] if ref else ['HEAD']
        if ref.startswith('refs/tags/'): patterns.append(ref+'^{}')
        url = f"https://github.com/{info['owner']}/{info['repo']}.git"
        try:
            output=lookup(url,patterns)
        except Exception as error:
            errors[key]=error
            raise errors[key]
        refs = dict(line.split('\t',1)[::-1] for line in output.splitlines() if '\t' in line)
        # A short name shared by a branch and tag is ambiguous; the API decides normally.
        if ref and not ref.startswith('refs/') and 'refs/heads/'+ref in refs and 'refs/tags/'+ref in refs:
            raise ValueError('Git branch and tag are ambiguous; bind an explicit ref')
        matched = next((p for p in reversed(patterns) if p in refs and not p.endswith('^{}')), None)
        commit = refs.get((matched or '')+'^{}', refs.get(matched))
        if not commit or not re.fullmatch(r'[0-9a-fA-F]{40}', commit):
            raise ValueError('Confirmed Git ref not found; no alternative branch was selected')
        cache[key] = (commit.lower(), ref or 'HEAD')
    return cache[key]
