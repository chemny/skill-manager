# Changelog

All notable changes to Agent Skill Manager are documented here.

## 1.0.0 - 2026-05-31

Initial public release.

- Cross-platform local skill inventory for Codex, Claude Code, OpenClaw, Hermes, and the public skills directory.
- Local HTML management console with Chinese/English UI.
- Skill grouping by name with per-platform tags.
- Status, health score, source, version, grade, and 30-day usage display.
- Real 30-day usage import from local session logs where available.
- Source management for configurable skill roots.
- Smart upgrade checks with 24-hour local cache.
- GitHub/Vercel-aware update checks and local version unification flow.
- Builtin/plugin skills are treated as observe-only by default.
- Delete flow includes confirmation and local backup.
- Snapshot report generation in Markdown and HTML.
- CLI wrapper for Unix-like shells and PowerShell compatibility wrapper.

## Release Notes

- Requires Python 3.10+.
- Uses only the Python standard library.
- Runtime data is stored under `~/.agent-skill-manager/` by default.
