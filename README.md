# Agent Skill Manager

English | [中文](./README.zh.md)

## Overview

Agent Skill Manager brings locally installed Skills into one workspace. It is for people who want to understand what is installed across Agents, maintain it safely, and find new Skills without hunting through separate catalogs.

The local Web UI is the main entry point. An Agent can also use the Python CLI. Both share a SQLite registry and the same maintenance logic; running the manager does not require a model API.

## Preview

![Home page with sample data showing installed Skills, rankings and the Skill store](assets/skill-manager-preview.png)

## Features

| Capability | What you can do |
| --- | --- |
| Unified inventory | Browse grouped Skills and see their Agent associations, installation time, size, category and update status. Independent installation directories remain separate on disk. |
| Home and discovery | See installed Skills, Agent coverage, available updates and disk use at a glance. Browse a paginated Skill store with rankings, categories and title search. The home page shows recent verified Skills; a daily recommendation board appears only when at least five eligible items are available. |
| Update checks | Check all eligible installed Skills against their recorded sources. The UI distinguishes failed queries from sources found without a trusted comparison baseline. Successful results are reused for 15 minutes; failed requests can be retried, subject to provider rate limits. |
| Installation | Install from supported public repository URLs, HTTPS ZIP links, a local folder or a local ZIP, then choose the Agent and installation location. |
| Agent directories | Add supported Agent presets with OS-specific default paths, or register custom directories in Settings. |
| Backup and recovery | Create and restore backups, scan configured locations for older backups, move deleted backups and uninstalled Skills to the recycle bin, and permanently delete recycle-bin entries after confirmation. Incomplete scans keep their error details visible without treating partial results as a complete inventory. |
| Categories and tags | Automatically fill missing categories or tags, adjust them manually, and filter or sort the list directly. |
| Local history | Inspect failed or interrupted operations and export diagnostic records when troubleshooting. |
| Cache cleanup | Inspect and remove the manager's expired temporary files without touching installed Skills or backups. |

## Installation

### Requirements

Use an Agent that supports local Skills and local command execution. Codex and Claude Code can use the root `SKILL.md` and Python entry point.

| Item | Requirement |
| --- | --- |
| Python | Python 3.10 or later; the core uses the standard library, including SQLite. |
| Browser | A current desktop browser for the local management UI. |
| Git and network | Needed for Git-backed source checks and updates; public providers may impose request limits. |
| File picker | macOS uses its system picker; other platforms use Python Tkinter when available. Local paths can also be entered manually. |
| Operating system | Tested on macOS. Windows paths and adapters have been reviewed and have simulated tests, but Windows has not been tested on a real machine. Automatic shared-link creation is not supported on Windows. |
| Optional translation | English summaries on the home page can be translated locally with Ollama and the `qwen3.5:4b` model (override with `SKILL_MANAGER_TRANSLATION_MODEL`). Without it, the manager remains usable and does not send descriptions to a translation service. |

### Quick Installation

Ask your Agent:

```text
Install https://github.com/chemny/skill-manager as a local Skill.
Check the required environment, install it, and verify that you can load and run it.
```

The Agent should report the installed location and whether the command-line entry point is ready.

## Quick Start

```text
Use skill-manager to scan my installed Skills and open the management page.
```

The page opens on your computer. The home page summarizes your installation and links to recent Skills. Open **All** to manage an Agent's Skills, or **Discover** to browse catalogs. **Refresh** checks already registered items; **Scan** discovers changes in configured directories.

## Usage Examples

### Check for updates

```text
Check all my installed Skills for updates and show which ones have newer versions.
```

Checking does not install anything. Updates use the recorded source, and local content is backed up before replacement. Missing sources, failed checks and a missing trusted comparison baseline are not treated as proof that a Skill is up to date.

### Install a Skill

```text
Install this Skill for Codex: <repository URL or local folder>.
```

The manager identifies the content and installation location before writing files. An existing destination is not silently overwritten.

### Back up and recover

```text
Back up my installed Skills so I can restore them later.
```

Backups appear in **Backups**. A scan can find backups in configured locations; an incomplete scan reports uncovered locations and keeps the results provisional. Deleting a backup moves it to **Recycle Bin**; permanent deletion is available there. Destructive Web actions show a confirmation dialog. Restoring over different existing content requires a replacement choice and first creates a backup.

### Manage another Agent

```text
Add OpenCode to the manager and show its installed Skills.
```

Presets supply platform-appropriate default directories. Removing an Agent's management configuration does not delete its Skill files. Shared directories may be used by multiple Agents; uninstalling shared content affects those users of the directory.

### Discover a Skill

```text
Show me recently added Skills for writing and documents.
```

The store provides rankings, categories and title search using public catalog data. Searches cover a bounded set of provider candidates, so a limited search is not a claim of exhaustive results. A catalog result is not automatically trusted as an update source for an installed Skill. Daily recommendations verify that a repository contains a usable `SKILL.md` and have a freshness rule; if fewer than five qualify, the home page shows new arrivals instead.

## How It Works

The manager scans configured directories and records installed items, source evidence, update results and operation history in SQLite. The browser reuses inventory data for 15 minutes and backup-list data for one hour. Discovery results and translated summaries are cached locally; remote catalog and GitHub requests are rate-limited and can fail independently. Writes refresh affected records.

| Component | Responsibility |
| --- | --- |
| `SKILL.md` | Instructions for an Agent using the manager. |
| `scripts/agent_skill_manager.py` and `bin/asm` | Python CLI, local Web server and macOS/POSIX command wrapper. Windows can invoke the Python script directly. |
| `assets/maintenance.html` | Home, inventory, discovery, updates, installation, backups, recycle bin and settings UI. |
| Maintenance modules in `scripts/` | Source checks, ownership rules, file validation, backups, task execution and recovery. |

The service binds to the local loopback interface and uses a local request token. Data stays in `~/.agent-skill-manager` by default; `ASM_HOME` selects an isolated data directory. Discovery, source searches and update checks contact public providers. The local translation option calls Ollama on loopback. Diagnostics may contain local paths, so review exported reports before sharing.

Built-in status uses ownership evidence rather than treating an entire Agent directory as protected. Cleanup advice is advisory: an old version or a same-name copy alone does not prove that content can be safely deleted.

## Repository Structure

```text
SKILL.md                         Agent instructions
scripts/                         CLI, server and maintenance modules
bin/asm                          POSIX command wrapper
assets/maintenance.html          Management UI
assets/skill-manager-preview.png Home-page screenshot with sample data
references/                      Usage, API and attribution documents
tests/                           Isolated automated tests
README.md / README.zh.md         English and Chinese introductions
LICENSE                          Project license
```

See [maintenance commands](references/maintenance.md), the [Web API](references/web-api.md), [browser regression tests](references/browser-tests.md), [Agent icon sources](references/agent-icons.md), and the [Chinese purpose catalog](references/purpose-zh.json). Development changes are recorded in [CHANGELOG.md](CHANGELOG.md).

## License

Project code is available under the [MIT License](LICENSE). Bundled third-party icons retain their own notices; see [Agent icon sources and license](references/agent-icons.md). Product names and logos identify their respective products and do not imply endorsement.

## About Me

Maintained in [chemny/skill-manager](https://github.com/chemny/skill-manager). Report reproducible problems or suggest improvements through the repository's [Issues](https://github.com/chemny/skill-manager/issues).
