---
name: skill-manager
description: Use when the user wants to list, search, inspect, audit, update-check, activate, deactivate, report on, or manage locally installed agent skills and adjacent capabilities across Codex, Claude Code, OpenClaw, Hermes, and configurable local roots.
version: 1.1.0
local_updated_at: 2026-06-02T00:00:00+08:00
---

# Agent Skill Manager

Agent Skill Manager is a cross-platform local registry for agent capabilities. It manages skills, commands, agents, workflows, plugins, tools, MCP servers, and prompt templates across multiple Agent runtimes.

The manager is intentionally local-first:

- Registry database: `~/.agent-skill-manager/skills.db`
- Config file: `~/.agent-skill-manager/config.json`
- Reports: `~/.agent-skill-manager/reports/`
- Backups: `~/.agent-skill-manager/backups/`

## Platforms

Default configured platforms:

- Public: `~/.agents/skills`
- Codex: `~/.codex/skills`, `~/.codex/plugins/cache`
- Claude Code: `~/.claude/commands`, `~/.claude/agents`, project `.claude/commands`, project `.claude/agents`
- OpenClaw: `~/.openclaw/skills`, `~/.openclaw/plugins`, `~/.openclaw/tools`, `~/.openclaw/workflows`
- Hermes: `~/.hermes`

If a platform uses different local paths, edit `~/.agent-skill-manager/config.json` after the first run.

## Commands

Prefer the cross-platform Python entrypoint:

```bash
python3 ~/.agents/skills/skill-manager/scripts/agent_skill_manager.py scan
python3 ~/.agents/skills/skill-manager/scripts/agent_skill_manager.py list
python3 ~/.agents/skills/skill-manager/scripts/agent_skill_manager.py web --open
```

On Unix-like systems, the shorter wrapper is:

```bash
~/.agents/skills/skill-manager/bin/asm scan
~/.agents/skills/skill-manager/bin/asm list
~/.agents/skills/skill-manager/bin/asm web --open
```

On Windows, the legacy PowerShell wrapper forwards to the Python implementation:

```powershell
powershell -ExecutionPolicy Bypass -File "$env:USERPROFILE\.agents\skills\skill-manager\scripts\skill-manager.ps1" -Action Scan
powershell -ExecutionPolicy Bypass -File "$env:USERPROFILE\.agents\skills\skill-manager\scripts\skill-manager.ps1" -Action List
powershell -ExecutionPolicy Bypass -File "$env:USERPROFILE\.agents\skills\skill-manager\scripts\skill-manager.ps1" -Action Web
```

## Inventory

Use when the user asks what skills are installed, which platform they belong to, whether they are active, or what versions they are on.

```bash
asm scan
asm list
asm list --platform codex
asm search pdf
asm show skill-manager
asm duplicates
```

The registry normalizes each item into a capability:

- `id`
- `name`
- `kind`
- `platform`
- `path`
- `status`
- `health`
- `version`
- `source_type`
- GitHub metadata when present
- usage counters

Supported kinds include:

- `skill`
- `command`
- `agent`
- `workflow`
- `plugin`
- `tool`
- `mcp-server`
- `prompt-template`
- `unknown`

## Status

Status changes are real filesystem moves for editable capabilities. Deactivate moves a capability from its configured source root into the matching `.disabled` root. Activate moves it back.

```bash
asm activate skill-manager --platform codex
asm deactivate skill-manager --platform codex
```

Use `deactivate` for capabilities that should stop being loaded by platforms that scan the active source root. Use delete only when the capability should be backed up and removed from disk.

## Usage

Usage can be logged from a user action, a runtime hook, a session-log parser, or manually:

```bash
asm log-use skill-manager --platform codex --source manual --confidence high
asm usage --days 30
```

Usage sources should be interpreted by confidence:

- `runtime-hook`: direct instrumentation, high confidence
- `session-log`: parsed from transcripts, medium confidence
- `manual`: explicitly recorded by the user or agent, high confidence
- `ui`: recorded from the HTML admin interface, high confidence
- `inferred`: weak text inference, low confidence

## Health

Run health checks when the user asks whether skills are broken, incomplete, missing metadata, or badly tracked:

```bash
asm health
```

Health checks currently validate:

- path exists
- `SKILL.md` exists for normalized skill directories
- remote-backed items have source metadata when available
- version is known when possible

## Version Checks

Remote-backed capabilities are detected from repository metadata in `SKILL.md` frontmatter or `_meta.json`, including GitHub, GitLab, Gitee, and skills.sh URLs.

```bash
asm check-updates
asm outdated
asm update skill-name --dry-run
```

Automatic overwrite is intentionally conservative. The update command reports local and remote hashes and leaves the final file update to a reviewed operation.

The HTML admin provides a reviewed update flow:

- Check bound repository metadata first, including GitHub, GitLab, Gitee, and skills.sh URLs found in `github_url`, `homepage`, or `repository`.
- If no bound source is usable, search SkillsMP, then `find-skills` / skills.sh.
- If a newer remote version is found, confirm before backing up and replacing every selected local copy.
- If no newer remote version is found but local copies have different versions, offer to unify all local copies to the highest local version.
- Builtin/plugin cache capabilities are not updated or deleted from the UI.

Recommended metadata:

```yaml
github_url: https://github.com/owner/repo
github_hash: commit-or-version-hash
github_ref: main
github_path: skills/example/SKILL.md
local_updated_at: 2026-06-02T00:00:00+08:00
```

## Reports

Generate a management report:

```bash
asm report
```

The report includes:

- total capabilities
- active and inactive counts
- zero usage in 30 days
- metadata gaps
- top usage
- cleanup candidates

## HTML Admin

Start the local backend HTML dashboard:

```bash
asm web --host 127.0.0.1 --port 8765 --open
```

The dashboard supports:

- English/Chinese language toggle
- scan and refresh
- platform/status/source filtering
- search
- usage visibility
- usage event viewer
- source management
- install skills from GitHub, GitLab, Gitee, skills.sh, or zip links
- background Smart Scan checks with full, platform, or grade scopes
- real activate by moving files back from `.disabled`
- real deactivate by moving files into `.disabled`
- checked update flow
- delete with confirmation
- health check
- report generation

The HTML admin is served only from the local Python process. It uses the same SQLite registry as the CLI.

## Safety

- Do not delete skills by default.
- Prefer deactivate over delete when the user wants a reversible change.
- Back up capability paths before any destructive or overwrite operation:

```bash
asm backup skill-name --platform codex
```

- Deleting from the HTML admin requires a browser confirmation and then creates a backup before removing files.
- CLI deletion requires explicit confirmation:

```bash
asm delete skill-name --platform codex --yes
```

- Treat builtin/plugin cache skills as managed inventory, not user-owned source.
- For OpenClaw and Hermes, use config-driven roots until the local runtime path contract is confirmed.
- Public skills are displayed as `public` in English and `公共` in Chinese.
