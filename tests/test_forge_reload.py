#!/usr/bin/env python3
"""Synthetic tests for forge-reload.py."""

from __future__ import annotations

import json
import importlib.util
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "forge-reload.py"


def load_forge_module():
    spec = importlib.util.spec_from_file_location("forge_reload", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def event(typ, sid, parent=None, **extra):
    payload = {
        "type": typ,
        "sessionId": sid,
        "timestamp": "2026-06-06T00:00:00.000Z",
        **extra,
    }
    if typ in {"user", "assistant", "attachment", "system"}:
        payload["uuid"] = str(uuid.uuid4())
        payload["parentUuid"] = parent
    return payload


def write_session(path: Path, events):
    with path.open("w", encoding="utf-8") as handle:
        for item in events:
            handle.write(json.dumps(item, ensure_ascii=False))
            handle.write("\n")


def make_session(project: Path, pair_count=12, sid="source-session"):
    events = [
        {"type": "custom-title", "sessionId": sid, "title": "Forge Fixture"},
        {"type": "mode", "sessionId": sid, "mode": "normal"},
    ]
    parent = None
    for idx in range(pair_count):
        user = event(
            "user",
            sid,
            parent,
            message={"role": "user", "content": f"user turn {idx}"},
        )
        parent = user["uuid"]
        events.append(user)
        if idx == pair_count - 1:
            keep_attachment = event(
                "attachment",
                sid,
                parent,
                subtype="system-reminder",
                message={"content": "remember output policy"},
            )
            parent = keep_attachment["uuid"]
            events.append(keep_attachment)
        else:
            drop_attachment = event(
                "attachment",
                sid,
                parent,
                subtype="deferred_tools_delta",
                message={"content": "tool schemas"},
            )
            parent = drop_attachment["uuid"]
            events.append(drop_attachment)
        assistant = event(
            "assistant",
            sid,
            parent,
            message={
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "private", "signature": "expired"},
                    {"type": "text", "text": f"assistant turn {idx}"},
                ],
            },
        )
        parent = assistant["uuid"]
        events.append(assistant)
        stop_summary = event(
            "system",
            sid,
            parent,
            subtype="stop_hook_summary",
            content="drop me",
        )
        parent = stop_summary["uuid"]
        events.append(stop_summary)
    path = project / f"{sid}.jsonl"
    write_session(path, events)
    return path


def make_tool_primer_session(project: Path, sid="source-session"):
    events = [
        {"type": "custom-title", "sessionId": sid, "title": "Tool Primer Fixture"},
        {"type": "mode", "sessionId": sid, "mode": "normal"},
    ]
    parent = None
    user = event(
        "user",
        sid,
        parent,
        message={"role": "user", "content": "older tool request"},
    )
    parent = user["uuid"]
    events.append(user)
    assistant_tool = event(
        "assistant",
        sid,
        parent,
        message={
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_safe_old",
                    "name": "read_file",
                    "input": {"path": "/tmp/example.txt"},
                }
            ],
        },
    )
    parent = assistant_tool["uuid"]
    events.append(assistant_tool)
    tool_result = event(
        "user",
        sid,
        parent,
        message={
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_safe_old",
                    "content": "x" * 3505,
                }
            ],
        },
    )
    parent = tool_result["uuid"]
    events.append(tool_result)
    assistant_done = event(
        "assistant",
        sid,
        parent,
        message={"role": "assistant", "content": [{"type": "text", "text": "tool done"}]},
    )
    parent = assistant_done["uuid"]
    events.append(assistant_done)
    sensitive_user = event(
        "user",
        sid,
        parent,
        message={"role": "user", "content": "older side-effect request"},
    )
    parent = sensitive_user["uuid"]
    events.append(sensitive_user)
    sensitive_tool = event(
        "assistant",
        sid,
        parent,
        message={
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_sensitive_old",
                    "name": "write_diary",
                    "input": {"content": "do not replay"},
                }
            ],
        },
    )
    parent = sensitive_tool["uuid"]
    events.append(sensitive_tool)
    sensitive_result = event(
        "user",
        sid,
        parent,
        message={
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_sensitive_old",
                    "content": "ok",
                }
            ],
        },
    )
    parent = sensitive_result["uuid"]
    events.append(sensitive_result)
    sensitive_done = event(
        "assistant",
        sid,
        parent,
        message={"role": "assistant", "content": [{"type": "text", "text": "sensitive done"}]},
    )
    parent = sensitive_done["uuid"]
    events.append(sensitive_done)
    for idx in range(15):
        user = event(
            "user",
            sid,
            parent,
            message={"role": "user", "content": f"tail user turn {idx}"},
        )
        parent = user["uuid"]
        events.append(user)
        assistant = event(
            "assistant",
            sid,
            parent,
            message={
                "role": "assistant",
                "content": [{"type": "text", "text": f"tail assistant turn {idx}"}],
            },
        )
        parent = assistant["uuid"]
        events.append(assistant)
    path = project / f"{sid}.jsonl"
    write_session(path, events)
    return path


def run_forge(project: Path, *args):
    cmd = [sys.executable, str(SCRIPT), "--project-dir", str(project), *args]
    return subprocess.run(cmd, text=True, capture_output=True, check=False)


def read_jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class ForgeReloadTests(unittest.TestCase):
    def test_skips_short_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            make_session(project, pair_count=3)
            result = run_forge(project, "source-session", "--force")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("skip:not-enough-pairs", result.stdout)

    def test_forges_valid_chain_and_filters_noise(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            make_session(project, pair_count=12)
            result = run_forge(
                project,
                "source-session",
                "--force",
                "--output-session-id",
                "forged-session",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("forged:forged-session", result.stdout)
            output = read_jsonl(project / "forged-session.jsonl")

            self.assertEqual(output[0]["type"], "custom-title")
            self.assertEqual(output[1]["type"], "mode")
            real = [item for item in output if "uuid" in item]
            self.assertGreaterEqual(len(real), 20)
            self.assertIsNone(real[0]["parentUuid"])
            for prev, cur in zip(real, real[1:]):
                self.assertEqual(cur["parentUuid"], prev["uuid"])

            types = [item["type"] for item in output]
            self.assertNotIn("queue-operation", types)
            self.assertFalse(
                any(item.get("subtype") == "stop_hook_summary" for item in output)
            )
            self.assertTrue(any(item.get("subtype") == "system-reminder" for item in output))
            self.assertTrue(all(item.get("sessionId") == "forged-session" for item in output))

    def test_dry_run_does_not_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            make_session(project, pair_count=12)
            result = run_forge(
                project,
                "source-session",
                "--force",
                "--dry-run",
                "--output-session-id",
                "dry-forge",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("dry-run:dry-forge", result.stdout)
            self.assertFalse((project / "dry-forge.jsonl").exists())

    def test_tail_keeps_recent_turns(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            make_session(project, pair_count=20)
            result = run_forge(
                project,
                "source-session",
                "--force",
                "--min-pairs",
                "1",
                "--keep-tokens",
                "250",
                "--output-session-id",
                "tail-forge",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            output = read_jsonl(project / "tail-forge.jsonl")
            user_texts = [
                item["message"]["content"]
                for item in output
                if item.get("type") == "user"
            ]
            self.assertTrue(user_texts)
            self.assertNotIn("user turn 0", user_texts)
            self.assertIn("user turn 19", user_texts)

    def test_tool_primer_backfills_earlier_tool_round(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            make_tool_primer_session(project)
            result = run_forge(
                project,
                "source-session",
                "--force",
                "--min-pairs",
                "1",
                "--keep-tokens",
                "350",
                "--output-session-id",
                "tool-primer-forge",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("tool-primer:inserted:1", result.stderr)
            output = read_jsonl(project / "tool-primer-forge.jsonl")
            real = [item for item in output if "uuid" in item]
            self.assertGreaterEqual(len(real), 4)

            first_blocks = real[0]["message"]["content"]
            second_blocks = real[1]["message"]["content"]
            self.assertEqual(first_blocks[0]["type"], "tool_use")
            self.assertEqual(first_blocks[0]["id"], "toolu_safe_old")
            self.assertEqual(second_blocks[0]["type"], "tool_result")
            self.assertEqual(second_blocks[0]["tool_use_id"], "toolu_safe_old")
            self.assertTrue(second_blocks[0]["content"].endswith("...(truncated)"))
            self.assertLessEqual(len(second_blocks[0]["content"]), len("x" * 3000 + "...(truncated)"))

            forge = load_forge_module()
            ok, reason = forge.validate_tool_results(output)
            self.assertTrue(ok, reason)

    def test_drops_leading_tool_result_continuation(self):
        forge = load_forge_module()
        sid = "source-session"
        orphan_result = event(
            "user",
            sid,
            message={
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_trimmed_away",
                        "content": "result from an earlier tool call",
                    }
                ],
            },
        )
        continuation = event(
            "assistant",
            sid,
            message={"role": "assistant", "content": [{"type": "text", "text": "continuing"}]},
        )
        human = event(
            "user",
            sid,
            message={"role": "user", "content": "real new user turn"},
        )
        assistant = event(
            "assistant",
            sid,
            message={"role": "assistant", "content": [{"type": "text", "text": "fresh reply"}]},
        )

        filtered, dropped = forge.filter_events(
            [forge.Chunk(events=[orphan_result, continuation, human, assistant], tokens=100)]
        )
        self.assertEqual([item["type"] for item in filtered], ["user", "assistant"])
        self.assertEqual(filtered[0]["message"]["content"], "real new user turn")
        self.assertEqual(dropped["leading-continuation:user"], 1)
        self.assertEqual(dropped["leading-continuation:assistant"], 1)

        rebuilt = forge.rebuild_events(filtered, "forged-session", None, None)
        ok, reason = forge.validate_tool_results(rebuilt)
        self.assertTrue(ok, reason)

    def test_image_only_user_gets_text_placeholder(self):
        forge = load_forge_module()
        sid = "source-session"
        image_user = event(
            "user",
            sid,
            message={
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": "abc123",
                        },
                    }
                ],
            },
        )
        assistant = event(
            "assistant",
            sid,
            image_user["uuid"],
            message={"role": "assistant", "content": [{"type": "text", "text": "saw image"}]},
        )

        rebuilt = forge.rebuild_events([image_user, assistant], "forged-session", None, None)
        blocks = rebuilt[0]["message"]["content"]
        self.assertEqual(blocks, [{"type": "text", "text": forge.OMITTED_IMAGE_TEXT}])
        ok, reason = forge.validate_nonempty_user_messages(rebuilt)
        self.assertTrue(ok, reason)


if __name__ == "__main__":
    unittest.main()
