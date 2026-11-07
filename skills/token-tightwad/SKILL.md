---
name: token-tightwad
description: "Find and cut what a coding agent wastes in a repository: facts it keeps rediscovering, tools it loads and never uses, turns stuck in a loop. Use when asked about agent token cost, wasted tokens, a high bill, unused tools or MCP servers, or an agent repeating the same mistake or failing loop. Makes no model calls."
---

# token-tightwad

Read a repository's agent session logs and report the waste. Needs Python 3.9+, and nothing is sent anywhere. Claude Code logs come from `~/.claude/projects`, Codex logs from `~/.codex/sessions`.

## Commands

Run from the repo root of this tool, and pass the repository to look at:

```bash
python tightwad.py report --repo /path/to/repo --html report.html
python tightwad.py facts  --repo /path/to/repo
python tightwad.py facts  --repo /path/to/repo --agent codex --write /path/to/repo/AGENTS.md
python tightwad.py prune  --repo /path/to/repo
python tightwad.py --demo
```

Only with the person's approval, because these change that repository's settings:

```bash
python tightwad.py prune --repo /path/to/repo --apply
python tightwad.py prune --repo /path/to/repo --restore
```

## How to use the results

1. Start with `report`. It reads and changes nothing.
2. Show the facts and the tool list, and ask before you run `prune --apply` or `facts --write`.
3. Say that pruned tools come back with `prune --restore`, and that it applies from the next session.

## What not to claim

- It reports waste, not savings. No savings figure has been measured for this tool.
- TokenEconomizer's 18.4-30.9% comes from its own paper, on more mechanisms. Do not quote it for this tool.
- Tools listed as deferred cost about one line each, so pruning them may save little.
- Codex has `report` and `facts` only, and its log reader has not been tried on a real Codex log.
