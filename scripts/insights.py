"""Read-only suggestions from inventory metadata. Never infer inactivity or equality."""
from collections import defaultdict
from skill_locations import AGENTS


def source_key(source, include_ref=True):
    if not source:
        return None
    fields = ('provider', 'url', 'path', 'ref') if include_ref else ('provider', 'url', 'path')
    return tuple(source.get(k) or '' for k in fields)


def summarize(items):
    targets = defaultdict(list)
    for item in items:
        if item['kind'] == 'skill' and item['present'] and item['health'] != 'broken-link':
            targets[item['real_path']].append(item)
    coverage = dict.fromkeys(('check_supported', 'source_unknown', 'provider_unsupported', 'source_conflict', 'observe_only'), 0)
    representatives, same_source, shared = [], defaultdict(list), []
    for path, refs in targets.items():
        primary = min(refs, key=lambda i: (i['reference_type'] != 'directory', i['source_type'] == 'builtin', i['path']))
        sources = {source_key(i['source']) for i in refs if i['source']}
        source = next((i['source'] for i in refs if i['source']), None)
        readiness = ('observe_only' if any(i.get('ownership', {}).get('read_only', i['source_type'] == 'builtin') for i in refs)
                     else 'source_conflict' if len(sources) > 1
                     else 'source_unknown' if not source
                     else 'check_supported' if source.get('provider') in ('github', 'skillhub', 'git')
                     else 'provider_unsupported')
        coverage[readiness] += 1
        for i in refs:
            i['source_readiness'] = readiness
            i['source_candidates'] = list(i.get('local_source_candidates', []))
        summary = {'id':primary['id'], 'name':primary['name'], 'path':path,
                   'version':primary.get('version'), 'agents':sorted({a for i in refs for a in i.get('known_agents', [])}),
                   'references':[i['id'] for i in refs], 'source':source,
                   'source_readiness':readiness, 'protected':any(i['protected'] for i in refs),
                   'space':primary.get('target_space', {}), 'usage_status':primary['usage_status']}
        representatives.append(summary)
        if len(summary['agents']) > 1:
            shared.append(summary)
        if source and readiness not in ('source_conflict', 'observe_only'):
            # A collection's installer may record only its repository root for
            # every child Skill. Same repository alone does not identify a Skill.
            same_source[(*source_key(source, False), primary['name'].casefold())].append(summary)
    # Suggestions are evidence to review, never an automatic source assignment.
    by_name = defaultdict(list)
    for group in targets.values():
        for i in group:
            if i['source'] and i['source'].get('provider') == 'github' and i['source_readiness'] != 'source_conflict':
                by_name[i['name'].casefold()].append(i)
    for refs in targets.values():
        for i in refs:
            if i['source'] or i['source_type'] == 'builtin':
                continue
            candidates, seen = [], set()
            known = [r for r in refs if r['source'] and r['source'].get('provider') == 'github']
            for other in known + by_name[i['name'].casefold()]:
                key = source_key(other['source'])
                if key in seen:
                    continue
                seen.add(key)
                candidates.append({'source':other['source'], 'from_id':other['id'], 'from_path':other['path'],
                                   'evidence':'same-target' if other['real_path'] == i['real_path'] else 'name-only'})
            i['source_candidates'] = (i.get('local_source_candidates', []) + candidates)[:8]
    copies = [{'source':group[0]['source'], 'targets':group,
               'allocated_bytes_estimate':sum(i['space'].get('allocated_bytes', 0) for i in group)}
              for group in same_source.values() if len(group) > 1]
    copies.sort(key=lambda g:g['allocated_bytes_estimate'], reverse=True)
    key = lambda i:i['space'].get('allocated_bytes', 0)
    return {'source_coverage':{'total_targets':len(targets), **coverage},
            'source_targets':sorted(representatives, key=lambda i:(i['source_readiness'], i['name'])),
            'shared_targets':sorted(shared, key=key, reverse=True),
            'independent_copies':copies,
            'space_review':sorted([i for i in representatives if not i['protected'] and i['source_readiness'] != 'observe_only'], key=key, reverse=True)[:20],
            'note':'Same source is not equal content. Unknown usage is not inactivity. Shared references already store one copy. Space is estimated, not promised reclaimable space.'}


def space_category(name):
    if name in ('node_modules', 'venv', '.venv', '__pypackages__') or name.startswith('.venv-'):
        return 'dependencies'
    if name in ('__pycache__', '.pytest_cache', '.ruff_cache', '.cache', '.mypy_cache'):
        return 'cache'
    if name in ('jobs', 'outputs', 'out', 'renders', 'artifacts'):
        return 'artifacts'
    if name == '.git':
        return 'version_records'
    return 'content'
