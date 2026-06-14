#!/usr/bin/env python3
"""Forge a trimmed Claude Code session from raw jsonl history.

This script never edits the source session. It writes a new jsonl file with a
fresh session id, filtered tail events, and a rebuilt parentUuid chain.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
import tempfile
import uuid
from collections import Counter, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


def claude_project_slug(cwd: Path) -> str:
    """Mirror Claude Code's slash-to-dash project directory naming."""
    return str(cwd.expanduser().resolve()).replace("/", "-")


def default_project_dir() -> Path:
    override = os.environ.get("CLAUDE_PROJECT_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".claude" / "projects" / claude_project_slug(Path.cwd())


DEFAULT_PROJECT_DIR = default_project_dir()
DEFAULT_KEEP_TOKENS = 80_000
DEFAULT_THRESHOLD = int(os.environ.get("WHALE_FORGE_THRESHOLD", "800000"))
DEFAULT_MIN_PAIRS = 10
DEFAULT_MIN_TOOL_ROUNDS = 1
TOOL_RESULT_MAX_CHARS = 3000
OMITTED_IMAGE_TEXT = "[image omitted by forge]"
SKIP_PRIMER_TOOL_NAME_PARTS = (
    "memory_remember",
    "write_diary",
    "send_email",
)

KEEP_ATTACHMENT_MARKERS = (
    "system-reminder",
    "system_reminder",
    "claudemd",
    "claude.md",
    "claude-md",
)
DROP_TYPES = {
    "queue-operation",
    "custom-title",
    "mode",
    "last-prompt",
}

# Some wrappers inject session-start context into a user-shaped event. For
# helper previews, strip that wrapper so the visible user text is easier to
# identify.
SESSION_START_WRAPPER_RE = re.compile(
    r"^<session-start-context[^>]*>.*?</session-start-context>\s*",
    re.S,
)
# Pseudo-user messages: system/harness injections that appear under the user
# role in the raw JSONL but are not human-authored chat turns.
PSEUDO_USER_RE = re.compile(
    r"^\s*<(task-notification|system-reminder|local-command-caveat"
    r"|command-name|command-message|local-command-stdout)\b"
)


@dataclass
class Chunk:
    events: list[dict[str, Any]]
    tokens: int


@dataclass
class SourceStats:
    path: Path
    event_count: int = 0
    parse_errors: int = 0
    total_tokens: int = 0
    user_count: int = 0
    assistant_count: int = 0
    title_event: dict[str, Any] | None = None
    mode_event: dict[str, Any] | None = None

    @property
    def pair_count(self) -> int:
        return min(self.user_count, self.assistant_count)


def event_tokens(event: dict[str, Any]) -> int:
    raw = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    return max(1, len(raw) // 3)


def iter_jsonl(path: Path) -> Iterable[tuple[int, dict[str, Any]]]:
    with path.open("r", encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, 1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                yield lineno, json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{lineno}: invalid json: {exc}") from exc


def find_latest_session(project_dir: Path) -> Path:
    candidates = list(project_dir.glob("*.jsonl"))
    if not candidates:
        raise FileNotFoundError(f"no .jsonl sessions found in {project_dir}")
    return max(candidates, key=lambda item: item.stat().st_mtime)


def resolve_source(project_dir: Path, session_id: str | None) -> Path:
    if not session_id:
        return find_latest_session(project_dir)
    candidate = Path(session_id).expanduser()
    if candidate.exists():
        return candidate
    name = session_id if session_id.endswith(".jsonl") else f"{session_id}.jsonl"
    path = project_dir / name
    if not path.exists():
        raise FileNotFoundError(f"session not found: {session_id} in {project_dir}")
    return path


def collect_tail_chunks(
    path: Path,
    keep_tokens: int,
    cut_before_uuid: str | None = None,
) -> tuple[SourceStats, list[Chunk], list[Chunk]]:
    stats = SourceStats(path=path)
    current: list[dict[str, Any]] = []
    current_tokens = 0
    tail: deque[Chunk] = deque()
    tail_tokens = 0
    retention_budget = max(keep_tokens * 3, keep_tokens + 50_000)

    def push_current() -> None:
        nonlocal current, current_tokens, tail_tokens
        if not current:
            return
        chunk = Chunk(events=current, tokens=current_tokens)
        tail.append(chunk)
        tail_tokens += current_tokens
        current = []
        current_tokens = 0
        while tail_tokens > retention_budget and len(tail) > 1:
            removed = tail.popleft()
            tail_tokens -= removed.tokens

    cut_found = False
    for _, event in iter_jsonl(path):
        # Rewind mode: stop at the target UUID; that event and everything
        # after it are discarded before tail selection.
        if cut_before_uuid is not None and event.get("uuid") == cut_before_uuid:
            cut_found = True
            break
        typ = event.get("type")
        # JSONL is append-only. After a compact boundary, Claude Code no
        # longer loads the earlier epoch, so threshold checks should count only
        # the latest epoch.
        if typ == "system" and event.get("subtype") == "compact_boundary":
            stats.total_tokens = 0
            stats.user_count = 0
            stats.assistant_count = 0
        tokens = event_tokens(event)
        stats.event_count += 1
        stats.total_tokens += tokens
        if typ == "user":
            stats.user_count += 1
            if current:
                push_current()
        elif typ == "assistant":
            stats.assistant_count += 1
        elif typ == "custom-title" and stats.title_event is None:
            stats.title_event = copy.deepcopy(event)
        elif typ == "mode":
            stats.mode_event = copy.deepcopy(event)
        current.append(event)
        current_tokens += tokens

    push_current()

    if cut_before_uuid is not None and not cut_found:
        raise ValueError(f"cut uuid not found: {cut_before_uuid}")

    tail_list = list(tail)
    start_index = max(0, len(tail_list) - 1)
    selected_tokens = 0
    for idx in range(len(tail_list) - 1, -1, -1):
        start_index = idx
        selected_tokens += tail_list[idx].tokens
        if selected_tokens >= keep_tokens:
            break

    # If the selected window begins in the middle of a multi-tool exchange,
    # widen it backward until it starts with a real human turn. This is safer
    # than dropping the leading tool results and losing a large slice of recent
    # context.
    while start_index > 0 and not chunk_starts_human_turn(tail_list[start_index]):
        start_index -= 1

    primer_candidates = tail_list[:start_index]
    selected = tail_list[start_index:]
    return stats, selected, primer_candidates


def attachment_kind(event: dict[str, Any]) -> str:
    values: list[str] = []
    for key in ("subtype", "name", "attachmentType", "attachment_type"):
        value = event.get(key)
        if isinstance(value, str):
            values.append(value)
    attachment = event.get("attachment")
    if isinstance(attachment, dict):
        for key in ("type", "name", "subtype"):
            value = attachment.get(key)
            if isinstance(value, str):
                values.append(value)
    message = event.get("message")
    if isinstance(message, dict):
        for key in ("type", "name", "subtype"):
            value = message.get(key)
            if isinstance(value, str):
                values.append(value)
    return " ".join(values).lower()


def keep_event(event: dict[str, Any]) -> bool:
    typ = event.get("type")
    if typ in ("user", "assistant"):
        return True
    if typ == "attachment":
        kind = attachment_kind(event)
        return any(marker in kind for marker in KEEP_ATTACHMENT_MARKERS)
    if typ == "system" and event.get("subtype") == "compact_boundary":
        return False
    if typ in DROP_TYPES:
        return False
    return False


def chunk_starts_human_turn(chunk: Chunk) -> bool:
    for event in chunk.events:
        if not keep_event(event):
            continue
        return (
            event.get("type") == "user"
            and user_has_human_text(event)
            and not user_has_tool_result(event)
        )
    return False


def filter_events(chunks: list[Chunk]) -> tuple[list[dict[str, Any]], Counter[str]]:
    kept: list[dict[str, Any]] = []
    dropped: Counter[str] = Counter()
    for chunk in chunks:
        for event in chunk.events:
            if keep_event(event):
                kept.append(copy.deepcopy(event))
            else:
                dropped[str(event.get("type", "<missing>"))] += 1

    # Avoid starting a forged conversation with an orphan assistant response or
    # a context attachment. The first real event should be a user message.
    while kept and kept[0].get("type") != "user":
        dropped[f"leading:{kept[0].get('type', '<missing>')}"] += 1
        kept.pop(0)
    kept = drop_leading_continuation(kept, dropped)
    return kept, dropped


def content_blocks(event: dict[str, Any]) -> list[dict[str, Any]]:
    message = event.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def user_has_tool_result(event: dict[str, Any]) -> bool:
    if event.get("type") != "user":
        return False
    return any(block.get("type") == "tool_result" for block in content_blocks(event))


def user_has_human_text(event: dict[str, Any]) -> bool:
    if event.get("type") != "user":
        return False
    message = event.get("message")
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    return any(
        block.get("type") == "text" and str(block.get("text", "")).strip()
        for block in content_blocks(event)
    )


def user_display_text(event: dict[str, Any]) -> str:
    """Return the human-visible text of a real user message, or "".

    Skips tool_result-only turns and compact summaries, strips injected
    session-start wrappers, and drops pseudo-user system injections
    (system-reminder / task-notification / ...).
    """
    if event.get("type") != "user" or event.get("isCompactSummary"):
        return ""
    if user_has_tool_result(event) or not user_has_human_text(event):
        return ""
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    text = ""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = str(block.get("text", ""))
                break
    text = SESSION_START_WRAPPER_RE.sub("", text)
    if PSEUDO_USER_RE.match(text):
        return ""
    return text.strip()


def normalize_preview(text: str) -> str:
    """Collapse whitespace runs to single spaces (line-based output needs
    newline-free previews; callers matching prefixes must normalize the same
    way)."""
    return " ".join(text.split())


def list_user_messages(source: Path) -> int:
    """Helper mode: print uuid, ISO timestamp, and an 80-char preview."""
    for _, event in iter_jsonl(source):
        text = user_display_text(event)
        if not text:
            continue
        uid = event.get("uuid")
        if not isinstance(uid, str) or not uid:
            continue
        ts = event.get("timestamp") or ""
        preview = normalize_preview(text)[:80]
        print(f"{uid}\t{ts}\t{preview}")
    return 0


def list_uuids_from(source: Path, start_uuid: str) -> int:
    """Helper mode: print event UUIDs from start_uuid, inclusive."""
    started = False
    for _, event in iter_jsonl(source):
        uid = event.get("uuid")
        if not isinstance(uid, str) or not uid:
            continue
        if uid == start_uuid:
            started = True
        if started:
            print(uid)
    if not started:
        print(f"error:uuid not found: {start_uuid}", file=sys.stderr)
        return 2
    return 0


def drop_leading_continuation(
    events: list[dict[str, Any]],
    dropped: Counter[str],
) -> list[dict[str, Any]]:
    """Drop a leading tool continuation split from an earlier turn.

    Claude Code records tool results as user events. If tail trimming starts at
    one of those, the matching tool_use may be outside the kept window and the
    forged session resumes in the middle of a tool exchange. Drop until the next
    human-text user event.
    """
    while events:
        first = events[0]
        if first.get("type") == "user" and user_has_human_text(first) and not user_has_tool_result(first):
            return events
        dropped[f"leading-continuation:{first.get('type', '<missing>')}"] += 1
        events.pop(0)
    return events


def fresh_session_id(project_dir: Path) -> str:
    while True:
        sid = str(uuid.uuid4())
        if not (project_dir / f"{sid}.jsonl").exists():
            return sid


def update_session_ids(value: Any, new_session_id: str) -> Any:
    if isinstance(value, dict):
        for key, child in list(value.items()):
            if key == "sessionId":
                value[key] = new_session_id
            else:
                update_session_ids(child, new_session_id)
    elif isinstance(value, list):
        for child in value:
            update_session_ids(child, new_session_id)
    return value


def strip_chain_fields(event: dict[str, Any]) -> None:
    for key in ("logicalParentUuid", "isSidechain"):
        event.pop(key, None)


def make_preamble_event(source: dict[str, Any], new_session_id: str) -> dict[str, Any]:
    event = copy.deepcopy(source)
    update_session_ids(event, new_session_id)
    event.pop("uuid", None)
    event.pop("parentUuid", None)
    strip_chain_fields(event)
    return event


def rebuild_events(
    source_events: list[dict[str, Any]],
    new_session_id: str,
    title_event: dict[str, Any] | None,
    mode_event: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    if title_event is not None:
        output.append(make_preamble_event(title_event, new_session_id))
    if mode_event is not None:
        output.append(make_preamble_event(mode_event, new_session_id))

    previous_uuid: str | None = None
    for source in source_events:
        event = sanitize_event_for_output(source)
        update_session_ids(event, new_session_id)
        strip_chain_fields(event)
        event["uuid"] = str(uuid.uuid4())
        event["parentUuid"] = previous_uuid
        previous_uuid = event["uuid"]
        output.append(event)
    return output


def validate_chain(events: list[dict[str, Any]]) -> tuple[bool, str]:
    previous_uuid: str | None = None
    real_seen = False
    for idx, event in enumerate(events):
        if "uuid" not in event:
            continue
        real_seen = True
        if event.get("parentUuid") != previous_uuid:
            return False, (
                f"bad parent chain at output event {idx}: "
                f"expected {previous_uuid}, got {event.get('parentUuid')}"
            )
        previous_uuid = event["uuid"]
    if not real_seen:
        return False, "no uuid-bearing events in forged output"
    return True, "ok"


def validate_tool_results(events: list[dict[str, Any]]) -> tuple[bool, str]:
    seen_tool_uses: set[str] = set()
    for idx, event in enumerate(events):
        for block in content_blocks(event):
            if block.get("type") == "tool_use" and isinstance(block.get("id"), str):
                seen_tool_uses.add(block["id"])
            elif block.get("type") == "tool_result":
                tool_use_id = block.get("tool_use_id")
                if isinstance(tool_use_id, str) and tool_use_id not in seen_tool_uses:
                    return False, (
                        f"orphan tool_result at output event {idx}: "
                        f"missing tool_use {tool_use_id}"
                    )
    return True, "ok"


def user_message_has_nonempty_content(event: dict[str, Any]) -> bool:
    if event.get("type") != "user":
        return True
    message = event.get("message")
    if not isinstance(message, dict) or message.get("role") != "user":
        return True
    content = message.get("content")
    if isinstance(content, str):
        return bool(content)
    if isinstance(content, list):
        if not content:
            return False
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and block.get("text"):
                return True
            if block.get("type") == "tool_result":
                return True
            if block.get("type") == "image":
                return True
        return False
    return False


def validate_nonempty_user_messages(events: list[dict[str, Any]]) -> tuple[bool, str]:
    for idx, event in enumerate(events):
        if not user_message_has_nonempty_content(event):
            return False, f"empty user message at output event {idx}"
    return True, "ok"


def is_skipped_primer_tool(name: Any) -> bool:
    if not isinstance(name, str):
        return False
    lowered = name.lower()
    return any(part in lowered for part in SKIP_PRIMER_TOOL_NAME_PARTS)


def is_successful_tool_result(block: dict[str, Any]) -> bool:
    return block.get("is_error") not in (True, "true", "True", 1)


def tool_use_ids_in_events(events: list[dict[str, Any]]) -> set[str]:
    ids: set[str] = set()
    for event in events:
        for block in content_blocks(event):
            if block.get("type") == "tool_use" and isinstance(block.get("id"), str):
                ids.add(block["id"])
    return ids


def count_tool_rounds(events: list[dict[str, Any]]) -> int:
    pending: set[str] = set()
    count = 0
    for event in events:
        for block in content_blocks(event):
            if block.get("type") == "tool_use" and isinstance(block.get("id"), str):
                pending.add(block["id"])
            elif block.get("type") == "tool_result":
                tool_use_id = block.get("tool_use_id")
                if isinstance(tool_use_id, str) and tool_use_id in pending:
                    pending.remove(tool_use_id)
                    if is_successful_tool_result(block):
                        count += 1
    return count


def truncate_tool_result_block(block: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(block)
    content = result.get("content")
    if isinstance(content, str):
        if len(content) > TOOL_RESULT_MAX_CHARS:
            result["content"] = content[:TOOL_RESULT_MAX_CHARS] + "...(truncated)"
        return result
    if isinstance(content, list):
        text_parts: list[str] = []
        omitted_images = 0
        for item in content:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "text":
                text = item.get("text")
                if isinstance(text, str) and text:
                    text_parts.append(text)
            elif item_type == "image":
                omitted_images += 1
            else:
                try:
                    text_parts.append(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
                except TypeError:
                    text_parts.append(str(item))
        if omitted_images:
            text_parts.append(OMITTED_IMAGE_TEXT)
        joined = "\n".join(part for part in text_parts if part)
        if len(joined) > TOOL_RESULT_MAX_CHARS:
            joined = joined[:TOOL_RESULT_MAX_CHARS] + "...(truncated)"
        result["content"] = joined
        return result
    if content is None:
        return result
    try:
        serialized = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
    except TypeError:
        serialized = str(content)
    if len(serialized) > TOOL_RESULT_MAX_CHARS:
        result["content"] = serialized[:TOOL_RESULT_MAX_CHARS] + "...(truncated)"
    return result


def sanitize_content_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    for block in blocks:
        block_type = block.get("type")
        if block_type == "image":
            cleaned.append({"type": "text", "text": OMITTED_IMAGE_TEXT})
            continue
        if block_type == "tool_result":
            cleaned.append(truncate_tool_result_block(block))
            continue
        cleaned.append(copy.deepcopy(block))
    return cleaned


def sanitize_event_for_output(event: dict[str, Any]) -> dict[str, Any]:
    clone = copy.deepcopy(event)
    # Claude Code keeps bulky duplicate tool output here; message.content is the
    # canonical transcript used for replay.
    clone.pop("toolUseResult", None)

    message = clone.get("message")
    if not isinstance(message, dict):
        return clone
    content = message.get("content")
    if isinstance(content, list):
        message["content"] = sanitize_content_blocks(
            [block for block in content if isinstance(block, dict)]
        )
    elif isinstance(content, str) and len(content) > TOOL_RESULT_MAX_CHARS * 20:
        message["content"] = content[: TOOL_RESULT_MAX_CHARS * 20] + "...(truncated)"
    return clone


def event_with_content_blocks(
    event: dict[str, Any],
    blocks: list[dict[str, Any]],
) -> dict[str, Any]:
    clone = copy.deepcopy(event)
    message = clone.get("message")
    if not isinstance(message, dict):
        clone["message"] = {"content": []}
        message = clone["message"]
    message["content"] = [copy.deepcopy(block) for block in blocks]
    return clone


def find_primer_tool_rounds(
    chunks: list[Chunk],
    needed: int,
    existing_tool_use_ids: set[str],
) -> list[list[dict[str, Any]]]:
    if needed <= 0:
        return []

    pending: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    rounds: list[list[dict[str, Any]]] = []
    for chunk in chunks:
        for event in chunk.events:
            if not keep_event(event):
                continue
            for block in content_blocks(event):
                if block.get("type") != "tool_use":
                    continue
                tool_use_id = block.get("id")
                if not isinstance(tool_use_id, str) or tool_use_id in existing_tool_use_ids:
                    continue
                if is_skipped_primer_tool(block.get("name")):
                    continue
                pending[tool_use_id] = (event, copy.deepcopy(block))

            for block in content_blocks(event):
                if block.get("type") != "tool_result":
                    continue
                tool_use_id = block.get("tool_use_id")
                if not isinstance(tool_use_id, str):
                    continue
                match = pending.pop(tool_use_id, None)
                if match is None or not is_successful_tool_result(block):
                    continue
                assistant_event, tool_use_block = match
                rounds.append(
                    [
                        event_with_content_blocks(assistant_event, [tool_use_block]),
                        event_with_content_blocks(
                            event,
                            [truncate_tool_result_block(block)],
                        ),
                    ]
                )

    return rounds[-needed:]


def ensure_tool_primer(
    events: list[dict[str, Any]],
    candidate_chunks: list[Chunk],
    min_tool_rounds: int,
    dropped: Counter[str],
) -> list[dict[str, Any]]:
    if min_tool_rounds <= 0:
        return events
    current_rounds = count_tool_rounds(events)
    if current_rounds >= min_tool_rounds:
        return events

    needed = min_tool_rounds - current_rounds
    rounds = find_primer_tool_rounds(
        candidate_chunks,
        needed,
        tool_use_ids_in_events(events),
    )
    if not rounds:
        dropped["tool-primer:missing"] += needed
        return events

    primer_events = [event for round_events in rounds for event in round_events]
    dropped["tool-primer:inserted"] += len(rounds)
    if len(rounds) < needed:
        dropped["tool-primer:missing"] += needed - len(rounds)
    return primer_events + events


def write_jsonl_atomic(path: Path, events: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for event in events:
                handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
                handle.write("\n")
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def summarize(
    stats: SourceStats,
    output_path: Path,
    new_session_id: str,
    kept_events: list[dict[str, Any]],
    dropped: Counter[str],
    dry_run: bool,
) -> str:
    kept_tokens = sum(event_tokens(event) for event in kept_events)
    lines = [
        f"source={stats.path}",
        f"output={output_path}",
        f"new_session_id={new_session_id}",
        f"dry_run={dry_run}",
        f"source_events={stats.event_count}",
        f"source_est_tokens={stats.total_tokens}",
        f"source_pairs={stats.pair_count}",
        f"kept_events={len(kept_events)}",
        f"kept_est_tokens={kept_tokens}",
    ]
    if dropped:
        dropped_text = ", ".join(f"{key}:{value}" for key, value in sorted(dropped.items()))
        lines.append(f"dropped={dropped_text}")
    return "\n".join(lines)


def forge(args: argparse.Namespace) -> int:
    project_dir = Path(args.project_dir).expanduser()
    source = resolve_source(project_dir, args.session_id)

    # Helper list modes are read-only.
    if args.list_user_messages:
        return list_user_messages(source)
    if args.list_uuids_from:
        return list_uuids_from(source, args.list_uuids_from)

    # Rewind mode: keep ALL content before the cut point; no tail truncation.
    # Normal forge: apply keep_tokens budget to select a trailing window.
    rewind_mode = args.cut_before_uuid is not None
    effective_keep = 999_999_999 if rewind_mode else args.keep_tokens
    stats, chunks, primer_chunks = collect_tail_chunks(
        source, effective_keep, cut_before_uuid=args.cut_before_uuid
    )

    if stats.pair_count < args.min_pairs:
        print(
            f"skip:not-enough-pairs pairs={stats.pair_count} min={args.min_pairs}",
            file=sys.stdout,
        )
        print(f"source={source}", file=sys.stderr)
        return 0

    if stats.total_tokens < args.threshold and not args.force:
        print(
            f"skip:below-threshold tokens={stats.total_tokens} threshold={args.threshold}",
            file=sys.stdout,
        )
        print(f"source={source}", file=sys.stderr)
        return 0

    filtered, dropped = filter_events(chunks)
    if min(
        sum(1 for event in filtered if event.get("type") == "user"),
        sum(1 for event in filtered if event.get("type") == "assistant"),
    ) < args.min_pairs:
        print("skip:not-enough-kept-pairs", file=sys.stdout)
        print(f"source={source}", file=sys.stderr)
        return 0

    filtered = ensure_tool_primer(
        filtered,
        primer_chunks,
        args.min_tool_rounds,
        dropped,
    )

    new_session_id = args.output_session_id or fresh_session_id(project_dir)
    output_path = project_dir / f"{new_session_id}.jsonl"
    output_events = rebuild_events(
        filtered,
        new_session_id,
        stats.title_event,
        stats.mode_event,
    )
    ok, reason = validate_chain(output_events)
    if not ok:
        print(f"error:{reason}", file=sys.stderr)
        return 2
    ok, reason = validate_tool_results(output_events)
    if not ok:
        print(f"error:{reason}", file=sys.stderr)
        return 2
    ok, reason = validate_nonempty_user_messages(output_events)
    if not ok:
        print(f"error:{reason}", file=sys.stderr)
        return 2

    print(
        summarize(stats, output_path, new_session_id, output_events, dropped, args.dry_run),
        file=sys.stderr,
    )

    if args.dry_run:
        print(f"dry-run:{new_session_id}", file=sys.stdout)
        return 0

    if output_path.exists():
        print(f"error:output already exists: {output_path}", file=sys.stderr)
        return 2
    write_jsonl_atomic(output_path, output_events)
    print(f"forged:{new_session_id}", file=sys.stdout)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Forge a trimmed Claude Code session jsonl with a fresh session id.",
    )
    parser.add_argument("session_id", nargs="?", help="source session id or jsonl path")
    parser.add_argument(
        "--project-dir",
        default=str(DEFAULT_PROJECT_DIR),
        help=f"session directory (default: {DEFAULT_PROJECT_DIR})",
    )
    parser.add_argument(
        "--keep-tokens",
        type=int,
        default=DEFAULT_KEEP_TOKENS,
        help=f"estimated tail tokens to keep (default: {DEFAULT_KEEP_TOKENS})",
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=DEFAULT_THRESHOLD,
        help=f"forge only when source estimate exceeds this (default: {DEFAULT_THRESHOLD})",
    )
    parser.add_argument(
        "--min-pairs",
        type=int,
        default=DEFAULT_MIN_PAIRS,
        help=f"minimum user/assistant pairs required (default: {DEFAULT_MIN_PAIRS})",
    )
    parser.add_argument(
        "--min-tool-rounds",
        type=int,
        default=DEFAULT_MIN_TOOL_ROUNDS,
        help=(
            "minimum complete successful tool_use/tool_result rounds to keep; "
            f"backfills safe earlier rounds as a primer when needed (default: {DEFAULT_MIN_TOOL_ROUNDS})"
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="do not write output")
    parser.add_argument(
        "--force",
        action="store_true",
        help="forge even when source estimate is below threshold",
    )
    parser.add_argument(
        "--output-session-id",
        help="test hook: use a fixed output session id instead of uuid4",
    )
    parser.add_argument(
        "--cut-before-uuid",
        help="rewind: stop copying at this event uuid (drop it and everything after); "
        "applied before --keep-tokens tail trimming",
    )
    parser.add_argument(
        "--list-user-messages",
        action="store_true",
        help="helper: print uuid<TAB>ISO-timestamp<TAB>first-80-chars for every real "
        "user message (skips tool_result-only and pseudo-user system injections)",
    )
    parser.add_argument(
        "--list-uuids-from",
        metavar="UUID",
        help="helper: print the uuid of every event from UUID (inclusive) onward",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return forge(args)
    except Exception as exc:
        print(f"error:{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
