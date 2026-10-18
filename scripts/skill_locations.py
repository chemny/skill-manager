"""Discovery locations, kept separate from Agent identities.

No folders are created and no Agent configuration is changed here. The legacy
``shared`` registry namespace remains a storage-only namespace to preserve IDs.
"""
import ast
import json
import os
import re
import shutil
import sys
import ntpath
import posixpath
from pathlib import Path

AGENTS = ('codex', 'claude_code', 'hermes', 'openclaw')
BUILTIN_AGENTS = {
    'codex': ('Codex', '~/.codex/skills', 'codex'),
    'claude_code': ('Claude Code', '~/.claude/skills', 'claude'),
    'hermes': ('Hermes', '~/.hermes/skills', 'hermes'),
    'openclaw': ('OpenClaw', '~/.openclaw/skills', 'openclaw'),
    'cursor': ('Cursor', '~/.cursor/skills', 'cursor'),
    'gemini': ('Gemini CLI', '~/.gemini/skills', 'gemini'),
    'copilot': ('GitHub Copilot', '~/.copilot/skills', 'copilot'),
    'workbuddy': ('WorkBuddy', '~/.codebuddy/skills', 'workbuddy'),
    'opencode': ('OpenCode', '~/.config/opencode/skills', 'opencode'),
    'pi': ('Pi', '~/.pi/agent/skills', 'pi'),
    'trae_cn': ('TRAE 中国版', '~/.trae-cn/skills', 'trae'),
    'qwen': ('Qwen Code', '~/.qwen/skills', 'qwen'),
    'qoder': ('Qoder CLI', '~/.qoder/skills', 'qodercli'),
    'qoderwork': ('QoderWork', '~/.qoderwork/skills', 'qoderwork'),
    'kimi_code': ('Kimi Code', '~/.kimi-code/skills', 'kimi'),
    'amp': ('Amp', '~/.config/agents/skills', 'amp'),
    'cline': ('Cline', '~/.cline/skills', 'cline'),
    'windsurf': ('Windsurf', '~/.codeium/windsurf/skills', 'windsurf'),
    'codebuddy': ('CodeBuddy Code', '~/.codebuddy/skills', 'codebuddy'),
}


def default_agent_roots(agent, system=None, environ=None, home=None):
    """Resolve on the service host, never from a browser's user agent."""
    if agent not in BUILTIN_AGENTS:
        raise ValueError('请选择预设 Agent')
    system = system or sys.platform
    env = os.environ if environ is None else environ
    windows = system == 'win32'
    paths = ntpath if windows else posixpath
    home = str(home or (env.get('USERPROFILE') if windows else env.get('HOME')) or Path.home())
    relative = BUILTIN_AGENTS[agent][1].removeprefix('~/').split('/')
    override = {'codex':'CODEX_HOME', 'claude_code':'CLAUDE_CONFIG_DIR',
                'hermes':'HERMES_HOME', 'openclaw':'OPENCLAW_STATE_DIR',
                'pi':'PI_CODING_AGENT_DIR', 'kimi_code':'KIMI_CODE_HOME'}.get(agent)
    base = env.get(override) if override else None
    if base:
        if base.startswith('~'): base = home + base[1:]
        base = re.sub(r'%([^%]+)%|\$\{([^}]+)\}|\$(\w+)',
                      lambda m: env.get(next(g for g in m.groups() if g), m[0]), base)
        if not paths.isabs(base): raise ValueError('Agent 配置目录必须是绝对路径')
    if not base and agent == 'opencode' and env.get('XDG_CONFIG_HOME'):
        base = paths.join(env['XDG_CONFIG_HOME'], 'opencode')
        if not paths.isabs(base): raise ValueError('Agent 配置目录必须是绝对路径')
    roots = [paths.normpath(paths.join(base, 'skills') if base else paths.join(home, *relative))]
    if agent == 'workbuddy':
        legacy = paths.join(home, '.workbuddy', 'skills')
        # Older WorkBuddy installations keep a separate personal skill store.
        if system == sys.platform and Path(legacy).is_dir(): roots.append(legacy)
    return roots


def add_preset_agent(legacy, agent):
    roots = default_agent_roots(agent)
    config = legacy.load_config()
    settings = config.setdefault('platforms', {}).setdefault(agent, {'roots':[]})
    settings.update(enabled=True, agent=True, added=True, removed=False, discover_defaults=True, label=BUILTIN_AGENTS[agent][0])
    settings['roots'] = list(dict.fromkeys([*settings.get('roots', []), *roots]))
    legacy.save_config(config)
    registry = legacy.Registry()
    try:
        registry.log_operation('agent-add', agent, {'roots':roots, 'system':sys.platform})
    finally:
        registry.conn.close()
    return {'agent':agent, 'roots':roots, 'system':sys.platform}


def agent_catalog(config):
    profiles = config.get('platforms', {})
    catalog = []
    for key in dict.fromkeys([*BUILTIN_AGENTS, *profiles]):
        if key == 'shared':
            continue
        settings = profiles.get(key, {})
        if key not in BUILTIN_AGENTS and not settings.get('agent'):
            continue
        label, root, command = BUILTIN_AGENTS.get(key, (key, '', key))
        roots = settings.get('roots', [])
        defaults = default_agent_roots(key) if key in BUILTIN_AGENTS else []
        detected = bool(shutil.which(command)) or any(path(r).parent.is_dir() for r in defaults)
        configured = not settings.get('removed') and (bool(settings.get('added')) or any(path(r).is_dir() for r in roots))
        catalog.append({'id':key, 'label':settings.get('label') or label, 'default_root':defaults[0] if defaults else '',
                        'default_roots':defaults, 'preset':key in BUILTIN_AGENTS, 'system':sys.platform,
                        'detected':detected, 'added':configured, 'removed':bool(settings.get('removed')), 'enabled':settings.get('enabled', True)})
    return catalog


def discover_agent_candidates(config, home=None):
    """Find unregistered skill stores in conventional local Agent homes."""
    home = Path(home or Path.home()).expanduser()
    known = set()
    for agent in BUILTIN_AGENTS:
        known.update(str(Path(root).expanduser().resolve()) for root in default_agent_roots(agent, home=home))
    for settings in config.get('platforms', {}).values():
        known.update(str(Path(root).expanduser().resolve()) for root in settings.get('roots', []))

    bases = []
    try:
        bases.extend(p for p in home.iterdir() if p.is_dir() and p.name.startswith('.') and not p.is_symlink())
    except OSError:
        return []
    config_home = home / '.config'
    if config_home.is_dir() and not config_home.is_symlink():
        try:
            bases.extend(p for p in config_home.iterdir() if p.is_dir() and not p.is_symlink())
        except OSError:
            pass

    candidates = []
    for base in bases:
        root = base / 'skills'
        if root.is_symlink() or not root.is_dir() or str(root.resolve()) in known:
            continue
        name = base.name.lstrip('.')
        agent = re.sub(r'[^a-z0-9_-]+', '-', name.lower()).strip('-_')
        if not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', agent) or agent in BUILTIN_AGENTS or agent in ('agents', 'skills-manager'):
            continue
        try:
            has_skill = any(child.is_dir() and not child.is_symlink() and (child / 'SKILL.md').is_file() for child in root.iterdir())
        except OSError:
            continue
        if has_skill:
            candidates.append({'id': agent, 'label': name.replace('-', ' ').replace('_', ' ').title(), 'root': str(root)})
    return sorted(candidates, key=lambda item: (item['label'].casefold(), item['root']))


def path(value):
    value = os.path.expandvars(os.path.expanduser(str(value)))
    if os.name == 'nt':
        value = re.sub(r'%([^%]+)%', lambda m: os.environ.get(m[1], m[0]), value)
    return Path(os.path.abspath(value))


def hermes_locations(home):
    """Read JSON or the ordinary skills YAML block without adding dependencies.

    Unsupported syntax is reported, never treated as evidence of a consumer.
    Only path fields are returned; credentials and other config are not exposed.
    """
    config = home / 'config.yaml'
    if not config.is_file():
        return [], []
    try:
        text = config.read_text(encoding='utf-8')
        if text.lstrip().startswith('{'):
            skills = json.loads(text).get('skills', {})
            values = skills.get('external_dirs', [])
            if not isinstance(values, list):
                raise ValueError()
            values = [(v, 'external_dirs') for v in values]
            if skills.get('create_dir'):
                values.append((skills['create_dir'], 'create_dir'))
        else:
            values, section, section_indent, list_indent = [], False, 0, None
            for line in text.splitlines():
                if not line.strip() or line.lstrip().startswith('#'):
                    continue
                indent = len(line) - len(line.lstrip())
                body = line.strip()
                if body == 'skills:':
                    section, section_indent, list_indent = True, indent, None
                    continue
                if not section:
                    continue
                if indent <= section_indent:
                    section = False
                    continue
                if body.startswith(('external_dirs:', 'create_dir:')):
                    key, raw = body.split(':', 1)
                    # Comments following an unquoted scalar are allowed.
                    raw = re.split(r'\s+#', raw.strip(), maxsplit=1)[0].strip()
                    list_indent = indent if key == 'external_dirs' and not raw else None
                    if not raw:
                        continue
                    if raw.startswith('['):
                        parsed = ast.literal_eval(raw)
                        if key != 'external_dirs' or not isinstance(parsed, list):
                            raise ValueError()
                        values.extend((v, key) for v in parsed)
                    elif key == 'create_dir':
                        values.append((scalar(raw), key))
                    elif raw not in ('null', '~', '[]'):
                        raise ValueError()
                elif list_indent is not None and indent >= list_indent and body.startswith('- '):
                    values.append((scalar(re.split(r'\s+#', body[2:], maxsplit=1)[0].strip()), 'external_dirs'))
                elif list_indent is not None and indent <= list_indent:
                    list_indent = None
        result = []
        for raw, field in values:
            if not isinstance(raw, str) or not raw:
                raise ValueError()
            expanded = os.path.expandvars(os.path.expanduser(raw))
            if '${' in expanded or re.search(r'\$\w+', expanded):
                raise ValueError()
            p = path(expanded) if os.path.isabs(expanded) else path(home / expanded)
            result.append((p, 'Hermes skills.' + field))
        return result, []
    except (OSError, ValueError, TypeError, SyntaxError, AttributeError):
        return [], ['Hermes 外部目录配置未能可靠解析，请手动添加扫描目录并核对归属。']


def scalar(raw):
    if raw.startswith(('"', "'")):
        value = ast.literal_eval(raw)
        if not isinstance(value, str):
            raise ValueError()
        return value
    if raw.startswith(('&', '*', '!', '{', '[', '|', '>')):
        raise ValueError()
    return raw


def locations(config):
    entries, warnings, seen = [], [], set()
    agent_ids = set(AGENTS) | {a['id'] for a in agent_catalog(config)}
    homes = {'codex': path(os.environ.get('CODEX_HOME', '~/.codex')),
             'claude_code': path(os.environ.get('CLAUDE_CONFIG_DIR', '~/.claude')),
             'hermes': path(os.environ.get('HERMES_HOME', '~/.hermes')),
             'openclaw': path(os.environ.get('OPENCLAW_STATE_DIR', '~/.openclaw'))}
    def add(platform, raw, enabled, evidence, configured=True, config_root=None):
        p = path(raw)
        key = (platform, os.path.normcase(str(p)))
        if key in seen:
            return
        seen.add(key)
        entries.append({'platform':platform, 'root':str(config_root if config_root is not None else raw), 'expanded':str(p),
                        'real_path':str(p.resolve()), 'enabled':enabled,
                        'exists':p.is_dir(), 'evidence':evidence, 'configured':configured,
                        'location_type':'custom' if platform not in agent_ids else 'agent',
                        'agent':platform if platform in agent_ids else None})
    for platform, settings in config.get('platforms', {}).items():
        if settings.get('removed'):
            continue
        enabled = bool(settings.get('enabled', True))
        raw_roots = settings.get('roots', [])
        for raw in raw_roots:
            expanded = raw
            native = {'codex':'~/.codex/', 'claude_code':'~/.claude/',
                      'hermes':'~/.hermes/', 'openclaw':'~/.openclaw/'}.get(platform)
            if native and str(raw).startswith(native):
                expanded = homes[platform] / str(raw)[len(native):]
            add(platform, expanded, enabled, 'configured-root', config_root=raw)
        # Existing explicit custom-root lists stay explicit. The opt-in switch
        # can enable conventions for them; default profiles use conventions.
        native_skill = {'codex':'~/.codex/skills', 'claude_code':'~/.claude/skills',
                        'hermes':'~/.hermes/skills', 'openclaw':'~/.openclaw/skills'}.get(platform)
        auto = settings.get('discover_defaults', native_skill in raw_roots)
        if not auto:
            continue
        # A common storage folder alone does not prove every compatible Agent
        # is installed. Require a profile footprint or a discoverable executable.
        available = homes.get(platform, path('~')).is_dir() or bool(shutil.which({'claude_code':'claude'}.get(platform, platform)))
        if platform == 'codex':
            add(platform, '~/.agents/skills', enabled and available, 'Codex personal skills convention', False)
        elif platform == 'openclaw' and homes[platform] == path('~/.openclaw'):
            add(platform, '~/.agents/skills', enabled and available, 'OpenClaw default-state convention', False)
        elif platform == 'hermes':
            found, issues = hermes_locations(homes[platform])
            warnings.extend(issues if enabled else [])
            for p, evidence in found:
                add(platform, p, enabled, evidence, False)
    # Associate physical locations, including a root alias, with real Agents.
    consumers = {}
    for e in entries:
        if e['enabled'] and e['agent'] and e['exists']:
            consumers.setdefault(e['real_path'], set()).add(e['agent'])
    for e in entries:
        e['agents'] = sorted(consumers.get(e['real_path'], set()))
    return entries, warnings
