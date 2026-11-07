"""Rules, thresholds and wording come from TokenEconomizer's source; the expected values below follow from them."""
import contextlib, io, json, os, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import tightwad as tw

MISS = ("Bash", {"command": "pytest tests/"}, "bash: pytest: command not found", False)
GOOD = ("Bash", {"command": "python -m pytest tests/"}, "12 passed in 0.4s", True)


def session(calls, prompts=3, offered=(), usage=0):
    return {"name": "s", "calls": list(calls), "prompts": prompts, "offered": set(offered), "usage": usage}


class Temp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.env = dict(os.environ)
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.tmp / "config")
        os.environ["TIGHTWAD_HOME"] = str(self.tmp / "home")
        os.environ["CODEX_HOME"] = str(self.tmp / "codex")

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env)
        shutil.rmtree(self.tmp)


class ReadClaude(Temp):
    def write(self, rows):
        path = self.tmp / "s.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in rows) + "not json\n", encoding="utf-8")
        return tw.read_claude(path)

    def test_pairs_calls_with_results(self):
        s = self.write([{"type": "assistant", "message": {"id": "m1", "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]}},
                        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": "boom"}], "is_error": True}]}}])
        self.assertEqual(s["calls"], [("Bash", {"command": "ls"}, "boom", False)])
        self.assertEqual(s["prompts"], 0)

    def test_usage_counts_each_message_once_with_cache_weights(self):
        u = {"input_tokens": 100, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 100, "output_tokens": 10}
        s = self.write([{"type": "assistant", "message": {"id": "m1", "usage": u, "content": [{"type": "text", "text": "a"}]}},
                        {"type": "assistant", "message": {"id": "m1", "usage": u, "content": [{"type": "text", "text": "b"}]}}])
        self.assertEqual(s["usage"], round(100 + 100 + 125 + 50))

    def test_request_blobs_do_not_count_calls_twice(self):
        block = {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}
        s = self.write([{"type": "assistant", "message": {"content": [block]}}, {"type": "api-request-blob", "message": {"content": [dict(block, id="t2")]}}])
        self.assertEqual(len(s["calls"]), 1)

    def test_prompts_exclude_tool_results_and_meta(self):
        s = self.write([{"type": "user", "message": {"content": "do it"}}, {"type": "user", "isMeta": True, "message": {"content": "note"}},
                        {"type": "user", "message": {"content": "[Request interrupted by user]"}}, {"type": "user", "isSidechain": True, "message": {"content": "sub"}}])
        self.assertEqual(s["prompts"], 1)

    def test_offered_tools_follow_add_and_remove(self):
        s = self.write([{"type": "attachment", "attachment": {"type": "deferred_tools_delta", "addedNames": ["A", "B"], "removedNames": []}},
                        {"type": "attachment", "attachment": {"type": "deferred_tools_delta", "addedNames": [], "removedNames": ["A"]}}])
        self.assertEqual(s["offered"], {"B"})

    def test_headless_init_event_names_loaded_tools(self):
        self.assertEqual(self.write([{"type": "system", "subtype": "init", "tools": ["Bash", "mcp__a__b"]}])["offered"], {"Bash", "mcp__a__b"})


class ReadCodex(Temp):
    def test_rollout_commands_files_and_usage(self):
        rows = [{"type": "session_meta", "payload": {"cwd": str(self.tmp)}}, {"type": "event_msg", "payload": {"type": "task_started"}},
                {"type": "event_msg", "payload": {"type": "item_completed", "item": {"type": "CommandExecution", "command": ["pytest", "tests/"], "aggregated_output": "not found", "exit_code": 127}}},
                {"type": "event_msg", "payload": {"type": "item_completed", "item": {"type": "FileChange", "changes": {"a.py": {"type": "update"}}}}},
                {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 1000, "cached_input_tokens": 800, "output_tokens": 10}}}}]
        folder = self.tmp / "codex" / "sessions" / "2026"
        folder.mkdir(parents=True)
        (folder / "rollout-1.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        s = tw.load_sessions(self.tmp, "codex")[0]
        self.assertEqual(s["calls"][0], ("shell", {"command": "pytest tests/"}, "not found", False))
        self.assertEqual(s["calls"][1][:2], ("apply_patch", {"path": "a.py"}))
        self.assertEqual(s["prompts"], 1)
        self.assertEqual(s["usage"], round(200 + 80 + 60))


class Forms(unittest.TestCase):
    def test_intents(self):
        for cmd, want in (("pytest -k x", [("run_tests", "pytest")]), ("python -m pytest tests", [("run_tests", "python -m pytest")]), ("python script.py", [("run_interpreter", "python")]),
                          ("npm test", [("run_tests", "npm test")]), ("npm run build", [("run_build", "npm run build")]), ("cargo clippy", [("run_build", "cargo clippy")]), ("pip install x", [("package_tool", "pip")]),
                          ("ls -la", []), ("poetry run pytest", [("run_tests", "poetry run pytest")]), ("npx -y vitest run", [("run_tests", "npx vitest")]), ("FOO=1 pytest", [("run_tests", "pytest")])):
            self.assertEqual(tw.forms_of(cmd), want, cmd)

    def test_cd_is_part_of_the_form(self):
        self.assertEqual(tw.forms_of("cd tests && python runtests.py"), [("run_interpreter", "cd tests && python")])

    def test_compound_and_piped_commands(self):
        self.assertEqual(tw.forms_of("make build && pytest | tail -5"), [("run_build", "make build"), ("run_tests", "pytest")])

    def test_heredoc_body_is_not_read_as_commands(self):
        self.assertEqual(tw.forms_of("cat > f <<EOF\npytest\nEOF\n"), [])

    def test_bad_quoting_does_not_raise(self):
        self.assertEqual(tw.forms_of("pytest 'oops"), [("run_tests", "pytest")])


class Facts(unittest.TestCase):
    def test_three_sessions_make_a_fact(self):
        two = tw.learn_facts([session([MISS, GOOD])] * 2)
        self.assertEqual(tw.active_facts(two), [])
        three = tw.learn_facts([session([MISS, GOOD])] * 3)
        self.assertEqual(tw.active_facts(three), ["`pytest ...` fails here with command not found; `python -m pytest ...` works."])

    def test_a_different_repair_each_time_is_not_a_fact(self):
        sessions = [session([("Bash", {"command": "python a.py"}, "ModuleNotFoundError: No module named 'x'", False), ("Bash", {"command": f"cd dir{i} && python a.py"}, "ok", True)]) for i in range(5)]
        self.assertEqual(tw.active_facts(tw.learn_facts(sessions)), [])

    def test_a_successful_command_that_prints_an_error_word_is_not_a_failure(self):
        printed = ("Bash", {"command": "pytest tests/"}, "12 passed\nchecking that 'command not found' is handled", True)
        self.assertEqual(tw.learn_facts([session([printed, GOOD])] * 3), [])

    def test_a_pipe_hides_the_exit_code_so_the_output_decides(self):
        piped = ("Bash", {"command": "pytest tests/ | tail -5"}, "bash: pytest: command not found", True)
        self.assertEqual(len(tw.active_facts(tw.learn_facts([session([piped, GOOD])] * 3))), 1)

    def test_a_failed_compound_command_is_no_evidence(self):
        both = ("Bash", {"command": "pytest && make build"}, "bash: pytest: command not found", False)
        self.assertEqual(tw.learn_facts([session([both, GOOD])] * 3), [])

    def test_repair_must_come_after_the_failure(self):
        self.assertEqual(tw.learn_facts([session([GOOD, MISS])] * 3), [])

    def test_test_run_only_counts_when_tests_ran(self):
        filtered = ("Bash", {"command": "python -m pytest | grep FAILED"}, "", True)
        self.assertEqual(tw.learn_facts([session([MISS, filtered])] * 3), [])

    def test_usage_error_in_a_test_run_is_a_mistake_not_a_fact(self):
        wrong = ("Bash", {"command": "pytest --nope"}, "usage: pytest [options]\nerror: unrecognized arguments: --nope", False)
        self.assertEqual(tw.learn_facts([session([wrong, GOOD])] * 3), [])

    def test_bare_repair_of_a_bare_interpreter_is_not_a_fact(self):
        py3 = ("Bash", {"command": "python3 x.py"}, "bash: python3: command not found", False)
        py = ("Bash", {"command": "python x.py"}, "done", True)
        self.assertEqual(tw.learn_facts([session([py3, py])] * 3), [])

    def test_fact_retires_when_the_failing_form_starts_working(self):
        worked = ("Bash", {"command": "pytest tests/"}, "12 passed in 0.4s", True)
        facts = tw.learn_facts([session([MISS, GOOD])] * 3 + [session([worked])])
        self.assertEqual(facts[0]["state"], "RETIRED")
        self.assertEqual(tw.active_facts(facts), [])

    def test_empty_block_clears_the_old_facts(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "AGENTS.md"
            p.write_bytes(b"# Mine\n")
            tw.write_block(p, "- `pytest` fails here")
            tw.write_block(p, "")
            text = p.read_bytes().decode()
        self.assertNotIn("pytest", text)
        self.assertTrue(text.startswith("# Mine\n"))

    def test_empty_block_never_creates_or_touches_a_file(self):
        with tempfile.TemporaryDirectory() as d:
            new, mine = Path(d) / "NEW.md", Path(d) / "MINE.md"
            mine.write_bytes(b"# Mine\n")
            tw.write_block(new, "")
            tw.write_block(mine, "")
            self.assertFalse(new.exists())
            self.assertEqual(mine.read_bytes(), b"# Mine\n")

    def test_write_block_keeps_every_line_ending(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "AGENTS.md"
            for raw, eol in ((b"# Mine\nline one\nline two\n", b"\n"), (b"# Mine\r\nline one\r\n", b"\r\n")):
                p.write_bytes(raw)
                tw.write_block(p, "one\ntwo")
                tw.write_block(p, "three\nfour")
                data = p.read_bytes()
                self.assertTrue(data.startswith(raw))
                self.assertEqual(data.count(b"\n"), data.count(eol))
                self.assertIn(b"three" + eol + b"four", data)

    def test_block_is_capped(self):
        lines = [f"`tool{i} ...` fails here with command not found; `python -m tool{i} ...` works." for i in range(9)]
        block = tw.render_block(lines)
        self.assertLessEqual(block.count("\n- ") , tw.MAX_FACTS)
        self.assertLessEqual(len(block), tw.MAX_TOKENS * 4)
        self.assertEqual(tw.render_block([]), "")

    def test_write_block_replaces_only_its_own_part(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "AGENTS.md"
            p.write_text("# Mine\nkeep\n", encoding="utf-8")
            tw.write_block(p, "one")
            tw.write_block(p, "two")
            text = p.read_text(encoding="utf-8")
        self.assertIn("# Mine\nkeep\n", text)
        self.assertIn("two", text)
        self.assertNotIn("one", text)
        self.assertEqual(text.count(tw.BEGIN), 1)


class Prune(Temp):
    def test_evidence_rule(self):
        never = {"mcp__x__y"}
        self.assertEqual(tw.propose_prune([session([], 5, never)]), [])  # one session never decides, whatever its prompts
        self.assertIn("mcp__x__y", tw.propose_prune([session([], 5, never)] * 2))  # 10 prompts over 2 sessions
        self.assertNotIn("mcp__x__y", tw.propose_prune([session([], 4, never)] * 2))
        self.assertIn("mcp__x__y", tw.propose_prune([session([], 0, never)] * 5))  # 5 sessions however short

    def test_used_and_protected_tools_are_never_proposed(self):
        s = session([("mcp__x__y", {}, "", True)], 5, {"mcp__x__y", "Bash", "WebFetch", "Agent"})
        got = tw.propose_prune([s] * 2)
        for name in ("mcp__x__y", "Bash", "WebFetch", "Agent"):
            self.assertNotIn(name, got)

    def test_high_risk_tool_needs_three_times_the_evidence(self):
        self.assertNotIn("NotebookEdit", tw.propose_prune([session([], 5, {"NotebookEdit"})] * 2))
        self.assertIn("NotebookEdit", tw.propose_prune([session([], 5, {"NotebookEdit"})] * 6))

    def test_only_tools_the_logs_show_are_proposed(self):
        self.assertEqual(tw.propose_prune([session([], 9)] * 9), [])

    def test_apply_and_restore_leave_your_rules_alone(self):
        repo = self.tmp / "repo"
        (repo / ".claude").mkdir(parents=True)
        (repo / ".git" / "info").mkdir(parents=True)
        (repo / ".claude" / "settings.local.json").write_text(json.dumps({"permissions": {"deny": ["Mine"], "allow": ["Bash"]}, "other": 1}), encoding="utf-8")
        self.assertEqual(tw.apply_prune(repo, ["Mine", "ToolA", "ToolB"]), ["ToolA", "ToolB"])
        doc = json.loads((repo / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
        self.assertEqual(doc["permissions"]["deny"], ["Mine", "ToolA", "ToolB"])
        self.assertEqual((doc["other"], doc["permissions"]["allow"]), (1, ["Bash"]))
        self.assertIn("tightwad-applied.json", (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8"))
        self.assertEqual(tw.restore_prune(repo), ["ToolA", "ToolB"])
        self.assertEqual(json.loads((repo / ".claude" / "settings.local.json").read_text(encoding="utf-8"))["permissions"]["deny"], ["Mine"])
        self.assertEqual(tw.restore_prune(repo), [])

    def test_broken_settings_are_left_alone(self):
        repo = self.tmp / "repo"
        (repo / ".claude").mkdir(parents=True)
        path = repo / ".claude" / "settings.local.json"
        path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(SystemExit):
            tw.apply_prune(repo, ["ToolA"])
        self.assertEqual(path.read_text(encoding="utf-8"), "{broken")


class PruneSafety(Temp):
    def repo(self, settings=None, record=None):
        repo = self.tmp / "r"
        (repo / ".claude").mkdir(parents=True)
        path, rec = tw._settings(repo)
        if settings is not None:
            path.write_bytes(settings)
        if record is not None:
            rec.write_bytes(record)
        return repo, path, rec

    def test_bad_record_stops_before_settings_change(self):
        repo, path, _ = self.repo(b"{}", b"{bad")
        with self.assertRaises(SystemExit):
            tw.apply_prune(repo, ["ToolA"])
        self.assertEqual(path.read_bytes(), b"{}")

    def test_wrong_types_get_a_message_and_change_nothing(self):
        for bad in (b'{"permissions": []}', b'{"permissions": {"deny": "x"}}'):
            repo, path, _ = self.repo(bad)
            for call in (lambda: tw.apply_prune(repo, ["ToolA"]), lambda: tw.restore_prune(repo)):
                with self.assertRaises(SystemExit) as e:
                    call()
                self.assertIn("nothing was changed", str(e.exception))
            self.assertEqual(path.read_bytes(), bad)
            shutil.rmtree(repo)

    def test_settings_keep_crlf(self):
        repo, path, _ = self.repo(b'{\r\n  "other": 1\r\n}\r\n')
        tw.apply_prune(repo, ["ToolA"])
        data = path.read_bytes()
        self.assertEqual(data.count(b"\n"), data.count(b"\r\n"))
        tw.restore_prune(repo)
        data = path.read_bytes()
        self.assertEqual(data.count(b"\n"), data.count(b"\r\n"))


class MalformedLog(Temp):
    def test_odd_shapes_do_not_stop_the_reader(self):
        rows = [{"type": "user", "message": "hi"}, {"type": "attachment", "attachment": "x"},
                {"type": "assistant", "message": {"id": ["x"], "usage": {"input_tokens": 1}, "content": [{"type": "tool_use", "id": ["a"], "name": "Bash"}]}},
                {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": ["a"], "content": "x"}]}},
                {"type": "assistant", "message": {"id": "m", "usage": {"input_tokens": "many"}, "content": []}},
                {"type": "attachment", "attachment": {"type": "deferred_tools_delta", "addedNames": "x"}}]
        path = self.tmp / "s.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        self.assertEqual(tw.read_claude(path)["calls"], [])


FAIL = "E   AssertionError: expected 3, got 2\nFAILED tests/test_app.py::test_sum"


def feed(st, rounds):
    for _ in range(rounds):
        tw.update_turn(st, "Edit", {"file_path": "src/app.py"}, "ok", False)
        tw.update_turn(st, "Bash", {"command": "python -m pytest"}, FAIL, True)
    return st


class LoopGuard(unittest.TestCase):
    def test_stuck_needs_every_signal(self):
        self.assertFalse(tw.is_stuck(feed(tw.new_turn(), 9)))  # 18 calls: too early
        self.assertTrue(tw.is_stuck(feed(tw.new_turn(), 10)))

    def test_changing_failures_are_a_hard_grind_not_a_loop(self):
        st = tw.new_turn()
        for i in range(12):
            tw.update_turn(st, "Edit", {"file_path": "a.py"}, "ok", False)
            tw.update_turn(st, "Bash", {"command": "pytest"}, f"E   ValueError: step {'abcdefghijklmn'[i]}", True)
        self.assertEqual(st["repeats"], 0)
        self.assertFalse(tw.is_stuck(st))

    def test_signature_ignores_numbers_and_paths(self):
        self.assertEqual(tw.failure_sig("ValueError at /a/b line 12"), tw.failure_sig("ValueError at /c/d line 99"))
        self.assertIsNone(tw.failure_sig("all good"))

    def test_reading_a_file_full_of_asserts_is_not_a_failure(self):
        st = tw.update_turn(tw.new_turn(), "Read", {"file_path": "t.py"}, "assert x == 1", None)
        self.assertEqual(st["errors"], 0)

    def test_nudge_mirrors_only_what_the_agent_did(self):
        text = tw.nudge_text(feed(tw.new_turn(), 10))
        self.assertIn("10 edits across 1 file(s) (most to `app.py`)", text)
        self.assertIn("Do NOT stop", text)


class Replay(unittest.TestCase):
    CALLS = [c for _ in range(12) for c in (("Edit", {"file_path": "a.py"}, "ok", True), ("Bash", {"command": "pytest"}, FAIL, False))]

    def test_one_hit_per_stuck_turn(self):
        self.assertEqual(tw.replay_loops([session(self.CALLS)]), [1])

    def test_prompts_split_a_long_session_into_turns(self):
        s = dict(session(self.CALLS), turns=[0, 8, 16])  # each stretch is too short to count as stuck
        self.assertEqual(tw.replay_loops([s]), [0])


class Hook(Temp):
    def fire(self, name, **kw):
        return tw.handle_event(dict({"hook_event_name": name, "session_id": "abc"}, **kw))

    def post(self, n):
        out = []
        for _ in range(n):
            out.append(self.fire("PostToolUse", tool_name="Edit", tool_input={"file_path": "app.py"}, tool_response={"content": "ok"}))
            out.append(self.fire("PostToolUseFailure", tool_name="Bash", tool_input={"command": "pytest"}, error=FAIL))
        return [o for o in out if o]

    def test_session_start_gives_the_facts(self):
        repo = self.tmp / "repo"
        repo.mkdir()
        tw.demo_logs(tw.claude_projects() / tw.claude_folder(repo))
        out = self.fire("SessionStart", cwd=str(repo), source="startup")
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIn("python -m pytest", out["hookSpecificOutput"]["additionalContext"])
        self.assertIsNone(self.fire("SessionStart", cwd=str(repo), source="resume"))

    def test_session_start_with_no_history_is_silent(self):
        self.assertIsNone(self.fire("SessionStart", cwd=str(self.tmp), source="startup"))

    def test_loop_gets_one_redirect_then_one_notice(self):
        got = self.post(10)
        self.assertEqual(len(got), 1)
        self.assertIn("Process check", got[0]["hookSpecificOutput"]["additionalContext"])
        later = self.post(6)
        self.assertEqual(len(later), 1)
        self.assertIn("still looks stuck", later[0]["systemMessage"])
        self.assertEqual(self.post(10), [])

    def test_a_new_prompt_starts_a_new_turn(self):
        self.post(9)
        self.fire("UserPromptSubmit")
        self.assertEqual(self.post(9), [])

    def test_subagents_are_left_alone(self):
        self.assertIsNone(self.fire("PostToolUse", agent_id="x", tool_name="Edit"))

    def test_hook_never_fails_the_session(self):
        p = subprocess.run([sys.executable, str(ROOT / "tightwad.py"), "hook"], input="{not json", capture_output=True, text=True)
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout, "")

    def test_hostile_session_id_stays_in_the_state_folder(self):
        tw.handle_event({"hook_event_name": "PostToolUse", "session_id": "../../evil", "tool_name": "Read", "tool_input": {}, "tool_response": "x"})
        self.assertFalse((self.tmp / "evil.json").exists())
        self.assertTrue((self.tmp / "home" / "turns" / "evil.json").exists())


class Cli(Temp):
    def test_demo_finds_all_three_kinds_of_waste(self):
        repo = self.tmp / "widget"
        repo.mkdir()
        tw.demo_logs(tw.claude_projects() / tw.claude_folder(repo))
        r = tw.build_report(repo, tw.load_sessions(repo))
        self.assertEqual(r["sessions"], 6)
        self.assertEqual(r["facts"], ["`pytest ...` fails here with command not found; `python -m pytest ...` works."])
        self.assertIn("mcp__figma__get_file", r["prune"])
        self.assertNotIn("WebFetch", r["prune"])
        self.assertEqual(r["loops"], [0, 0, 0, 0, 1, 0])
        self.assertIn("<title>token-tightwad</title>", tw.report_html(r))

    def test_no_sessions_is_a_clear_error(self):
        with self.assertRaises(SystemExit) as e:
            tw.main(["report", "--repo", str(self.tmp)])
        self.assertIn("No claude sessions", str(e.exception))

    def test_reading_logs_changes_nothing(self):
        repo = self.tmp / "widget"
        repo.mkdir()
        tw.demo_logs(tw.claude_projects() / tw.claude_folder(repo))
        with contextlib.redirect_stdout(io.StringIO()):
            tw.main(["report", "--repo", str(repo)])
            tw.main(["facts", "--repo", str(repo)])
            tw.main(["prune", "--repo", str(repo)])
        self.assertEqual(list(repo.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
