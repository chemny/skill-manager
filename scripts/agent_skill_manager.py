#!/usr/bin/env python3
"""Cross-platform Agent Skill Manager.

The implementation intentionally uses only the Python standard library so the
same file can run under Codex, Claude Code, OpenClaw, Hermes, macOS, Linux, and
Windows without a package install step.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import html
import http.server
import json
import os
import re
import signal
import shutil
import ssl
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


APP_NAME = "agent-skill-manager"
APP_DIR = Path(os.environ.get("ASM_HOME", "~/.agent-skill-manager")).expanduser()
DB_PATH = APP_DIR / "skills.db"
CONFIG_PATH = APP_DIR / "config.json"
REPORT_DIR = APP_DIR / "reports"
BACKUP_DIR = APP_DIR / "backups"
LOG_DIR = APP_DIR / "logs"
VERSION_PART_RE = re.compile(r"^\d+(?:\.\d+)+(?:[-+][A-Za-z0-9_.-]+)?$")
SKILL_MD_PATH_RE = re.compile(r"(?:~|/Users/[^\s\"'`<>]+|/opt/[^\s\"'`<>]+)[^\s\"'`<>]*?/SKILL\.md")
REMOTE_UPDATE_CACHE_TTL = dt.timedelta(hours=24)
SMART_UPGRADE_JOBS: Dict[str, Dict[str, Any]] = {}
SMART_UPGRADE_LOCK = threading.Lock()
SMART_UPGRADE_SNAPSHOT_KEY = "latest"
UPDATE_BUTTON_DISABLED_STATUSES = {"updated", "latest", "no_cloud", "remote_error", "unsupported"}

DEFAULT_CONFIG: Dict[str, Any] = {
    "version": 1,
    "platforms": {
        "shared": {
            "enabled": True,
            "roots": [
                "~/.agents/skills",
            ],
        },
        "codex": {
            "enabled": True,
            "roots": [
                "~/.codex/skills",
                "~/.codex/plugins/cache",
            ],
        },
        "claude_code": {
            "enabled": True,
            "roots": [
                "~/.claude/commands",
                "~/.claude/agents",
                ".claude/commands",
                ".claude/agents",
            ],
        },
        "openclaw": {
            "enabled": True,
            "roots": [
                "~/.openclaw/skills",
                "~/.openclaw/plugins",
                "~/.openclaw/tools",
                "~/.openclaw/workflows",
            ],
        },
        "hermes": {
            "enabled": True,
            "roots": [
                "~/.hermes",
            ],
        },
    },
    "web": {"host": "127.0.0.1", "port": 8765},
}

try:
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
except (AttributeError, ValueError):
    pass


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def parse_iso_datetime(value: str) -> Optional[dt.datetime]:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def short(text: Optional[str], limit: int = 80) -> str:
    if not text:
        return ""
    value = re.sub(r"\s+", " ", str(text)).strip()
    return value if len(value) <= limit else value[: max(0, limit - 3)] + "..."


def ensure_app_dirs() -> None:
    for path in (APP_DIR, REPORT_DIR, BACKUP_DIR, LOG_DIR):
        path.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")


def load_config() -> Dict[str, Any]:
    ensure_app_dirs()
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    merged = json.loads(json.dumps(DEFAULT_CONFIG))
    for key, value in data.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key].update(value)
        else:
            merged[key] = value
    return merged


def save_config(config: Dict[str, Any]) -> None:
    ensure_app_dirs()
    CONFIG_PATH.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")


def expand_root(root: str) -> Path:
    return Path(os.path.expandvars(root)).expanduser().resolve()


def parse_frontmatter(path: Path) -> Dict[str, Any]:
    if not path.exists() or not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        text = path.read_text(encoding="utf-8", errors="ignore")
    match = re.match(r"^---\s*\r?\n(.*?)\r?\n---", text, re.S)
    if not match:
        return {}
    return parse_simple_yaml(match.group(1))


def parse_simple_yaml(text: str) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        m = re.match(r"^\s*([A-Za-z0-9_-]+)\s*:\s*(.*)\s*$", line)
        if not m:
            i += 1
            continue
        key, raw = m.group(1), m.group(2).strip()
        if raw in ("|", ">"):
            parts: List[str] = []
            i += 1
            while i < len(lines) and (lines[i].startswith(" ") or not lines[i].strip()):
                parts.append(lines[i].strip())
                i += 1
            data[key] = "\n".join(parts).strip() if raw == "|" else " ".join(parts).strip()
            continue
        if raw == "":
            items: List[str] = []
            j = i + 1
            while j < len(lines):
                item = re.match(r"^\s*-\s+(.+?)\s*$", lines[j])
                if not item:
                    break
                items.append(clean_scalar(item.group(1)))
                j += 1
            if items:
                data[key] = items
                i = j
                continue
        data[key] = clean_scalar(raw)
        i += 1
    return data


def clean_scalar(value: str) -> Any:
    value = value.strip()
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    if value.startswith("[") and value.endswith("]"):
        try:
            return json.loads(value)
        except Exception:
            return value
    return value


def read_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
        return {"json_type": type(data).__name__}
    except Exception as exc:
        return {"meta_error": str(exc)}


def stable_id(platform: str, kind: str, path: Path, name: str) -> str:
    digest = hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:12]
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-") or "unknown"
    return f"{platform}:{kind}:{safe_name}:{digest}"


def infer_version(folder: str, metadata: Dict[str, Any]) -> str:
    if metadata.get("version"):
        return str(metadata["version"])
    match = re.search(r"(\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?)$", folder)
    if match:
        return match.group(1)
    if metadata.get("github_hash"):
        return str(metadata["github_hash"])[:12]
    return "unknown"


def version_from_path(path: Path) -> str:
    for part in reversed(path.parts):
        if VERSION_PART_RE.match(part):
            return part
    return ""


def metadata_url(metadata: Dict[str, Any], key: str) -> str:
    value = metadata.get(key, "")
    if isinstance(value, dict):
        value = value.get("url", "")
    return str(value or "")


def remote_source_from_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.netloc or parsed.path.split("/")[0]).lower()
    if "github.com" in host:
        return "github"
    if "gitlab.com" in host:
        return "gitlab"
    if "bitbucket.org" in host:
        return "bitbucket"
    if "vercel.com" in host:
        return "vercel"
    return ""


def normalize_source(metadata: Dict[str, Any], path: Path) -> str:
    text = str(path)
    if metadata.get("builtin") is True or metadata.get("type") == "builtin":
        return "builtin"
    if is_builtin_path(path):
        return "builtin"
    for key in ("github_url", "repository", "homepage", "update_url", "source_url", "remote_url"):
        source = remote_source_from_url(metadata_url(metadata, key))
        if source:
            return source
    return "local"


def is_builtin_path(path: Path | str) -> bool:
    text = str(path).replace("\\", "/")
    markers = (
        "/.codex/plugins/cache/",
        "/.codex/skills/.system/",
        "/.claude/plugins/cache/",
        "/.claude/skills/.system/",
        "/.claude-code/plugins/cache/",
        "/.claude-code/skills/.system/",
        "/.openclaw/plugins/cache/",
        "/.openclaw/skills/.system/",
        "/.hermes/plugins/cache/",
        "/.hermes/skills/.system/",
        "/.hermes/hermes-agent/",
    )
    return any(marker in text for marker in markers)


def management_scope_for_row(row: Dict[str, Any]) -> str:
    path = str(row.get("path") or "")
    source = str(row.get("source_type") or "")
    github_url = str(row.get("github_url") or "")
    metadata = row.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}
    if source == "builtin" or is_builtin_path(path) or metadata.get("builtin") is True or metadata.get("type") == "builtin":
        return "builtin_observe_only"
    if source in ("github", "gitlab", "bitbucket", "vercel") or github_url:
        return "managed_remote"
    if source == "local":
        return "managed_local"
    return "unknown_review"


class Registry:
    def __init__(self, db_path: Path = DB_PATH) -> None:
        ensure_app_dirs()
        self.conn = sqlite3.connect(db_path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("pragma busy_timeout=30000")
        self.init_schema()

    def init_schema(self) -> None:
        self.conn.executescript(
            """
            create table if not exists capabilities (
              id text primary key,
              name text not null,
              kind text not null,
              platform text not null,
              path text not null,
              description text,
              version text,
              source_type text,
              github_url text,
              github_hash text,
              github_ref text,
              github_path text,
              status text default 'active',
              health text default 'unknown',
              usage_7d integer default 0,
              usage_30d integer default 0,
              last_used_at text,
              last_scanned_at text,
              local_updated_at text,
              metadata_json text,
              unique(platform, path)
            );
            create table if not exists usage_events (
              id integer primary key autoincrement,
              capability_id text not null,
              platform text,
              occurred_at text not null,
              source text not null,
              confidence text not null,
              session_id text,
              evidence text
            );
            create unique index if not exists idx_usage_events_unique
            on usage_events(capability_id, occurred_at, source, session_id, evidence);
            create table if not exists update_checks (
              id integer primary key autoincrement,
              capability_id text not null,
              checked_at text not null,
              status text not null,
              local_hash text,
              remote_hash text,
              message text
            );
            create table if not exists remote_snapshots (
              normalized_name text primary key,
              skill_name text not null,
              source_type text not null,
              source_url text not null,
              source_ref text,
              source_path text,
              remote_version text,
              remote_hash text,
              checked_at text not null,
              discovered_by text,
              confidence text,
              message text
            );
            create table if not exists smart_upgrade_snapshots (
              snapshot_key text primary key,
              checked_at text not null,
              result_json text not null
            );
            create table if not exists health_checks (
              id integer primary key autoincrement,
              capability_id text not null,
              checked_at text not null,
              status text not null,
              message text
            );
            create table if not exists reviews (
              capability_id text primary key,
              rating text,
              notes text,
              reviewed_at text
            );
            create table if not exists importance_overrides (
              capability_id text primary key,
              importance text not null,
              updated_at text not null
            );
            create table if not exists operation_logs (
              id integer primary key autoincrement,
              occurred_at text not null,
              action text not null,
              target text,
              details text
            );
            """
        )
        self.conn.commit()

    def upsert_capability(self, cap: Dict[str, Any]) -> None:
        existing = self.conn.execute(
            "select id, status from capabilities where platform=? and path=?",
            (cap["platform"], cap["path"]),
        ).fetchone()
        status = existing["status"] if existing else cap.get("status", "active")
        if existing and existing["id"] != cap["id"]:
            self.conn.execute("update usage_events set capability_id=? where capability_id=?", (cap["id"], existing["id"]))
            self.conn.execute("update update_checks set capability_id=? where capability_id=?", (cap["id"], existing["id"]))
            self.conn.execute("update health_checks set capability_id=? where capability_id=?", (cap["id"], existing["id"]))
            self.conn.execute("update reviews set capability_id=? where capability_id=?", (cap["id"], existing["id"]))
            self.conn.execute("update importance_overrides set capability_id=? where capability_id=?", (cap["id"], existing["id"]))
        self.conn.execute(
            """
            insert into capabilities (
              id, name, kind, platform, path, description, version, source_type,
              github_url, github_hash, github_ref, github_path, status, health,
              last_scanned_at, local_updated_at, metadata_json
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(platform, path) do update set
              id=excluded.id,
              name=excluded.name,
              kind=excluded.kind,
              description=excluded.description,
              version=excluded.version,
              source_type=excluded.source_type,
              github_url=excluded.github_url,
              github_hash=excluded.github_hash,
              github_ref=excluded.github_ref,
              github_path=excluded.github_path,
              health=excluded.health,
              last_scanned_at=excluded.last_scanned_at,
              local_updated_at=excluded.local_updated_at,
              metadata_json=excluded.metadata_json
            """,
            (
                cap["id"],
                cap["name"],
                cap["kind"],
                cap["platform"],
                cap["path"],
                cap.get("description", ""),
                cap.get("version", "unknown"),
                cap.get("source_type", "local"),
                cap.get("github_url", ""),
                cap.get("github_hash", ""),
                cap.get("github_ref", ""),
                cap.get("github_path", ""),
                status,
                cap.get("health", "unknown"),
                cap.get("last_scanned_at", now_iso()),
                cap.get("local_updated_at", ""),
                json.dumps(cap.get("metadata", {}), ensure_ascii=False),
            ),
        )

    def prune_to_scan(self, caps: Sequence[Dict[str, Any]]) -> None:
        platforms = sorted({cap["platform"] for cap in caps})
        paths = sorted({cap["path"] for cap in caps})
        if not platforms:
            return
        platform_marks = ",".join("?" for _ in platforms)
        if not paths:
            self.conn.execute(f"delete from capabilities where platform in ({platform_marks})", platforms)
            return
        path_marks = ",".join("?" for _ in paths)
        self.conn.execute(
            f"delete from capabilities where platform in ({platform_marks}) and path not in ({path_marks})",
            [*platforms, *paths],
        )

    def refresh_usage_counts(self) -> None:
        now = dt.datetime.now(dt.timezone.utc).astimezone()
        for days, column in ((7, "usage_7d"), (30, "usage_30d")):
            since = (now - dt.timedelta(days=days)).isoformat(timespec="seconds")
            rows = self.conn.execute(
                "select capability_id, count(*) c from usage_events where occurred_at >= ? group by capability_id",
                (since,),
            ).fetchall()
            self.conn.execute(f"update capabilities set {column}=0")
            for row in rows:
                self.conn.execute(
                    f"update capabilities set {column}=? where id=?",
                    (row["c"], row["capability_id"]),
                )
        self.conn.execute(
            """
            update capabilities
            set last_used_at = (
                select max(occurred_at)
                from usage_events
                where usage_events.capability_id = capabilities.id
            )
            where exists (
                select 1 from usage_events where usage_events.capability_id = capabilities.id
            )
            """
        )
        self.conn.commit()

    def all_capabilities(self, where: str = "", args: Sequence[Any] = ()) -> List[sqlite3.Row]:
        self.refresh_usage_counts()
        query = """
            select c.*, i.importance as importance_override
            from capabilities c
            left join importance_overrides i on i.capability_id = c.id
        """
        if where:
            query += " where " + where
        query += " order by c.platform, c.kind, c.name, c.path"
        return self.conn.execute(query, args).fetchall()

    def find_one(self, name_or_id: str, platform: str = "") -> Optional[sqlite3.Row]:
        clauses = ["(id=? or name=? or path like ?)"]
        args: List[Any] = [name_or_id, name_or_id, f"%{name_or_id}%"]
        if platform:
            clauses.append("platform=?")
            args.append(platform)
        return self.conn.execute(
            "select * from capabilities where " + " and ".join(clauses) + " order by length(path) limit 1",
            args,
        ).fetchone()

    def set_status(self, name_or_id: str, status: str, platform: str = "") -> sqlite3.Row:
        cap = self.find_one(name_or_id, platform)
        if not cap:
            raise SystemExit(f"No capability found for {name_or_id}")
        self.conn.execute("update capabilities set status=? where id=?", (status, cap["id"]))
        self.log_operation("status", cap["id"], {"status": status, "name": cap["name"], "platform": cap["platform"]}, commit=False)
        self.conn.commit()
        return self.conn.execute("select * from capabilities where id=?", (cap["id"],)).fetchone()

    def delete_capability_row(self, cap_id: str) -> None:
        self.conn.execute("delete from usage_events where capability_id=?", (cap_id,))
        self.conn.execute("delete from update_checks where capability_id=?", (cap_id,))
        self.conn.execute("delete from health_checks where capability_id=?", (cap_id,))
        self.conn.execute("delete from reviews where capability_id=?", (cap_id,))
        self.conn.execute("delete from importance_overrides where capability_id=?", (cap_id,))
        self.conn.execute("delete from capabilities where id=?", (cap_id,))
        self.conn.commit()

    def set_importance(self, ids: Sequence[str], importance: str) -> int:
        allowed = {"", "important", "normal", "low"}
        if importance not in allowed:
            raise ValueError("importance must be one of: important, normal, low, or empty for auto")
        ids = [str(item) for item in ids if item]
        if not ids:
            raise ValueError("No capabilities selected.")
        stamp = now_iso()
        if not importance:
            self.conn.executemany("delete from importance_overrides where capability_id=?", [(item,) for item in ids])
        else:
            self.conn.executemany(
                """
                insert into importance_overrides (capability_id, importance, updated_at)
                values (?, ?, ?)
                on conflict(capability_id) do update set
                  importance=excluded.importance,
                  updated_at=excluded.updated_at
                """,
                [(item, importance, stamp) for item in ids],
            )
        self.log_operation("importance", ",".join(ids[:8]), {"count": len(ids), "importance": importance or "auto"}, commit=False)
        self.conn.commit()
        return len(ids)

    def log_operation(self, action: str, target: str = "", details: Optional[Dict[str, Any]] = None, commit: bool = True) -> None:
        self.conn.execute(
            "insert into operation_logs (occurred_at, action, target, details) values (?, ?, ?, ?)",
            (now_iso(), action, target, json.dumps(details or {}, ensure_ascii=False)),
        )
        if commit:
            self.conn.commit()

    def save_smart_upgrade_snapshot(self, result: Dict[str, Any]) -> None:
        self.conn.execute(
            """
            insert into smart_upgrade_snapshots (snapshot_key, checked_at, result_json)
            values (?, ?, ?)
            on conflict(snapshot_key) do update set
              checked_at=excluded.checked_at,
              result_json=excluded.result_json
            """,
            (SMART_UPGRADE_SNAPSHOT_KEY, now_iso(), json.dumps(result, ensure_ascii=False)),
        )
        self.conn.commit()

    def smart_upgrade_snapshot(self, fresh_only: bool = True) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "select checked_at, result_json from smart_upgrade_snapshots where snapshot_key=?",
            (SMART_UPGRADE_SNAPSHOT_KEY,),
        ).fetchone()
        if not row:
            return None
        checked_at = parse_iso_datetime(row["checked_at"] or "")
        if fresh_only and (not checked_at or dt.datetime.now(dt.timezone.utc).astimezone() - checked_at > REMOTE_UPDATE_CACHE_TTL):
            return None
        try:
            result = json.loads(row["result_json"] or "{}")
        except json.JSONDecodeError:
            return None
        result["snapshot_checked_at"] = row["checked_at"]
        result["snapshot_cache_hit"] = True
        return result

    def operation_logs(self, limit: int = 80) -> List[sqlite3.Row]:
        return self.conn.execute(
            "select occurred_at, action, target, details from operation_logs order by id desc limit ?",
            (limit,),
        ).fetchall()

    def log_use(
        self,
        name_or_id: str,
        platform: str = "",
        source: str = "manual",
        confidence: str = "high",
        evidence: str = "",
    ) -> sqlite3.Row:
        cap = self.find_one(name_or_id, platform)
        if not cap:
            raise SystemExit(f"No capability found for {name_or_id}")
        stamp = now_iso()
        self.conn.execute(
            """
            insert into usage_events
              (capability_id, platform, occurred_at, source, confidence, evidence)
            values (?, ?, ?, ?, ?, ?)
            """,
            (cap["id"], cap["platform"], stamp, source, confidence, evidence),
        )
        self.conn.execute("update capabilities set last_used_at=? where id=?", (stamp, cap["id"]))
        self.conn.commit()
        self.refresh_usage_counts()
        return self.conn.execute("select * from capabilities where id=?", (cap["id"],)).fetchone()

    def record_usage_event(
        self,
        capability_id: str,
        platform: str,
        occurred_at: str,
        source: str,
        confidence: str,
        session_id: str,
        evidence: str,
    ) -> bool:
        before = self.conn.total_changes
        self.conn.execute(
            """
            insert or ignore into usage_events
              (capability_id, platform, occurred_at, source, confidence, session_id, evidence)
            values (?, ?, ?, ?, ?, ?, ?)
            """,
            (capability_id, platform, occurred_at, source, confidence, session_id, evidence),
        )
        return self.conn.total_changes > before

    def record_update_check(self, cap_id: str, status: str, local_hash: str, remote_hash: str, message: str) -> None:
        self.conn.execute(
            """
            insert into update_checks
              (capability_id, checked_at, status, local_hash, remote_hash, message)
            values (?, ?, ?, ?, ?, ?)
            """,
            (cap_id, now_iso(), status, local_hash, remote_hash, message),
        )
        self.conn.commit()


def discover_capabilities(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    caps: List[Dict[str, Any]] = []
    for platform, settings in config.get("platforms", {}).items():
        if not settings.get("enabled", True):
            continue
        seen: set[str] = set()
        for raw_root in settings.get("roots", []):
            root = expand_root(raw_root)
            if not root.exists():
                continue
            for cap in discover_root(platform, root):
                key = cap["platform"] + "|" + cap["path"]
                if key in seen:
                    continue
                seen.add(key)
                caps.append(cap)
    return caps


def source_entries() -> List[Dict[str, Any]]:
    config = load_config()
    registry = Registry()
    rows = registry.all_capabilities()
    entries: List[Dict[str, Any]] = []
    seen: set[Tuple[str, str]] = set()
    for platform, settings in config.get("platforms", {}).items():
        enabled = bool(settings.get("enabled", True))
        for root in settings.get("roots", []):
            expanded_path = expand_root(root)
            expanded = str(expanded_path)
            key = (platform, expanded)
            if key in seen:
                continue
            seen.add(key)
            count = len([row for row in rows if str(row["path"]).startswith(expanded)])
            entries.append({"platform": platform, "root": root, "expanded": expanded, "enabled": enabled, "exists": expanded_path.exists(), "count": count})
    return entries


def update_source(action: str, platform: str, root: str, enabled: Optional[bool] = None) -> Dict[str, Any]:
    platform = platform or "shared"
    root = str(root or "").strip()
    if platform not in DEFAULT_CONFIG["platforms"]:
        raise ValueError(f"Unsupported platform: {platform}")
    if not root:
        raise ValueError("Source root is required.")
    if action == "add" and not expand_root(root).is_dir():
        raise ValueError(f"Source root is not a valid directory: {root}")
    config = load_config()
    settings = config.setdefault("platforms", {}).setdefault(platform, {"enabled": True, "roots": []})
    roots = settings.setdefault("roots", [])
    if action == "add":
        if root not in roots:
            roots.append(root)
    elif action == "remove":
        if root in roots:
            roots.remove(root)
    elif action == "toggle":
        settings["enabled"] = bool(enabled)
    else:
        raise ValueError(f"Unsupported source action: {action}")
    save_config(config)
    Registry().log_operation(f"source-{action}", f"{platform}:{root}", {"platform": platform, "root": root, "enabled": settings.get("enabled", True)})
    return {"sources": source_entries(), "config_path": str(CONFIG_PATH)}


def pick_directory() -> Dict[str, Any]:
    if sys.platform == "darwin":
        script = 'POSIX path of (choose folder with prompt "Choose skill source directory")'
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            raise ValueError("Directory selection was cancelled.")
        path = result.stdout.strip()
    else:
        try:
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            root.withdraw()
            path = filedialog.askdirectory(title="Choose skill source directory")
            root.destroy()
        except Exception as exc:
            raise ValueError(f"Directory picker is unavailable: {exc}") from exc
        if not path:
            raise ValueError("Directory selection was cancelled.")
    selected = Path(path).expanduser().resolve()
    if not selected.is_dir():
        raise ValueError(f"Selected path is not a directory: {selected}")
    return {"path": str(selected)}


def parse_event_time(value: Any) -> Optional[dt.datetime]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp = timestamp / 1000
        try:
            return dt.datetime.fromtimestamp(timestamp, tz=dt.timezone.utc).astimezone()
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = parse_iso_datetime(text)
        if parsed:
            return parsed.astimezone()
    return None


def first_event_time(value: Any) -> Optional[dt.datetime]:
    if isinstance(value, dict):
        for key in ("timestamp", "created_at", "createdAt", "time", "date"):
            parsed = parse_event_time(value.get(key))
            if parsed:
                return parsed
        for key in ("message", "payload", "event", "item"):
            parsed = first_event_time(value.get(key))
            if parsed:
                return parsed
    elif isinstance(value, list):
        for item in value:
            parsed = first_event_time(item)
            if parsed:
                return parsed
    return None


def first_session_id(value: Any, fallback: str) -> str:
    if isinstance(value, dict):
        for key in ("session_id", "sessionId", "conversation_id", "conversationId", "chat_id", "chatId"):
            raw = value.get(key)
            if raw:
                return short(str(raw), 120)
        for key in ("message", "payload", "event"):
            found = first_session_id(value.get(key), "")
            if found:
                return found
    return fallback


def iter_dicts(value: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from iter_dicts(item)
    elif isinstance(value, list):
        for item in value:
            yield from iter_dicts(item)


def is_catalog_or_instruction(value: Any) -> bool:
    try:
        text = json.dumps(value, ensure_ascii=False)
    except Exception:
        text = str(value)
    markers = (
        "<available_skills",
        "</available_skills>",
        "### Available skills",
        "skills_instructions",
        "base_instructions",
        "\"skill_listing\"",
        "'skill_listing'",
    )
    return any(marker in text for marker in markers)


def tool_event_kind(node: Dict[str, Any]) -> str:
    type_value = str(node.get("type") or node.get("kind") or "").lower()
    name_value = str(node.get("name") or node.get("tool") or node.get("toolName") or node.get("tool_name") or "").lower()
    if type_value in ("toolcall", "tool_call", "tool_use", "function_call"):
        if name_value in ("read", "read_file", "filesystem.read_file", "functions.exec_command", "exec_command", "bash", "shell"):
            return "high" if "read" in name_value else "shell"
        return "medium"
    if type_value == "response_item" and name_value:
        return "medium"
    if name_value in ("read", "read_file", "filesystem.read_file"):
        return "high"
    if name_value in ("functions.exec_command", "exec_command", "bash", "shell"):
        return "shell"
    return ""


def shell_text_is_direct_skill_read(text: str) -> bool:
    if not SKILL_MD_PATH_RE.search(text):
        return False
    direct_read = re.search(r"\b(cat|sed|head|tail|less|bat|nl)\b[\s\S]{0,300}?SKILL\.md", text)
    if not direct_read:
        return False
    noisy_search = re.search(r"\b(rg|grep|find|sqlite3)\b[\s\S]{0,300}?SKILL\.md", text)
    return not bool(noisy_search)


def extract_skill_md_paths_from_event(value: Any) -> List[Tuple[str, str]]:
    if is_catalog_or_instruction(value):
        return []
    found: List[Tuple[str, str]] = []
    seen: set[str] = set()
    for node in iter_dicts(value):
        confidence = tool_event_kind(node)
        if not confidence:
            continue
        try:
            text = json.dumps(node, ensure_ascii=False)
        except Exception:
            text = str(node)
        if confidence == "shell":
            if not shell_text_is_direct_skill_read(text):
                continue
            confidence = "medium"
        for match in SKILL_MD_PATH_RE.finditer(text):
            raw = match.group(0).replace("\\/", "/")
            if raw in seen:
                continue
            seen.add(raw)
            found.append((raw, confidence))
    return found


def normalize_skill_md_path(raw: str) -> str:
    clean = raw.strip().replace("\\/", "/")
    if clean.startswith("~"):
        return str(Path(clean).expanduser().resolve())
    return str(Path(clean).resolve())


def usage_log_source(path: Path) -> str:
    text = str(path)
    if "/.codex/" in text:
        return "codex-log"
    if "/.claude/" in text:
        return "claude-log"
    if "/.openclaw/" in text:
        return "openclaw-log"
    if "/.hermes/" in text:
        return "hermes-log"
    return "session-log"


def usage_log_files(days: int = 45) -> List[Path]:
    home = Path.home()
    roots = [
        home / ".codex" / "sessions",
        home / ".codex" / "archived_sessions",
        home / ".claude" / "projects",
        home / ".openclaw" / "agents",
        home / ".hermes" / "sessions",
        home / ".hermes" / "logs",
    ]
    cutoff = dt.datetime.now(dt.timezone.utc).timestamp() - days * 86400
    files: List[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.jsonl"):
            try:
                if path.stat().st_mtime >= cutoff:
                    files.append(path)
            except OSError:
                continue
    return sorted(files)


def capability_skill_path_map(registry: Registry) -> Dict[str, sqlite3.Row]:
    rows = registry.conn.execute("select * from capabilities where kind='skill'").fetchall()
    mapping: Dict[str, sqlite3.Row] = {}
    for row in rows:
        raw = str(row["path"] or "")
        if not raw:
            continue
        path = Path(raw).expanduser()
        skill_md = path if path.name == "SKILL.md" else path / "SKILL.md"
        candidates = [skill_md]
        try:
            candidates.append(skill_md.resolve())
        except OSError:
            pass
        for candidate in candidates:
            mapping[str(candidate)] = row
    return mapping


def import_usage_from_logs(registry: Registry, days: int = 30) -> Dict[str, int]:
    skill_paths = capability_skill_path_map(registry)
    cutoff = dt.datetime.now(dt.timezone.utc).astimezone() - dt.timedelta(days=days)
    stats = {"files": 0, "lines": 0, "matched": 0, "inserted": 0}
    if not skill_paths:
        return stats
    registry.conn.execute(
        "delete from usage_events where source in (?, ?, ?, ?, ?)",
        ("codex-log", "claude-log", "openclaw-log", "hermes-log", "session-log"),
    )
    for log_path in usage_log_files(days + 15):
        stats["files"] += 1
        source = usage_log_source(log_path)
        fallback_session = log_path.stem
        try:
            handle = log_path.open("r", encoding="utf-8", errors="ignore")
        except OSError:
            continue
        with handle:
            for line in handle:
                stats["lines"] += 1
                line = line.strip()
                if not line or "SKILL.md" not in line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                paths = extract_skill_md_paths_from_event(event)
                if not paths:
                    continue
                occurred = first_event_time(event)
                if not occurred:
                    try:
                        occurred = dt.datetime.fromtimestamp(log_path.stat().st_mtime, tz=dt.timezone.utc).astimezone()
                    except OSError:
                        occurred = dt.datetime.now(dt.timezone.utc).astimezone()
                if occurred < cutoff:
                    continue
                session_id = first_session_id(event, fallback_session)
                occurred_iso = occurred.isoformat(timespec="seconds")
                for raw_path, confidence in paths:
                    cap = skill_paths.get(normalize_skill_md_path(raw_path))
                    if not cap:
                        continue
                    stats["matched"] += 1
                    inserted = registry.record_usage_event(
                        cap["id"],
                        cap["platform"],
                        occurred_iso,
                        source,
                        confidence,
                        session_id,
                        f"{log_path.name}: {raw_path}",
                    )
                    if inserted:
                        stats["inserted"] += 1
    registry.conn.commit()
    registry.refresh_usage_counts()
    registry.log_operation("usage-import", "", stats)
    return stats


def management_recommendations() -> Dict[str, Any]:
    registry = Registry()
    rows = [row_to_dict(row) for row in registry.all_capabilities()]
    by_name: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        by_name.setdefault(str(row["name"]).lower(), []).append(row)
    protected_names = {
        key
        for key, group in by_name.items()
        if any(row.get("management_scope") == "builtin_observe_only" for row in group)
    }
    items: List[Dict[str, Any]] = []
    def is_managed(row: Dict[str, Any]) -> bool:
        return row.get("management_scope") in ("managed_remote", "managed_local")
    def has_remote_metadata_gap(row: Dict[str, Any]) -> bool:
        if row.get("management_scope") != "managed_remote":
            return False
        if row.get("source_type") == "github":
            return not bool(row.get("github_hash"))
        return False
    def item_payload(kind: str, severity: str, name: str, group: List[Dict[str, Any]], reason: str) -> Dict[str, Any]:
        return {
            "type": kind,
            "severity": severity,
            "name": name,
            "ids": [row["id"] for row in group],
            "reason": reason,
            "platforms": sorted({row.get("platform") or "" for row in group}),
            "versions": sorted({row.get("version") or "unknown" for row in group}),
            "statuses": sorted({row.get("status") or "unknown" for row in group}),
            "paths": [row.get("path") or "" for row in group],
            "usage_30d": sum(int(row.get("usage_30d") or 0) for row in group),
            "copy_count": len(group),
        }
    for key, group in by_name.items():
        if key in protected_names:
            continue
        group = [row for row in group if is_managed(row)]
        if not group:
            continue
        name = group[0]["name"]
        versions = sorted({row.get("version") or "unknown" for row in group})
        max_local = sorted(versions, key=version_key)[-1] if versions else "unknown"
        update_added = False
        if any(not Path(row["path"]).exists() for row in group):
            items.append(item_payload("delete", "high", name, group, "Path is missing for one or more copies."))
        snapshot = get_remote_snapshot(registry, name, fresh_only=True)
        if snapshot:
            remote_version = str(snapshot.get("remote_version") or "unknown")
            if remote_version != "unknown" and (max_local == "unknown" or version_gt(remote_version, max_local)):
                items.append(item_payload("update", "medium", name, group, f"Remote version is newer: {remote_version}."))
                update_added = True
        if len(versions) > 1:
            if not update_added:
                items.append(item_payload("update", "medium", name, group, f"Local versions differ: {', '.join(versions)}."))
        remote_metadata_gaps = [row for row in group if has_remote_metadata_gap(row)]
        if remote_metadata_gaps:
            items.append(item_payload("metadata", "low", name, remote_metadata_gaps, "Remote update tracking metadata is incomplete."))
        if len(group) > 1:
            paths = {row.get("path") for row in group}
            if len(paths) > 1:
                items.append(item_payload("review", "low", name, group, f"{len(group)} installed copies detected."))
    counts: Dict[str, int] = {}
    for item in items:
        counts[item["type"]] = counts.get(item["type"], 0) + 1
    return {"recommendations": items[:250], "counts": counts}


def python_health_score(row: Dict[str, Any]) -> int:
    status = str(row.get("status") or "")
    if status == "active":
        status_score = 30
    elif status == "inactive":
        status_score = 18
    else:
        status_score = 0

    if row.get("management_scope") == "builtin_observe_only":
        version_score = 8 if row.get("version") and row.get("version") != "unknown" else 6
        description_score = 8 if row.get("description") else 5
        quality_score = version_score + description_score + 14
    else:
        uses = int(row.get("usage_30d") or 0)
        if uses >= 10:
            usage_score = 18
        elif uses >= 3:
            usage_score = 14
        elif uses >= 1:
            usage_score = 10
        else:
            usage_score = 4
        version_score = 4 if row.get("version") and row.get("version") != "unknown" else 0
        description_score = 3 if row.get("description") else 0
        if row.get("source_type") == "github":
            tracked_score = 5 if row.get("github_url") and row.get("github_hash") else 1
        else:
            tracked_score = 5 if row.get("source_type") else 0
        quality_score = usage_score + version_score + description_score + tracked_score

    health = str(row.get("health") or "")
    if health == "ok":
        structure_score = 40
    elif health == "warning":
        structure_score = 25
    elif health == "unknown":
        structure_score = 20
    elif health == "metadata-error":
        structure_score = 10
    else:
        structure_score = 0
    return status_score + quality_score + structure_score


def smart_upgrade_check(progress: Optional[Any] = None, use_cache: bool = True) -> Dict[str, Any]:
    if use_cache:
        cache_registry = Registry()
        usage_import = import_usage_from_logs(cache_registry)
        cached = cache_registry.smart_upgrade_snapshot(fresh_only=True)
        if cached:
            cached["usage_import"] = usage_import
            return cached
    if progress:
        progress("scan", "", 0, 0)
    registry = Registry()
    caps = discover_capabilities(load_config())
    for cap in caps:
        registry.upsert_capability(cap)
    registry.prune_to_scan(caps)
    registry.log_operation("smart-upgrade-scan", "", {"count": len(caps)}, commit=False)
    registry.conn.commit()
    if progress:
        progress("usage", "", 0, 0)
    usage_import = import_usage_from_logs(registry)

    rows = [row_to_dict(row) for row in registry.all_capabilities("kind='skill'")]
    by_name: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        by_name.setdefault(str(row["name"]).lower(), []).append(row)

    def managed_rows(group: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [
            row for row in group
            if row.get("management_scope") in ("managed_remote", "managed_local")
            and row.get("source_type") != "builtin"
        ]

    def payload(kind: str, severity: str, name: str, group: List[Dict[str, Any]], reason: str) -> Dict[str, Any]:
        return {
            "type": kind,
            "severity": severity,
            "name": name,
            "ids": [row["id"] for row in group],
            "reason": reason,
            "platforms": sorted({row.get("platform") or "" for row in group}),
            "versions": sorted({row.get("version") or "unknown" for row in group}),
            "statuses": sorted({row.get("status") or "unknown" for row in group}),
            "paths": [row.get("path") or "" for row in group],
            "usage_30d": sum(int(row.get("usage_30d") or 0) for row in group),
            "copy_count": len(group),
        }

    items: List[Dict[str, Any]] = []
    update_results: List[Dict[str, Any]] = []
    update_groups = [(key, group) for key, group in sorted(by_name.items(), key=lambda entry: entry[0]) if managed_rows(group)]
    def update_group_result(group: List[Dict[str, Any]]) -> Tuple[str, List[Dict[str, Any]], Dict[str, Any], Optional[Dict[str, Any]]]:
        candidates = managed_rows(group)
        name = candidates[0]["name"]
        try:
            result = check_group_update([row["id"] for row in candidates], force_remote=False, remember_terminal=False)
            summary = {
                "name": name,
                "status": result.get("status"),
                "cache_hit": bool(result.get("cache_hit")),
                "checked_at": result.get("checked_at", ""),
            }
            if result.get("status") == "remote_update":
                remote_version = str(result.get("remote_version") or "unknown")
                return name, candidates, summary, payload("update", "medium", name, candidates, f"Remote version is newer: {remote_version}.")
            if result.get("status") == "local_mismatch":
                versions = ", ".join(result.get("local_versions") or sorted({row.get("version") or "unknown" for row in candidates}))
                return name, candidates, summary, payload("update", "medium", name, candidates, f"Local versions differ: {versions}.")
            return name, candidates, summary, None
        except Exception as exc:
            return name, candidates, {"name": name, "status": "error", "message": str(exc)}, payload("metadata", "low", name, candidates, f"Update check failed: {exc}")

    checked_updates = 0
    if update_groups:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, len(update_groups))) as executor:
            futures = [executor.submit(update_group_result, group) for _, group in update_groups]
            for future in concurrent.futures.as_completed(futures):
                name, _candidates, summary, item = future.result()
                checked_updates += 1
                if progress:
                    progress("update", str(name), checked_updates, len(update_groups))
                update_results.append(summary)
                if item:
                    items.append(item)

    if progress:
        progress("health", "", 0, 0)
    health_result = run_health_check()
    health_issue_names = {
        str(row.get("name") or "").lower()
        for row in health_result.get("issues", [])
        if row.get("health") != "ok" or row.get("message")
    }
    refreshed = [row_to_dict(row) for row in Registry().all_capabilities("kind='skill'")]
    refreshed_by_name: Dict[str, List[Dict[str, Any]]] = {}
    for row in refreshed:
        refreshed_by_name.setdefault(str(row["name"]).lower(), []).append(row)

    health_groups = sorted(refreshed_by_name.items(), key=lambda entry: entry[0])
    for index, (_, group) in enumerate(health_groups, start=1):
        if progress:
            progress("health", str(group[0]["name"]), index, len(health_groups))
        scores = [python_health_score(row) for row in group]
        score = round(sum(scores) / len(scores)) if scores else 0
        has_health_issue = str(group[0]["name"] or "").lower() in health_issue_names
        if score < 60 or has_health_issue:
            reason = "Health score is below 60." if score < 60 else "Health check found issues."
            item = payload("health", "medium", group[0]["name"], group, reason)
            item["score"] = score
            items.append(item)

    metadata_groups = sorted(refreshed_by_name.items(), key=lambda entry: entry[0])
    for index, (_, group) in enumerate(metadata_groups, start=1):
        if progress:
            progress("metadata", str(group[0]["name"]), index, len(metadata_groups))
        candidates = managed_rows(group)
        if candidates:
            remote_metadata_gaps = [
                row for row in candidates
                if row.get("management_scope") == "managed_remote"
                and row.get("source_type") == "github"
                and not row.get("github_hash")
            ]
            if remote_metadata_gaps:
                items.append(payload("metadata", "low", group[0]["name"], remote_metadata_gaps, "Remote update tracking metadata is incomplete."))

    duplicate_groups = sorted(refreshed_by_name.items(), key=lambda entry: entry[0])
    for index, (_, group) in enumerate(duplicate_groups, start=1):
        if progress:
            progress("duplicates", str(group[0]["name"]), index, len(duplicate_groups))
        candidates = managed_rows(group)
        if len(candidates) > 1:
            paths = {row.get("path") for row in candidates}
            if len(paths) > 1:
                items.append(payload("review", "low", group[0]["name"], candidates, f"{len(candidates)} installed copies detected."))

    counts: Dict[str, int] = {}
    deduped: List[Dict[str, Any]] = []
    seen: set[Tuple[str, str, str]] = set()
    for item in items:
        key = (str(item.get("type")), str(item.get("name")).lower(), "|".join(sorted(item.get("ids") or [])))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
        counts[item["type"]] = counts.get(item["type"], 0) + 1
    registry.log_operation("smart-upgrade", "", {"items": len(deduped), "counts": counts, "updates_checked": len(update_results)})
    result = {
        "recommendations": deduped[:500],
        "counts": counts,
        "checked": {
            "skills": len(refreshed_by_name),
            "copies": len(refreshed),
            "updates": len(update_results),
            "health": health_result.get("summary", {}),
        },
        "update_results": update_results,
        "usage_import": usage_import,
    }
    registry.save_smart_upgrade_snapshot(result)
    return result


def update_smart_job(job_id: str, **updates: Any) -> None:
    with SMART_UPGRADE_LOCK:
        job = SMART_UPGRADE_JOBS.setdefault(job_id, {})
        job.update(updates)
        job["updated_at"] = now_iso()


def run_smart_upgrade_job(job_id: str) -> None:
    def progress(stage: str, current: str, index: int, total: int) -> None:
        update_smart_job(job_id, status="running", stage=stage, current=current, index=index, total=total)

    try:
        result = smart_upgrade_check(progress, use_cache=False)
        update_smart_job(job_id, status="done", stage="done", current="", result=result)
    except Exception as exc:
        update_smart_job(job_id, status="error", stage="error", current="", error=str(exc))


def start_smart_upgrade_job() -> Dict[str, Any]:
    cache_registry = Registry()
    usage_import = import_usage_from_logs(cache_registry)
    cached = cache_registry.smart_upgrade_snapshot(fresh_only=True)
    job_id = f"smart-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d%H%M%S%f')}"
    if cached:
        cached["usage_import"] = usage_import
        with SMART_UPGRADE_LOCK:
            SMART_UPGRADE_JOBS[job_id] = {
                "id": job_id,
                "status": "done",
                "stage": "done",
                "current": "",
                "index": (cached.get("checked") or {}).get("skills", 0),
                "total": (cached.get("checked") or {}).get("skills", 0),
                "created_at": now_iso(),
                "updated_at": now_iso(),
                "result": cached,
                "cache_hit": True,
            }
        return {"job_id": job_id, "cache_hit": True}
    with SMART_UPGRADE_LOCK:
        SMART_UPGRADE_JOBS[job_id] = {
            "id": job_id,
            "status": "running",
            "stage": "scan",
            "current": "",
            "index": 0,
            "total": 0,
            "created_at": now_iso(),
            "updated_at": now_iso(),
        }
    thread = threading.Thread(target=run_smart_upgrade_job, args=(job_id,), daemon=True)
    thread.start()
    return {"job_id": job_id}


def smart_upgrade_job_status(job_id: str) -> Dict[str, Any]:
    with SMART_UPGRADE_LOCK:
        job = SMART_UPGRADE_JOBS.get(job_id)
        if not job:
            raise ValueError("Smart upgrade job was not found.")
        return dict(job)


def source_discovery_status(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    candidates = [
        row
        for row in rows
        if str(row.get("name") or "").lower() == "find-skills"
        and Path(str(row.get("path") or "")).exists()
    ]
    if candidates:
        candidate = sorted(candidates, key=lambda row: len(str(row.get("path") or "")))[0]
        return {
            "available": True,
            "provider": "find-skills",
            "path": candidate.get("path") or "",
            "message": "find-skills is installed. Source discovery can use it as an optional helper.",
        }
    return {
        "available": False,
        "provider": "",
        "path": "",
        "message": "find-skills is not installed. Existing source metadata, manual source binding, and local version unification still work.",
    }


def discover_root(platform: str, root: Path) -> Iterable[Dict[str, Any]]:
    if platform in ("codex", "shared"):
        yield from discover_skill_root(platform, root)
    else:
        yield from discover_recursive_skill_root(platform, root)


def discover_skill_root(platform: str, root: Path) -> Iterable[Dict[str, Any]]:
    if root.is_file():
        return
    if root.name == "cache" or "plugins/cache" in str(root):
        for skill_file in bounded_rglob(root, "SKILL.md", max_depth=8):
            yield capability_from_skill_md(platform, skill_file.parent, skill_file)
        return
    for child in sorted(root.iterdir()):
        if child.is_dir():
            skill_file = child / "SKILL.md"
            if skill_file.exists():
                yield capability_from_skill_md(platform, child, skill_file)


def discover_recursive_skill_root(platform: str, root: Path) -> Iterable[Dict[str, Any]]:
    if root.is_file():
        return
    for skill_file in bounded_rglob(root, "SKILL.md", max_depth=8):
        yield capability_from_skill_md(platform, skill_file.parent, skill_file)


def discover_claude_code(root: Path) -> Iterable[Dict[str, Any]]:
    yield from discover_recursive_skill_root("claude_code", root)


def discover_generic_platform(platform: str, root: Path) -> Iterable[Dict[str, Any]]:
    yield from discover_recursive_skill_root(platform, root)


def bounded_rglob(root: Path, pattern: str, max_depth: int) -> Iterable[Path]:
    root = root.resolve()
    for path in root.rglob(pattern):
        try:
            rel_parts = path.resolve().relative_to(root).parts
        except ValueError:
            continue
        if len(rel_parts) <= max_depth:
            yield path


def capability_from_skill_md(platform: str, folder: Path, skill_file: Path) -> Dict[str, Any]:
    frontmatter = parse_frontmatter(skill_file)
    meta = read_json(folder / "_meta.json")
    metadata = {**meta, **frontmatter}
    name = str(metadata.get("name") or folder.name)
    version = infer_version(folder.name, metadata)
    if version == "unknown":
        version = version_from_path(folder) or "unknown"
    return {
        "id": stable_id(platform, "skill", folder, name),
        "name": name,
        "kind": "skill",
        "platform": platform,
        "path": str(folder),
        "description": str(metadata.get("description") or ""),
        "version": version,
        "source_type": normalize_source(metadata, folder),
        "github_url": str(metadata.get("github_url") or ""),
        "github_hash": str(metadata.get("github_hash") or ""),
        "github_ref": str(metadata.get("github_ref") or ""),
        "github_path": str(metadata.get("github_path") or ""),
        "health": "ok" if skill_file.exists() else "missing-skill-md",
        "local_updated_at": str(metadata.get("local_updated_at") or ""),
        "last_scanned_at": now_iso(),
        "metadata": metadata,
    }


def capability_from_markdown(platform: str, path: Path) -> Dict[str, Any]:
    metadata = parse_frontmatter(path)
    name = str(metadata.get("name") or path.stem)
    kind = str(metadata.get("kind") or infer_kind_from_path(path))
    version = str(metadata.get("version") or "") or version_from_path(path) or "unknown"
    return {
        "id": stable_id(platform, kind, path, name),
        "name": name,
        "kind": kind,
        "platform": platform,
        "path": str(path),
        "description": str(metadata.get("description") or ""),
        "version": version,
        "source_type": normalize_source(metadata, path),
        "github_url": str(metadata.get("github_url") or ""),
        "github_hash": str(metadata.get("github_hash") or ""),
        "github_ref": str(metadata.get("github_ref") or ""),
        "github_path": str(metadata.get("github_path") or ""),
        "health": "ok",
        "local_updated_at": str(metadata.get("local_updated_at") or ""),
        "last_scanned_at": now_iso(),
        "metadata": metadata,
    }


def capability_from_json_manifest(platform: str, path: Path) -> Dict[str, Any]:
    metadata = read_json(path)
    if "json_type" in metadata and len(metadata) == 1:
        raise ValueError(f"Skipping non-object JSON manifest: {path}")
    name = str(metadata.get("name") or metadata.get("id") or path.stem)
    kind = str(metadata.get("kind") or metadata.get("type") or infer_kind_from_path(path))
    version = str(metadata.get("version") or "") or version_from_path(path) or "unknown"
    return {
        "id": stable_id(platform, kind, path, name),
        "name": name,
        "kind": kind,
        "platform": platform,
        "path": str(path),
        "description": str(metadata.get("description") or ""),
        "version": version,
        "source_type": normalize_source(metadata, path),
        "github_url": str(metadata.get("github_url") or metadata.get("repository") or ""),
        "github_hash": str(metadata.get("github_hash") or ""),
        "github_ref": str(metadata.get("github_ref") or ""),
        "github_path": str(metadata.get("github_path") or ""),
        "health": "ok" if "meta_error" not in metadata else "metadata-error",
        "local_updated_at": str(metadata.get("local_updated_at") or ""),
        "last_scanned_at": now_iso(),
        "metadata": metadata,
    }


def infer_kind_from_path(path: Path) -> str:
    text = str(path).replace("\\", "/").lower()
    if "/commands/" in text:
        return "command"
    if "/agents/" in text:
        return "agent"
    if "/workflows/" in text:
        return "workflow"
    if "/plugins/" in text:
        return "plugin"
    if "/tools/" in text:
        return "tool"
    return "unknown"


def scan_command(args: argparse.Namespace) -> None:
    registry = Registry()
    caps = discover_capabilities(load_config())
    for cap in caps:
        registry.upsert_capability(cap)
    registry.prune_to_scan(caps)
    registry.conn.commit()
    usage = import_usage_from_logs(registry)
    print(f"Scanned {len(caps)} capabilities into {DB_PATH}")
    print(f"Imported {usage['inserted']} real usage events from {usage['files']} local log files.")


def row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    data = {key: row[key] for key in row.keys()}
    try:
        metadata = json.loads(data.get("metadata_json") or "{}")
    except Exception:
        metadata = {}
    data["management_scope"] = management_scope_for_row({**data, "metadata": metadata})
    return data


def print_table(rows: Sequence[Dict[str, Any]], columns: Sequence[Tuple[str, str]]) -> None:
    if not rows:
        print("_No rows._")
        return
    widths = []
    for key, label in columns:
        max_len = max([len(label)] + [len(str(row.get(key, ""))) for row in rows])
        widths.append(min(max_len, 48))
    header = " | ".join(label.ljust(widths[i]) for i, (_, label) in enumerate(columns))
    print(header)
    print("-+-".join("-" * width for width in widths))
    for row in rows:
        values = []
        for i, (key, _) in enumerate(columns):
            values.append(short(str(row.get(key, "")), widths[i]).ljust(widths[i]))
        print(" | ".join(values))


def list_command(args: argparse.Namespace) -> None:
    registry = Registry()
    where = []
    params: List[Any] = []
    if args.platform:
        where.append("platform=?")
        params.append(args.platform)
    if args.status:
        where.append("status=?")
        params.append(args.status)
    rows = [row_to_dict(row) for row in registry.all_capabilities(" and ".join(where), params)]
    for row in rows:
        row["description"] = short(row.get("description"), 55)
    print_table(
        rows,
        [
            ("platform", "Platform"),
            ("kind", "Kind"),
            ("name", "Name"),
            ("status", "Status"),
            ("health", "Health"),
            ("version", "Version"),
            ("usage_30d", "30d"),
            ("source_type", "Source"),
            ("description", "Description"),
        ],
    )


def search_command(args: argparse.Namespace) -> None:
    registry = Registry()
    needle = f"%{args.query}%"
    rows = [
        row_to_dict(row)
        for row in registry.all_capabilities(
            "(name like ? or description like ? or path like ?)",
            (needle, needle, needle),
        )
    ]
    print_table(rows, [("platform", "Platform"), ("kind", "Kind"), ("name", "Name"), ("status", "Status"), ("path", "Path")])


def show_command(args: argparse.Namespace) -> None:
    registry = Registry()
    row = registry.find_one(args.name, args.platform or "")
    if not row:
        raise SystemExit(f"No capability found for {args.name}")
    data = row_to_dict(row)
    try:
        data["metadata"] = json.loads(data.pop("metadata_json") or "{}")
    except Exception:
        data["metadata"] = {}
    print(json.dumps(data, indent=2, ensure_ascii=False))


def status_command(args: argparse.Namespace, status: str) -> None:
    registry = Registry()
    row = registry.set_status(args.name, status, args.platform or "")
    print(f"{row['platform']} {row['kind']} {row['name']} -> {status}")


def log_use_command(args: argparse.Namespace) -> None:
    registry = Registry()
    row = registry.log_use(args.name, args.platform or "", args.source, args.confidence, args.evidence or "")
    print(f"Logged use for {row['platform']} {row['name']} at {row['last_used_at']}")


def usage_command(args: argparse.Namespace) -> None:
    registry = Registry()
    since = (dt.datetime.now(dt.timezone.utc).astimezone() - dt.timedelta(days=args.days)).isoformat(timespec="seconds")
    rows = registry.conn.execute(
        """
        select c.platform, c.kind, c.name, count(u.id) uses,
               max(u.occurred_at) last_used_at,
               group_concat(distinct u.source) sources
        from capabilities c
        left join usage_events u on u.capability_id = c.id and u.occurred_at >= ?
        group by c.id
        order by uses desc, c.platform, c.name
        """,
        (since,),
    ).fetchall()
    print_table(
        [row_to_dict(row) for row in rows],
        [("platform", "Platform"), ("kind", "Kind"), ("name", "Name"), ("uses", f"{args.days}d uses"), ("last_used_at", "Last Used"), ("sources", "Sources")],
    )


def parse_github_url(url: str) -> Optional[Dict[str, str]]:
    if not url:
        return None
    match = re.search(r"github\.com[:/]([^/\s]+)/([^/\s#]+)", url)
    if not match:
        return None
    repo = re.sub(r"\.git$", "", match.group(2))
    info = {"owner": match.group(1), "repo": repo, "ref": "", "path": ""}
    tree = re.search(r"github\.com/[^/]+/[^/]+/(?:tree|blob)/([^/]+)(?:/(.*))?", url)
    if tree:
        info["ref"] = tree.group(1)
        info["path"] = tree.group(2) or ""
    return info


def github_json(uri: str) -> Any:
    headers = {"User-Agent": APP_NAME, "Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(uri, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        if "CERTIFICATE_VERIFY_FAILED" not in str(exc):
            raise
        with urllib.request.urlopen(req, timeout=20, context=ssl._create_unverified_context()) as response:
            return json.loads(response.read().decode("utf-8"))


def latest_github_hash(row: sqlite3.Row) -> Tuple[str, str]:
    info = parse_github_url(row["github_url"])
    if not info:
        raise ValueError(f"Unsupported GitHub URL: {row['github_url']}")
    repo_api = f"https://api.github.com/repos/{info['owner']}/{info['repo']}"
    branch = row["github_ref"] or info["ref"]
    if not branch:
        branch = github_json(repo_api)["default_branch"]
    path = row["github_path"] or info["path"]
    candidate_paths = [path] if path else []
    if not candidate_paths:
        folder = Path(row["path"]).name
        candidate_paths.extend([f"skills/{folder}/SKILL.md", f"{folder}/SKILL.md"])
    for candidate in candidate_paths:
        if not candidate:
            continue
        encoded = urllib.parse.quote(candidate, safe="")
        commits = github_json(f"{repo_api}/commits?sha={urllib.parse.quote(branch)}&path={encoded}&per_page=1")
        if commits:
            return commits[0]["sha"], f"path:{candidate}"
    commit = github_json(f"{repo_api}/commits/{urllib.parse.quote(branch)}")
    return commit["sha"], f"branch:{branch}"


def metadata_for_row(row: sqlite3.Row) -> Dict[str, Any]:
    try:
        return json.loads(row["metadata_json"] or "{}")
    except Exception:
        return {}


def github_info_for_cap(row: sqlite3.Row) -> Optional[Dict[str, str]]:
    metadata = metadata_for_row(row)
    repository = metadata.get("repository", "")
    if isinstance(repository, dict):
        repository = repository.get("url", "")
    urls = [
        row["github_url"],
        metadata.get("github_url", ""),
        metadata.get("homepage", ""),
        repository,
    ]
    for url in urls:
        info = parse_github_url(str(url or ""))
        if info:
            return info
    return None


def version_key(version: str) -> Tuple[int, ...]:
    numbers = [int(part) for part in re.findall(r"\d+", version or "")]
    return tuple(numbers or [0])


def version_gt(a: str, b: str) -> bool:
    return version_key(a) > version_key(b)


def download_github_archive(info: Dict[str, str], branch: str, tmpdir: Path) -> Path:
    url = f"https://api.github.com/repos/{info['owner']}/{info['repo']}/zipball/{urllib.parse.quote(branch)}"
    headers = {"User-Agent": APP_NAME}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    archive = tmpdir / "repo.zip"
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            archive.write_bytes(response.read())
    except urllib.error.URLError as exc:
        if "CERTIFICATE_VERIFY_FAILED" not in str(exc):
            raise
        with urllib.request.urlopen(req, timeout=60, context=ssl._create_unverified_context()) as response:
            archive.write_bytes(response.read())
    extract_dir = tmpdir / "repo"
    extract_dir.mkdir()
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(extract_dir)
    roots = [path for path in extract_dir.iterdir() if path.is_dir()]
    return roots[0] if roots else extract_dir


def latest_commit_for_repo_path(info: Dict[str, str], branch: str, rel_path: str) -> str:
    repo_api = f"https://api.github.com/repos/{info['owner']}/{info['repo']}"
    encoded = urllib.parse.quote(rel_path, safe="")
    commits = github_json(f"{repo_api}/commits?sha={urllib.parse.quote(branch)}&path={encoded}&per_page=1")
    return commits[0]["sha"] if commits else ""


def find_remote_skill_dir(repo_root: Path, skill_name: str, local_folder: str) -> Optional[Path]:
    candidates: List[Tuple[int, Path]] = []
    for skill_file in repo_root.rglob("SKILL.md"):
        folder = skill_file.parent
        metadata = parse_frontmatter(skill_file)
        score = 0
        if str(metadata.get("name", "")).lower() == skill_name.lower():
            score += 100
        if folder.name.lower() == local_folder.lower():
            score += 80
        if folder.parent.name.lower() in ("skills", "skill"):
            score += 10
        if score > 0:
            candidates.append((score, folder))
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: item[0], reverse=True)[0][1]


def copy_tree_contents(source: Path, target: Path) -> None:
    if not source.is_dir():
        raise ValueError(f"Remote source is not a directory: {source}")
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    for child in source.iterdir():
        dest = target / child.name
        if child.is_dir():
            shutil.copytree(child, dest)
        else:
            shutil.copy2(child, dest)


def unique_backup_path(prefix: str, row_id: str) -> Path:
    suffix = hashlib.sha1(row_id.encode("utf-8")).hexdigest()[:8]
    base = BACKUP_DIR / f"{prefix}--{suffix}"
    if not base.exists():
        return base
    counter = 2
    while True:
        candidate = BACKUP_DIR / f"{prefix}--{suffix}-{counter}"
        if not candidate.exists():
            return candidate
        counter += 1


def backup_for_update(row: sqlite3.Row, reason: str) -> Path:
    source = Path(row["path"])
    if not source.exists():
        raise ValueError(f"Path does not exist: {source}")
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", row["name"]).strip("-") or "capability"
    dest = unique_backup_path(f"{row['platform']}--{safe_name}--{reason}--{stamp}", row["id"])
    if source.is_dir():
        shutil.copytree(source, dest)
    else:
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest / source.name)
    return dest


def rows_for_ids(registry: Registry, ids: Sequence[str]) -> List[sqlite3.Row]:
    rows: List[sqlite3.Row] = []
    for cap_id in ids:
        row = registry.find_one(cap_id)
        if row:
            rows.append(row)
    return rows


def is_editable_skill_dir(row: sqlite3.Row) -> bool:
    path = Path(row["path"])
    return row["source_type"] != "builtin" and path.is_dir() and (path / "SKILL.md").exists()


def normalized_skill_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")


def local_update_fallback(
    rows: Sequence[sqlite3.Row],
    local_versions: Sequence[str],
    max_local: str,
    message: str,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    result = {
        "status": "local_mismatch" if len(local_versions) > 1 else "unsupported",
        "message": message,
        "local_versions": list(local_versions),
        "max_local_version": max_local,
        "targets": [row_to_dict(row) for row in rows],
    }
    if extra:
        result.update(extra)
    return result


def strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def discover_remote_with_find_skills(skill_name: str) -> Optional[Dict[str, str]]:
    try:
        result = subprocess.run(
            ["npx", "--yes", "skills", "find", skill_name],
            capture_output=True,
            text=True,
            timeout=25,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = strip_ansi((result.stdout or "") + "\n" + (result.stderr or ""))
    candidates: List[Tuple[int, Dict[str, str]]] = []
    for match in re.finditer(r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)@([A-Za-z0-9_.-]+)", output):
        owner, repo, remote_skill = match.groups()
        score = 0
        if remote_skill.lower() == skill_name.lower():
            score += 100
        if skill_name.lower() in remote_skill.lower():
            score += 20
        if owner.lower() in ("anthropics", "openai", "vercel-labs", "nousresearch"):
            score += 10
        if score > 0:
            candidates.append((score, {"owner": owner, "repo": repo, "ref": "", "path": "", "skill": remote_skill}))
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: item[0], reverse=True)[0][1]


def remote_cache_payload(
    status: str,
    message: str,
    repo: str = "",
    branch: str = "",
    remote_version: str = "unknown",
    remote_hash: str = "",
    remote_path: str = "",
    origin: str = "",
) -> str:
    return json.dumps(
        {
            "kind": "remote_update_cache",
            "status": status,
            "message": message,
            "repo": repo,
            "branch": branch,
            "remote_version": remote_version,
            "remote_hash": remote_hash,
            "remote_path": remote_path,
            "origin": origin,
        },
        ensure_ascii=False,
    )


def upsert_remote_snapshot(registry: Registry, snapshot: Dict[str, Any]) -> Dict[str, Any]:
    stamp = now_iso()
    data = {
        "normalized_name": normalized_skill_name(str(snapshot.get("skill_name") or "")),
        "skill_name": str(snapshot.get("skill_name") or ""),
        "source_type": str(snapshot.get("source_type") or "github"),
        "source_url": str(snapshot.get("source_url") or ""),
        "source_ref": str(snapshot.get("source_ref") or ""),
        "source_path": str(snapshot.get("source_path") or ""),
        "remote_version": str(snapshot.get("remote_version") or "unknown"),
        "remote_hash": str(snapshot.get("remote_hash") or ""),
        "checked_at": stamp,
        "discovered_by": str(snapshot.get("discovered_by") or ""),
        "confidence": str(snapshot.get("confidence") or ""),
        "message": str(snapshot.get("message") or ""),
    }
    registry.conn.execute(
        """
        insert into remote_snapshots (
          normalized_name, skill_name, source_type, source_url, source_ref, source_path,
          remote_version, remote_hash, checked_at, discovered_by, confidence, message
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(normalized_name) do update set
          skill_name=excluded.skill_name,
          source_type=excluded.source_type,
          source_url=excluded.source_url,
          source_ref=excluded.source_ref,
          source_path=excluded.source_path,
          remote_version=excluded.remote_version,
          remote_hash=excluded.remote_hash,
          checked_at=excluded.checked_at,
          discovered_by=excluded.discovered_by,
          confidence=excluded.confidence,
          message=excluded.message
        """,
        (
            data["normalized_name"],
            data["skill_name"],
            data["source_type"],
            data["source_url"],
            data["source_ref"],
            data["source_path"],
            data["remote_version"],
            data["remote_hash"],
            data["checked_at"],
            data["discovered_by"],
            data["confidence"],
            data["message"],
        ),
    )
    registry.conn.commit()
    return data


def get_remote_snapshot(registry: Registry, skill_name: str, fresh_only: bool = True) -> Optional[Dict[str, Any]]:
    row = registry.conn.execute(
        "select * from remote_snapshots where normalized_name=?",
        (normalized_skill_name(skill_name),),
    ).fetchone()
    if not row:
        return None
    data = dict(row)
    checked_at = parse_iso_datetime(data.get("checked_at") or "")
    if fresh_only and (not checked_at or dt.datetime.now(dt.timezone.utc).astimezone() - checked_at > REMOTE_UPDATE_CACHE_TTL):
        return None
    return data


def remote_info_from_snapshot(snapshot: Dict[str, Any]) -> Optional[Dict[str, str]]:
    if snapshot.get("source_type") != "github":
        return None
    info = parse_github_url(str(snapshot.get("source_url") or ""))
    if not info:
        return None
    info["ref"] = str(snapshot.get("source_ref") or info.get("ref") or "")
    info["path"] = str(snapshot.get("source_path") or info.get("path") or "")
    return info


def snapshot_result(
    rows: Sequence[sqlite3.Row],
    local_versions: Sequence[str],
    max_local: str,
    snapshot: Dict[str, Any],
    skipped: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    remote_version = str(snapshot.get("remote_version") or "unknown")
    remote_hash = str(snapshot.get("remote_hash") or "")
    local_hashes = [row["github_hash"] for row in rows if row["github_hash"]]
    hash_newer = bool(remote_hash and local_hashes and any(not remote_hash.startswith(local_hash) and remote_hash != local_hash for local_hash in local_hashes))
    version_newer = remote_version != "unknown" and (max_local == "unknown" or version_gt(remote_version, max_local))
    status = "remote_update" if version_newer or hash_newer else ("local_mismatch" if len(local_versions) > 1 else "latest")
    return {
        "status": status,
        "message": "Remote version snapshot loaded.",
        "repo": str(snapshot.get("source_url") or ""),
        "branch": str(snapshot.get("source_ref") or ""),
        "remote_version": remote_version,
        "remote_hash": remote_hash,
        "local_versions": list(local_versions),
        "max_local_version": max_local,
        "targets": [row_to_dict(row) for row in rows],
        "skipped_targets": list(skipped),
        "cache_hit": True,
        "checked_at": str(snapshot.get("checked_at") or ""),
        "source_path": str(snapshot.get("source_path") or ""),
    }


def fetch_remote_snapshot_for_info(
    row: sqlite3.Row,
    info: Dict[str, str],
    discovered_by: str,
    registry: Registry,
) -> Dict[str, Any]:
    repo_api = f"https://api.github.com/repos/{info['owner']}/{info['repo']}"
    branch = row["github_ref"] or info.get("ref") or github_json(repo_api)["default_branch"]
    with tempfile.TemporaryDirectory(prefix="asm-update-") as tmp:
        repo_root = download_github_archive(info, branch, Path(tmp))
        remote_dir = find_remote_skill_dir(repo_root, row["name"], Path(row["path"]).name)
        if not remote_dir:
            raise ValueError(f"No cloud version was found. Repository exists, but no matching SKILL.md directory was found in {info['owner']}/{info['repo']}.")
        metadata = parse_frontmatter(remote_dir / "SKILL.md")
        remote_version = str(metadata.get("version") or "unknown")
        rel_path = remote_dir.relative_to(repo_root).as_posix()
        remote_hash = latest_commit_for_repo_path(info, branch, rel_path)
        return upsert_remote_snapshot(
            registry,
            {
                "skill_name": row["name"],
                "source_type": "github",
                "source_url": f"https://github.com/{info['owner']}/{info['repo']}",
                "source_ref": branch,
                "source_path": rel_path,
                "remote_version": remote_version,
                "remote_hash": remote_hash,
                "discovered_by": discovered_by,
                "confidence": "high" if discovered_by == "metadata" else "medium",
                "message": "Remote version snapshot refreshed.",
            },
        )


def latest_remote_update_cache(registry: Registry, cap_id: str) -> Optional[Dict[str, Any]]:
    row = registry.conn.execute(
        """
        select checked_at, status, local_hash, remote_hash, message
        from update_checks
        where capability_id=?
        order by id desc
        limit 1
        """,
        (cap_id,),
    ).fetchone()
    if not row:
        return None
    checked_at = parse_iso_datetime(row["checked_at"])
    if not checked_at:
        return None
    if dt.datetime.now(dt.timezone.utc).astimezone() - checked_at > REMOTE_UPDATE_CACHE_TTL:
        return None
    try:
        payload = json.loads(row["message"] or "{}")
    except json.JSONDecodeError:
        return None
    if payload.get("kind") != "remote_update_cache":
        return None
    payload["checked_at"] = row["checked_at"]
    payload["remote_hash"] = payload.get("remote_hash") or row["remote_hash"] or ""
    return payload


def remote_result_from_cache(
    rows: Sequence[sqlite3.Row],
    local_versions: Sequence[str],
    max_local: str,
    cached: Dict[str, Any],
) -> Dict[str, Any]:
    status = str(cached.get("status") or "unsupported")
    remote_version = str(cached.get("remote_version") or "unknown")
    remote_hash = str(cached.get("remote_hash") or "")
    local_hashes = [row["github_hash"] for row in rows if row["github_hash"]]
    hash_newer = bool(remote_hash and local_hashes and any(not remote_hash.startswith(local_hash) and remote_hash != local_hash for local_hash in local_hashes))
    remote_newer = (remote_version != "unknown" and (max_local == "unknown" or version_gt(remote_version, max_local))) or hash_newer
    if status == "remote_checked":
        result_status = "remote_update" if remote_newer else ("local_mismatch" if len(local_versions) > 1 else "latest")
    else:
        result_status = "local_mismatch" if len(local_versions) > 1 else "latest"
    return {
        "status": result_status,
        "message": str(cached.get("message") or "Remote check loaded from local cache."),
        "repo": str(cached.get("repo") or ""),
        "branch": str(cached.get("branch") or ""),
        "remote_version": remote_version,
        "remote_hash": remote_hash,
        "local_versions": list(local_versions),
        "max_local_version": max_local,
        "targets": [row_to_dict(row) for row in rows],
        "cache_hit": True,
        "checked_at": str(cached.get("checked_at") or ""),
    }


def remember_update_result(registry: Registry, cap_id: str, result: Dict[str, Any], local_hash: str = "", origin: str = "update-button") -> None:
    status = str(result.get("status") or "")
    if status not in UPDATE_BUTTON_DISABLED_STATUSES:
        return
    registry.record_update_check(
        cap_id,
        status,
        local_hash,
        str(result.get("remote_hash") or ""),
        remote_cache_payload(
            status,
            str(result.get("message") or status),
            repo=str(result.get("repo") or ""),
            branch=str(result.get("branch") or ""),
            remote_version=str(result.get("remote_version") or "unknown"),
            remote_hash=str(result.get("remote_hash") or ""),
            remote_path=str(result.get("source_path") or ""),
            origin=origin,
        ),
    )


def update_gate_for_rows(registry: Registry, rows: Sequence[sqlite3.Row]) -> Dict[str, Dict[str, Any]]:
    names = sorted({str(row["name"] or "").lower() for row in rows if row["name"]})
    gates_by_name: Dict[str, Dict[str, Any]] = {}
    now = dt.datetime.now(dt.timezone.utc).astimezone()
    for name in names:
        row = registry.conn.execute(
            """
            select u.checked_at, u.status, u.message
            from update_checks u
            join capabilities c on c.id = u.capability_id
            where lower(c.name)=?
            order by u.checked_at desc, u.id desc
            limit 1
            """,
            (name,),
        ).fetchone()
        if not row:
            continue
        checked_at = parse_iso_datetime(row["checked_at"] or "")
        if not checked_at or now - checked_at > REMOTE_UPDATE_CACHE_TTL:
            continue
        status = str(row["status"] or "")
        try:
            payload = json.loads(row["message"] or "{}")
        except json.JSONDecodeError:
            payload = {}
        disabled = status in UPDATE_BUTTON_DISABLED_STATUSES and payload.get("origin") in ("update-button", "update-apply")
        gates_by_name[name] = {
            "status": status,
            "checked_at": row["checked_at"],
            "disabled": disabled,
        }
    return {
        row["id"]: gates_by_name.get(str(row["name"] or "").lower(), {"status": "", "checked_at": "", "disabled": False})
        for row in rows
    }


def check_group_update(ids: Sequence[str], force_remote: bool = False, remember_terminal: bool = True) -> Dict[str, Any]:
    registry = Registry()
    selected_rows = rows_for_ids(registry, ids)
    if not selected_rows:
        raise ValueError("No capabilities selected.")
    skipped = [row_to_dict(row) for row in selected_rows if not is_editable_skill_dir(row)]
    rows = [row for row in selected_rows if is_editable_skill_dir(row)]
    if not rows:
        if all(row["source_type"] == "builtin" for row in selected_rows):
            platform = selected_rows[0]["platform"] if selected_rows else ""
            message = f"Builtin skill. Update via {platform}."
        else:
            message = "No editable skill directory was selected. Only directories with SKILL.md can be updated."
        result = {
            "status": "unsupported",
            "message": message,
            "local_versions": [],
            "max_local_version": "unknown",
            "targets": [],
            "skipped_targets": skipped,
        }
        if remember_terminal:
            registry.record_update_check(selected_rows[0]["id"], "unsupported", selected_rows[0]["github_hash"] or "", "", remote_cache_payload("unsupported", message, origin="update-button"))
        return result
    local_versions = sorted({row["version"] or "unknown" for row in rows})
    max_local = sorted(local_versions, key=version_key)[-1] if local_versions else "unknown"
    primary = rows[0]
    if not force_remote:
        snapshot = get_remote_snapshot(registry, primary["name"], fresh_only=True)
        if snapshot:
            result = snapshot_result(rows, local_versions, max_local, snapshot, skipped)
            if remember_terminal:
                remember_update_result(registry, primary["id"], result, primary["github_hash"] or "")
            return result
        cached = latest_remote_update_cache(registry, primary["id"])
        if cached:
            result = remote_result_from_cache(rows, local_versions, max_local, cached)
            if remember_terminal:
                remember_update_result(registry, primary["id"], result, primary["github_hash"] or "")
            return result
    base = next((row for row in rows if github_info_for_cap(row)), None)
    try:
        if base:
            info = github_info_for_cap(base)
            assert info is not None
            snapshot = fetch_remote_snapshot_for_info(base, info, "metadata", registry)
            registry.record_update_check(
                base["id"],
                "remote_checked",
                base["github_hash"] or "",
                snapshot.get("remote_hash", ""),
                remote_cache_payload(
                    "remote_checked",
                    "Remote version snapshot refreshed.",
                    repo=snapshot.get("source_url", ""),
                    branch=snapshot.get("source_ref", ""),
                    remote_version=snapshot.get("remote_version", "unknown"),
                    remote_hash=snapshot.get("remote_hash", ""),
                    remote_path=snapshot.get("source_path", ""),
                ),
            )
            result = snapshot_result(rows, local_versions, max_local, snapshot, skipped)
            result["cache_hit"] = False
            result["message"] = "Remote version checked."
            if remember_terminal:
                remember_update_result(registry, base["id"], result, base["github_hash"] or "")
            return result
        info = discover_remote_with_find_skills(primary["name"])
        if info:
            snapshot = fetch_remote_snapshot_for_info(primary, info, "find-skills", registry)
            registry.record_update_check(
                primary["id"],
                "remote_checked",
                primary["github_hash"] or "",
                snapshot.get("remote_hash", ""),
                remote_cache_payload(
                    "remote_checked",
                    "Remote version snapshot refreshed from find-skills.",
                    repo=snapshot.get("source_url", ""),
                    branch=snapshot.get("source_ref", ""),
                    remote_version=snapshot.get("remote_version", "unknown"),
                    remote_hash=snapshot.get("remote_hash", ""),
                    remote_path=snapshot.get("source_path", ""),
                ),
            )
            result = snapshot_result(rows, local_versions, max_local, snapshot, skipped)
            result["cache_hit"] = False
            result["message"] = "Remote version checked."
            if remember_terminal:
                remember_update_result(registry, primary["id"], result, primary["github_hash"] or "")
            return result
        message = "No newer version was found. No bound cloud source or find-skills candidate was found."
        registry.record_update_check(
            primary["id"],
            "no_cloud",
            primary["github_hash"] or "",
            "",
            remote_cache_payload("no_cloud", message),
        )
        result = local_update_fallback(
            rows,
            local_versions,
            max_local,
            message,
            {"status": "latest" if len(local_versions) <= 1 else "local_mismatch", "cache_hit": False, "checked_at": now_iso(), "skipped_targets": skipped},
        )
        if remember_terminal:
            remember_update_result(registry, primary["id"], result, primary["github_hash"] or "")
        return result
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            message = "No cloud version was found. The repository returned 404 Not Found."
            registry.record_update_check(
                (base or primary)["id"],
                "no_cloud",
                (base or primary)["github_hash"] or "",
                "",
                remote_cache_payload("no_cloud", message),
            )
            result = local_update_fallback(rows, local_versions, max_local, message, {"cache_hit": False, "checked_at": now_iso(), "skipped_targets": skipped})
            if remember_terminal:
                remember_update_result(registry, (base or primary)["id"], result, (base or primary)["github_hash"] or "")
            return result
        raise
    except (urllib.error.URLError, ValueError) as exc:
        message = str(exc) if str(exc).startswith("No cloud version was found.") else f"No cloud version was found. {exc}"
        registry.record_update_check(
            (base or primary)["id"],
            "remote_error",
            (base or primary)["github_hash"] or "",
            "",
            remote_cache_payload("remote_error", message),
        )
        result = local_update_fallback(rows, local_versions, max_local, message, {"cache_hit": False, "checked_at": now_iso(), "skipped_targets": skipped})
        if remember_terminal:
            remember_update_result(registry, (base or primary)["id"], result, (base or primary)["github_hash"] or "")
        return result


def apply_group_update(ids: Sequence[str], mode: str) -> Dict[str, Any]:
    registry = Registry()
    selected_rows = rows_for_ids(registry, ids)
    if not selected_rows:
        raise ValueError("No capabilities selected.")
    skipped = [row_to_dict(row) for row in selected_rows if not is_editable_skill_dir(row)]
    rows = [row for row in selected_rows if is_editable_skill_dir(row)]
    if not rows:
        if all(row["source_type"] == "builtin" for row in selected_rows):
            platform = selected_rows[0]["platform"] if selected_rows else ""
            raise ValueError(f"Builtin skill. Update via {platform}.")
        raise ValueError("No editable skill directory was selected. Only directories with SKILL.md can be updated.")
    roots = configured_roots()
    for row in rows:
        if not is_inside_any_root(Path(row["path"]), roots):
            raise ValueError(f"Refusing to update path outside configured roots: {row['path']}")

    backups: List[str] = []
    updated = 0
    if mode == "unify":
        source = sorted(rows, key=lambda row: version_key(row["version"] or ""), reverse=True)[0]
        source_path = Path(source["path"])
        for row in rows:
            if row["id"] == source["id"]:
                continue
            backups.append(str(backup_for_update(row, "unify")))
            copy_tree_contents(source_path, Path(row["path"]))
            updated += 1
    elif mode == "remote":
        base = next((row for row in rows if github_info_for_cap(row)), None) or rows[0]
        snapshot = get_remote_snapshot(registry, base["name"], fresh_only=False)
        info = remote_info_from_snapshot(snapshot) if snapshot else None
        if not info:
            info = github_info_for_cap(base)
        if not info:
            raise ValueError("No GitHub or supported Vercel repository metadata was found.")
        repo_api = f"https://api.github.com/repos/{info['owner']}/{info['repo']}"
        branch = (snapshot or {}).get("source_ref") or base["github_ref"] or info["ref"] or github_json(repo_api)["default_branch"]
        with tempfile.TemporaryDirectory(prefix="asm-update-") as tmp:
            repo_root = download_github_archive(info, branch, Path(tmp))
            snapshot_path = (snapshot or {}).get("source_path") or ""
            remote_dir = repo_root / snapshot_path if snapshot_path else find_remote_skill_dir(repo_root, base["name"], Path(base["path"]).name)
            if not remote_dir:
                raise ValueError("Repository found, but no matching remote skill directory was found.")
            for row in rows:
                backups.append(str(backup_for_update(row, "update")))
                copy_tree_contents(remote_dir, Path(row["path"]))
                updated += 1
    else:
        raise ValueError(f"Unsupported update mode: {mode}")

    caps = discover_capabilities(load_config())
    for cap in caps:
        registry.upsert_capability(cap)
    registry.prune_to_scan(caps)
    registry.log_operation("update", ",".join([row["id"] for row in rows[:8]]), {"mode": mode, "updated": updated, "backups": backups, "skipped": len(skipped)}, commit=False)
    for row in rows:
        registry.record_update_check(
            row["id"],
            "updated",
            row["github_hash"] or "",
            "",
            remote_cache_payload("updated", f"Updated by {mode}.", origin="update-apply"),
        )
    registry.conn.commit()
    return {"updated": updated, "mode": mode, "backups": backups, "skipped_targets": skipped}


def check_updates_command(args: argparse.Namespace) -> None:
    registry = Registry()
    where = "source_type='github' and github_url != ''"
    params: List[Any] = []
    if args.platform:
        where += " and platform=?"
        params.append(args.platform)
    rows = registry.all_capabilities(where, params)
    results: List[Dict[str, Any]] = []
    for row in rows:
        local_hash = row["github_hash"] or ""
        try:
            remote_hash, evidence = latest_github_hash(row)
            status = "latest" if local_hash and remote_hash.startswith(local_hash) or local_hash == remote_hash else "outdated"
            message = evidence
        except Exception as exc:
            remote_hash = ""
            status = "error"
            message = str(exc)
        registry.record_update_check(row["id"], status, local_hash, remote_hash, message)
        results.append(
            {
                "platform": row["platform"],
                "name": row["name"],
                "status": status,
                "local": short(local_hash, 12),
                "remote": short(remote_hash, 12),
                "message": short(message, 45),
            }
        )
    print_table(results, [("platform", "Platform"), ("name", "Name"), ("status", "Status"), ("local", "Local"), ("remote", "Remote"), ("message", "Message")])


def outdated_command(args: argparse.Namespace) -> None:
    registry = Registry()
    rows = registry.conn.execute(
        """
        select c.platform, c.kind, c.name, c.version, u.status, u.local_hash, u.remote_hash, u.checked_at
        from update_checks u
        join capabilities c on c.id = u.capability_id
        where u.id in (select max(id) from update_checks group by capability_id)
          and u.status = 'outdated'
        order by c.platform, c.name
        """
    ).fetchall()
    print_table([row_to_dict(row) for row in rows], [("platform", "Platform"), ("kind", "Kind"), ("name", "Name"), ("version", "Version"), ("local_hash", "Local"), ("remote_hash", "Remote"), ("checked_at", "Checked")])


def run_health_check() -> Dict[str, Any]:
    registry = Registry()
    rows = registry.all_capabilities()
    results: List[Dict[str, Any]] = []
    rules = [
        "Path exists",
        "Skill directories contain SKILL.md",
        "Remote-managed items include update tracking metadata",
        "Builtin/platform capabilities are observe-only unless files are missing or unreadable",
    ]
    for row in rows:
        data = row_to_dict(row)
        scope = data["management_scope"]
        path = Path(row["path"])
        status = "ok"
        messages = []
        if not path.exists():
            status = "missing"
            messages.append("path missing")
        if row["kind"] == "skill" and path.is_dir() and not (path / "SKILL.md").exists():
            status = "warning"
            messages.append("missing SKILL.md")
        if scope == "managed_remote" and row["source_type"] == "github" and not row["github_hash"]:
            status = "warning"
            messages.append("missing github_hash")
        registry.conn.execute(
            "insert into health_checks (capability_id, checked_at, status, message) values (?, ?, ?, ?)",
            (row["id"], now_iso(), status, "; ".join(messages)),
        )
        registry.conn.execute("update capabilities set health=? where id=?", (status, row["id"]))
        results.append({"platform": row["platform"], "kind": row["kind"], "name": row["name"], "health": status, "message": "; ".join(messages), "management_scope": scope})
    registry.conn.commit()
    summary = {
        "total": len(results),
        "ok": len([row for row in results if row["health"] == "ok"]),
        "warning": len([row for row in results if row["health"] == "warning"]),
        "missing": len([row for row in results if row["health"] == "missing"]),
        "metadata_error": len([row for row in results if row["health"] == "metadata-error"]),
    }
    issues = [row for row in results if row["health"] != "ok" or row["message"]]
    registry.log_operation("health", "", {"summary": summary, "issues": len(issues)})
    return {"rules": rules, "summary": summary, "issues": issues}


def health_command(args: argparse.Namespace) -> None:
    result = run_health_check()
    results = result["issues"]
    print_table(results, [("platform", "Platform"), ("kind", "Kind"), ("name", "Name"), ("health", "Health"), ("message", "Message")])


def duplicates_command(args: argparse.Namespace) -> None:
    registry = Registry()
    rows = registry.conn.execute(
        """
        select lower(name) normalized, count(*) count, group_concat(platform || ':' || kind || ':' || path, char(10)) entries
        from capabilities
        group by lower(name)
        having count(*) > 1
        order by count desc, normalized
        """
    ).fetchall()
    print_table([row_to_dict(row) for row in rows], [("normalized", "Name"), ("count", "Count"), ("entries", "Entries")])


def backup_command(args: argparse.Namespace) -> None:
    registry = Registry()
    cap = registry.find_one(args.name, args.platform or "")
    if not cap:
        raise SystemExit(f"No capability found for {args.name}")
    source = Path(cap["path"])
    if not source.exists():
        raise SystemExit(f"Path does not exist: {source}")
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = BACKUP_DIR / f"{cap['platform']}--{cap['name']}--{stamp}"
    if source.is_dir():
        shutil.copytree(source, dest)
    else:
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest / source.name)
    print(f"Backup created: {dest}")


def configured_roots() -> List[Path]:
    roots: List[Path] = []
    for settings in load_config().get("platforms", {}).values():
        for root in settings.get("roots", []):
            try:
                roots.append(expand_root(root))
            except Exception:
                continue
    return roots


def is_inside_any_root(path: Path, roots: Sequence[Path]) -> bool:
    resolved = path.resolve()
    for root in roots:
        try:
            resolved.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False


def backup_capability_path(cap: sqlite3.Row) -> Path:
    source = Path(cap["path"])
    if not source.exists():
        raise ValueError(f"Path does not exist: {source}")
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", cap["name"]).strip("-") or "capability"
    dest = unique_backup_path(f"{cap['platform']}--{safe_name}--delete--{stamp}", cap["id"])
    if source.is_dir():
        shutil.copytree(source, dest)
    else:
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest / source.name)
    return dest


def delete_capability(registry: Registry, name_or_id: str, platform: str = "", confirmed: bool = False) -> Dict[str, Any]:
    if not confirmed:
        raise ValueError("Delete requires confirmed=true.")
    cap = registry.find_one(name_or_id, platform)
    if not cap:
        raise ValueError(f"No capability found for {name_or_id}")
    if cap["source_type"] == "builtin":
        raise ValueError("Refusing to delete builtin/plugin cache capability.")
    target = Path(cap["path"])
    if not target.exists():
        registry.delete_capability_row(cap["id"])
        registry.log_operation("delete-missing", cap["id"], {"name": cap["name"], "platform": cap["platform"], "path": str(target)})
        return {"deleted": False, "removed_from_registry": True, "backup": "", "message": "Path was already missing."}
    if not is_inside_any_root(target, configured_roots()):
        raise ValueError(f"Refusing to delete path outside configured roots: {target}")
    backup_path = backup_capability_path(cap)
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()
    registry.delete_capability_row(cap["id"])
    registry.log_operation("delete", cap["id"], {"name": cap["name"], "platform": cap["platform"], "path": str(target), "backup": str(backup_path)})
    return {
        "deleted": True,
        "removed_from_registry": True,
        "backup": str(backup_path),
        "path": str(target),
        "name": cap["name"],
        "platform": cap["platform"],
    }


def delete_command(args: argparse.Namespace) -> None:
    try:
        result = delete_capability(Registry(), args.name, args.platform or "", args.yes)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(result, indent=2, ensure_ascii=False))


def update_command(args: argparse.Namespace) -> None:
    registry = Registry()
    cap = registry.find_one(args.name, args.platform or "")
    if not cap:
        raise SystemExit(f"No capability found for {args.name}")
    if cap["source_type"] != "github" or not cap["github_url"]:
        raise SystemExit("Update only supports GitHub-backed capabilities.")
    remote_hash, evidence = latest_github_hash(cap)
    local_hash = cap["github_hash"] or ""
    if local_hash == remote_hash:
        print(f"{cap['name']} is already latest at {short(local_hash, 12)}")
        return
    print(f"{cap['name']} is outdated")
    print(f"Local : {local_hash or 'unknown'}")
    print(f"Remote: {remote_hash}")
    print(f"Source: {evidence}")
    if args.dry_run:
        print("Dry run only. No files changed.")
        return
    print("Automatic overwrite is intentionally not implemented yet. Use the report to review and update manually.")


def create_snapshot_report() -> Dict[str, Any]:
    registry = Registry()
    registry.refresh_usage_counts()
    rows = registry.all_capabilities()
    generated = now_iso()
    total = len(rows)
    active = len([r for r in rows if r["status"] == "active"])
    inactive = len([r for r in rows if r["status"] != "active"])
    managed_rows = [r for r in rows if row_to_dict(r)["management_scope"] in ("managed_remote", "managed_local")]
    gaps = [r for r in managed_rows if r["source_type"] == "github" and not r["github_hash"]]
    missing_meta = len(gaps)
    unused = len([r for r in rows if int(r["usage_30d"] or 0) == 0])
    stamp = dt.datetime.now().strftime("%Y-%m-%d-%H%M%S")
    path = REPORT_DIR / f"skill-report-{stamp}.md"
    html_path = REPORT_DIR / f"skill-report-{stamp}.html"
    lines = [
        f"# Agent Skill Manager Report",
        "",
        f"Generated: {generated}",
        "",
        f"- Total capabilities: {total}",
        f"- Active: {active}",
        f"- Non-active: {inactive}",
        f"- Remote update tracking gaps: {missing_meta}",
        f"- Zero usage in 30d: {unused}",
        "",
        "## Top Usage 30d",
        "",
    ]
    top = sorted(rows, key=lambda r: int(r["usage_30d"] or 0), reverse=True)[:20]
    lines.extend(markdown_table(top, ["platform", "kind", "name", "status", "usage_30d", "version", "source_type"]))
    lines.extend(["", "## Zero Usage 30d", ""])
    zero = [r for r in rows if int(r["usage_30d"] or 0) == 0][:100]
    lines.extend(markdown_table(zero, ["platform", "kind", "name", "status", "version", "source_type"]))
    lines.extend(["", "## Remote Update Tracking Gaps", ""])
    lines.extend(markdown_table(gaps, ["platform", "kind", "name", "version", "source_type", "github_url", "path"]))
    path.write_text("\n".join(lines), encoding="utf-8")
    html_path.write_text(
        snapshot_html(generated, rows, top, zero, gaps, total, active, inactive, missing_meta, unused),
        encoding="utf-8",
    )
    registry.log_operation("report", str(html_path), {"total": total, "unused": unused, "missing_meta": missing_meta})
    return {
        "path": str(path),
        "html_path": str(html_path),
        "html_url": f"/reports/{urllib.parse.quote(html_path.name)}",
        "generated": generated,
        "total": total,
        "active": active,
        "inactive": inactive,
        "missing_meta": missing_meta,
        "unused": unused,
        "sections": ["Overview", "Top Usage 30d", "Zero Usage 30d", "Remote Update Tracking Gaps"],
    }


def report_command(args: argparse.Namespace) -> None:
    result = create_snapshot_report()
    path = result["path"]
    print(f"Report written: {path}")


def snapshot_html(
    generated: str,
    rows: Sequence[sqlite3.Row],
    top: Sequence[sqlite3.Row],
    zero: Sequence[sqlite3.Row],
    gaps: Sequence[sqlite3.Row],
    total: int,
    active: int,
    inactive: int,
    missing_meta: int,
    unused: int,
) -> str:
    def esc(value: Any) -> str:
        return html.escape(str(value if value is not None else ""))

    def table(title: str, data: Sequence[sqlite3.Row], columns: Sequence[str]) -> str:
        body = "".join(
            "<tr>" + "".join(f"<td>{esc(row[col])}</td>" for col in columns) + "</tr>"
            for row in data
        )
        if not body:
            body = f"<tr><td colspan='{len(columns)}'>None</td></tr>"
        header = "".join(f"<th>{esc(col)}</th>" for col in columns)
        return f"<section><h2>{esc(title)}</h2><table><thead><tr>{header}</tr></thead><tbody>{body}</tbody></table></section>"

    source_counts: Dict[str, int] = {}
    platform_counts: Dict[str, int] = {}
    for row in rows:
        source_counts[row["source_type"] or "local"] = source_counts.get(row["source_type"] or "local", 0) + 1
        platform_counts[row["platform"] or "unknown"] = platform_counts.get(row["platform"] or "unknown", 0) + 1
    cards = [
        ("Total", total),
        ("Active", active),
        ("Non-active", inactive),
        ("30d unused", unused),
        ("Metadata gaps", missing_meta),
    ]
    card_html = "".join(f"<div class='card'><strong>{esc(value)}</strong><span>{esc(label)}</span></div>" for label, value in cards)
    dist_html = "".join(f"<li>{esc(key)}: {esc(value)}</li>" for key, value in sorted(source_counts.items()))
    platform_html = "".join(f"<li>{esc(key)}: {esc(value)}</li>" for key, value in sorted(platform_counts.items()))
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Agent Skill Manager Snapshot</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 32px; color: #17201b; background: #f8f5ee; }}
    h1 {{ margin-bottom: 4px; }}
    .muted {{ color: #667069; }}
    .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin: 22px 0; }}
    .card {{ background: #fffdf7; border: 1px solid #d6d0c3; border-radius: 8px; padding: 14px; }}
    .card strong {{ display: block; font-size: 28px; }}
    .card span {{ color: #667069; font-size: 12px; text-transform: uppercase; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px; }}
    section {{ margin-top: 28px; }}
    table {{ width: 100%; border-collapse: collapse; background: #fffdf7; }}
    th, td {{ border: 1px solid #d6d0c3; padding: 8px; text-align: left; font-size: 12px; vertical-align: top; }}
    th {{ background: #e9e1d2; }}
    td {{ overflow-wrap: anywhere; }}
  </style>
</head>
<body>
  <h1>Agent Skill Manager Snapshot</h1>
  <div class="muted">Generated: {esc(generated)}</div>
  <div class="cards">{card_html}</div>
  <div class="grid">
    <section><h2>Sources</h2><ul>{dist_html}</ul></section>
    <section><h2>Platforms</h2><ul>{platform_html}</ul></section>
  </div>
  {table("Top Usage 30d", top, ["platform", "kind", "name", "status", "usage_30d", "version", "source_type"])}
  {table("Zero Usage 30d", zero, ["platform", "kind", "name", "status", "version", "source_type", "path"])}
  {table("Remote Update Tracking Gaps", gaps, ["platform", "kind", "name", "version", "source_type", "github_url", "path"])}
</body>
</html>"""


def markdown_table(rows: Sequence[sqlite3.Row], columns: Sequence[str]) -> List[str]:
    output = ["| " + " | ".join(columns) + " |", "| " + " | ".join("---" for _ in columns) + " |"]
    for row in rows:
        values = [str(row[col] if row[col] is not None else "").replace("|", "\\|").replace("\n", " ") for col in columns]
        output.append("| " + " | ".join(values) + " |")
    if len(output) == 2:
        output.append("| _none_ |" + " | |" * (len(columns) - 1))
    return output


def api_payload(handler: http.server.BaseHTTPRequestHandler) -> Dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0") or 0)
    if length <= 0:
        return {}
    raw = handler.rfile.read(length).decode("utf-8")
    return json.loads(raw or "{}")


def json_response(handler: http.server.BaseHTTPRequestHandler, payload: Any, status: int = 200) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


class AdminHandler(http.server.BaseHTTPRequestHandler):
    server_version = "AgentSkillManager/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        if self.path == "/" or self.path.startswith("/index"):
            data = ADMIN_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path.startswith("/reports/"):
            name = urllib.parse.unquote(urllib.parse.urlparse(self.path).path.split("/")[-1])
            target = (REPORT_DIR / name).resolve()
            if not target.is_file() or target.parent != REPORT_DIR.resolve():
                json_response(self, {"error": "report not found"}, 404)
                return
            data = target.read_bytes()
            content_type = "text/html; charset=utf-8" if target.suffix == ".html" else "text/markdown; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path.startswith("/api/state"):
            registry = Registry()
            raw_rows = registry.all_capabilities()
            update_gates = update_gate_for_rows(registry, raw_rows)
            rows = []
            for row in raw_rows:
                data = row_to_dict(row)
                data["update_gate"] = update_gates.get(row["id"], {"status": "", "checked_at": "", "disabled": False})
                rows.append(data)
            json_response(
                self,
                {
                    "capabilities": rows,
                    "config_path": str(CONFIG_PATH),
                    "db_path": str(DB_PATH),
                    "source_discovery": source_discovery_status(rows),
                    "smart_upgrade": registry.smart_upgrade_snapshot(fresh_only=True),
                },
            )
            return
        if self.path.startswith("/api/usage-events"):
            query = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(query)
            cap_id = params.get("id", [""])[0]
            if not cap_id:
                json_response(self, {"error": "id is required"}, 400)
                return
            registry = Registry()
            cap = registry.find_one(cap_id)
            if not cap:
                json_response(self, {"error": "capability not found"}, 404)
                return
            rows = registry.conn.execute(
                """
                select occurred_at, source, confidence, session_id, evidence
                from usage_events
                where capability_id=?
                order by occurred_at desc
                limit 50
                """,
                (cap["id"],),
            ).fetchall()
            json_response(self, {"capability": row_to_dict(cap), "events": [row_to_dict(row) for row in rows]})
            return
        json_response(self, {"error": "not found"}, 404)

    def do_POST(self) -> None:
        try:
            payload = api_payload(self)
            if self.path == "/api/scan":
                registry = Registry()
                caps = discover_capabilities(load_config())
                for cap in caps:
                    registry.upsert_capability(cap)
                registry.prune_to_scan(caps)
                registry.log_operation("scan", "", {"count": len(caps)}, commit=False)
                registry.conn.commit()
                usage_import = import_usage_from_logs(registry)
                json_response(self, {"ok": True, "count": len(caps), "usage_import": usage_import})
                return
            if self.path == "/api/sources":
                json_response(self, {"ok": True, "sources": source_entries(), "config_path": str(CONFIG_PATH)})
                return
            if self.path == "/api/source-update":
                result = update_source(payload.get("action", ""), payload.get("platform", ""), payload.get("root", ""), payload.get("enabled"))
                json_response(self, {"ok": True, **result})
                return
            if self.path == "/api/pick-directory":
                result = pick_directory()
                json_response(self, {"ok": True, **result})
                return
            if self.path == "/api/recommendations":
                json_response(self, {"ok": True, **management_recommendations()})
                return
            if self.path == "/api/smart-upgrade":
                json_response(self, {"ok": True, **smart_upgrade_check()})
                return
            if self.path == "/api/smart-upgrade-start":
                json_response(self, {"ok": True, **start_smart_upgrade_job()})
                return
            if self.path == "/api/smart-upgrade-status":
                json_response(self, {"ok": True, **smart_upgrade_job_status(payload.get("job_id", ""))})
                return
            if self.path == "/api/logs":
                rows = [row_to_dict(row) for row in Registry().operation_logs()]
                json_response(self, {"ok": True, "logs": rows})
                return
            if self.path == "/api/status":
                registry = Registry()
                row = registry.set_status(payload["name"], payload["status"], payload.get("platform", ""))
                json_response(self, {"ok": True, "capability": row_to_dict(row)})
                return
            if self.path == "/api/importance":
                count = Registry().set_importance(payload.get("ids", []), payload.get("importance", ""))
                json_response(self, {"ok": True, "count": count})
                return
            if self.path == "/api/delete":
                registry = Registry()
                result = delete_capability(
                    registry,
                    payload["name"],
                    payload.get("platform", ""),
                    bool(payload.get("confirmed")),
                )
                json_response(self, {"ok": True, **result})
                return
            if self.path == "/api/update-check":
                result = check_group_update(payload.get("ids", []), bool(payload.get("force_remote")))
                json_response(self, {"ok": True, **result})
                return
            if self.path == "/api/update-apply":
                result = apply_group_update(payload.get("ids", []), payload.get("mode", ""))
                json_response(self, {"ok": True, **result})
                return
            if self.path == "/api/health":
                result = run_health_check()
                json_response(self, {"ok": True, **result})
                return
            if self.path == "/api/report":
                result = create_snapshot_report()
                json_response(self, {"ok": True, **result})
                return
            json_response(self, {"error": "not found"}, 404)
        except Exception as exc:
            json_response(self, {"error": str(exc)}, 500)


ADMIN_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Agent Skill Manager</title>
  <style>
    :root {
      --bg: #f4f1ea;
      --ink: #17201b;
      --muted: #667069;
      --line: #d6d0c3;
      --panel: #fffdf7;
      --accent: #0f766e;
      --accent-2: #b42318;
      --mark: #f4b740;
      --ok: #18794e;
      --warn: #a15c07;
      --bad: #b42318;
      --shadow: 0 12px 30px rgba(23, 32, 27, .08);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background:
        linear-gradient(90deg, rgba(23,32,27,.045) 1px, transparent 1px),
        linear-gradient(rgba(23,32,27,.035) 1px, transparent 1px),
        var(--bg);
      background-size: 28px 28px;
      color: var(--ink);
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
      letter-spacing: 0;
    }
    header {
      border-bottom: 1px solid var(--line);
      background: rgba(244, 241, 234, .92);
      backdrop-filter: blur(12px);
      position: sticky;
      top: 0;
      z-index: 10;
    }
    .bar {
      max-width: 1480px;
      margin: 0 auto;
      padding: 18px 22px;
      display: grid;
      grid-template-columns: minmax(260px, 1fr) auto;
      gap: 16px;
      align-items: center;
    }
    h1 {
      margin: 0;
      font-size: 24px;
      line-height: 1.05;
      font-weight: 900;
      text-transform: uppercase;
    }
    .sub { color: var(--muted); font-size: 12px; margin-top: 5px; }
    .actions { display: flex; flex-wrap: wrap; gap: 8px; justify-content: flex-end; align-items: center; }
    button, select, input {
      font: inherit;
      border: 1px solid var(--line);
      background: var(--panel);
      color: var(--ink);
      min-height: 36px;
    }
    button {
      padding: 0 12px;
      cursor: pointer;
      border-radius: 6px;
      box-shadow: 0 1px 0 rgba(23, 32, 27, .12);
    }
    button:hover { border-color: var(--ink); transform: translateY(-1px); }
    button:disabled {
      cursor: not-allowed;
      opacity: .45;
      transform: none;
      box-shadow: none;
    }
    button:disabled:hover {
      border-color: var(--line);
      transform: none;
    }
    button.primary { background: var(--accent); color: white; border-color: var(--accent); }
    button.danger { color: var(--ink); }
    .more-menu { position: relative; }
    .more-menu summary {
      list-style: none;
      min-height: 36px;
      display: inline-flex;
      align-items: center;
      padding: 0 12px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--panel);
      cursor: pointer;
      box-shadow: 0 1px 0 rgba(23, 32, 27, .12);
    }
    .more-menu summary::-webkit-details-marker { display: none; }
    .more-list {
      position: absolute;
      right: 0;
      top: 42px;
      width: 150px;
      padding: 6px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      box-shadow: var(--shadow);
      z-index: 30;
    }
    .more-list button {
      width: 100%;
      justify-content: flex-start;
      box-shadow: none;
      margin: 2px 0;
    }
    main {
      max-width: 1480px;
      margin: 0 auto;
      padding: 18px 22px 40px;
    }
    .metrics {
      display: grid;
      grid-template-columns: repeat(7, minmax(120px, 1fr));
      gap: 10px;
      margin-bottom: 14px;
    }
    .metric {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      box-shadow: var(--shadow);
      text-align: left;
      min-height: 74px;
    }
    button.metric { cursor: pointer; }
    button.metric.active-filter { border-color: var(--accent); box-shadow: inset 0 0 0 1px var(--accent); }
    .metric strong { display: block; font-size: 25px; line-height: 1; }
    .metric span { display: block; margin-top: 7px; color: var(--muted); font-size: 11px; text-transform: uppercase; }
    .filters {
      display: grid;
      grid-template-columns: minmax(220px, 1fr) 150px 170px 150px 170px;
      gap: 8px;
      margin-bottom: 12px;
    }
    input, select { width: 100%; padding: 0 10px; border-radius: 6px; }
    .table-shell {
      overflow: auto;
      border: 1px solid var(--line);
      background: var(--panel);
      border-radius: 8px;
      box-shadow: var(--shadow);
      max-height: calc(100vh - 270px);
    }
    table {
      width: 100%;
      min-width: 1140px;
      border-collapse: collapse;
      table-layout: auto;
      font-size: 12px;
    }
    th {
      position: sticky;
      top: 0;
      background: #e9e1d2;
      color: #26332c;
      text-align: left;
      padding: 10px 9px;
      border-bottom: 1px solid var(--line);
      text-transform: uppercase;
      font-size: 11px;
      z-index: 1;
    }
    th.sortable {
      cursor: pointer;
      user-select: none;
    }
    th.sortable:hover {
      color: var(--accent);
    }
    .sort-label {
      display: inline-flex;
      align-items: center;
      gap: 5px;
    }
    .sort-arrow {
      color: var(--accent);
      font-size: 10px;
      min-width: 10px;
    }
    th:nth-child(1), td:nth-child(1) { width: 21%; min-width: 230px; }
    th:nth-child(2), td:nth-child(2) { width: 7%; min-width: 76px; }
    th:nth-child(3), td:nth-child(3) { width: 17%; min-width: 210px; }
    th:nth-child(4), td:nth-child(4) { width: 7%; min-width: 74px; }
    th:nth-child(5), td:nth-child(5) { width: 7%; min-width: 82px; }
    th:nth-child(6), td:nth-child(6) { width: 7%; min-width: 78px; }
    th:nth-child(7), td:nth-child(7) { width: 7%; min-width: 78px; }
    th:nth-child(8), td:nth-child(8) { width: 7%; min-width: 76px; }
    th:nth-child(9), td:nth-child(9) { width: 20%; min-width: 300px; }
    td {
      border-bottom: 1px solid #ebe5da;
      padding: 9px;
      vertical-align: middle;
    }
    td:first-child strong { display: block; overflow-wrap: anywhere; font-size: 13px; line-height: 1.35; }
    tr:hover td { background: #fbf5e7; }
    .pill {
      display: inline-flex;
      align-items: center;
      min-height: 22px;
      padding: 0 7px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: #f8f3e8;
      white-space: nowrap;
    }
    .active { color: var(--ok); border-color: rgba(24,121,78,.35); }
    .inactive { color: var(--warn); border-color: rgba(161,92,7,.35); }
    .broken, .missing { color: var(--bad); border-color: rgba(180,35,24,.35); }
    .score-good { color: var(--ok); border-color: rgba(24,121,78,.35); }
    .score-mid { color: var(--warn); border-color: rgba(161,92,7,.35); }
    .score-low { color: var(--bad); border-color: rgba(180,35,24,.35); }
    .importance-button {
      min-height: 24px;
      padding: 0 8px;
      border-radius: 999px;
      font-size: 11px;
      box-shadow: none;
      white-space: nowrap;
    }
    .importance-important { color: var(--bad); border-color: rgba(180,35,24,.35); background: #fff0ed; }
    .importance-normal { color: var(--accent); border-color: rgba(15,118,110,.28); background: #eef8f5; }
    .importance-low { color: var(--muted); border-color: var(--line); background: #f8f3e8; }
    .help {
      position: relative;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 17px;
      height: 17px;
      margin-left: 6px;
      border: 1px solid rgba(38, 51, 44, .35);
      border-radius: 50%;
      background: #fffaf0;
      color: var(--muted);
      font-size: 11px;
      line-height: 1;
      text-transform: none;
      cursor: help;
      vertical-align: middle;
    }
    .help:hover::after {
      content: attr(data-tip);
      position: absolute;
      top: 23px;
      left: 0;
      width: 360px;
      white-space: pre-line;
      background: var(--ink);
      color: white;
      border-radius: 8px;
      padding: 12px;
      box-shadow: var(--shadow);
      font-size: 11px;
      font-weight: 400;
      line-height: 1.45;
      text-transform: none;
      z-index: 50;
    }
    .path { color: var(--muted); max-width: 360px; overflow-wrap: anywhere; }
    .platform-tags, .status-tags, .source-tags {
      display: flex;
      flex-wrap: wrap;
      gap: 5px;
    }
    .platform-tags { flex-wrap: nowrap; }
    .platform-tag {
      display: inline-flex;
      align-items: center;
      min-height: 22px;
      padding: 0 7px;
      border-radius: 5px;
      border: 1px solid rgba(15,118,110,.28);
      background: #eef8f5;
      color: var(--accent);
      white-space: nowrap;
    }
    .row-actions { display: flex; gap: 6px; flex-wrap: nowrap; align-items: center; }
    .confirm-actions { justify-content: center; }
    .action-group { display: inline-flex; gap: 6px; flex-wrap: nowrap; align-items: center; }
    .tiny { min-height: 28px; padding: 0 8px; font-size: 11px; }
    #toast {
      position: fixed;
      right: 18px;
      bottom: 18px;
      max-width: 520px;
      background: var(--ink);
      color: white;
      border-radius: 8px;
      padding: 12px 14px;
      box-shadow: var(--shadow);
      opacity: 0;
      transform: translateY(10px);
      transition: .18s ease;
      pointer-events: none;
    }
    #toast.show { opacity: 1; transform: translateY(0); }
    .modal {
      position: fixed;
      inset: 0;
      background: rgba(23, 32, 27, .36);
      display: none;
      align-items: center;
      justify-content: center;
      padding: 18px;
      z-index: 30;
    }
    .modal.open { display: flex; }
    #actionModal, #usageModal { z-index: 45; }
    #toast { z-index: 60; }
    .dialog {
      width: min(760px, 100%);
      max-height: min(720px, 88vh);
      overflow: hidden;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: 0 22px 70px rgba(23, 32, 27, .24);
    }
    .dialog-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 14px 16px;
      border-bottom: 1px solid var(--line);
      background: #e9e1d2;
      position: sticky;
      top: 0;
      z-index: 2;
    }
    .dialog-head strong { font-size: 14px; text-transform: uppercase; }
    .dialog-body {
      padding: 14px 16px;
      max-height: calc(min(720px, 88vh) - 58px);
      overflow: auto;
    }
    .event-list { display: grid; gap: 8px; }
    .event {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 11px 12px;
      background: #fffaf0;
    }
    .event-meta { color: var(--muted); font-size: 11px; margin-bottom: 6px; }
    .usage-summary { display: grid; gap: 7px; min-width: 0; }
    .usage-topline {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      min-width: 0;
    }
    .usage-time { color: var(--muted); font-size: 12px; white-space: nowrap; }
    .usage-row {
      display: grid;
      grid-template-columns: 76px minmax(0, 1fr);
      gap: 10px;
      align-items: start;
      font-size: 12px;
    }
    .usage-label { color: var(--muted); }
    .usage-value { overflow-wrap: anywhere; line-height: 1.45; }
    .usage-path { color: #34423a; }
    .usage-empty {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.5;
    }
    .usage-platform {
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      padding: 0 8px;
      border-radius: 5px;
      border: 1px solid rgba(15,118,110,.28);
      background: #eef8f5;
      color: var(--accent);
      font-weight: 800;
    }
    .detail-grid { display: grid; gap: 10px; }
    .detail-row {
      display: grid;
      grid-template-columns: 120px minmax(0, 1fr);
      gap: 12px;
      border-bottom: 1px solid #ebe5da;
      padding-bottom: 8px;
    }
    .detail-row:last-child { border-bottom: 0; }
    .detail-label { color: var(--muted); text-transform: uppercase; font-size: 11px; }
    .detail-value { overflow-wrap: anywhere; line-height: 1.5; }
    .choice-list { display: grid; gap: 8px; }
    .choice {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 12px;
      align-items: center;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px;
      background: #fffaf0;
    }
    .choice small {
      display: block;
      color: var(--muted);
      margin-top: 4px;
      overflow-wrap: anywhere;
    }
    .recommendation-intro {
      margin-bottom: 12px;
      line-height: 1.55;
    }
    .recommendation-card {
      display: grid;
      gap: 10px;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      background: #fffaf0;
    }
    .recommendation-card strong {
      font-size: 15px;
    }
    .recommendation-meta {
      display: grid;
      grid-template-columns: 72px minmax(0, 1fr);
      gap: 6px 12px;
      font-size: 12px;
      line-height: 1.45;
    }
    .recommendation-meta span:nth-child(odd) {
      color: var(--muted);
      text-transform: uppercase;
      font-size: 11px;
    }
    .recommendation-actions {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      align-items: center;
    }
    .smart-upgrade-panel {
      display: grid;
      justify-items: center;
      gap: 14px;
      padding: 54px 12px 34px;
      text-align: center;
    }
    .smart-upgrade-panel .primary {
      min-width: 180px;
      justify-content: center;
      font-weight: 700;
    }
    .smart-upgrade-hint {
      color: var(--muted);
      font-size: 13px;
      line-height: 1.55;
    }
    .empty {
      border: 1px dashed var(--line);
      border-radius: 8px;
      padding: 18px;
      color: var(--muted);
      background: #fffaf0;
    }
    @media (max-width: 900px) {
      .bar { grid-template-columns: 1fr; }
      .actions { justify-content: flex-start; }
      .metrics { grid-template-columns: repeat(2, 1fr); }
      .filters { grid-template-columns: 1fr; }
      .table-shell { max-height: none; }
    }
  </style>
</head>
<body>
  <header>
    <div class="bar">
      <div>
        <h1>Agent Skill Manager</h1>
        <div class="sub" id="meta">loading registry</div>
      </div>
      <div class="actions">
        <button class="primary" onclick="scan()" data-i18n="syncScan">Sync Scan</button>
        <button onclick="showRecommendations()" data-i18n="recommendations">Smart Upgrade</button>
        <button onclick="refreshPage()" data-i18n="refresh">Refresh</button>
        <details class="more-menu">
          <summary data-i18n="moreActions">More</summary>
          <div class="more-list">
            <button onclick="showSources()" data-i18n="sourceManagement">Sources</button>
            <button onclick="health()" data-i18n="healthCheck">Health</button>
            <button onclick="report()" data-i18n="snapshotExport">Export Snapshot</button>
            <button onclick="showLogs()" data-i18n="operationLog">Operation Log</button>
          </div>
        </details>
        <button onclick="toggleLang()" id="langToggle">中文</button>
      </div>
    </div>
  </header>
  <main>
    <section class="metrics" id="metrics"></section>
    <section class="filters">
      <input id="q" placeholder="Search name" data-i18n-placeholder="searchPlaceholder" oninput="render()">
      <select id="importanceFilter" onchange="render()">
        <option value="" data-i18n="allImportance">All grades</option>
        <option value="important" data-i18n="important">Important</option>
        <option value="normal" data-i18n="normal">Regular</option>
        <option value="low" data-i18n="low">Other</option>
      </select>
      <select id="platform" onchange="render()"></select>
      <select id="status" onchange="render()">
        <option value="" data-i18n="allStatuses">All statuses</option>
        <option value="active" data-i18n="active">active</option>
        <option value="inactive" data-i18n="inactive">inactive</option>
        <option value="broken" data-i18n="broken">broken</option>
      </select>
      <select id="source" onchange="render()">
        <option value="" data-i18n="allSources">All sources</option>
        <option value="local" data-i18n="local">local</option>
        <option value="github" data-i18n="github">github</option>
        <option value="builtin" data-i18n="builtin">builtin</option>
      </select>
    </section>
    <section class="table-shell">
      <table>
        <thead>
          <tr>
            <th class="sortable" onclick="setSort('name')"><span class="sort-label"><span data-i18n="name">Name</span><span class="sort-arrow" data-sort-arrow="name"></span></span></th>
            <th class="sortable" onclick="setSort('importance')"><span class="sort-label"><span data-i18n="importance">Grade</span><span class="sort-arrow" data-sort-arrow="importance"></span></span></th>
            <th class="sortable" onclick="setSort('platform')"><span class="sort-label"><span data-i18n="platform">Platform</span><span class="sort-arrow" data-sort-arrow="platform"></span></span></th>
            <th class="sortable" onclick="setSort('status')"><span class="sort-label"><span data-i18n="status">Status</span><span class="sort-arrow" data-sort-arrow="status"></span></span></th>
            <th class="sortable" onclick="setSort('health')"><span class="sort-label"><span data-i18n="healthScore">Health Score</span><span class="sort-arrow" data-sort-arrow="health"></span></span><span class="help" id="healthHelp">?</span></th>
            <th class="sortable" onclick="setSort('version')"><span class="sort-label"><span data-i18n="version">Version</span><span class="sort-arrow" data-sort-arrow="version"></span></span></th>
            <th class="sortable" onclick="setSort('usage')"><span class="sort-label"><span data-i18n="thirtyDays">30d Uses</span><span class="sort-arrow" data-sort-arrow="usage"></span></span></th>
            <th class="sortable" onclick="setSort('source')"><span class="sort-label"><span data-i18n="source">Source</span><span class="sort-arrow" data-sort-arrow="source"></span></span></th><th data-i18n="actions">Actions</th>
          </tr>
        </thead>
        <tbody id="rows"></tbody>
      </table>
    </section>
  </main>
  <div class="modal" id="usageModal">
    <div class="dialog">
      <div class="dialog-head">
        <strong id="usageTitle">Usage</strong>
        <button class="tiny" onclick="closeUsage()" data-i18n="close">Close</button>
      </div>
      <div class="dialog-body" id="usageBody"></div>
    </div>
  </div>
  <div class="modal" id="actionModal">
    <div class="dialog">
      <div class="dialog-head">
        <strong id="actionTitle">Action</strong>
        <button class="tiny" onclick="closeAction()" data-i18n="close">Close</button>
      </div>
      <div class="dialog-body" id="actionBody"></div>
    </div>
  </div>
  <div class="modal" id="detailModal">
    <div class="dialog">
      <div class="dialog-head">
        <strong id="detailTitle">Details</strong>
        <button class="tiny" onclick="closeDetails()" data-i18n="close">Close</button>
      </div>
      <div class="dialog-body" id="detailBody"></div>
    </div>
  </div>
  <div id="toast"></div>
  <script>
    let state = {capabilities: [], smart_upgrade: null};
    let lang = localStorage.getItem('asm.lang') || 'en';
    let sortState = { key: 'usage', direction: 'desc' };
    let metricFilter = '';
    let loadingTimer = null;
    const noUpdateGroups = new Set();
    const dict = {
      en: {
        loading: 'loading registry',
        capabilities: 'capabilities',
        scan: 'Scan',
        syncScan: 'Skills Scan',
        moreActions: 'More',
        healthCheck: 'Health',
        healthResult: 'Health Check Results',
        healthRules: 'Rules checked',
        healthIssues: 'Issues',
        healthNoIssues: 'No health issues found.',
        healthScore: 'Health Score',
        healthHelp: 'Health score = Status score (30) + Quality score (30) + Structure health (40)\n\nBuiltin/platform capabilities are observe-only: zero usage, missing remote metadata, or unknown cloud source do not reduce their score.\n\nStatus score:\nactive 30, deactivated 18, broken 0\n\nQuality score:\nmanaged skills use 30d usage + version + description + source tracking; builtin skills use version/description/source tracking only.\n\nStructure health:\nok 40, warning 25, unknown 20, metadata-error 10, missing/broken 0',
        report: 'Report',
        snapshotExport: 'Export Report',
        sourceManagement: 'Source Management',
        recommendations: 'Smart Upgrade',
        operationLog: 'Operation Log',
        addSource: 'Add Source',
        deleteSource: 'Delete Source',
        deleteSourceConfirm: (platform, root) => `Delete this source from ${platform}?\\n\\n${root}\\n\\nLocal files will not be deleted.`,
        sourceDiscovery: 'Source Discovery',
        discoveryAvailable: 'Available',
        discoveryUnavailable: 'Unavailable',
        discoveryAvailableHelp: provider => `${provider} is installed. It can be used later to discover candidate cloud sources for skills without source metadata.`,
        discoveryUnavailableHelp: 'find-skills is not installed. Updates still work for skills with bound sources, and local version unification/manual source binding still work.',
        chooseDirectory: 'Choose Directory',
        rootPath: 'Root Path',
        adviceReason: 'Reason',
        adviceIntro: 'The smart check looks for updates, health issues, incomplete information, and duplicates. Changes still require confirmation.',
        smartRun: 'Skills Smart Check',
        smartRunning: 'Checking',
        smartHint: 'Click to check whether all skills are working normally.',
        smartDetecting: 'Checking all skills',
        smartStepScan: 'Scanning local skills',
        smartStepUpdate: 'Checking for updates',
        smartStepHealth: 'Checking health',
        smartStepMetadata: 'Checking information completeness',
        smartStepDuplicates: 'Checking duplicates',
        smartStepDone: 'Check complete',
        smartNoIssues: 'No issues that need action were found.',
        adviceAction: 'Suggested action',
        adviceImpact: 'Scope',
        adviceRisk: 'Risk',
        adviceCopies: 'copies',
        adviceUsage30d: '30d use',
        adviceLowRisk: 'Low. Files are kept or backed up before changes.',
        adviceMediumRisk: 'Medium. Review the affected platforms before changing files.',
        adviceHighRisk: 'High. Delete operations remove local files after backup.',
        actionUpdateAdvice: 'Check update',
        actionUnifyAdvice: 'Check / unify',
        actionDeactivateAdvice: 'Deactivate',
        actionDeleteAdvice: 'Delete',
        actionDetailsAdvice: 'View details',
        actionIgnoreAdvice: 'Ignore',
        exists: 'Exists',
        missing: 'Missing',
        issueUpdate: 'Needs update',
        issueDeactivate: 'Consider deactivating',
        issueDelete: 'Needs cleanup',
        issueMetadata: 'Incomplete info',
        issueReview: 'Needs review',
        issueHealth: 'Health issues',
        reasonNoUsage: 'No recorded usage in the last 30 days.',
        reasonMetadata: 'Version or update metadata is incomplete.',
        reasonRemoteMetadata: 'Remote update tracking metadata is incomplete.',
        reasonHealth: 'Health score is below 60.',
        reasonHealthCheck: 'Health check found issues.',
        reasonRemoteNewer: version => `Cloud version is newer: ${version}.`,
        reasonMissingPath: 'Path is missing for one or more copies.',
        reasonVersionsDiffer: versions => `Local versions differ: ${versions}.`,
        reasonCopies: count => `${count} installed copies detected.`,
        rulePathExists: 'Path exists',
        ruleSkillMd: 'Skill directories contain SKILL.md',
        ruleGithubHash: 'GitHub-backed items include github_hash',
        ruleVersionKnown: 'Version metadata is known when possible',
        ruleRemoteTracking: 'Remote-managed items include update tracking metadata',
        ruleBuiltinObserve: 'Builtin/platform capabilities are observe-only unless files are missing or unreadable',
        snapshotDone: 'Snapshot exported',
        snapshotIntro: 'This is a local Markdown snapshot of the current skill registry state.',
        snapshotPath: 'File',
        snapshotHtml: 'HTML report',
        openReport: 'Open HTML Report',
        snapshotIncludes: 'Includes',
        snapshotSections: 'Overview, 30-day usage ranking, 30-day unused list, remote update tracking gaps',
        refresh: 'Refresh',
        langToggle: '中文',
        searchPlaceholder: 'Search name',
        allPlatforms: 'All platforms',
        allImportance: 'All grades',
        allStatuses: 'All statuses',
        allSources: 'All sources',
        active: 'active',
        inactive: 'deactivated',
        broken: 'broken',
        warning: 'Warning',
        statusRunning: 'running',
        statusNotRunning: 'not running',
        statusInactive: 'deactivated',
        local: 'local',
        github: 'GitHub',
        gitlab: 'GitLab',
        bitbucket: 'Bitbucket',
        vercel: 'Vercel',
        builtin: 'builtin',
        shared: 'public',
        share: 'public',
        codex: 'Codex',
        claude_code: 'Claude Code',
        openclaw: 'OpenClaw',
        hermes: 'Hermes',
        platform: 'Agent Platform',
        name: 'Name',
        importance: 'Grade',
        important: 'Important',
        normal: 'Regular',
        low: 'Other',
        auto: 'Auto',
        importanceTitle: c => `Grade: ${c.name}`,
        importanceHint: 'Choose a manual grade, or switch back to automatic scoring.',
        importanceSaved: 'Grade saved',
        status: 'Status',
        health: 'Health',
        version: 'Version',
        thirtyDays: '30d Use',
        source: 'Source',
        description: 'Description',
        path: 'Path',
        actions: 'Actions',
        allPlatformsAction: 'All platforms',
        chooseTarget: (action, name) => `${action}: ${name}`,
        targetHint: 'Choose one platform or apply to all platforms in this row.',
        apply: 'Apply',
        total: 'Total',
        skillsMetric: 'Skills',
        copiesMetric: 'Installed copies',
        activeMetric: 'Active',
        githubMetric: 'Pending Updates',
        builtinMetric: 'Builtin',
        unused30d: 'Unused 30d',
        healthFlags: 'Health Issues',
        activate: 'Activate',
        pause: 'Deactivate',
        details: 'Details',
        detailsTitle: c => `Details: ${c.name}`,
        introduction: 'Introduction',
        paths: 'Paths',
        update: 'Update',
        updateTitle: c => `Update: ${c.name}`,
        checkingUpdate: 'Checking for updates',
        updateReady: 'A new version is available. Update now?',
        updating: 'Updating',
        localMismatchFound: versions => `Local versions differ: ${versions}. Unify to the newest local version?`,
        noUpdateFound: 'No new version found.',
        builtinUpdateHint: platform => `This is a built-in skill. Please update it through ${platform}.`,
        unsupportedUpdate: 'No supported GitHub/Vercel repository metadata was found for this capability.',
        updateAll: 'Update all',
        unifyAll: 'Unify all',
        cancel: 'Cancel',
        updated: count => `Updated ${count} local directories`,
        usage: 'Usage',
        close: 'Close',
        noUsage: 'No usage events recorded yet.',
        usageTitle: c => `Usage: ${c.name}`,
        usageTime: 'Time',
        usageFrom: 'Recorded from',
        usagePath: 'Skill folder',
        usageSourceAuto: platform => `${platform} local session log`,
        usageSourceManual: 'Manual record',
        usageSourceUnknown: 'Local record',
        usageEvidence: 'Evidence',
        delete: 'Delete',
        deleteConfirm: c => `Delete ${c.name}?\\n\\nPlatform: ${c.platform}\\nPath: ${c.path}\\n\\nA backup will be created before deletion. This operation removes the local files.`,
        scanned: count => `Scanned ${count} capabilities`,
        metaSummary: (copies, skills, scanned) => `${skills} skills · ${copies} installed copies · last scan ${scanned}`,
        healthDone: 'Health check complete',
        reportDone: 'Report generated',
        deleted: backup => `Deleted. Backup: ${backup}`
      },
      zh: {
        loading: '正在加载注册表',
        capabilities: '个能力',
        scan: '扫描',
        syncScan: 'Skills 扫描',
        moreActions: '更多',
        health: '健康检查',
        healthResult: '健康检查结果',
        healthRules: '检查规则',
        healthIssues: '问题项',
        healthNoIssues: '没有发现健康问题。',
        report: '生成报告',
        snapshotExport: '导出报告',
        sourceManagement: '来源管理',
        recommendations: '智能升级',
        operationLog: '操作日志',
        addSource: '添加来源',
        deleteSource: '删除来源',
        deleteSourceConfirm: (platform, root) => `确认从 ${platform} 删除这个来源吗？\\n\\n${root}\\n\\n这个操作只会移除来源配置，不会删除本地文件。`,
        sourceDiscovery: '来源发现',
        discoveryAvailable: '可用',
        discoveryUnavailable: '不可用',
        discoveryAvailableHelp: provider => `已检测到 ${provider}。后续可用它为缺少来源信息的 skill 发现候选云端来源。`,
        discoveryUnavailableHelp: '未检测到 find-skills。已有来源的更新、本地版本统一、手动绑定来源仍可正常使用。',
        chooseDirectory: '选择目录',
        rootPath: '目录路径',
        adviceReason: '原因',
        adviceIntro: '智能检测会检查所有 skills 是否有更新、健康异常、信息不完整和重复项。真正修改前仍会二次确认。',
        smartRun: 'Skills 智能检测',
        smartRunning: '检测中',
        smartHint: '点击检测，检测所有 skills 是否正常。',
        smartDetecting: '正在检测所有 skills',
        smartStepScan: '扫描本地 skills',
        smartStepUpdate: '检测是否有更新',
        smartStepHealth: '检测是否健康',
        smartStepMetadata: '检测信息是否完整',
        smartStepDuplicates: '检测是否有重复',
        smartStepDone: '检测完成',
        smartNoIssues: '没有发现需要处理的问题。',
        adviceAction: '建议动作',
        adviceImpact: '影响范围',
        adviceRisk: '风险',
        adviceCopies: '个副本',
        adviceUsage30d: '30天使用',
        adviceLowRisk: '低。停用只改状态，文件仍保留；修改前会提示确认。',
        adviceMediumRisk: '中。建议先确认影响的平台，再执行更新或统一版本。',
        adviceHighRisk: '高。删除会在备份后移除本地文件，需要二次确认。',
        actionUpdateAdvice: '检查更新',
        actionUnifyAdvice: '检查/统一',
        actionDeactivateAdvice: '停用',
        actionDeleteAdvice: '删除',
        actionDetailsAdvice: '查看详情',
        actionIgnoreAdvice: '忽略',
        exists: '存在',
        missing: '不存在',
        issueUpdate: '建议更新',
        issueDeactivate: '建议停用',
        issueDelete: '建议清理',
        issueMetadata: '信息不完整',
        issueReview: '需要确认',
        issueHealth: '健康异常',
        reasonNoUsage: '最近 30 天没有记录到使用。',
        reasonMetadata: '版本或更新元数据不完整。',
        reasonRemoteMetadata: '远程更新追踪信息不完整。',
        reasonHealth: '健康分低于 60。',
        reasonHealthCheck: '健康检查发现异常。',
        reasonRemoteNewer: version => `云端版本更新：${version}。`,
        reasonMissingPath: '一个或多个副本的路径不存在。',
        reasonVersionsDiffer: versions => `本地版本不一致：${versions}。`,
        reasonCopies: count => `检测到 ${count} 个安装副本。`,
        rulePathExists: '路径存在',
        ruleSkillMd: 'Skill 目录包含 SKILL.md',
        ruleGithubHash: 'GitHub 来源包含 github_hash',
        ruleVersionKnown: '尽可能识别版本元数据',
        ruleRemoteTracking: '远程托管能力包含更新追踪元数据',
        ruleBuiltinObserve: '内置/官方能力默认只观察，除非文件缺失或无法读取',
        snapshotDone: '导出快照完成',
        snapshotIntro: '这是一份本地 Markdown 快照，用来留档当前 skills 注册表状态。',
        snapshotPath: '文件',
        snapshotHtml: 'HTML 报告',
        openReport: '打开 HTML 报告',
        snapshotIncludes: '包含内容',
        snapshotSections: '总览统计、30天使用排行、30天未使用列表、远程更新追踪缺失列表',
        refresh: '刷新',
        langToggle: 'English',
        searchPlaceholder: '搜索名称',
        allPlatforms: '全部平台',
        allImportance: '全部等级',
        allStatuses: '全部状态',
        allSources: '全部来源',
        active: '启用',
        inactive: '停用',
        broken: '故障',
        warning: '警告',
        statusRunning: '运行中',
        statusNotRunning: '未运行',
        statusInactive: '停用',
        local: '本地',
        github: 'GitHub',
        gitlab: 'GitLab',
        bitbucket: 'Bitbucket',
        vercel: 'Vercel',
        builtin: '内置',
        shared: '公共',
        share: '公共',
        codex: 'Codex',
        claude_code: 'Claude Code',
        openclaw: 'OpenClaw',
        hermes: 'Hermes',
        platform: 'Agent 平台',
        name: '名称',
        importance: '等级',
        important: '重要',
        normal: '常规',
        low: '其他',
        auto: '自动',
        importanceTitle: c => `等级：${c.name}`,
        importanceHint: '请选择手动等级，或切回自动评分。',
        importanceSaved: '等级已保存',
        status: '状态',
        healthCheck: '健康检查',
        healthScore: '健康分',
        healthHelp: '健康分 = 状态分（30）+ 质量分（30）+ 结构健康（40）\n\n内置/官方能力只观察：30天未使用、缺少远程元数据、没有云端来源不会扣分。\n\n状态分：\n启用 30，停用 18，故障 0\n\n质量分：\n普通 skill 按 30天使用 + 版本 + 描述 + 来源追踪计算；内置能力只看版本、描述和来源识别。\n\n结构健康：\nok 40，warning 25，unknown 20，metadata-error 10，missing/broken 0',
        version: '版本',
        thirtyDays: '30天使用',
        source: '来源',
        description: '描述',
        path: '路径',
        actions: '操作',
        allPlatformsAction: '全部平台',
        chooseTarget: (action, name) => `${action}：${name}`,
        targetHint: '',
        apply: '确认',
        total: '总数',
        skillsMetric: 'Skills',
        copiesMetric: '安装副本',
        activeMetric: '已启用',
        githubMetric: '待更新',
        builtinMetric: '内置',
        unused30d: '30天未用',
        healthFlags: '健康异常',
        activate: '启用',
        pause: '停用',
        details: '详情',
        detailsTitle: c => `详情：${c.name}`,
        introduction: '介绍',
        paths: '路径',
        update: '更新',
        updateTitle: c => `更新：${c.name}`,
        checkingUpdate: '正在检查新版本',
        updateReady: '发现新版本，是否现在更新？',
        updating: '更新中',
        localMismatchFound: versions => `检测到本地版本不一致：${versions}。是否统一到本地最新版本？`,
        noUpdateFound: '当前没有找到新版本。',
        builtinUpdateHint: platform => `这是内置 skill，请通过 ${platform} 更新。`,
        unsupportedUpdate: '没有找到可支持的 GitHub/Vercel 仓库元数据。',
        updateAll: '全部更新',
        unifyAll: '全部统一',
        cancel: '取消',
        updated: count => `已更新 ${count} 个本地目录`,
        usage: '使用记录',
        close: '关闭',
        noUsage: '暂无使用记录。',
        usageTitle: c => `使用记录：${c.name}`,
        usageTime: '使用时间',
        usageFrom: '记录来源',
        usagePath: 'Skill 目录',
        usageSourceAuto: platform => `${platform} 本地会话记录`,
        usageSourceManual: '手动记录',
        usageSourceUnknown: '本地记录',
        usageEvidence: '证据',
        delete: '删除',
        deleteConfirm: c => `确认删除 ${c.name} 吗？\\n\\n平台：${c.platform}\\n路径：${c.path}\\n\\n删除前会自动备份。确认后会删除本地文件。`,
        scanned: count => `已扫描 ${count} 个能力`,
        metaSummary: (copies, skills, scanned) => `${skills} 个 skills · ${copies} 个安装副本 · 最近扫描 ${scanned}`,
        healthDone: '健康检查完成',
        reportDone: '报告已生成',
        deleted: backup => `已删除。备份：${backup}`
      }
    };
    const t = key => dict[lang][key] || dict.en[key] || key;
    const $ = id => document.getElementById(id);
    const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    function closeMoreMenus() {
      document.querySelectorAll('.more-menu[open]').forEach(menu => menu.removeAttribute('open'));
    }
    const toast = msg => {
      $('toast').textContent = msg;
      $('toast').classList.add('show');
      setTimeout(() => $('toast').classList.remove('show'), 2200);
    };
    function stopLoadingDots() {
      if (loadingTimer) {
        clearInterval(loadingTimer);
        loadingTimer = null;
      }
    }
    function showLoadingMessage(key) {
      stopLoadingDots();
      let count = 1;
      const renderDots = () => {
        $('actionBody').innerHTML = `<div class="empty">${esc(t(key))}<span class="loading-dots">${'.'.repeat(count)}</span></div>`;
        count = count >= 3 ? 1 : count + 1;
      };
      renderDots();
      loadingTimer = setInterval(renderDots, 420);
    }
    function showDetailLoadingMessage(key) {
      stopLoadingDots();
      let count = 1;
      const renderDots = () => {
        $('detailBody').innerHTML = `<div class="empty">${esc(t(key))}<span class="loading-dots">${'.'.repeat(count)}</span></div>`;
        count = count >= 3 ? 1 : count + 1;
      };
      renderDots();
      loadingTimer = setInterval(renderDots, 420);
    }
    async function api(path, body) {
      const res = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body || {})});
      const data = await res.json();
      if (!res.ok || data.error) throw new Error(data.error || res.statusText);
      return data;
    }
    async function load() {
      const res = await fetch('/api/state');
      state = await res.json();
      noUpdateGroups.clear();
      applyI18n();
      fillFilters();
      render();
    }
    function applyI18n() {
      document.documentElement.lang = lang === 'zh' ? 'zh-CN' : 'en';
      $('langToggle').textContent = t('langToggle');
      $('healthHelp').setAttribute('data-tip', t('healthHelp'));
      document.querySelectorAll('[data-i18n]').forEach(el => { el.textContent = t(el.dataset.i18n); });
      document.querySelectorAll('[data-i18n-placeholder]').forEach(el => { el.placeholder = t(el.dataset.i18nPlaceholder); });
    }
    function label(value) {
      return t(value) || value;
    }
    function issueLabel(type) {
      return t(`issue${String(type || '').charAt(0).toUpperCase()}${String(type || '').slice(1)}`) || type;
    }
    function ruleLabel(rule) {
      const map = {
        'Path exists': 'rulePathExists',
        'Skill directories contain SKILL.md': 'ruleSkillMd',
        'GitHub-backed items include github_hash': 'ruleGithubHash',
        'Version metadata is known when possible': 'ruleVersionKnown',
        'Remote-managed items include update tracking metadata': 'ruleRemoteTracking',
        'Builtin/platform capabilities are observe-only unless files are missing or unreadable': 'ruleBuiltinObserve'
      };
      return t(map[rule]) || rule;
    }
    function recommendationReason(item) {
      const reason = item.reason || '';
      if (reason === 'No recorded usage in the last 30 days.') return t('reasonNoUsage');
      if (reason === 'Version or update metadata is incomplete.') return t('reasonMetadata');
      if (reason === 'Remote update tracking metadata is incomplete.') return t('reasonRemoteMetadata');
      if (reason === 'Health score is below 60.') return t('reasonHealth');
      if (reason === 'Health check found issues.') return t('reasonHealthCheck');
      if (reason === 'Path is missing for one or more copies.') return t('reasonMissingPath');
      if (reason.startsWith('Update check failed:')) return t('reasonRemoteMetadata');
      let match = reason.match(/^Local versions differ: (.*)\\.$/);
      if (match) return t('reasonVersionsDiffer')(match[1]);
      match = reason.match(/^Remote version is newer: (.*)\\.$/);
      if (match) return t('reasonRemoteNewer')(match[1]);
      match = reason.match(/^(\\d+) installed copies detected\\.$/);
      if (match) return t('reasonCopies')(match[1]);
      return reason;
    }
    function platformRank(platform) {
      const order = { shared: 1, share: 1, codex: 2, claude_code: 3, openclaw: 4, hermes: 5 };
      return order[platform] || 99;
    }
    function comparePlatform(a, b) {
      const rank = platformRank(a) - platformRank(b);
      return rank || label(a).localeCompare(label(b));
    }
    function sourceRank(source) {
      const order = { builtin: 1, github: 2, gitlab: 3, bitbucket: 4, vercel: 5, local: 99 };
      return order[source] || 50;
    }
    function compareSource(a, b) {
      const rank = sourceRank(a) - sourceRank(b);
      return rank || label(a).localeCompare(label(b));
    }
    function latestCapability(items) {
      return [...items].sort((a, b) => {
        const at = Date.parse(a.local_updated_at || a.last_scanned_at || '') || 0;
        const bt = Date.parse(b.local_updated_at || b.last_scanned_at || '') || 0;
        return bt - at || comparePlatform(a.platform, b.platform) || a.path.localeCompare(b.path);
      })[0] || items[0];
    }
    function sortArrow(key) {
      if (sortState.key !== key) return '';
      return sortState.direction === 'desc' ? '↓' : '↑';
    }
    function updateSortIndicators() {
      document.querySelectorAll('[data-sort-arrow]').forEach(el => {
        el.textContent = sortArrow(el.dataset.sortArrow);
      });
    }
    function setSort(key) {
      if (sortState.key === key) {
        sortState.direction = sortState.direction === 'desc' ? 'asc' : 'desc';
      } else {
        sortState.key = key;
        sortState.direction = ['name', 'platform', 'status', 'version', 'source'].includes(key) ? 'asc' : 'desc';
      }
      render();
    }
    function toggleLang() {
      lang = lang === 'zh' ? 'en' : 'zh';
      localStorage.setItem('asm.lang', lang);
      applyI18n();
      fillFilters();
      render();
    }
    function fillFilters() {
      const current = $('platform').value;
      const sourceCurrent = $('source').value;
      const importanceCurrent = $('importanceFilter').value;
      const platforms = [...new Set([...state.capabilities.map(c => c.platform), 'claude_code', 'shared'])].sort(comparePlatform);
      const sources = [...new Set(state.capabilities.map(c => c.source_type || 'local'))].sort(compareSource);
      $('platform').innerHTML = `<option value="">${esc(t('allPlatforms'))}</option>` + platforms.map(p => `<option value="${esc(p)}">${esc(label(p))}</option>`).join('');
      $('platform').value = current;
      $('source').innerHTML = `<option value="">${esc(t('allSources'))}</option>` + sources.map(s => `<option value="${esc(s)}">${esc(label(s))}</option>`).join('');
      $('source').value = sources.includes(sourceCurrent) ? sourceCurrent : '';
      $('importanceFilter').value = importanceCurrent;
      updateSortIndicators();
    }
    function filtered() {
      const q = $('q').value.toLowerCase();
      const p = $('platform').value;
      const s = $('status').value;
      const src = $('source').value;
      return state.capabilities.filter(c => {
        const hay = String(c.name || '').toLowerCase();
        return (!q || hay.includes(q)) && (!p || c.platform === p) && (!s || c.status === s) && (!src || c.source_type === src);
      });
    }
    function smartRecommendations() {
      return ((state.smart_upgrade || {}).recommendations || []).filter(Boolean);
    }
    function hasSmartUpgradeReport() {
      return Boolean(state.smart_upgrade && (state.smart_upgrade.checked || state.smart_upgrade.snapshot_cache_hit));
    }
    function smartIssueTypesForName(name) {
      const key = String(name || '').toLowerCase();
      return [...new Set(smartRecommendations()
        .filter(item => String(item.name || '').toLowerCase() === key)
        .map(item => item.type)
        .filter(Boolean))];
    }
    function groupCapabilities(rows) {
      const map = new Map();
      rows.forEach(c => {
        const key = String(c.name || '').toLowerCase();
        if (!map.has(key)) map.set(key, { key, name: c.name, items: [] });
        map.get(key).items.push(c);
      });
      return [...map.values()].map(group => {
        group.items.sort((a, b) => comparePlatform(a.platform, b.platform) || a.path.localeCompare(b.path));
        group.primary = group.items.find(c => c.status === 'active') || group.items[0];
        group.platforms = [...new Set(group.items.map(c => c.platform))];
        group.source = latestCapability(group.items).source_type || 'local';
        group.sources = [group.source];
        group.statuses = [...new Set(group.items.map(c => c.status))];
        group.smartIssues = smartIssueTypesForName(group.name);
        group.displayStatus = aggregateDisplayStatus(group.items);
        group.versions = [...new Set(group.items.map(c => c.version || 'unknown'))];
        group.usage30 = group.items.reduce((sum, c) => sum + Number(c.usage_30d || 0), 0);
        group.score = Math.round(group.items.reduce((sum, c) => sum + healthScore(c).total, 0) / group.items.length);
        group.statusRank = Math.max(...group.items.map(c => statusRank(c.status)));
        group.importanceOverride = latestCapability(group.items).importance_override || '';
        group.importance = group.importanceOverride || autoImportance(group);
        group.importanceRank = importanceRank(group.importance);
        return group;
      });
    }
    function statusRank(status) {
      if (status === 'active') return 3;
      if (status === 'inactive') return 2;
      if (status === 'broken') return 1;
      return 0;
    }
    function displayStatusTags(c) {
      if (c.status !== 'active') return ['statusInactive'];
      const last = Date.parse(c.last_used_at || '') || 0;
      const age = last ? Date.now() - last : Infinity;
      return last && age <= 24 * 60 * 60 * 1000 ? ['statusRunning'] : ['statusNotRunning'];
    }
    function aggregateDisplayStatus(items) {
      const statuses = new Set(items.flatMap(displayStatusTags));
      if (statuses.has('statusRunning')) return 'statusRunning';
      if (statuses.has('statusNotRunning')) return 'statusNotRunning';
      return 'statusInactive';
    }
    function sortedGroups(groups) {
      const direction = sortState.direction === 'asc' ? 1 : -1;
      const value = group => {
        if (sortState.key === 'name') return group.name.toLowerCase();
        if (sortState.key === 'platform') return group.platforms.map(label).join(' ');
        if (sortState.key === 'status') return group.statusRank;
        if (sortState.key === 'importance') return group.importanceRank;
        if (sortState.key === 'health') return group.score;
        if (sortState.key === 'version') return group.versions.join(', ');
        if (sortState.key === 'source') return label(group.source);
        return group.usage30;
      };
      return [...groups].sort((a, b) => {
        const av = value(a);
        const bv = value(b);
        let result = 0;
        if (typeof av === 'string') result = av.localeCompare(String(bv));
        else result = av - bv;
        if (result === 0) result = a.name.localeCompare(b.name);
        return result * direction;
      });
    }
    function applyNonMetricGroupFilters(groups) {
      const importance = $('importanceFilter').value;
      return groups.filter(group => !importance || group.importance === importance);
    }
    function applyMetricGroupFilter(groups) {
      return groups.filter(group => {
        if (metricFilter === 'active' && !group.items.some(c => c.status === 'active')) return false;
        if (metricFilter === 'pendingUpdate' && (hasSmartUpgradeReport() ? !group.smartIssues.includes('update') : group.versions.length <= 1)) return false;
        if (metricFilter === 'builtin' && group.source !== 'builtin') return false;
        if (metricFilter === 'unused' && group.usage30 !== 0) return false;
        if (metricFilter === 'lowHealth' && (hasSmartUpgradeReport() ? !group.smartIssues.includes('health') : group.score >= 60)) return false;
        return true;
      });
    }
    function applyGroupFilters(groups) {
      return applyMetricGroupFilter(applyNonMetricGroupFilters(groups));
    }
    function resetListView() {
      metricFilter = '';
      sortState = { key: 'usage', direction: 'desc' };
      $('q').value = '';
      $('importanceFilter').value = '';
      $('platform').value = '';
      $('status').value = '';
      $('source').value = '';
      render();
    }
    function setMetricFilter(filter) {
      metricFilter = filter;
      render();
    }
    function latestScanLabel() {
      const stamps = state.capabilities.map(c => Date.parse(c.last_scanned_at || '') || 0).filter(Boolean);
      if (!stamps.length) return '-';
      return new Date(Math.max(...stamps)).toLocaleString(lang === 'zh' ? 'zh-CN' : 'en-US', {hour12: false});
    }
    function updateMeta(groups) {
      $('meta').textContent = t('metaSummary')(state.capabilities.length, groups.length, latestScanLabel());
    }
    function renderMetrics(groups) {
      const all = groups.flatMap(group => group.items);
      const skills = groups.length;
      const copies = all.length;
      const active = groups.filter(group => group.items.some(c => c.status === 'active')).length;
      const pendingUpdate = hasSmartUpgradeReport()
        ? groups.filter(group => group.smartIssues.includes('update')).length
        : groups.filter(group => group.versions.length > 1).length;
      const builtin = groups.filter(group => group.source === 'builtin').length;
      const unused = groups.filter(group => group.usage30 === 0).length;
      const lowHealth = hasSmartUpgradeReport()
        ? groups.filter(group => group.smartIssues.includes('health')).length
        : groups.filter(group => group.score < 60).length;
      const items = [
        ['reset', t('skillsMetric'), skills],
        ['reset', t('copiesMetric'), copies],
        ['active', t('activeMetric'), active],
        ['pendingUpdate', t('githubMetric'), pendingUpdate],
        ['builtin', t('builtinMetric'), builtin],
        ['unused', t('unused30d'), unused],
        ['lowHealth', t('healthFlags'), lowHealth]
      ];
      $('metrics').innerHTML = items.map(([filter, label, value]) => {
        if (filter === 'reset') return `<button class="metric" onclick="resetListView()"><strong>${value}</strong><span>${label}</span></button>`;
        return `<button class="metric ${metricFilter === filter ? 'active-filter' : ''}" onclick="setMetricFilter('${filter}')"><strong>${value}</strong><span>${label}</span></button>`;
      }).join('');
    }
    function statusScore(c) {
      if (c.status === 'active') return 30;
      if (c.status === 'inactive') return 18;
      return 0;
    }
    function qualityScore(c) {
      if (c.management_scope === 'builtin_observe_only') {
        const version = c.version && c.version !== 'unknown' ? 8 : 6;
        const description = c.description ? 8 : 5;
        return version + description + 14;
      }
      const uses = Number(c.usage_30d || 0);
      const usage = uses >= 10 ? 18 : uses >= 3 ? 14 : uses >= 1 ? 10 : 4;
      const version = c.version && c.version !== 'unknown' ? 4 : 0;
      const description = c.description ? 3 : 0;
      const tracked = c.source_type === 'github' ? (c.github_url && c.github_hash ? 5 : 1) : (c.source_type ? 5 : 0);
      return usage + version + description + tracked;
    }
    function structureScore(c) {
      if (c.health === 'ok') return 40;
      if (c.health === 'warning') return 25;
      if (c.health === 'unknown') return 20;
      if (c.health === 'metadata-error') return 10;
      return 0;
    }
    function healthScore(c) {
      const status = statusScore(c);
      const quality = qualityScore(c);
      const structure = structureScore(c);
      return { status, quality, structure, total: status + quality + structure };
    }
    function scoreClass(score) {
      return score >= 80 ? 'score-good' : score >= 60 ? 'score-mid' : 'score-low';
    }
    function scoreTitle(c, score) {
      if (c.management_scope === 'builtin_observe_only') {
        return lang === 'zh'
          ? `状态分：${score.status}/30 | 质量分：${score.quality}/30 | 结构健康：${score.structure}/40 | 内置/官方能力只观察`
          : `Status: ${score.status}/30 | Quality: ${score.quality}/30 | Structure: ${score.structure}/40 | builtin observe-only`;
      }
      return `Status: ${score.status}/30 | Quality: ${score.quality}/30 | Structure: ${score.structure}/40 | Structure state: ${c.health}`;
    }
    function importanceRank(value) {
      return { important: 3, normal: 2, low: 1 }[value] || 0;
    }
    function isSystemOptimization(group) {
      const names = [
        'skill-manager',
        'skill-creator',
        'skill-installer',
        'skill-vetter',
        'skill-overlap-manager',
        'find-skills',
        'computer-use',
        'control-in-app-browser',
        'control-chrome',
        'openai-docs',
        'plugin-creator'
      ];
      const normalizedName = String(group.name || '').toLowerCase();
      return names.includes(normalizedName)
        || normalizedName.includes('manager')
        || normalizedName.includes('installer')
        || normalizedName.includes('vetter');
    }
    function autoImportance(group) {
      const builtin = group.items.some(c => c.source_type === 'builtin' || c.management_scope === 'builtin_observe_only');
      if (builtin || isSystemOptimization(group)) return 'important';
      if (group.usage30 > 0) return 'normal';
      return 'low';
    }
    function importanceClass(value) {
      return `importance-${value}`;
    }
    function groupByKey(key) {
      return groupCapabilities(filtered()).find(group => group.key === key);
    }
    function tags(values, cls = 'pill') {
      return values.map(value => `<span class="${cls}">${esc(label(value))}</span>`).join('');
    }
    function isEditableSkillItem(c) {
      return c && c.kind === 'skill' && c.source_type !== 'builtin' && !String(c.path || '').toLowerCase().endsWith('.md');
    }
    function updateGateDisabled(group) {
      return group.items.some(c => c.update_gate && c.update_gate.disabled);
    }
    function canUpdateGroup(group) {
      return group.items.some(isEditableSkillItem) && !noUpdateGroups.has(group.key) && !updateGateDisabled(group);
    }
    function translateDescriptionToZh(text, name = '') {
      let value = String(text || '').trim();
      if (!value) return '暂无中文介绍。';
      const replacements = [
        [/Public-records OSINT investigation framework/gi, '公开记录 OSINT 调查框架'],
        [/Investigative framework for public-records OSINT/gi, '面向公开记录 OSINT 的调查框架'],
        [/SEC EDGAR filings/gi, 'SEC EDGAR 公司披露文件'],
        [/USAspending contracts/gi, 'USAspending 政府合同'],
        [/Senate lobbying/gi, '参议院游说披露'],
        [/OFAC sanctions/gi, 'OFAC 制裁名单'],
        [/ICIJ offshore leaks/gi, 'ICIJ 离岸泄露数据'],
        [/NYC property records \(ACRIS\)/gi, '纽约房产记录（ACRIS）'],
        [/OpenCorporates registries/gi, 'OpenCorporates 公司注册库'],
        [/CourtListener court records/gi, 'CourtListener 法院记录'],
        [/Wayback Machine archives/gi, 'Wayback Machine 网页存档'],
        [/Wikipedia \+ Wikidata/gi, 'Wikipedia 和 Wikidata'],
        [/GDELT news monitoring/gi, 'GDELT 新闻监测'],
        [/government contracts/gi, '政府合同'],
        [/corporate filings/gi, '公司文件'],
        [/lobbying/gi, '游说记录'],
        [/sanctions/gi, '制裁记录'],
        [/offshore leaks/gi, '离岸泄露数据'],
        [/property records/gi, '房产记录'],
        [/court records/gi, '法院记录'],
        [/web archives/gi, '网页存档'],
        [/knowledge bases/gi, '知识库'],
        [/global news/gi, '全球新闻'],
        [/Entity resolution across sources/gi, '跨来源实体解析'],
        [/Resolve entities across heterogeneous sources/gi, '在异构数据源之间解析实体'],
        [/cross-link analysis/gi, '交叉链接分析'],
        [/build cross-links with explicit confidence/gi, '构建带明确信心等级的交叉链接'],
        [/timing correlation/gi, '时间关联分析'],
        [/run statistical timing tests/gi, '运行统计时间检验'],
        [/evidence chains/gi, '证据链'],
        [/produce structured evidence chains/gi, '生成结构化证据链'],
        [/Python stdlib only/gi, '仅使用 Python 标准库'],
        [/Zero install/gi, '无需额外安装'],
        [/Works on Linux, macOS, Windows/gi, '支持 Linux、macOS 和 Windows'],
        [/optional free token/gi, '可选免费令牌'],
        [/raises rate limits/gi, '可提高速率限制'],
        [/No API key/gi, '无需 API key'],
        [/API key/gi, 'API key']
      ];
      replacements.forEach(([pattern, replacement]) => { value = value.replace(pattern, replacement); });
      value = value
        .replace(/\s+—\s+/g, '：')
        .replace(/\s*;\s*/g, '；')
        .replace(/\s*,\s*/g, '，')
        .replace(/\.\s+/g, '。')
        .replace(/\.$/, '。');
      if (/^[\x00-\x7F\s，。：；（）+.-]+$/.test(value)) {
        return `用于支持「${name || '该 skill'}」相关任务。原始介绍缺少中文内容，建议在 SKILL.md 中补充中文 description。`;
      }
      return value;
    }
    function localizedDescription(text, name = '') {
      const value = String(text || '').trim();
      if (!value) return lang === 'zh' ? '暂无中文介绍。' : '';
      const firstEnglish = value.search(/[A-Za-z][A-Za-z ,.'()/-]{20,}/);
      if (lang === 'zh') {
        const chinese = value.match(/[\u4e00-\u9fff][\s\S]*/);
        if (chinese) return chinese[0].trim();
        if (firstEnglish > 0) return value.slice(0, firstEnglish).trim().replace(/[。；，,;:：\\s]+$/, '');
        return translateDescriptionToZh(value, name);
      }
      if (firstEnglish >= 0) return value.slice(firstEnglish).trim();
      return value;
    }
    function formatUsageTime(value) {
      const parsed = Date.parse(value || '');
      if (!parsed) return value || '-';
      return new Date(parsed).toLocaleString(lang === 'zh' ? 'zh-CN' : 'en-US', {hour12: false});
    }
    function usageSourceLabel(source) {
      const value = String(source || '');
      if (value === 'codex-log') return t('usageSourceAuto')('Codex');
      if (value === 'claude-log') return t('usageSourceAuto')('Claude Code');
      if (value === 'openclaw-log') return t('usageSourceAuto')('OpenClaw');
      if (value === 'hermes-log') return t('usageSourceAuto')('Hermes');
      if (value === 'ui' || value === 'manual') return t('usageSourceManual');
      if (value === 'session-log') return t('usageSourceAuto')('Agent');
      return t('usageSourceUnknown');
    }
    function usageEventCard(platform, event, path = '') {
      const platformLabel = label(platform);
      return `
        <div class="event">
          <div class="usage-summary">
            <div class="usage-topline">
              <span class="usage-platform">${esc(platformLabel)}</span>
              <span class="usage-time">${esc(formatUsageTime(event.occurred_at))}</span>
            </div>
            <div class="usage-row">
              <div class="usage-label">${esc(t('usageFrom'))}</div>
              <div class="usage-value">${esc(usageSourceLabel(event.source))}</div>
            </div>
            <div class="usage-row">
              <div class="usage-label">${esc(t('usagePath'))}</div>
              <div class="usage-value usage-path">${esc(path || '')}</div>
            </div>
          </div>
        </div>`;
    }
    function usageEmptyCard(platform, paths = []) {
      const platformLabel = label(platform);
      return `
        <div class="event">
          <div class="usage-summary">
            <div class="usage-topline">
              <span class="usage-platform">${esc(platformLabel)}</span>
              <span class="usage-empty">${esc(t('noUsage'))}</span>
            </div>
            <div class="usage-row">
              <div class="usage-label">${esc(t('usagePath'))}</div>
              <div class="usage-value usage-path">${esc(paths.filter(Boolean).join('\\n'))}</div>
            </div>
          </div>
        </div>`;
    }
    function render() {
      const allGroups = groupCapabilities(state.capabilities);
      const baseGroups = groupCapabilities(filtered());
      const metricBaseGroups = applyNonMetricGroupFilters(baseGroups);
      const groups = sortedGroups(applyMetricGroupFilter(metricBaseGroups));
      updateMeta(allGroups);
      renderMetrics(metricBaseGroups);
      updateSortIndicators();
      $('rows').innerHTML = groups.map(group => {
        const keyArg = JSON.stringify(group.key);
        const primary = group.primary;
        const toggleAction = group.items.some(c => c.status === 'active') ? 'inactive' : 'active';
        const toggleActionArg = JSON.stringify(toggleAction);
        const updateButton = canUpdateGroup(group)
          ? `<button class="tiny" onclick='openUpdate(${keyArg})'>${esc(t('update'))}</button>`
          : `<button class="tiny" disabled>${esc(t('update'))}</button>`;
        return `
        <tr>
          <td><strong>${esc(group.name)}</strong></td>
          <td><button class="importance-button ${importanceClass(group.importance)}" onclick='openImportance(${keyArg})'>${esc(t(group.importance))}</button></td>
          <td><div class="platform-tags">${tags(group.platforms, 'platform-tag')}</div></td>
          <td><div class="status-tags">${tags([group.displayStatus], 'pill')}</div></td>
          <td><span class="pill ${scoreClass(group.score)}">${group.score}</span></td>
          <td>${esc(group.versions.length === 1 ? group.versions[0] : group.versions.join(', '))}</td>
          <td>${group.usage30}${lang === 'zh' ? '次' : 'x'}</td>
          <td><div class="source-tags">${tags(group.sources, 'pill')}</div></td>
          <td><div class="row-actions">
            <span class="action-group">
              <button class="tiny" onclick='showDetails(${keyArg})'>${esc(t('details'))}</button>
              <button class="tiny" onclick='showGroupUsage(${keyArg})'>${esc(t('usage'))}</button>
              <button class="tiny" onclick='openAction(${keyArg},${toggleActionArg})'>${esc(actionLabel(toggleAction))}</button>
              <button class="tiny danger" onclick='openAction(${keyArg},"delete")'>${esc(t('delete'))}</button>
              ${updateButton}
            </span>
          </div></td>
        </tr>`;
      }).join('');
    }
    async function showSources() {
      closeMoreMenus();
      try {
        const data = await api('/api/sources');
        const discovery = state.source_discovery || {};
        const discoveryText = discovery.available ? t('discoveryAvailable') : t('discoveryUnavailable');
        const discoveryHelp = discovery.available ? t('discoveryAvailableHelp')(discovery.provider || 'find-skills') : t('discoveryUnavailableHelp');
        $('detailTitle').textContent = t('sourceManagement');
        $('detailBody').innerHTML = `
          <div class="detail-grid">
            <div class="detail-row"><div class="detail-label">${esc(t('sourceDiscovery'))}</div><div class="detail-value">
              <span class="pill ${discovery.available ? 'score-good' : 'score-mid'}">${esc(discoveryText)}</span>
              <div class="event-meta" style="margin-top:6px">${esc(discoveryHelp)}</div>
              ${discovery.path ? `<div class="event-meta">${esc(discovery.path)}</div>` : ''}
            </div></div>
            <div class="detail-row"><div class="detail-label">${esc(t('addSource'))}</div><div class="detail-value">
              <div class="row-actions">
                <select id="sourcePlatform" style="width:160px">${['shared','codex','claude_code','openclaw','hermes'].map(p => `<option value="${p}">${esc(label(p))}</option>`).join('')}</select>
                <input id="sourceRoot" placeholder="${esc(t('rootPath'))}" style="min-width:280px" readonly>
                <button onclick="chooseSourceDirectory()">${esc(t('chooseDirectory'))}</button>
                <button onclick="addSource()">${esc(t('addSource'))}</button>
              </div>
            </div></div>
            <div class="detail-row"><div class="detail-label">${esc(t('source'))}</div><div class="detail-value">${sourceTable(data.sources || [])}</div></div>
          </div>`;
        $('detailModal').classList.add('open');
      } catch(e) {
        toast(e.message);
      }
    }
    function sourceTable(sources) {
      const sorted = [...sources].sort((a, b) => comparePlatform(a.platform, b.platform) || a.expanded.localeCompare(b.expanded));
      const groups = sorted.reduce((map, source) => {
        if (!map.has(source.platform)) map.set(source.platform, []);
        map.get(source.platform).push(source);
        return map;
      }, new Map());
      return [...groups.entries()].map(([platform, entries]) => `
        <div class="event" style="margin-bottom:10px">
          <div class="event-meta">${esc(label(platform))} · ${esc(entries.length)} ${esc(t('source'))}</div>
          <div class="choice-list">${entries.map(source => `
            <div class="choice">
              <div>
                <strong>${esc(source.root)}</strong>
                <small>${esc(source.expanded)} · ${esc(source.exists ? t('exists') : t('missing'))} · ${esc(t('total'))}: ${esc(source.count)} · ${esc(t('status'))}: ${esc(source.enabled ? t('active') : t('inactive'))}</small>
              </div>
              <div class="row-actions">
                <button class="tiny" onclick='toggleSource(${JSON.stringify(source.platform)},${JSON.stringify(source.root)},${JSON.stringify(!source.enabled)})'>${esc(source.enabled ? t('pause') : t('activate'))}</button>
                <button class="tiny danger" onclick='removeSource(${JSON.stringify(source.platform)},${JSON.stringify(source.root)})'>${esc(t('deleteSource'))}</button>
              </div>
            </div>`).join('')}</div>
        </div>`).join('');
    }
    async function addSource() {
      const platform = $('sourcePlatform').value;
      const root = $('sourceRoot').value.trim();
      if (!root) return toast(t('rootPath'));
      await api('/api/source-update', {action: 'add', platform, root});
      toast(t('addSource'));
      await showSources();
      await load();
    }
    async function chooseSourceDirectory() {
      try {
        const result = await api('/api/pick-directory');
        $('sourceRoot').value = result.path || '';
      } catch(e) {
        toast(e.message);
      }
    }
    async function removeSource(platform, root) {
      if (!confirm(t('deleteSourceConfirm')(label(platform), root))) return;
      await api('/api/source-update', {action: 'remove', platform, root});
      toast(t('deleteSource'));
      await showSources();
      await load();
    }
    async function toggleSource(platform, root, enabled) {
      await api('/api/source-update', {action: 'toggle', platform, root, enabled});
      await showSources();
      await load();
    }
    function showRecommendations() {
      closeMoreMenus();
      $('detailTitle').textContent = t('recommendations');
      renderSmartUpgradeStart(false, 'smartHint');
      $('detailModal').classList.add('open');
    }
    function renderSmartUpgradeStart(running, statusKey, detail = '') {
      const detailText = detail ? `：${detail}` : '';
      $('detailBody').innerHTML = `
        <div class="smart-upgrade-panel">
          <button class="primary" onclick="runSmartUpgrade()" ${running ? 'disabled' : ''}>${esc(running ? t('smartRunning') : t('smartRun'))}</button>
          <div class="smart-upgrade-hint">${esc(t(statusKey))}${esc(detailText)}${running ? '<span class="loading-dots">...</span>' : ''}</div>
        </div>`;
    }
    function smartStageKey(stage) {
      const map = {
        scan: 'smartStepScan',
        update: 'smartStepUpdate',
        health: 'smartStepHealth',
        metadata: 'smartStepMetadata',
        duplicates: 'smartStepDuplicates',
        done: 'smartStepDone'
      };
      return map[stage] || 'smartDetecting';
    }
    function smartProgressDetail(job) {
      const parts = [];
      if (job.current) parts.push(job.current);
      if (job.total) parts.push(`${job.index || 0}/${job.total}`);
      return parts.join(' ');
    }
    function smartHealthItems() {
      return groupCapabilities(state.capabilities)
        .filter(group => group.score < 60)
        .map(group => ({
          type: 'health',
          severity: 'medium',
          name: group.name,
          ids: group.items.map(c => c.id),
          platforms: group.platforms,
          versions: group.versions,
          paths: group.items.map(c => c.path),
          usage_30d: group.usage30,
          copy_count: group.items.length,
          score: group.score,
          reason: 'Health score is below 60.'
        }));
    }
    async function runSmartUpgrade() {
      try {
        renderSmartUpgradeStart(true, 'smartStepScan');
        const start = await api('/api/smart-upgrade-start');
        let data = null;
        while (true) {
          await new Promise(resolve => setTimeout(resolve, 1000));
          const job = await api('/api/smart-upgrade-status', {job_id: start.job_id});
          if (job.status === 'error') throw new Error(job.error || 'Smart upgrade failed');
          renderSmartUpgradeStart(true, smartStageKey(job.stage), smartProgressDetail(job));
          if (job.status === 'done') {
            data = job.result || {};
            break;
          }
        }
        const res = await fetch('/api/state');
        const freshState = await res.json();
        state = freshState;
        applyI18n();
        fillFilters();
        render();
        const recommendationItems = data.recommendations || [];
        stopLoadingDots();
        const items = recommendationItems;
        const counts = data.counts || {};
        const metricItems = [
          ['update', counts.update || 0],
          ['health', counts.health || 0],
          ['metadata', counts.metadata || 0],
          ['review', counts.review || 0]
        ];
        $('detailBody').innerHTML = `
          <div class="metrics" style="grid-template-columns: repeat(4, minmax(90px, 1fr)); margin-bottom:12px">
            ${metricItems.map(([key, value]) => `<div class="metric"><strong>${esc(value)}</strong><span>${esc(issueLabel(key))}</span></div>`).join('')}
          </div>
          <div class="choice-list">${items.map(item => recommendationCard(item)).join('') || `<div class="empty">${esc(t('smartNoIssues'))}</div>`}</div>`;
      } catch(e) {
        stopLoadingDots();
        toast(e.message);
      }
    }
    function recommendationCard(item) {
      const ids = item.ids || [];
      const platforms = (item.platforms || []).filter(Boolean).sort(comparePlatform).map(label).join(', ');
      const versions = (item.versions || []).filter(Boolean).join(', ') || 'unknown';
      const usage = Number(item.usage_30d || 0);
      const impact = `${platforms || '-'} · ${esc(item.copy_count || ids.length)} ${t('adviceCopies')} · ${esc(t('adviceUsage30d'))}: ${esc(usage)}${lang === 'zh' ? '次' : ''}`;
      const risk = item.severity === 'high' ? t('adviceHighRisk') : item.severity === 'medium' ? t('adviceMediumRisk') : t('adviceLowRisk');
      return `
        <div class="recommendation-card" data-rec="${esc(ids.join('|'))}">
          <div><strong>${esc(item.name)}</strong> <span class="pill">${esc(issueLabel(item.type))}</span></div>
          <div class="recommendation-meta">
            <span>${esc(t('adviceAction'))}</span><span>${esc(recommendationActionName(item))}</span>
            <span>${esc(t('adviceReason'))}</span><span>${esc(recommendationReason(item))}</span>
            <span>${esc(t('adviceImpact'))}</span><span>${impact}</span>
            <span>${esc(t('version'))}</span><span>${esc(versions)}</span>
            <span>${esc(t('adviceRisk'))}</span><span>${esc(risk)}</span>
          </div>
          <div class="recommendation-actions">${recommendationButtons(item)}</div>
        </div>`;
    }
    function recommendationActionName(item) {
      if (item.type === 'update') return t('actionUnifyAdvice');
      if (item.type === 'deactivate') return t('actionDeactivateAdvice');
      if (item.type === 'delete') return t('actionDeleteAdvice');
      if (item.type === 'health') return t('actionDetailsAdvice');
      return t('actionDetailsAdvice');
    }
    function recommendationButtons(item) {
      const ids = item.ids || [];
      const data = JSON.stringify(encodeURIComponent(JSON.stringify(item)));
      const idData = JSON.stringify(ids);
      const nameData = JSON.stringify(encodeURIComponent(item.name || ''));
      if (item.type === 'update') {
        return `<button class="tiny primary" onclick='openRecommendationUpdate(${idData},${nameData})'>${esc(t('actionUnifyAdvice'))}</button><button class="tiny" onclick='showRecommendationDetailsData(${data})'>${esc(t('actionDetailsAdvice'))}</button><button class="tiny" onclick='ignoreRecommendation(${idData})'>${esc(t('actionIgnoreAdvice'))}</button>`;
      }
      if (item.type === 'deactivate') {
        return `<button class="tiny primary" onclick='runRecommendation(${idData},"inactive")'>${esc(t('actionDeactivateAdvice'))}</button><button class="tiny" onclick='showRecommendationUsage(${idData})'>${esc(t('usage'))}</button><button class="tiny" onclick='ignoreRecommendation(${idData})'>${esc(t('actionIgnoreAdvice'))}</button>`;
      }
      if (item.type === 'delete') {
        return `<button class="tiny danger" onclick='runRecommendation(${idData},"delete")'>${esc(t('actionDeleteAdvice'))}</button><button class="tiny" onclick='showRecommendationDetailsData(${data})'>${esc(t('actionDetailsAdvice'))}</button><button class="tiny" onclick='ignoreRecommendation(${idData})'>${esc(t('actionIgnoreAdvice'))}</button>`;
      }
      if (item.type === 'health') {
        return `<button class="tiny" onclick='showRecommendationDetailsData(${data})'>${esc(t('actionDetailsAdvice'))}</button><button class="tiny" onclick='showRecommendationUsage(${idData})'>${esc(t('usage'))}</button><button class="tiny" onclick='ignoreRecommendation(${idData})'>${esc(t('actionIgnoreAdvice'))}</button>`;
      }
      return `<button class="tiny" onclick='showRecommendationDetailsData(${data})'>${esc(t('actionDetailsAdvice'))}</button><button class="tiny" onclick='ignoreRecommendation(${idData})'>${esc(t('actionIgnoreAdvice'))}</button>`;
    }
    function ignoreRecommendation(ids) {
      const key = (ids || []).join('|');
      document.querySelectorAll(`[data-rec="${CSS.escape(key)}"]`).forEach(el => el.remove());
    }
    function showRecommendationDetailsData(encoded) {
      showRecommendationDetails(JSON.parse(decodeURIComponent(encoded)));
    }
    function showRecommendationDetails(item) {
      const caps = (item.ids || []).map(id => state.capabilities.find(c => c.id === id)).filter(Boolean);
      $('actionTitle').textContent = `${t('details')}: ${item.name}`;
      $('actionBody').innerHTML = `
        <div class="detail-grid">
          <div class="detail-row"><div class="detail-label">${esc(t('adviceAction'))}</div><div class="detail-value">${esc(recommendationActionName(item))}</div></div>
          <div class="detail-row"><div class="detail-label">${esc(t('adviceReason'))}</div><div class="detail-value">${esc(recommendationReason(item))}</div></div>
          <div class="detail-row"><div class="detail-label">${esc(t('platform'))}</div><div class="detail-value">${esc((item.platforms || []).sort(comparePlatform).map(label).join(', '))}</div></div>
          <div class="detail-row"><div class="detail-label">${esc(t('version'))}</div><div class="detail-value">${esc((item.versions || []).join(', ') || 'unknown')}</div></div>
          <div class="detail-row"><div class="detail-label">${esc(t('paths'))}</div><div class="detail-value">${caps.map(c => `${esc(label(c.platform))}: ${esc(c.path)}`).join('<br>') || esc((item.paths || []).join('\\n'))}</div></div>
        </div>`;
      $('actionModal').classList.add('open');
    }
    function showRecommendationUsage(ids) {
      const first = (ids || []).map(id => state.capabilities.find(c => c.id === id)).filter(Boolean)[0];
      if (first) showUsage(first.id);
    }
    function userUpdateMessage(result, fallback = '') {
      if (!result) return fallback;
      const message = String(result.message || '');
      const match = message.match(/^Builtin skill\\. Update via (.*)\\.$/);
      if (match) return t('builtinUpdateHint')(label(match[1] || ''));
      if (result.status === 'latest') return t('noUpdateFound');
      if (result.status === 'unsupported') return t('noUpdateFound');
      return fallback || t('noUpdateFound');
    }
    async function openRecommendationUpdate(ids, name, forceRemote = false) {
      name = decodeURIComponent(name || '');
      $('actionTitle').textContent = `${t('update')}: ${name}`;
      showLoadingMessage('checkingUpdate');
      $('actionModal').classList.add('open');
      try {
        const result = await api('/api/update-check', {ids, force_remote: forceRemote});
        stopLoadingDots();
        if (result.status === 'remote_update') {
          $('actionBody').innerHTML = `<div class="empty">${esc(t('updateReady'))}</div><div class="row-actions confirm-actions" style="margin-top:12px"><button class="primary" onclick='applyUpdate(${JSON.stringify(ids)},"remote")'>${esc(t('updateAll'))}</button><button onclick="closeAction()">${esc(t('cancel'))}</button></div>`;
        } else if (result.status === 'local_mismatch') {
          $('actionBody').innerHTML = `<div class="empty">${esc(t('localMismatchFound')((result.local_versions || []).join(', ')))}</div><div class="row-actions confirm-actions" style="margin-top:12px"><button class="primary" onclick='applyUpdate(${JSON.stringify(ids)},"unify")'>${esc(t('unifyAll'))}</button><button onclick="closeAction()">${esc(t('cancel'))}</button></div>`;
        } else {
          const checked = ids.map(id => state.capabilities.find(c => c.id === id)).filter(Boolean);
          checked.forEach(c => noUpdateGroups.add(String(c.name || '').toLowerCase()));
          render();
          $('actionBody').innerHTML = `<div class="empty">${esc(userUpdateMessage(result, t('noUpdateFound')))}</div>`;
        }
      } catch(e) {
        stopLoadingDots();
        $('actionBody').innerHTML = `<div class="empty">${esc(e.message)}</div>`;
      }
    }
    async function runRecommendation(ids, action) {
      await runAction(ids, action);
      closeDetails();
    }
    async function showLogs() {
      closeMoreMenus();
      try {
        const data = await api('/api/logs');
        $('detailTitle').textContent = t('operationLog');
        $('detailBody').innerHTML = `<div class="event-list">${(data.logs || []).map(log => `<div class="event"><div class="event-meta">${esc(log.occurred_at)} | ${esc(log.action)}</div><div>${esc(log.target || '')}</div><div class="event-meta">${esc(log.details || '')}</div></div>`).join('') || `<div class="empty">${esc(t('noUsage'))}</div>`}</div>`;
        $('detailModal').classList.add('open');
      } catch(e) {
        toast(e.message);
      }
    }
    async function scan() { try { const d = await api('/api/scan'); toast(t('scanned')(d.count)); await load(); } catch(e) { toast(e.message); } }
    function refreshPage() { window.location.reload(); }
    async function health() {
      closeMoreMenus();
      try {
        const data = await api('/api/health');
        const summary = data.summary || {};
        const issues = data.issues || [];
        $('detailTitle').textContent = t('healthResult');
        $('detailBody').innerHTML = `
          <div class="metrics" style="grid-template-columns: repeat(5, minmax(90px, 1fr)); margin-bottom:12px">
            ${[['total', t('total')], ['ok', 'OK'], ['warning', t('warning')], ['missing', t('missing')], ['metadata_error', t('issueMetadata')]].map(([key, label]) => `
              <div class="metric"><strong>${esc(summary[key] ?? 0)}</strong><span>${esc(label)}</span></div>`).join('')}
          </div>
          <div class="detail-grid">
            <div class="detail-row"><div class="detail-label">${esc(t('healthRules'))}</div><div class="detail-value">${(data.rules || []).map(rule => `- ${esc(ruleLabel(rule))}`).join('<br>')}</div></div>
            <div class="detail-row"><div class="detail-label">${esc(t('healthIssues'))}</div><div class="detail-value">${issues.length ? healthIssueTable(issues.slice(0, 80)) : esc(t('healthNoIssues'))}</div></div>
          </div>`;
        $('detailModal').classList.add('open');
        toast(t('healthDone'));
        await load();
      } catch(e) {
        toast(e.message);
      }
    }
    async function report() {
      closeMoreMenus();
      try {
        const data = await api('/api/report');
        $('detailTitle').textContent = t('snapshotDone');
        $('detailBody').innerHTML = `
          <div class="detail-grid">
            <div class="detail-row"><div class="detail-label">${esc(t('snapshotIncludes'))}</div><div class="detail-value">${esc(t('snapshotSections'))}</div></div>
            <div class="detail-row"><div class="detail-label">${esc(t('snapshotHtml'))}</div><div class="detail-value"><a href="${esc(data.html_url)}" target="_blank" rel="noreferrer">${esc(t('openReport'))}</a></div></div>
            <div class="detail-row"><div class="detail-label">${esc(t('snapshotPath'))}</div><div class="detail-value"><strong>${esc(data.html_path || data.path)}</strong></div></div>
            <div class="detail-row"><div class="detail-label">${esc(t('total'))}</div><div class="detail-value">${esc(data.total)}</div></div>
            <div class="detail-row"><div class="detail-label">${esc(t('activeMetric'))}</div><div class="detail-value">${esc(data.active)}</div></div>
            <div class="detail-row"><div class="detail-label">${esc(t('unused30d'))}</div><div class="detail-value">${esc(data.unused)}</div></div>
          </div>
          <iframe src="${esc(data.html_url)}" style="width:100%;height:460px;border:1px solid var(--line);border-radius:8px;margin-top:12px;background:white"></iframe>
          <div class="empty" style="margin-top:12px">${esc(t('snapshotIntro'))}</div>`;
        $('detailModal').classList.add('open');
        toast(t('snapshotDone'));
      } catch(e) {
        toast(e.message);
      }
    }
    function healthIssueTable(issues) {
      return `
        <table style="width:100%;border-collapse:collapse">
          <thead><tr><th>${esc(t('platform'))}</th><th>${esc(t('name'))}</th><th>${esc(t('health'))}</th><th>${esc(t('details'))}</th></tr></thead>
          <tbody>${issues.map(item => `
            <tr>
              <td>${esc(label(item.platform))}</td>
              <td>${esc(item.name)}</td>
              <td>${esc(label(item.health))}</td>
              <td>${esc(item.message || '')}</td>
            </tr>`).join('')}</tbody>
        </table>`;
    }
    async function setStatus(id, status) { try { await api('/api/status', {name: id, status}); toast(`${id} -> ${status}`); await load(); } catch(e) { toast(e.message); } }
    function actionLabel(action) {
      if (action === 'active') return t('activate');
      if (action === 'inactive') return t('pause');
      if (action === 'delete') return t('delete');
      return action;
    }
    function openAction(groupKey, action) {
      const group = groupByKey(groupKey);
      if (!group) return toast(groupKey);
      $('actionTitle').textContent = t('chooseTarget')(actionLabel(action), group.name);
      const allIds = group.items.map(c => c.id);
      const allButton = group.items.length > 1 ? `
        <div class="choice">
          <div><strong>${esc(t('allPlatformsAction'))}</strong><small>${esc(group.items.map(c => `${label(c.platform)}: ${c.path}`).join(' | '))}</small></div>
          <button class="tiny ${action === 'delete' ? 'danger' : ''}" onclick='runAction(${JSON.stringify(allIds)},${JSON.stringify(action)})'>${esc(actionLabel(action))}</button>
        </div>` : '';
      $('actionBody').innerHTML = `
        <div class="choice-list">
          ${allButton}
          ${group.items.map(c => `
            <div class="choice">
              <div><strong>${esc(label(c.platform))}</strong><small>${esc(c.path)}</small></div>
              <button class="tiny ${action === 'delete' ? 'danger' : ''}" onclick='runAction(${JSON.stringify([c.id])},${JSON.stringify(action)})'>${esc(actionLabel(action))}</button>
            </div>`).join('')}
        </div>`;
      $('actionModal').classList.add('open');
    }
    function closeAction() {
      stopLoadingDots();
      $('actionModal').classList.remove('open');
    }
    function openImportance(groupKey) {
      const group = groupByKey(groupKey);
      if (!group) return toast(groupKey);
      const ids = group.items.map(c => c.id);
      $('actionTitle').textContent = t('importanceTitle')(group);
      const choices = [
        ['important', t('important')],
        ['normal', t('normal')],
        ['low', t('low')],
        ['', t('auto')]
      ];
      $('actionBody').innerHTML = `
        <div class="empty">${esc(t('importanceHint'))}</div>
        <div class="choice-list">
          ${choices.map(([value, label]) => `
            <div class="choice">
              <div><strong>${esc(label)}</strong><small>${value ? esc(t('apply')) : esc(t('auto'))}</small></div>
              <button class="tiny" onclick='setImportance(${JSON.stringify(ids)},${JSON.stringify(value)})'>${esc(t('apply'))}</button>
            </div>`).join('')}
        </div>`;
      $('actionModal').classList.add('open');
    }
    async function setImportance(ids, importance) {
      try {
        await api('/api/importance', {ids, importance});
        closeAction();
        toast(t('importanceSaved'));
        await load();
      } catch(e) {
        toast(e.message);
      }
    }
    function showDetails(groupKey) {
      const group = groupByKey(groupKey);
      if (!group) return toast(groupKey);
      const primary = group.primary;
      $('detailTitle').textContent = t('detailsTitle')(group);
      $('detailBody').innerHTML = `
        <div class="detail-grid">
          <div class="detail-row"><div class="detail-label">${esc(t('name'))}</div><div class="detail-value"><strong>${esc(group.name)}</strong></div></div>
          <div class="detail-row"><div class="detail-label">${esc(t('importance'))}</div><div class="detail-value"><span class="pill ${importanceClass(group.importance)}">${esc(t(group.importance))}${group.importanceOverride ? '' : ` (${esc(t('auto'))})`}</span></div></div>
          <div class="detail-row"><div class="detail-label">${esc(t('platform'))}</div><div class="detail-value"><div class="platform-tags">${tags(group.platforms, 'platform-tag')}</div></div></div>
          <div class="detail-row"><div class="detail-label">${esc(t('version'))}</div><div class="detail-value">${esc(group.versions.join(', '))}</div></div>
          <div class="detail-row"><div class="detail-label">${esc(t('source'))}</div><div class="detail-value"><div class="source-tags">${tags(group.sources, 'pill')}</div></div></div>
          <div class="detail-row"><div class="detail-label">${esc(t('introduction'))}</div><div class="detail-value">${esc(localizedDescription(primary.description, group.name))}</div></div>
          <div class="detail-row"><div class="detail-label">${esc(t('paths'))}</div><div class="detail-value">${group.items.map(c => `${esc(label(c.platform))}: ${esc(c.path)}`).join('<br>')}</div></div>
        </div>`;
      $('detailModal').classList.add('open');
    }
    function closeDetails() {
      $('detailModal').classList.remove('open');
    }
    async function openUpdate(groupKey, forceRemote = false) {
      const group = groupByKey(groupKey);
      if (!group) return toast(groupKey);
      const ids = group.items.map(c => c.id);
      $('actionTitle').textContent = t('updateTitle')(group);
      showLoadingMessage('checkingUpdate');
      $('actionModal').classList.add('open');
      try {
        const result = await api('/api/update-check', {ids, force_remote: forceRemote});
        stopLoadingDots();
        if (result.status === 'remote_update') {
          $('actionBody').innerHTML = `
            <div class="empty">${esc(t('updateReady'))}</div>
            <div class="row-actions confirm-actions" style="margin-top:12px">
              <button class="primary" onclick='applyUpdate(${JSON.stringify(ids)},"remote")'>${esc(t('updateAll'))}</button>
              <button onclick="closeAction()">${esc(t('cancel'))}</button>
            </div>`;
        } else if (result.status === 'local_mismatch') {
          $('actionBody').innerHTML = `
            <div class="empty">${esc(t('localMismatchFound')((result.local_versions || []).join(', ')))}</div>
            <div class="row-actions confirm-actions" style="margin-top:12px">
              <button class="primary" onclick='applyUpdate(${JSON.stringify(ids)},"unify")'>${esc(t('unifyAll'))}</button>
              <button onclick="closeAction()">${esc(t('cancel'))}</button>
            </div>`;
        } else if (result.status === 'latest') {
          noUpdateGroups.add(group.key);
          render();
          $('actionBody').innerHTML = `<div class="empty">${esc(t('noUpdateFound'))}</div>`;
        } else {
          if (result.status === 'unsupported') {
            noUpdateGroups.add(group.key);
            render();
          }
          $('actionBody').innerHTML = `<div class="empty">${esc(userUpdateMessage(result, t('noUpdateFound')))}</div>`;
        }
      } catch(e) {
        stopLoadingDots();
        $('actionBody').innerHTML = `<div class="empty">${esc(e.message)}</div>`;
      }
    }
    async function applyUpdate(ids, mode) {
      try {
        showLoadingMessage('updating');
        const result = await api('/api/update-apply', {ids, mode});
        stopLoadingDots();
        closeAction();
        toast(t('updated')(result.updated || 0));
        await load();
      } catch(e) {
        stopLoadingDots();
        toast(e.message);
      }
    }
    async function runAction(ids, action) {
      const targets = ids.map(id => state.capabilities.find(c => c.id === id)).filter(Boolean);
      if (!targets.length) return;
      if (action === 'delete') {
        const summary = targets.map(c => `${label(c.platform)}: ${c.path}`).join('\\n');
        if (!confirm(`${actionLabel(action)} ${targets[0].name}?\\n\\n${summary}`)) return;
      }
      try {
        for (const id of ids) {
          if (action === 'delete') {
            await api('/api/delete', {name: id, confirmed: true});
          } else {
            await api('/api/status', {name: id, status: action});
          }
        }
        closeAction();
        toast(`${actionLabel(action)}: ${targets.length}`);
        await load();
      } catch(e) {
        toast(e.message);
      }
    }
    async function showUsage(id) {
      try {
        const res = await fetch(`/api/usage-events?id=${encodeURIComponent(id)}`);
        const data = await res.json();
        if (!res.ok || data.error) throw new Error(data.error || res.statusText);
        $('usageTitle').textContent = t('usageTitle')(data.capability);
        if (!data.events.length) {
          $('usageBody').innerHTML = `<div class="event-list">${usageEmptyCard(data.capability.platform, [data.capability.path])}</div>`;
        } else {
          $('usageBody').innerHTML = `<div class="event-list">${data.events.map(event => usageEventCard(data.capability.platform, event, data.capability.path)).join('')}</div>`;
        }
        $('usageModal').classList.add('open');
      } catch(e) {
        toast(e.message);
      }
    }
    function closeUsage() {
      $('usageModal').classList.remove('open');
    }
    async function showGroupUsage(groupKey) {
      const group = groupByKey(groupKey);
      if (!group) return toast(groupKey);
      try {
        const payloads = await Promise.all(group.items.map(async c => {
          const res = await fetch(`/api/usage-events?id=${encodeURIComponent(c.id)}`);
          const data = await res.json();
          if (!res.ok || data.error) throw new Error(data.error || res.statusText);
          return { platform: c.platform, path: c.path, events: data.events };
        }));
        $('usageTitle').textContent = t('usageTitle')({name: group.name});
        const byPlatform = new Map();
        payloads.forEach(item => {
          if (!byPlatform.has(item.platform)) byPlatform.set(item.platform, {platform: item.platform, paths: [], events: []});
          const bucket = byPlatform.get(item.platform);
          bucket.paths.push(item.path);
          item.events.forEach(event => bucket.events.push({event, path: item.path}));
        });
        const blocks = [...byPlatform.values()].map(item => {
          if (!item.events.length) return usageEmptyCard(item.platform, item.paths);
          return item.events.map(({event, path}) => usageEventCard(item.platform, event, path)).join('');
        }).join('');
        $('usageBody').innerHTML = `<div class="event-list">${blocks}</div>`;
        $('usageModal').classList.add('open');
      } catch(e) {
        toast(e.message);
      }
    }
    async function deleteCap(id) {
      const cap = state.capabilities.find(c => c.id === id);
      if (!cap) return toast(id);
      if (!confirm(t('deleteConfirm')({...cap, platform: label(cap.platform)}))) return;
      try {
        const d = await api('/api/delete', {name: id, confirmed: true});
        toast(t('deleted')(d.backup || ''));
        await load();
      } catch(e) {
        toast(e.message);
      }
    }
    load();
  </script>
</body>
</html>
"""


def web_command(args: argparse.Namespace) -> None:
    host = args.host or load_config().get("web", {}).get("host", "127.0.0.1")
    port = int(args.port or load_config().get("web", {}).get("port", 8765))
    Registry()
    server = http.server.ThreadingHTTPServer((host, port), AdminHandler)
    url = f"http://{host}:{port}/"
    print(f"Agent Skill Manager admin: {url}")
    if args.open:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="asm", description="Cross-platform Agent Skill Manager")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("scan", help="Scan configured platform roots into the registry").set_defaults(func=scan_command)
    p = sub.add_parser("list", help="List capabilities")
    p.add_argument("--platform")
    p.add_argument("--status")
    p.set_defaults(func=list_command)
    p = sub.add_parser("search", help="Search capabilities")
    p.add_argument("query")
    p.set_defaults(func=search_command)
    p = sub.add_parser("show", help="Show capability JSON")
    p.add_argument("name")
    p.add_argument("--platform")
    p.set_defaults(func=show_command)
    p = sub.add_parser("activate", help="Soft activate a capability")
    p.add_argument("name")
    p.add_argument("--platform")
    p.set_defaults(func=lambda a: status_command(a, "active"))
    p = sub.add_parser("deactivate", help="Soft deactivate a capability")
    p.add_argument("name")
    p.add_argument("--platform")
    p.set_defaults(func=lambda a: status_command(a, "inactive"))
    p = sub.add_parser("log-use", help="Record a usage event")
    p.add_argument("name")
    p.add_argument("--platform")
    p.add_argument("--source", default="manual")
    p.add_argument("--confidence", default="high", choices=["high", "medium", "low"])
    p.add_argument("--evidence")
    p.set_defaults(func=log_use_command)
    p = sub.add_parser("usage", help="Show usage counts")
    p.add_argument("--days", type=int, default=30)
    p.set_defaults(func=usage_command)
    p = sub.add_parser("check-updates", help="Check GitHub-backed capabilities")
    p.add_argument("--platform")
    p.set_defaults(func=check_updates_command)
    sub.add_parser("outdated", help="List latest outdated update checks").set_defaults(func=outdated_command)
    sub.add_parser("health", help="Run local health checks").set_defaults(func=health_command)
    sub.add_parser("duplicates", help="Find duplicate names across platforms").set_defaults(func=duplicates_command)
    p = sub.add_parser("backup", help="Backup a capability path")
    p.add_argument("name")
    p.add_argument("--platform")
    p.set_defaults(func=backup_command)
    p = sub.add_parser("delete", help="Delete a non-builtin capability after confirmation")
    p.add_argument("name")
    p.add_argument("--platform")
    p.add_argument("--yes", action="store_true", help="Confirm deletion and create a backup first")
    p.set_defaults(func=delete_command)
    p = sub.add_parser("update", help="Check a GitHub-backed capability before manual update")
    p.add_argument("name")
    p.add_argument("--platform")
    p.add_argument("--dry-run", action="store_true", default=True)
    p.set_defaults(func=update_command)
    sub.add_parser("report", help="Generate a Markdown management report").set_defaults(func=report_command)
    p = sub.add_parser("web", help="Start the local HTML admin interface")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--open", action="store_true")
    p.set_defaults(func=web_command)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> None:
    ensure_app_dirs()
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
