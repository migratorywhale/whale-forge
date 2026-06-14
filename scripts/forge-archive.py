#!/usr/bin/env python3
"""Forge a Claude Code session and archive the old JSONL to Markdown.

This is a convenience wrapper around forge-reload.py:

1. run forge-reload.py to create a new session jsonl;
2. write ~/.claude/forge-ready/<old>.signal with the new session id;
3. run jsonl2md.py on the old session, if you have such a converter.

The source JSONL is never modified.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path


def claude_project_slug(cwd: Path) -> str:
    """Mirror Claude Code's slash-to-dash project directory naming."""
    return str(cwd.expanduser().resolve()).replace("/", "-")


def default_project_dir() -> Path:
    override = os.environ.get("CLAUDE_PROJECT_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".claude" / "projects" / claude_project_slug(Path.cwd())


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_PROJECT_DIR = default_project_dir()
DEFAULT_THRESHOLD = int(os.environ.get("FORGE_THRESHOLD", "950000"))
FORGE_SCRIPT = Path(os.environ.get("FORGE_RELOAD_SCRIPT", str(SCRIPT_DIR / "forge-reload.py"))).expanduser()
JSONL2MD_SCRIPT = Path(os.environ.get("JSONL2MD_SCRIPT", str(Path.home() / "scripts" / "jsonl2md.py"))).expanduser()
ARCHIVE_DIR = Path(os.environ.get("FORGE_ARCHIVE_DIR", str(Path.home() / ".claude" / "forge-archive"))).expanduser()
SIGNAL_DIR = Path(os.environ.get("FORGE_SIGNAL_DIR", str(Path.home() / ".claude" / "forge-ready"))).expanduser()
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def parse_forge_id(stdout: str, dry_run: bool) -> str | None:
    prefix = "dry-run" if dry_run else "forged"
    match = re.search(rf"^{prefix}:([0-9a-f-]{{36}})$", stdout, re.M)
    if not match:
        return None
    sid = match.group(1)
    return sid if UUID_RE.match(sid) else None


def parse_source_path(stderr: str, project_dir: Path, source_arg: str | None) -> Path | None:
    match = re.search(r"^source=(.+)$", stderr, re.M)
    if match:
        return Path(match.group(1)).expanduser()
    if not source_arg:
        return None
    candidate = Path(source_arg).expanduser()
    if candidate.exists():
        return candidate
    if UUID_RE.match(source_arg):
        return project_dir / f"{source_arg}.jsonl"
    return None


def archive_source(source_path: Path, force: bool) -> tuple[bool, str]:
    if not JSONL2MD_SCRIPT.exists():
        return False, f"jsonl2md.py not found: {JSONL2MD_SCRIPT}"
    cmd = [sys.executable, str(JSONL2MD_SCRIPT), str(source_path)]
    if force:
        cmd.append("--force")
    result = run(cmd, timeout=120)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        return False, detail or f"jsonl2md.py exited {result.returncode}"
    return True, (result.stdout or "").strip()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Forge a session, write a forge-ready signal, and archive the old session to Markdown.",
    )
    parser.add_argument(
        "session",
        nargs="?",
        help="source session id or jsonl path; omit to let forge-reload.py pick the latest session",
    )
    parser.add_argument(
        "--project-dir",
        default=str(DEFAULT_PROJECT_DIR),
        help=f"Claude Code project dir (default: {DEFAULT_PROJECT_DIR})",
    )
    parser.add_argument(
        "--keep-tokens",
        type=int,
        default=DEFAULT_THRESHOLD,
        help=f"tail token budget passed to forge-reload.py (default: {DEFAULT_THRESHOLD})",
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=DEFAULT_THRESHOLD,
        help=f"threshold passed to forge-reload.py (default: {DEFAULT_THRESHOLD})",
    )
    parser.add_argument(
        "--no-force",
        action="store_true",
        help="do not force forge; allow forge-reload.py to skip below threshold",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="preview forge only; do not write signal or archive markdown",
    )
    parser.add_argument(
        "--force-archive",
        action="store_true",
        help="reconvert markdown even if jsonl2md.py thinks the archive is up to date",
    )
    parser.add_argument(
        "--min-pairs",
        type=int,
        help="optional pass-through to forge-reload.py",
    )
    parser.add_argument(
        "--min-tool-rounds",
        type=int,
        help="optional pass-through to forge-reload.py",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    project_dir = Path(args.project_dir).expanduser()

    cmd = [
        sys.executable,
        str(FORGE_SCRIPT),
    ]
    if args.session:
        cmd.append(args.session)
    cmd.extend(
        [
            "--project-dir",
            str(project_dir),
            "--keep-tokens",
            str(args.keep_tokens),
            "--threshold",
            str(args.threshold),
        ]
    )
    if not args.no_force:
        cmd.append("--force")
    if args.dry_run:
        cmd.append("--dry-run")
    if args.min_pairs is not None:
        cmd.extend(["--min-pairs", str(args.min_pairs)])
    if args.min_tool_rounds is not None:
        cmd.extend(["--min-tool-rounds", str(args.min_tool_rounds)])

    if not FORGE_SCRIPT.exists():
        print(f"error: forge-reload.py not found: {FORGE_SCRIPT}", file=sys.stderr)
        return 2

    result = run(cmd, timeout=120)
    if result.stderr:
        print(result.stderr.rstrip(), file=sys.stderr)
    if result.stdout:
        print(result.stdout.rstrip())

    if result.returncode != 0:
        return result.returncode

    new_id = parse_forge_id(result.stdout or "", args.dry_run)
    if not new_id:
        print("forge-archive: forge skipped; no archive written", file=sys.stderr)
        return 0

    source_path = parse_source_path(result.stderr or "", project_dir, args.session)
    if source_path is None or not source_path.exists():
        print("forge-archive: could not resolve source path; no archive written", file=sys.stderr)
        return 2

    old_id = source_path.stem
    if args.dry_run:
        print(f"dry-run resume command: claude --resume {new_id}")
        print(f"dry-run archive target: {ARCHIVE_DIR / f'code_session_{old_id}.md'}")
        return 0

    SIGNAL_DIR.mkdir(parents=True, exist_ok=True)
    signal_path = SIGNAL_DIR / f"{old_id}.signal"
    signal_path.write_text(f"{new_id}\n", encoding="utf-8")

    ok, archive_log = archive_source(source_path, force=args.force_archive)
    if archive_log:
        print(archive_log)
    if not ok:
        print("forge-archive: forge succeeded but markdown archive failed", file=sys.stderr)
        print(f"signal={signal_path}", file=sys.stderr)
        print(f"resume: claude --resume {new_id}")
        return 1

    archive_path = ARCHIVE_DIR / f"code_session_{old_id}.md"
    size_kb = archive_path.stat().st_size / 1024 if archive_path.exists() else 0
    print(f"signal={signal_path}")
    print(f"archive={archive_path} ({size_kb:.1f} KB)")
    print(f"resume: claude --resume {new_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
