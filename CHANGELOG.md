# Changelog

All notable changes to Agent Skill Manager are documented here.

## Unreleased

- Changed activate/deactivate from registry-only status edits to real filesystem moves using matching `.disabled` folders.
- Added rescan verification after delete and update operations so the dashboard reflects actual filesystem state.
- Added Smart Scan background jobs so long scans continue after the modal is closed and interrupted scans can be detected.
- Added Smart Scan scope selection for full scan, Agent platform, or grade-based priority.
- Added GitCode support for direct installs and GitCode GitHub mirror fallback when GitHub checks/downloads fail.
- Added update channel management with per-channel search/check/download/update statistics and score-based channel ordering.

## 1.1.0 - 2026-06-02

- Added a dashboard install flow for installing skills from GitHub, GitLab, Gitee, skills.sh, or direct zip links.
- Added install preview so repositories with multiple `SKILL.md` files can be reviewed before installation.
- Added backup-and-replace support when installing over an existing local skill folder.
- Added SkillsMP as a remote discovery fallback before `find-skills`.
- Updated remote checks so one failed source falls through to the next source before local version unification.

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
