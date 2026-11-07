# token-tightwad

Read a repository's agent session logs and report the waste: facts the agent keeps rediscovering, tools it loads and never uses, turns stuck in a loop. Needs Python 3.9+, and nothing is sent anywhere. Claude Code logs come from `~/.claude/projects`, Codex logs from `~/.codex/sessions`.

Use it when asked about agent token cost, wasted tokens, a high bill, unused tools or MCP servers, or an agent repeating the same mistake or failing loop.

## Commands

Run from the repo root of this tool, and pass the repository to look at:

```bash
python tightwad.py report --repo /path/to/repo --agent codex --html report.html
python tightwad.py facts  --repo /path/to/repo --agent codex
python tightwad.py facts  --repo /path/to/repo --agent codex --write /path/to/repo/AGENTS.md
python tightwad.py --demo
```

## How to use the results

1. Start with `report`. It reads and changes nothing.
2. Show the facts, and ask before you run `facts --write`.

## What not to claim

- It reports waste, not savings. No savings figure has been measured for this tool.
- TokenEconomizer's 18.4-30.9% comes from its own paper, on more mechanisms. Do not quote it for this tool.
- Codex has `report` and `facts` only, and its log reader has not been tried on a real Codex log.
