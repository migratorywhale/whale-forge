# Whale Forge

Whale Forge is an experimental toolkit for carrying a large Claude Code
session into a fresh transcript without asking the model to summarize itself
first.

It started as a practical fix for a specific problem: a forged Claude Code JSONL
transcript can be valid, but Claude Desktop Code mode will not see it until its
separate Desktop session card points at the new `cliSessionId`. Whale Forge
therefore handles both parts:

- forge a new JSONL transcript from a recent tail window;
- optionally register that forged transcript as a Claude Desktop Code session.

The repository is named `whale-forge`; the main script keeps the descriptive
name `forge-reload.py`.

## What It Does

`scripts/forge-reload.py` reads a Claude Code JSONL transcript, keeps a recent
tail of user/assistant events, rewrites `sessionId`, rebuilds the `parentUuid`
chain, and writes a fresh transcript. The source file is never modified.

`scripts/forge-desktop-register.py` is macOS/Claude Desktop specific. It clones
a `local_*.json` session card under:

```text
~/Library/Application Support/Claude/claude-code-sessions/*/*/
```

and sets the clone's `cliSessionId` to the forged transcript id.

`scripts/forge-archive.py` is a convenience wrapper that can run forge-reload,
write a signal file, and optionally archive the source JSONL to Markdown if you
have a `jsonl2md.py` converter.

## Quick Start

Run from the same working directory as the Claude Code project you want to
forge:

```bash
python3 scripts/forge-reload.py <old-session-id> \
  --keep-tokens 950000 \
  --threshold 950000 \
  --force
```

Resume the new transcript:

```bash
claude --resume <new-session-id>
```

If your transcript is in a different Claude Code project directory:

```bash
python3 scripts/forge-reload.py <old-session-id> \
  --project-dir ~/.claude/projects/<project-slug> \
  --force
```

You can also set:

```bash
export CLAUDE_PROJECT_DIR="$HOME/.claude/projects/<project-slug>"
```

## Claude Desktop

After `forge-reload.py` prints `forged:<new-session-id>`, register it in Claude
Desktop:

```bash
python3 scripts/forge-desktop-register.py <new-session-id> \
  --cli-project-dir ~/.claude/projects/<project-slug>
```

Preview first:

```bash
python3 scripts/forge-desktop-register.py <new-session-id> \
  --cli-project-dir ~/.claude/projects/<project-slug> \
  --dry-run
```

Then restart or refresh Claude Desktop and open the new session card. The title
includes `forge <first-8-chars>`.

## Guardrails

Whale Forge is intentionally conservative:

- source JSONL transcripts are not edited;
- `parentUuid` chains are rebuilt from scratch;
- leading half-tool continuations are dropped;
- a complete safe tool round can be inserted as a primer;
- side-effectful primer tools are skipped by name, including
  `memory_remember`, `write_diary`, and `send_email`;
- large tool results are truncated;
- image blocks become `[image omitted by forge]` instead of empty messages;
- output validation rejects empty user messages before writing.

Claude Desktop registration writes a new `local_*.json` card and backs up the
template card. It relies on Claude Desktop's current local metadata shape, so
use `--dry-run` after Desktop updates.

## Tests

```bash
python3 tests/test_forge_reload.py
```

The test suite uses synthetic JSONL fixtures and does not touch your real Claude
Code history.

## Status

Experimental. This works by understanding Claude Code's local transcript format
and Claude Desktop's local session card metadata. Those formats can change.

Use at your own risk, keep backups, and test with `--dry-run`.

## Credits

See [CREDITS.md](CREDITS.md). Whale Forge was shaped by Isa, 小克, and
小G / 玻璃齿轮: continuity target, implementation, and pressure testing all had
to meet before it was worth publishing.
