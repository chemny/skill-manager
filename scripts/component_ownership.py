"""Local installation evidence, independent of remote source and Skill metadata.

Documentation names are candidates, never a cross-version deletion blacklist.
All checks are repeated at write time; no network request or Agent execution.
"""
import json
import os
import shutil
from pathlib import Path


# Observed Codex distribution marker, not an invented semantic version.
CODEX_DISTRIBUTIONS = {
    '4af37f669ff79b4f': frozenset(('imagegen', 'openai-docs', 'review-agent',
                                'skill-creator', 'skill-installer')),
}
PACKAGES = {
    '@google/gemini-cli': ('gemini', ('dist/src/skills/builtin', 'packages/core/src/skills/builtin')),
    '@qwen-code/qwen-code': ('qwen', ('dist/src/skills/bundled', 'packages/core/src/skills/bundled')),
    'openclaw': ('openclaw', ('skills',)),
}


def read_json(path):
    try:
        if path.stat().st_size > 2 * 1024 * 1024:
            return {}
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, UnicodeError):
        return {}


def under(path, root):
    return path == root or root in path.parents


class Ownership:
    """One read-only snapshot per inventory; fresh snapshot for every mutation."""
    def __init__(self):
        self.codex = Path(os.environ.get('CODEX_HOME', '~/.codex')).expanduser().absolute()
        self.claude = Path(os.environ.get('CLAUDE_CONFIG_DIR', '~/.claude')).expanduser().absolute()
        system = self.codex / 'skills/.system'
        try:
            self.distribution = (system / '.codex-system-skills.marker').read_text().strip()
        except (OSError, UnicodeError):
            self.distribution = ''
        self.claude_installs = read_json(self.claude / 'plugins/installed_plugins.json').get('plugins', {})
        if not isinstance(self.claude_installs, dict):
            self.claude_installs = {}
        self.packages = []
        for command in ('gemini', 'qwen', 'openclaw'):
            executable = shutil.which(command)
            if not executable:
                continue
            for parent in list(Path(executable).resolve().parents)[:8]:
                info = read_json(parent / 'package.json')
                name = info.get('name')
                if isinstance(name, str) and name in PACKAGES and isinstance(info.get('version'), str) and info['version'] and PACKAGES[name][0] == command:
                    self.packages.append((parent, info, PACKAGES[name]))
                    break

    def classify(self, value):
        path = Path(value).expanduser().absolute()
        # Removing an individual link does not mutate its component target.
        if path.is_symlink():
            return self.result('user', False, reason='仅卸载此链接，保留目标文件')
        resolved = path.resolve()
        system = (self.codex / 'skills/.system').resolve()
        if under(resolved, system):
            relative = resolved.relative_to(system)
            names = CODEX_DISTRIBUTIONS.get(self.distribution, ())
            known = bool(relative.parts and relative.parts[0] in names)
            return self.result('system' if known else 'package_unverified', True, 'codex',
                               relative.parts[0] if relative.parts else '', self.distribution,
                               'Codex 系统分发组件' if known else '系统分发清单未匹配，不能直接修改软件包文件',
                               str(system / '.codex-system-skills.marker'))
        for home, agent, manifest_name in ((self.codex, 'codex', '.codex-plugin/plugin.json'),
                                            (self.claude, 'claude_code', '.claude-plugin/plugin.json')):
            cache = (home / 'plugins/cache').resolve()
            if not under(resolved, cache):
                continue
            for parent in (resolved, *resolved.parents):
                if parent == cache:
                    break
                info = read_json(parent / manifest_name)
                if not isinstance(info.get('name'), str) or not info['name']:
                    continue
                installed = None
                if agent == 'claude_code':
                    for key, records in self.claude_installs.items():
                        if not isinstance(records, list):
                            continue
                        if any(isinstance(r, dict) and isinstance(r.get('installPath'), str) and os.path.isabs(r['installPath']) and
                               Path(r['installPath']).expanduser().resolve() == parent and
                               isinstance(info.get('version'), str) and info['version'] and
                               r.get('version') == info.get('version') for r in records):
                            installed = key
                            break
                return self.result('plugin', True, agent, installed or info['name'],
                                   str(info.get('version') or ''),
                                   '属于插件 ' + (installed or info['name']) + '；请在对应 Agent 的插件管理中卸载整个插件',
                                   str(parent / manifest_name), (installed is not None) if agent == 'claude_code' else None)
            return self.result('package_unverified', True, agent, reason='插件缓存未找到可靠归属清单；不能直接删除缓存文件')
        for agent, home_name, manifest_name, id_key in (
                ('gemini', '.gemini', 'gemini-extension.json', 'name'),
                ('qwen', '.qwen', 'qwen-extension.json', 'name'),
                ('openclaw', os.environ.get('OPENCLAW_STATE_DIR', '~/.openclaw'), 'openclaw.plugin.json', 'id')):
            home = Path(home_name).expanduser() if agent == 'openclaw' else Path.home() / home_name
            extensions = (home / 'extensions').resolve()
            if not under(resolved, extensions):
                continue
            for parent in (resolved, *resolved.parents):
                if parent == extensions:
                    break
                info = read_json(parent / manifest_name)
                if not isinstance(info.get(id_key), str) or not info[id_key]:
                    continue
                component = info[id_key]
                return self.result('plugin', True, agent, component, str(info.get('version') or ''),
                                   '属于扩展 ' + component + '；请在对应 Agent 的扩展管理中卸载整个扩展',
                                   str(parent / manifest_name))
            return self.result('package_unverified', True, agent, reason='扩展安装目录未找到可靠归属清单；不能直接删除扩展文件')
        for root, info, (agent, builtin_roots) in self.packages:
            if not under(resolved, root):
                continue
            builtin = any(under(resolved, root / sub) for sub in builtin_roots)
            return self.result('system' if builtin else 'package_unverified', True, agent,
                               info['name'], info['version'],
                               '当前安装发行包内置组件' if builtin else '属于 Agent 安装包，不是用户独立安装的 Skill',
                               str(root / 'package.json'))
        for parent in resolved.parents:
            info = read_json(parent / 'package.json')
            if isinstance(info.get('name'), str) and info['name'] in PACKAGES:
                return self.result('package_unverified', True, PACKAGES[info['name']][0],
                                   info['name'], str(info.get('version') or ''),
                                   'Agent 软件包尚未与运行版本匹配，不能直接删除软件包文件',
                                   str(parent / 'package.json'))
        if any(p.name.endswith('.app') and under(resolved, p / 'Contents') for p in resolved.parents):
            return self.result('package_unverified', True, reason='属于应用安装包，不能直接删除软件包文件')
        return self.result('user', False, reason='用户管理的独立 Skill')

    @staticmethod
    def result(kind, read_only, agent='', component='', version='', reason='', evidence='', installed=None):
        return {'kind':kind, 'read_only':read_only, 'agent':agent, 'component_id':component,
                'version':version, 'reason':reason, 'evidence':evidence, 'installed':installed}


def classify(path):
    return Ownership().classify(path)
