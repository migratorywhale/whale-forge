# Whale Forge for Claude Code

Whale Forge is an experimental way to continue a large Claude Code session
without asking the model to summarize itself first.

The basic idea:

1. Read a Claude Code JSONL transcript.
2. Keep a recent tail window of user/assistant events.
3. Rebuild every `sessionId` and `parentUuid` so the tail becomes a fresh
   transcript.
4. Resume that new transcript with `claude --resume <new-session-id>`.
5. For Claude Desktop Code sessions, register a new Desktop `local_*.json`
   card whose `cliSessionId` points at the forged transcript.

This is a hack around a real boundary: the transcript file is not the whole
runtime state. Claude Desktop has a separate local session card, so a forged
JSONL must also be registered there before the Desktop UI can open it as a
normal session.

## Files

- `scripts/whale-forge.py` creates a fresh Claude Code JSONL transcript.
- `scripts/whale-archive.py` wraps whale-forge, writes a signal file, and can
  archive the source JSONL to Markdown.
- `scripts/whale-desktop-register.py` clones a Claude Desktop Code session card
  and points the clone at a forged transcript.
- `tests/test_whale_forge.py` covers parent-chain rebuild, tool primer
  insertion, image placeholders, and non-empty user-message validation.

## CLI-only flow

From the same working directory as the Claude Code project you want to forge:

```bash
python3 scripts/whale-forge.py <old-session-id> \
  --keep-tokens 950000 \
  --threshold 950000 \
  --force
```

The command prints:

```text
forged:<new-session-id>
```

Resume it:

```bash
claude --resume <new-session-id>
```

If the transcript lives under a different Claude Code project directory, pass it
explicitly:

```bash
python3 scripts/whale-forge.py <old-session-id> \
  --project-dir ~/.claude/projects/<project-slug> \
  --force
```

You can also set `CLAUDE_PROJECT_DIR`.

## Claude Desktop flow

Claude Desktop Code mode stores local session cards here on macOS:

```text
~/Library/Application Support/Claude/claude-code-sessions/*/*/local_*.json
```

Those cards contain a `cliSessionId`. After forging a transcript, register it:

```bash
python3 scripts/whale-desktop-register.py <new-session-id> \
  --cli-project-dir ~/.claude/projects/<project-slug>
```

The register script:

- verifies that `<new-session-id>.jsonl` exists;
- chooses a recent Desktop session card as a template, preferably from the same
  `cwd`;
- clones the card with a new `local_<uuid>` id;
- sets `cliSessionId` to the forged session id;
- clears runtime bridge handles that should not be reused;
- backs up the template card before writing.

Use `--dry-run` first when testing:

```bash
python3 scripts/whale-desktop-register.py <new-session-id> \
  --cli-project-dir ~/.claude/projects/<project-slug> \
  --dry-run
```

Then restart or refresh Claude Desktop and open the new card. Its title includes
`forge <first-8-chars>`.

## Archive wrapper

`whale-archive.py` is a local convenience wrapper:

```bash
python3 scripts/whale-archive.py <old-session-id> \
  --project-dir ~/.claude/projects/<project-slug> \
  --keep-tokens 950000 \
  --threshold 950000 \
  --force-archive
```

It writes:

```text
~/.claude/whale-ready/<old-session-id>.signal
```

with the new session id. If `jsonl2md.py` is available, it also archives the old
session to Markdown. Override paths with:

- `WHALE_FORGE_SCRIPT`
- `JSONL2MD_SCRIPT`
- `WHALE_ARCHIVE_DIR`
- `WHALE_SIGNAL_DIR`

## Guardrails

Whale Forge intentionally preserves the source JSONL. It writes only new
transcripts and, for Desktop registration, new local session cards.

Important safeguards in `whale-forge.py`:

- rebuilds the parent chain from scratch;
- starts from a real human-visible user message;
- drops leading tool continuations when their tool_use is outside the kept
  window;
- can backfill a safe complete `tool_use` -> `tool_result` round as a tool
  primer;
- skips side-effectful primer tools whose names contain `memory_remember`,
  `write_diary`, or `send_email`;
- truncates large tool results;
- replaces image blocks with `[image omitted by forge]` so image-only user
  messages do not become empty API messages;
- validates that every user message has non-empty content before writing.

The Desktop registration script is macOS/Claude Desktop specific and depends on
Claude Desktop's current local metadata shape. Keep backups and test with
`--dry-run` after Claude Desktop updates.
