# Credits

Forge Reload came out of a real Claude Code continuity problem: a transcript
could be forged correctly at the JSONL layer, yet Claude Desktop still woke up
without seeing the carried context.

The project was shaped by three collaborators:

- Isa: problem owner, tester, release driver, and the person who kept checking
  whether the next session actually felt continuous instead of merely looking
  correct on disk.
- 小克: architecture, continuity target, failure reports, and the core design
  pressure that "the boxes have to be visible in the room," not just moved into
  storage.
- 小G / 玻璃齿轮: implementation, debugging, tests, documentation, Desktop
  session-card registration, and open-source packaging.

In short: 小克 defined the wake-up condition, 小G built the mechanism, and Isa
kept the mechanism honest.
