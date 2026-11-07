#!/usr/bin/env python3
"""token-tightwad: break your coding agent's stuck loops and prune tools it never uses, with no model calls.

Reads the session logs your agent already keeps and finds three kinds of waste: facts it keeps
rediscovering, tools it loads and never uses, and turns stuck in a loop. Python standard library only.

  python tightwad.py report [--repo DIR] [--agent claude|codex] [--html FILE]
  python tightwad.py facts  [--repo DIR] [--agent claude|codex] [--write FILE]
  python tightwad.py prune  [--repo DIR] [--apply | --restore]
  python tightwad.py hook   (Claude Code hook: reads the event on stdin)
  python tightwad.py --demo
"""
import argparse, hashlib, html, json, os, re, shlex, sys, tempfile
from pathlib import Path

# ---- reading sessions ----

def claude_projects():
    base = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    return Path(base) / "projects"

def claude_folder(repo):
    """The folder name Claude Code files a directory's sessions under."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(repo))

def _lines(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if isinstance(ev, dict):
                yield ev

def _is_prompt(ev):
    if ev.get("type") != "user" or ev.get("isMeta") or ev.get("isSidechain") or ev.get("isCompactSummary"):
        return False
    msg = ev.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, list):
        blocks = [b for b in content if isinstance(b, dict)]
        if any(b.get("type") == "tool_result" for b in blocks):
            return False
        content = " ".join(b.get("text") or "" for b in blocks if b.get("type") == "text")
    return isinstance(content, str) and not content.startswith(("<local-command", "[Request interrupted"))

def _strs(v):
    return [n for n in v if isinstance(n, str)] if isinstance(v, list) else []

def _key(v):
    return v if isinstance(v, str) else None

def read_claude(path):
    """One Claude Code log as {name, calls: [(tool, args, text, ok)], prompts, usage, offered}.
    Only assistant and user entries count, since newer logs repeat each request in `api-request-blob`,
    and usage is keyed by message id, since a log repeats it on every content block."""
    order, seen, results, usage, offered, prompts, turns = [], {}, {}, {}, set(), 0, []
    for ev in _lines(path):
        kind = ev.get("type")
        if kind == "attachment":
            att = ev.get("attachment")
            if isinstance(att, dict) and att.get("type") == "deferred_tools_delta":
                offered.update(_strs(att.get("addedNames")))
                offered.difference_update(_strs(att.get("removedNames")))
            continue
        if kind == "system" and isinstance(ev.get("tools"), list):  # the init event of a headless run names what was loaded
            offered.update(n for n in ev["tools"] if isinstance(n, str))
        if kind not in ("assistant", "user"):
            continue
        if _is_prompt(ev):
            prompts += 1
            turns.append(len(order))
        msg = ev.get("message") if isinstance(ev.get("message"), dict) else {}
        if kind == "assistant" and isinstance(msg.get("usage"), dict):
            usage[_key(msg.get("id")) or len(usage)] = msg["usage"]
        content = msg.get("content")
        for b in content if isinstance(content, list) else []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use" and _key(b.get("id")) and b["id"] not in seen:
                seen[b["id"]] = (b.get("name"), b.get("input") if isinstance(b.get("input"), dict) else {})
                order.append(b["id"])
            elif b.get("type") == "tool_result":
                c = b.get("content")
                if isinstance(c, list):
                    c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
                results[_key(b.get("tool_use_id"))] = (c if isinstance(c, str) else "", not b.get("is_error", False))
    calls = [(seen[t][0], seen[t][1], *results.get(t, ("", True))) for t in order]
    return {"name": Path(path).stem, "calls": calls, "prompts": prompts, "turns": turns, "offered": offered, "usage": effective_tokens(usage.values(), CLAUDE_WEIGHTS)}

def read_codex(path):
    """One Codex rollout. Codex's input total includes both cache fields, so fresh input is the difference."""
    calls, prompts, total, cwd, contexts, turns = [], 0, None, None, 0, []
    for ev in _lines(path):
        p = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
        if ev.get("type") == "session_meta":
            cwd = cwd or p.get("cwd")
        elif ev.get("type") == "turn_context":
            contexts += 1
        elif ev.get("type") == "event_msg":
            if p.get("type") == "task_started":
                prompts += 1
                turns.append(len(calls))
            elif p.get("type") == "token_count" and (p.get("info") or {}).get("total_token_usage"):
                total = p["info"]["total_token_usage"]
            elif p.get("type") == "item_completed":
                item = p.get("item") or {}
                if item.get("type") == "CommandExecution":
                    cmd = item.get("command")
                    cmd = shlex.join(str(c) for c in cmd) if isinstance(cmd, list) else str(cmd or "")
                    text = item.get("aggregated_output") or (item.get("stdout") or "") + (item.get("stderr") or "")
                    calls.append(("shell", {"command": cmd}, text, item.get("exit_code") == 0))
                elif item.get("type") == "FileChange":
                    for changed in item.get("changes") or {}:
                        calls.append(("apply_patch", {"path": changed}, "", True))
    usage = 0
    if total:
        cached, written = int(total.get("cached_input_tokens") or total.get("cache_read_input_tokens") or 0), int(total.get("cache_creation_input_tokens") or 0)
        fresh = int(total.get("input_tokens") or 0) - cached - written
        usage = effective_tokens([{"input_tokens": fresh, "cache_read_input_tokens": cached, "cache_creation_input_tokens": written,
                                   "output_tokens": int(total.get("output_tokens") or 0)}], CODEX_WEIGHTS)
    return {"name": Path(path).stem, "calls": calls, "prompts": prompts or contexts, "turns": turns, "offered": set(), "usage": usage, "cwd": cwd}

# Fresh-input-token equivalents, from TokenEconomizer's price table: a cached re-read costs a tenth, output several times more.
CLAUDE_WEIGHTS = {"input_tokens": 1.0, "cache_read_input_tokens": 0.10, "cache_creation_input_tokens": 1.25, "output_tokens": 5.0}
CODEX_WEIGHTS = {"input_tokens": 1.0, "cache_read_input_tokens": 0.10, "cache_creation_input_tokens": 1.25, "output_tokens": 6.0}

def effective_tokens(usages, weights):
    return round(sum(u[k] * w for u in usages for k, w in weights.items() if isinstance(u.get(k), (int, float))))

def load_sessions(repo, agent="claude", newest=200):
    """A repo's sessions, oldest first."""
    if agent == "codex":
        root = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "sessions"
        files = sorted(root.rglob("rollout-*.jsonl"), key=lambda p: p.stat().st_mtime) if root.is_dir() else []
        sessions = [read_codex(p) for p in files]
        return [s for s in sessions if s.get("cwd") and Path(s["cwd"]).resolve() == Path(repo).resolve()][-newest:]
    folder = claude_projects() / claude_folder(repo)
    files = sorted((p for p in folder.glob("*.jsonl") if not p.name.startswith("agent-")), key=lambda p: p.stat().st_mtime) if folder.is_dir() else []
    return [read_claude(p) for p in files[-newest:]]

# ---- what a shell command is trying to do ----

INTERPRETERS = {"python", "python3", "py", "node", "nodejs", "deno", "bun", "tsx", "ts-node", "ruby", "perl", "php", "Rscript", "julia", "lua", "elixir"}
RUNNER_HEADS = {"pytest", "py.test", "tox", "nox", "jest", "vitest", "mocha", "playwright", "cypress", "ava", "karma", "rspec", "phpunit", "ctest", "bats"}
RUNNER_MODULES = {"pytest", "unittest", "nose", "django"}
TASK_HEADS = {"make", "npm", "yarn", "pnpm", "cargo", "go", "mvn", "gradle", "gradlew", "dotnet", "mix", "swift", "rake"}
PACKAGE_TOOLS = {"pip", "pip3", "uv", "poetry", "pipenv", "conda", "npm", "yarn", "pnpm", "corepack", "apt", "apt-get", "gem", "bundle", "composer", "brew", "rustup", "pyenv", "nvm", "asdf"}
BUILD_HEADS = {"make", "cmake", "ninja", "bazel", "tsc", "eslint", "prettier", "ruff", "flake8", "pylint", "mypy", "pyright", "black", "isort", "golangci-lint", "gcc", "g++", "clang", "javac", "webpack", "vite"}
BUILD_SUBS = {"build", "check", "compile", "lint", "vet", "typecheck", "type-check", "clippy", "package", "verify", "fmt", "format"}
PREFIX_HEADS = {"sudo", "env", "time", "exec", "nice", "nohup"}
FORM_ERRORS = (("module_missing", r"ModuleNotFoundError|No module named|Cannot find module|ERR_MODULE_NOT_FOUND", "ModuleNotFoundError"),
               ("command_missing", r"command not found|: not found\b|is not recognized", "command not found"),
               ("script_missing", r"can't open file .*No such file or directory", "a file-not-found error"),
               ("permission_denied", r"Permission denied|EACCES", "permission denied"),
               ("usage_error", r"(?m)^usage:|unrecognized arguments|no such option|error: argument|unknown option", "a usage error"))
_TESTS_RAN = re.compile(r"\bRan [1-9]\d* tests?\b|\b[1-9]\d* (?:passed|failed|passing|failing)\b|\bcollected [1-9]\d* items?\b|^ok\s+\S+\s+[\d.]+s\b|\btest result: (?:ok|FAILED)\b|\bTests run: [1-9]\d*", re.M)
_ASSIGN = re.compile(r"^[A-Za-z_]\w*=")

def forms_of(cmd):
    """[(intent, form)] for a command. `cd tests && python runtests.py` keeps its `cd`, because that is what makes it work here."""
    out, cd = [], ""
    cmd = re.sub(r"<<-?\s*['\"]?(\w+)['\"]?[^\n]*\n.*?\n\s*\1\b", "", cmd or "", flags=re.S)
    for seg in re.split(r"&&|\|\||;|\n", cmd):
        seg = seg.split("|")[0].strip()
        m = re.match(r"^cd\s+(['\"]?)([^\s;&|'\"]+)\1$", seg)
        if m:
            cd = f"cd {m.group(2)} && "
            continue
        try:
            toks = shlex.split(seg)
        except ValueError:
            toks = seg.split()
        while toks and (toks[0] in PREFIX_HEADS or _ASSIGN.match(toks[0])):
            toks = toks[1:]
        if toks and toks[0] == "timeout":
            toks = toks[2:]
        if not toks:
            continue
        run = ""
        if toks[0] in ("poetry", "pipenv", "uv", "bundle") and toks[1:2] in (["run"], ["exec"]):
            run, toks = f"{toks[0]} run ", toks[2:]
        elif toks[0] == "npx":
            run, toks = "npx ", [t for t in toks[1:] if t not in ("--yes", "-y", "--no-install", "--no")]
        if not toks:
            continue
        head, rest = toks[0], toks[1:]
        base = head.replace("\\", "/").rsplit("/", 1)[-1]
        base = base[:-4] if base.endswith(".exe") else base
        pick = None
        if base in INTERPRETERS or re.match(r"^(?:python|node)\d(?:\.\d+)?$", base):
            if "-m" in rest and rest.index("-m") + 1 < len(rest) and rest[rest.index("-m") + 1] in RUNNER_MODULES:
                pick = ("run_tests", f"{head} -m {rest[rest.index('-m') + 1]}")
            elif not (rest and all(t in ("--version", "-V", "-VV", "-v") for t in rest)):
                pick = ("run_interpreter", head)
        elif base in RUNNER_HEADS:
            pick = ("run_tests", head)
        elif base in TASK_HEADS and rest[:1] == ["test"]:
            pick = ("run_tests", f"{base} test")
        elif base in ("npm", "yarn", "pnpm", "bun") and rest[:1] == ["run"] and len(rest) > 1:
            kind = rest[1].split(":")[0]
            pick = ("run_tests" if kind == "test" else "run_build" if kind in BUILD_SUBS else None, f"{base} run {rest[1]}")
        elif base in TASK_HEADS and rest[:1] and rest[0] in BUILD_SUBS:
            pick = ("run_build", f"{base} {rest[0]}")
        elif base in BUILD_HEADS:
            pick = ("run_build", base)
        elif base in PACKAGE_TOOLS:
            pick = ("package_tool", base)
        if pick and pick[0]:
            out.append((pick[0], cd + run + pick[1]))
    return out

def error_class(text):
    for name, rx, _ in FORM_ERRORS:
        if re.search(rx, text or "", re.I):
            return name
    return None

def is_bare(form):
    return " " not in form

# ---- facts: the same failure, the same repair, in session after session ----

K_PROMOTE, GAMMA, LAMBDA, MAX_FACTS, MAX_TOKENS = 3, 0.8, 5.0, 4, 150  # TokenEconomizer's validated lifecycle constants
HEADER = "# environment facts learned from earlier sessions in this repository"

def learn_facts(sessions):
    """A fact is a form that failed with an environment error, then one other form of the same intent that worked.
    Active once three sessions confirm the same pair, and dropped when the recency score falls below 0: a contradiction,
    the failing form working after all, weighs five confirmations.
    shortcut: a failed compound command is skipped, since its output does not say which part failed."""
    facts = {}
    for now, s in enumerate(sessions):
        failed, repaired, worked = {}, set(), set()
        for tool, args, text, ok in s["calls"]:
            cmd = args.get("command") if tool in ("Bash", "shell") else None
            if not isinstance(cmd, str):
                continue
            err = error_class(text) if not ok or "|" in cmd else None  # a pipe hides the exit code, so read the output
            forms = forms_of(cmd)
            for intent, form in forms:
                if err:
                    if len(forms) > 1:
                        continue
                    if not (intent == "run_tests" and err == "usage_error"):
                        failed.setdefault((intent, form), err)
                elif ok and (intent != "run_tests" or _TESTS_RAN.search(text or "")):
                    worked.add((intent, form))
                    for (i, a), e in failed.items():
                        if i == intent and a != form and (intent in ("run_tests", "run_build") or not is_bare(form)):
                            repaired.add((intent, a, form, e))
        for intent, a, b, err in repaired:
            f = facts.setdefault((intent, a, b), {"intent": intent, "a": a, "b": b, "err": err, "confirms": [], "contradicts": [], "state": "CANDIDATE"})
            f["confirms"].append(now)
        for key, f in facts.items():
            if (key[0], key[1]) in worked and (key[0], key[1]) not in failed:
                f["contradicts"].append(now)
            score = sum(GAMMA ** (now - i) for i in f["confirms"]) - LAMBDA * sum(GAMMA ** (now - i) for i in f["contradicts"])
            if f["state"] == "CANDIDATE" and len(set(f["confirms"])) >= K_PROMOTE:
                f["state"] = "ACTIVE"
            if f["state"] == "ACTIVE" and score < 0:
                f["state"] = "RETIRED"
    return list(facts.values())

def active_facts(facts):
    phrase = {name: p for name, _, p in FORM_ERRORS}
    out = []
    for f in sorted((f for f in facts if f["state"] == "ACTIVE"), key=lambda f: -len(f["confirms"])):
        out.append(f"`{f['a']} ...` fails here with {phrase[f['err']]}; `{f['b']} ...` works.")
    return out

def render_block(lines):
    """The block for the start of a session, capped at 4 facts and 150 tokens at about 4 characters a token."""
    kept, size = [], len(HEADER) + 1
    for line in lines[:MAX_FACTS]:
        if size + len(line) + 3 > MAX_TOKENS * 4:
            break
        kept.append(f"- {line}")
        size += len(line) + 3
    return "\n".join([HEADER] + kept) if kept else ""

BEGIN, END = "<!-- token-tightwad:start -->", "<!-- token-tightwad:end -->"

def _read_raw(path):
    """Text with its line endings untouched, so a rewrite never changes lines this tool does not own."""
    if not Path(path).exists():
        return ""
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()

def _write_raw(path, text):
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)

def _eol(text):
    return "\r\n" if "\r\n" in text else "\n"

def write_block(path, block):
    """Put the block in a file such as AGENTS.md, replacing only what this tool wrote before."""
    path = Path(path)
    old = _read_raw(path)
    has_markers = BEGIN in old and END in old
    if not block and not has_markers:
        return  # nothing learned and nothing to clear: leave the file alone
    eol = _eol(old)
    part = eol.join([BEGIN, *block.splitlines(), END])
    if has_markers:
        new = old[:old.index(BEGIN)] + part + old[old.index(END) + len(END):]
    else:
        new = old + (eol if old and not old.endswith("\n") else "") + part + eol
    _write_raw(path, new)

# ---- tool pruning: loaded in every request, never called here ----

PROTECTED = {"Bash", "Read", "Edit", "MultiEdit", "Write", "Grep", "Glob", "LS", "NotebookRead", "BashOutput", "KillShell", "KillBash", "AskUserQuestion", "EnterPlanMode", "ExitPlanMode",
             "TodoWrite", "TaskCreate", "TaskUpdate", "TaskGet", "TaskList", "TaskOutput", "TaskStop", "Agent", "Task", "Skill", "ToolSearch", "SlashCommand", "Monitor", "WebFetch", "WebSearch"}
HIGH_RISK, HIGH_RISK_FACTOR = {"NotebookEdit"}, 3
P_PROMPTS, M_SESSIONS, S_SESSIONS = 10, 2, 5  # p, m and s in TokenEconomizer's README

def propose_prune(sessions):
    """Tools never used here: over 10 prompts across at least 2 sessions, or over 5 sessions however short.
    Work tools, the web and a person's own tools are never proposed. A high-risk tool needs three times the evidence."""
    used = {c[0] for s in sessions for c in s["calls"]}
    prompts, count, proposed = sum(s["prompts"] for s in sessions), len(sessions), []
    offered = set().union(*(s["offered"] for s in sessions)) if sessions else set()
    for tool in sorted(offered - used - PROTECTED):
        k = HIGH_RISK_FACTOR if tool in HIGH_RISK else 1
        if (count >= M_SESSIONS and prompts >= P_PROMPTS * k) or count >= S_SESSIONS * k:
            proposed.append(tool)
    return proposed

def _settings(repo):
    return Path(repo) / ".claude" / "settings.local.json", Path(repo) / ".claude" / "tightwad-applied.json"

def _read_json(path):
    if not path.exists():
        return {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        sys.exit(f"{path} is not valid JSON, so nothing was changed")
    if not isinstance(doc, dict):
        sys.exit(f"{path} is not a JSON object, so nothing was changed")
    return doc

def _dump_settings(path, doc):
    eol = _eol(_read_raw(path))
    _write_raw(path, (json.dumps(doc, indent=2) + "\n").replace("\n", eol))

def _deny_list(path, doc):
    perms = doc.get("permissions", {})
    deny = perms.get("deny", []) if isinstance(perms, dict) else None
    if not isinstance(deny, list):
        sys.exit(f"{path} has a `permissions.deny` that is not a list, so nothing was changed")
    return deny

def _recorded(record):
    known = _read_json(record).get("added", [])
    if not isinstance(known, list):
        sys.exit(f"{record} has an `added` that is not a list, so nothing was changed")
    return known

def apply_prune(repo, tools):
    """Bare-name `permissions.deny` rules, which remove a tool from Claude Code's list from the next session on.
    The rules this tool adds are recorded, so restore takes back exactly those and never one you wrote."""
    path, record = _settings(repo)
    doc = _read_json(path)
    deny = _deny_list(path, doc)
    known = _recorded(record)  # read before the first write: a bad record must not leave rules restore cannot take back
    added = [t for t in tools if t not in deny]
    doc.setdefault("permissions", {})["deny"] = deny + added
    path.parent.mkdir(parents=True, exist_ok=True)
    created = not path.exists()
    _dump_settings(path, doc)
    record.write_text(json.dumps({"added": sorted(set(known) | set(added))}, indent=1) + "\n", encoding="utf-8")
    exclude = Path(repo) / ".git" / "info" / "exclude"
    if exclude.parent.is_dir():
        text = _read_raw(exclude)
        lines = [p for p in (".claude/settings.local.json" if created else "", ".claude/tightwad-applied.json") if p and p not in text]
        if lines:
            eol = _eol(text)
            _write_raw(exclude, text + (eol if text and not text.endswith("\n") else "") + eol.join(lines) + eol)
    return added

def restore_prune(repo):
    path, record = _settings(repo)
    ours = _recorded(record)
    doc = _read_json(path)
    deny = _deny_list(path, doc)
    removed = [t for t in deny if t in ours]
    if removed:
        doc["permissions"]["deny"] = [t for t in deny if t not in ours]
        _dump_settings(path, doc)
    if record.exists():
        record.unlink()
    return removed

# ---- the loop guard: TokenEconomizer's no-judge rule ----

THRESHOLDS = {"min_turns": 20, "edit_churn": 4, "distinct_edit_files": 3, "error_outputs": 6, "stagnation": 0.20}
_ERR = re.compile(r"(Traceback \(most recent call last\)|\b\w*Error\b|\bFAILED\b|\bassert\b|Exception|errors during collection|^# fail [1-9]|^not ok \d|\b[1-9]\d* (?:failing|failures?)\b|Failures: [1-9]|^(?-i:--- FAIL: |FAIL\t|FAIL$))", re.I | re.M)
_TEST_RUN = re.compile(r"(runtests(\.py)?|\bpytest\b|py\.test|manage\.py\s+test|python\s+-m\s+unittest|\btox\b|\bnox\b|\btrial\b|python\d?\s+-c\b)", re.I)
_CANON = {"bash": "bash", "shell": "bash", "read": "view", "view": "view", "grep": "grep", "glob": "glob", "edit": "edit", "multiedit": "edit", "str_replace": "edit", "apply_patch": "edit", "write": "create", "create": "create"}

def new_turn():
    return {"turns": 0, "edits": {}, "errors": 0, "test_runs": 0, "sigs": {}, "repeats": 0, "nudged": 0}

def failure_sig(text):
    first = next((ln for ln in (text or "").splitlines() if _ERR.search(ln)), None)
    if first is None:
        return None
    return hashlib.md5(re.sub(r"0x[0-9a-f]+|\d+|/\S+", "", first).strip().encode("utf-8", "replace")).hexdigest()[:10]

def update_turn(st, tool, args, text, failed=None):
    st["turns"] += 1
    args = args if isinstance(args, dict) else {}
    canon = _CANON.get((tool or "").lower(), tool or "?")
    if canon in ("edit", "create"):
        name = str(args.get("path") or args.get("filePath") or args.get("file_path") or "").replace("\\", "/").rstrip("/").split("/")[-1]
        if name:
            st["edits"][name] = st["edits"].get(name, 0) + 1
    if canon == "bash" and _TEST_RUN.search(str(args.get("command") or "")):
        st["test_runs"] += 1
    can_fail = failed is True if canon in ("view", "grep", "glob") else failed is not False
    if text and can_fail and _ERR.search(text):
        st["errors"] += 1
        sig = failure_sig(text)
        if sig:
            st["repeats"] += sig in st["sigs"]
            st["sigs"][sig] = st["sigs"].get(sig, 0) + 1
    return st

def is_stuck(st, t=THRESHOLDS):
    """Must have run a while, AND churn or scatter its edits, AND keep erroring, AND repeat the same failure.
    The last term is what separates a loop from a hard but productive grind."""
    total = sum(st["sigs"].values())
    return (st["turns"] >= t["min_turns"] and (max(st["edits"].values(), default=0) >= t["edit_churn"] or len(st["edits"]) >= t["distinct_edit_files"])
            and st["errors"] >= t["error_outputs"] and total > 0 and st["repeats"] / total >= t["stagnation"])

def nudge_text(st):
    busiest = max(st["edits"], key=st["edits"].get) if st["edits"] else "one file"
    return (f"Process check (automated): so far you've made {sum(st['edits'].values())} edits across {len(st['edits'])} file(s) (most to `{busiest}`) and run {st['test_runs']} verification(s), "
            f"and the same failure has recurred {st['repeats']} time(s) without the tests going green. This looks like a repetitive loop. Do NOT stop. Instead step back and try a MATERIALLY "
            "DIFFERENT hypothesis: re-read the failing test's expectation, consider a different file or root cause than the ones you've been editing, and verify the new hypothesis directly before editing again.")

# ---- hooks ----

def _state_dir():
    return Path(os.environ.get("TIGHTWAD_HOME") or Path.home() / ".token-tightwad") / "turns"

def _result_text(resp):
    if isinstance(resp, str):
        return resp
    if isinstance(resp, dict):
        return "\n".join(str(resp[k]) for k in ("stdout", "stderr", "content", "result", "error", "message") if resp.get(k))
    return ""

def handle_event(p):
    """One Claude Code hook event in, the JSON to print out, or None."""
    event, sid = p.get("hook_event_name"), re.sub(r"[^A-Za-z0-9_-]", "", str(p.get("session_id") or ""))[:128]
    if event == "SessionStart":
        if p.get("source") not in (None, "startup", "clear"):  # a resumed session already has it
            return None
        block = render_block(active_facts(learn_facts(load_sessions(p.get("cwd") or os.getcwd()))))
        return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": block}} if block else None
    if not sid or p.get("agent_id"):
        return None
    path = _state_dir() / f"{sid}.json"
    if event == "UserPromptSubmit":
        path.unlink(missing_ok=True)
        return None
    if event not in ("PostToolUse", "PostToolUseFailure"):
        return None
    st = json.loads(path.read_text(encoding="utf-8")) if path.exists() else new_turn()
    failed = True if event == "PostToolUseFailure" else None
    update_turn(st, p.get("tool_name"), p.get("tool_input"), p.get("error") or _result_text(p.get("tool_response")), failed)
    out = None
    if is_stuck(st) and st["errors"] and st["nudged"] < 2:
        if st["nudged"] == 0:
            out = {"hookSpecificOutput": {"hookEventName": event, "additionalContext": nudge_text(st)}}
            st["stuck_at"], st["nudged"] = st["turns"], 1
        elif st["turns"] - st["stuck_at"] >= 10:
            out = {"systemMessage": f"token-tightwad: this turn still looks stuck after a redirect ({st['repeats']} repeated failures). Press Esc to stop it if you want to. It will not stop by itself."}
            st["nudged"] = 2
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(st), encoding="utf-8")
    return out

def hook():
    """A hook must never get in the way of the session: anything that goes wrong here is silent."""
    try:
        out = handle_event(json.loads(sys.stdin.read() or "{}"))
        if out:
            print(json.dumps(out))
    except Exception as err:  # noqa: BLE001 - the session carries on as if this were not installed
        print(f"token-tightwad: {err}", file=sys.stderr)
    return 0

# ---- report ----

def replay_loops(sessions):
    """How many prompt-turns of each session the guard would have stepped into. The state starts over at each prompt, as it does live."""
    loops = []
    for s in sessions:
        cuts = sorted(set(s.get("turns", [])) | {0, len(s["calls"])})
        hits = 0
        for a, b in zip(cuts, cuts[1:]):
            st = new_turn()
            for tool, args, text, ok in s["calls"][a:b]:
                update_turn(st, tool, args, text, None if ok else True)
                if is_stuck(st):
                    hits += 1
                    break
        loops.append(hits)
    return loops

def build_report(repo, sessions):
    facts = learn_facts(sessions)
    return {"repo": str(repo), "sessions": len(sessions), "prompts": sum(s["prompts"] for s in sessions), "calls": sum(len(s["calls"]) for s in sessions),
            "tokens": [s["usage"] for s in sessions], "facts": active_facts(facts), "candidates": [f["a"] for f in facts if f["state"] == "CANDIDATE"],
            "retired": [f["a"] for f in facts if f["state"] == "RETIRED"], "failures": sum(len(f["confirms"]) for f in facts),
            "prune": (prune := propose_prune(sessions)), "listing_tokens": sum(len(t) + 1 for t in prune) // 4, "loops": replay_loops(sessions)}

def print_report(r):
    print(f"{r['repo']}: {r['sessions']} sessions, {r['prompts']} prompts, {r['calls']} tool calls, {sum(r['tokens']):,} effective tokens\n")
    print(f"Facts it keeps rediscovering ({len(r['facts'])} active, {len(r['candidates'])} still collecting evidence):")
    for f in r["facts"] or ["none yet"]:
        print(f"  {f}")
    print(f"\nTools offered and never used here ({len(r['prune'])}, at least {r['listing_tokens']} tokens per request if each costs only its listing line):")
    print("  " + (", ".join(r["prune"][:12]) + (f" and {len(r['prune']) - 12} more" if len(r["prune"]) > 12 else "") if r["prune"] else "none yet"))
    print(f"\nTurns the loop guard would have stepped into: {sum(r['loops'])} across {sum(1 for n in r['loops'] if n)} sessions")

def report_html(r):
    e, top = html.escape, max(r["tokens"], default=1) or 1
    bars = "".join(f'<div class="b"><i style="width:{t / top * 100:.1f}%" class="{"l" if n else ""}"></i><span>{t:,}{" · loop" if n else ""}</span></div>' for t, n in zip(r["tokens"], r["loops"]))
    li = lambda items, empty: "".join(f"<li>{e(i)}</li>" for i in items) or f"<li class=n>{empty}</li>"
    return f"""<!doctype html><meta charset="utf-8"><title>token-tightwad</title><style>
:root{{--bg:#fff;--fg:#1b1f24;--mu:#5c6570;--li:#e3e6ea;--ac:#2f6fb0;--wa:#c4561d}}@media (prefers-color-scheme:dark){{:root{{--bg:#14171a;--fg:#e6e8eb;--mu:#98a1ab;--li:#2b3036;--ac:#6ba5dc;--wa:#ec9b6b}}}}
body{{font:14px/1.5 ui-sans-serif,system-ui,sans-serif;margin:24px auto;max-width:820px;padding:0 16px;background:var(--bg);color:var(--fg)}}h1{{font-size:20px;margin:0}}h2{{font-size:15px;margin:24px 0 6px}}p{{color:var(--mu);margin:2px 0 14px}}
.s{{display:flex;gap:28px;flex-wrap:wrap}}.s b{{display:block;font-size:24px}}.s span{{color:var(--mu);font-size:12px}}ul{{margin:0;padding-left:18px}}.n{{color:var(--mu)}}.b{{position:relative;height:20px;margin:3px 0;border-bottom:1px solid var(--li)}}.b i{{display:block;height:100%;background:var(--ac)}}.b i.l{{background:var(--wa)}}.b span{{position:absolute;right:6px;top:0;font-size:12px;color:var(--fg);background:var(--bg);padding:0 4px}}code{{font:12px ui-monospace,monospace}}</style>
<h1>What your agent wastes in {e(Path(r['repo']).name)}</h1><p>Read from {r['sessions']} session logs. Nothing was sent anywhere and no model was called.</p>
<div class="s"><div><b>{sum(r['tokens']):,}</b><span>effective tokens</span></div><div><b>{len(r['facts'])}</b><span>facts it keeps rediscovering</span></div><div><b>{len(r['prune'])}</b><span>tools offered, never used</span></div><div><b>{sum(r['loops'])}</b><span>turns stuck in a loop</span></div></div>
<h2>Facts to state at session start</h2><ul>{li(r['facts'], 'None yet: a fact needs three sessions.')}</ul><h2>Tools to leave out</h2><ul>{li(r['prune'], 'None yet.')}</ul><h2>Effective tokens per session, oldest first</h2>{bars}"""

# ---- demo ----

def _demo_log(path, sid, steps, usage=(4000, 60000, 3000, 800)):
    """A made-up session in Claude Code's log format. `steps` is a list of (tool, input, output, is_error)."""
    rows, tid = [{"type": "user", "message": {"role": "user", "content": "Fix the failing test"}}], 0
    rows.append({"type": "attachment", "attachment": {"type": "deferred_tools_delta", "addedNames": ["CronCreate", "CronDelete", "NotebookEdit", "PushNotification", "SendMessage", "WebFetch", "mcp__figma__get_file", "mcp__figma__get_image", "mcp__slack__post_message", "mcp__slack__search"], "removedNames": []}})
    for tool, args, out, err in steps:
        tid += 1
        rows.append({"type": "assistant", "message": {"id": f"{sid}-{tid}", "content": [{"type": "tool_use", "id": f"t{tid}", "name": tool, "input": args}],
                                                      "usage": dict(zip(("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens"), usage))}})
        rows.append({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": f"t{tid}", "content": out, "is_error": err}]}})
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

def demo_logs(folder):
    """Six sessions in a made-up repo: `pytest` is not on PATH here, and the fifth session gets stuck."""
    folder.mkdir(parents=True, exist_ok=True)
    miss = ("Bash", {"command": "pytest tests/"}, "bash: pytest: command not found", True)
    good = ("Bash", {"command": "python -m pytest tests/"}, "collected 12 items\n12 passed in 0.4s", False)
    work = [miss, good, ("Read", {"file_path": "src/app.py"}, "code", False), ("Edit", {"file_path": "src/app.py"}, "ok", False), good]
    bad = ("Bash", {"command": "python -m pytest tests/"}, "E   AssertionError: expected 3, got 2\nFAILED tests/test_app.py::test_sum", True)
    loop = [miss, good] + [s for _ in range(11) for s in (("Edit", {"file_path": "src/app.py"}, "ok", False), bad)]
    for i in range(6):
        os.utime(folder, None)
        path = folder / f"session-{i + 1}.jsonl"
        _demo_log(path, f"s{i}", loop if i == 4 else work, (4000 + i * 300, 60000 + i * 4000, 3000, 800 if i != 4 else 3200))
        os.utime(path, (1700000000 + i * 100, 1700000000 + i * 100))

def demo():
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["CLAUDE_CONFIG_DIR"] = tmp
        os.environ["TIGHTWAD_HOME"] = tmp
        repo = Path(tmp) / "widget"
        repo.mkdir()
        demo_logs(claude_projects() / claude_folder(repo))
        sessions = load_sessions(repo)
        r = build_report(repo.name, sessions)
        print_report(r)
        Path("tightwad-demo.html").write_text(report_html(r), encoding="utf-8")
        print("\nWhat session 7 starts with:\n")
        print(render_block(r["facts"]))
        print("\nWhat a dry run of `prune --apply` would deny: " + ", ".join(r["prune"]))
        print("\nReport: tightwad-demo.html")

# ---- cli ----

def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if "--demo" in args:
        return demo()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("report", "facts", "prune"):
        s = sub.add_parser(name)
        s.add_argument("--repo", default=".")
        if name != "prune":
            s.add_argument("--agent", choices=("claude", "codex"), default="claude")
        if name == "report":
            s.add_argument("--html")
        if name == "facts":
            s.add_argument("--write", help="put the facts in this file, for example AGENTS.md")
        if name == "prune":
            s.add_argument("--apply", action="store_true", help="write the deny list to .claude/settings.local.json")
            s.add_argument("--restore", action="store_true", help="take back what --apply added")
    sub.add_parser("hook")
    a = ap.parse_args(args)
    if a.cmd == "hook":
        return hook()
    repo = Path(a.repo).resolve()
    if a.cmd == "prune" and a.restore:
        removed = restore_prune(repo)
        return print(f"Gave back {len(removed)} tools: {', '.join(removed)}" if removed else "Nothing to give back.")
    sessions = load_sessions(repo, getattr(a, "agent", "claude"))
    if not sessions:
        sys.exit(f"No {getattr(a, 'agent', 'claude')} sessions found for {repo}. Run this from a repo you have worked in with the agent, or pass --repo.")
    if a.cmd == "report":
        r = build_report(repo, sessions)
        print_report(r)
        if a.html:
            Path(a.html).write_text(report_html(r), encoding="utf-8")
            print(f"\nReport: {a.html}")
    elif a.cmd == "facts":
        block = render_block(active_facts(learn_facts(sessions)))
        print(block or "No active facts yet: a fact needs three sessions that confirm it.")
        if a.write:
            write_block(a.write, block)
    else:
        tools = propose_prune(sessions)
        if not tools:
            return print("No tool has enough evidence yet.")
        print(f"{len(tools)} tools never used here:\n  " + "\n  ".join(tools))
        if a.apply:
            added = apply_prune(repo, tools)
            print(f"\nAdded {len(added)} deny rules to {_settings(repo)[0]}. They apply from the next session. Undo with: python tightwad.py prune --restore --repo {repo}")
        else:
            print("\nDry run. Pass --apply to write them.")

if __name__ == "__main__":
    sys.exit(main())
