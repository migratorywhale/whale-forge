#!/usr/bin/env python3
"""Register a forged Claude Code transcript in Claude Desktop.

whale-forge.py creates a fresh CLI transcript under ~/.claude/projects.
Claude Desktop's Code UI has an extra local session card:

    ~/Library/Application Support/Claude/claude-code-sessions/*/*/local_*.json

That card points at the CLI transcript through `cliSessionId`. This helper
clones an existing Desktop card and points the clone at the forged transcript.
It does not modify the transcript or the template card.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Any


DEFAULT_DESKTOP_ROOT = (
    Path.home() / "Library" / "Application Support" / "Claude" / "claude-code-sessions"
)
BACKUP_DIR = Path.home() / ".claude" / "desktop-register-backups"


def claude_project_slug(cwd: Path) -> str:
    """Mirror Claude Code's slash-to-dash project directory naming."""
    return str(cwd.expanduser().resolve()).replace("/", "-")


def default_cli_project_dir() -> Path:
    override = os.environ.get("CLAUDE_PROJECT_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".claude" / "projects" / claude_project_slug(Path.cwd())


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"not a JSON object: {path}")
    return data


def read_jsonl_summary(path: Path) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "exists": path.exists(),
        "events": 0,
        "assistant_events": 0,
        "user_events": 0,
        "title": None,
        "cwd": None,
    }
    if not path.exists():
        return summary
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            event = json.loads(line)
            summary["events"] += 1
            typ = event.get("type")
            if typ == "assistant":
                summary["assistant_events"] += 1
            elif typ == "user":
                summary["user_events"] += 1
            if event.get("cwd") and not summary["cwd"]:
                summary["cwd"] = event.get("cwd")
            if event.get("type") == "custom-title" and event.get("customTitle"):
                summary["title"] = event.get("customTitle")
    return summary


def iter_desktop_cards(root: Path) -> list[tuple[Path, dict[str, Any]]]:
    cards: list[tuple[Path, dict[str, Any]]] = []
    for path in root.glob("*/*/local_*.json"):
        try:
            data = load_json(path)
        except Exception:
            continue
        if "sessionId" not in data or "cliSessionId" not in data:
            continue
        cards.append((path, data))
    return cards


def score_card(path: Path, data: dict[str, Any]) -> tuple[int, float]:
    archived_penalty = 0 if not data.get("isArchived") else -1
    focused = data.get("lastFocusedAt")
    activity = data.get("lastActivityAt")
    stamp = max(
        float(focused or 0) / 1000,
        float(activity or 0) / 1000,
        path.stat().st_mtime,
    )
    return archived_penalty, stamp


def resolve_template(
    cards: list[tuple[Path, dict[str, Any]]],
    template: str | None,
    source_cli_session: str | None,
    cwd: str | None,
) -> tuple[Path, dict[str, Any]]:
    candidates = cards
    if template:
        for path, data in cards:
            if path.name == template or data.get("sessionId") == template or str(path) == template:
                return path, data
        raise ValueError(f"template not found: {template}")

    if source_cli_session:
        matched = [(p, d) for p, d in cards if d.get("cliSessionId") == source_cli_session]
        if matched:
            return max(matched, key=lambda item: score_card(*item))
        raise ValueError(f"no Desktop card points to cliSessionId={source_cli_session}")

    if cwd:
        cwd_cards = [(p, d) for p, d in cards if d.get("cwd") == cwd or d.get("originCwd") == cwd]
        if cwd_cards:
            candidates = cwd_cards

    if not candidates:
        raise ValueError("no Desktop local_*.json cards found")
    return max(candidates, key=lambda item: score_card(*item))


def unique_local_id(existing: set[str]) -> str:
    while True:
        sid = f"local_{uuid.uuid4()}"
        if sid not in existing:
            return sid


def make_title(template_title: str | None, transcript_title: str | None, cli_session_id: str) -> str:
    base = (transcript_title or template_title or "Forged session").strip()
    suffix = f"forge {cli_session_id[:8]}"
    if suffix in base:
        return base
    return f"{base} - {suffix}"


def clone_card(
    template_data: dict[str, Any],
    new_local_id: str,
    cli_session_id: str,
    title: str,
    now_ms: int,
    completed_turns: int,
) -> dict[str, Any]:
    data = json.loads(json.dumps(template_data, ensure_ascii=False))
    data["sessionId"] = new_local_id
    data["cliSessionId"] = cli_session_id
    data["title"] = title
    data["titleSource"] = "user"
    data["createdAt"] = now_ms
    data["lastFocusedAt"] = now_ms
    data["lastActivityAt"] = now_ms
    data["completedTurns"] = completed_turns
    data["isArchived"] = False

    # These are runtime bridge handles for the template session. Reusing them can
    # make the Desktop UI point at the old live bridge instead of starting clean.
    data["bridgeSessionIds"] = []
    data["sessionPermissionUpdates"] = []
    data["spawnSeed"] = {}
    data.pop("chromeTabGroupId", None)
    return data


def write_json_atomic(path: Path, data: dict[str, Any], mode_from: Path) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    try:
        os.chmod(tmp, mode_from.stat().st_mode & 0o777)
    except OSError:
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Clone a Claude Desktop local session card for a forged CLI session.",
    )
    parser.add_argument("cli_session_id", help="forged CLI session id, e.g. 563b3ebf-...")
    parser.add_argument("--desktop-root", default=str(DEFAULT_DESKTOP_ROOT))
    parser.add_argument("--cli-project-dir", default=str(default_cli_project_dir()))
    parser.add_argument("--template", help="template local session id, file name, or full path")
    parser.add_argument("--source-cli-session", help="choose template by old cliSessionId")
    parser.add_argument("--cwd", help="prefer template cards for this cwd")
    parser.add_argument("--title", help="title for the new Desktop card")
    parser.add_argument("--dry-run", action="store_true", help="preview without writing")
    args = parser.parse_args()

    if not (len(args.cli_session_id) == 36 and args.cli_session_id.count("-") == 4):
        print(f"error: invalid cli session id: {args.cli_session_id}", file=sys.stderr)
        return 2

    desktop_root = Path(args.desktop_root).expanduser()
    cli_project_dir = Path(args.cli_project_dir).expanduser()
    transcript = cli_project_dir / f"{args.cli_session_id}.jsonl"
    summary = read_jsonl_summary(transcript)
    if not summary["exists"]:
        print(f"error: forged transcript not found: {transcript}", file=sys.stderr)
        return 2

    cards = iter_desktop_cards(desktop_root)
    existing_local_ids = {data.get("sessionId") for _, data in cards}
    existing_cli_ids = {data.get("cliSessionId") for _, data in cards}
    if args.cli_session_id in existing_cli_ids:
        print(f"already-registered:{args.cli_session_id}")
        for path, data in cards:
            if data.get("cliSessionId") == args.cli_session_id:
                print(f"desktop_card={path}", file=sys.stderr)
        return 0

    template_path, template_data = resolve_template(
        cards,
        args.template,
        args.source_cli_session,
        args.cwd or summary.get("cwd"),
    )
    new_local_id = unique_local_id({x for x in existing_local_ids if isinstance(x, str)})
    output_path = template_path.with_name(f"{new_local_id}.json")
    now_ms = int(time.time() * 1000)
    title = args.title or make_title(
        template_data.get("title"),
        summary.get("title"),
        args.cli_session_id,
    )
    completed_turns = max(0, int(summary.get("assistant_events") or 0))
    new_data = clone_card(
        template_data,
        new_local_id,
        args.cli_session_id,
        title,
        now_ms,
        completed_turns,
    )

    print(f"template_card={template_path}", file=sys.stderr)
    print(f"new_card={output_path}", file=sys.stderr)
    print(f"cli_transcript={transcript}", file=sys.stderr)
    print(f"title={title}", file=sys.stderr)
    print(f"model={new_data.get('model')}", file=sys.stderr)
    print(f"events={summary['events']} users={summary['user_events']} assistants={summary['assistant_events']}", file=sys.stderr)

    if args.dry_run:
        print(f"dry-run:{new_local_id}")
        return 0

    if output_path.exists():
        print(f"error: output exists: {output_path}", file=sys.stderr)
        return 2

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup_path = BACKUP_DIR / f"{template_path.name}.template.{int(time.time())}.bak"
    shutil.copy2(template_path, backup_path)
    write_json_atomic(output_path, new_data, template_path)
    print(f"registered:{new_local_id}")
    print(f"backup_template={backup_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
