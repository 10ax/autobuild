import json, subprocess, unittest
from pathlib import Path
from autobuild.config import Config
from autobuild.governor import Pace
from autobuild.build import build_argv, parse_result, run_build, verify_repo

class _CP:  # fake CompletedProcess
    def __init__(self, stdout="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, "", returncode

def _stream(*events):  # build a stream-json (JSONL) stdout blob
    return "\n".join(json.dumps(e) for e in events)

class TestBuild(unittest.TestCase):
    def test_argv_includes_safety_flags_and_model(self):
        argv = build_argv(Path("/b/x.md"), Path("/r/x"), "opus", Config(root="/home/tenax/autobuild"))
        self.assertIn("--permission-mode", argv)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "bypassPermissions")
        self.assertEqual(argv[argv.index("--model") + 1], "opus")
        self.assertIn("--add-dir", argv)

    def test_build_mode_argv_names_the_build_playbook(self):
        argv = build_argv(Path("/b/x.md"), Path("/r/x"), "sonnet",
                          Config(root="/home/tenax/autobuild"))
        prompt = argv[argv.index("-p") + 1]
        self.assertIn("CLAUDE.md", prompt)
        self.assertNotIn("AUTODOC.md", prompt)

    def test_document_mode_argv_names_the_autodoc_playbook_and_worktree(self):
        wt = Path("/home/tenax/autobuild/state/worktrees/autodoc-x")
        argv = build_argv(Path("/b/x.md"), Path("/home/u/repo"), "sonnet",
                          Config(root="/home/tenax/autobuild"), mode="document", work_dir=wt)
        prompt = argv[argv.index("-p") + 1]
        self.assertIn("AUTODOC.md", prompt)
        self.assertIn("/home/u/repo", prompt)
        self.assertIn(str(wt), prompt)
        dirs = [argv[i + 1] for i, a in enumerate(argv) if a == "--add-dir"]
        self.assertIn(str(wt), dirs)
        self.assertIn("/home/tenax/autobuild", dirs)

    def test_improve_mode_argv_names_the_improve_playbook_and_worktree(self):
        wt = Path("/home/tenax/autobuild/state/worktrees/quality-x")
        argv = build_argv(Path("/b/x.md"), Path("/home/u/repo"), "opus",
                          Config(root="/home/tenax/autobuild"), mode="improve", work_dir=wt)
        prompt = argv[argv.index("-p") + 1]
        self.assertIn("IMPROVE.md", prompt)
        self.assertNotIn("AUTODOC.md", prompt)
        self.assertIn("/home/u/repo", prompt)
        self.assertIn(str(wt), prompt)
        self.assertIn("/b/x.md", prompt)
        dirs = [argv[i + 1] for i, a in enumerate(argv) if a == "--add-dir"]
        self.assertIn(str(wt), dirs)

    def test_improve_mode_prompt_falls_back_when_the_file_is_absent(self):
        argv = build_argv(Path("/b/x.md"), Path("/home/u/repo"), "opus",
                          Config(root="/nonexistent-root"), mode="improve", work_dir=Path("/wt"))
        self.assertIn("IMPROVE.md", argv[argv.index("-p") + 1])

    def test_document_mode_prompt_falls_back_when_the_file_is_absent(self):
        argv = build_argv(Path("/b/x.md"), Path("/home/u/repo"), "sonnet",
                          Config(root="/nonexistent-root"), mode="document",
                          work_dir=Path("/wt"))
        self.assertIn("AUTODOC.md", argv[argv.index("-p") + 1])

    def test_run_build_runs_the_agent_inside_the_worktree(self):
        seen = {}

        def runner(argv, **kw):
            seen.update(kw)
            return _CP(stdout=json.dumps({"is_error": False, "total_cost_usd": 0.0}))

        run_build(Path("/b/x.md"), Path("/home/u/repo"), "sonnet",
                  Pace("low", 1, False, "sonnet"), Config(root="/tmp"),
                  mode="document", work_dir=Path("/tmp/wt"), runner=runner)
        self.assertEqual(seen.get("cwd"), "/tmp/wt")

    def test_parse_success_result(self):
        out = json.dumps({"is_error": False, "total_cost_usd": 0.42,
                          "usage": {"output_tokens": 100}, "session_id": "s1"})
        r = parse_result(out)
        self.assertFalse(r.is_error)
        self.assertAlmostEqual(r.cost_usd, 0.42)
        self.assertFalse(r.rate_limited)

    def test_parse_detects_rate_limit_and_reset(self):
        out = json.dumps({"is_error": True, "subtype": "error_max_turns",
                          "result": "Usage limit reached. reset_at:1753305600"})
        r = parse_result(out)
        self.assertTrue(r.rate_limited)
        self.assertEqual(r.reset_at, 1753305600.0)

    def test_run_build_uses_injected_runner(self):
        out = json.dumps({"is_error": False, "total_cost_usd": 1.0})
        r = run_build(Path("/b/x.md"), Path("/r/x"), "sonnet",
                      Pace("high", 3, True, "sonnet"), Config(root="/tmp"),
                      runner=lambda *a, **k: _CP(stdout=out))
        self.assertAlmostEqual(r.cost_usd, 1.0)

    def test_run_build_records_duration(self):
        out = json.dumps({"is_error": False, "total_cost_usd": 1.0})
        clock = iter([100.0, 107.5]).__next__          # start, end → 7.5s elapsed
        r = run_build(Path("/b/x.md"), Path("/r/x"), "sonnet",
                      Pace("high", 3, True, "sonnet"), Config(root="/tmp"),
                      runner=lambda *a, **k: _CP(stdout=out), clock=clock)
        self.assertAlmostEqual(r.duration_s, 7.5)

    def test_run_build_records_duration_on_timeout(self):
        def boom(*a, **k):
            raise subprocess.TimeoutExpired(cmd="claude", timeout=1)
        clock = iter([100.0, 130.0]).__next__          # timed out after 30s of wall time
        r = run_build(Path("/b/x.md"), Path("/r/x"), "sonnet",
                      Pace("low", 1, False, "sonnet"), Config(root="/tmp"),
                      runner=boom, clock=clock)
        self.assertTrue(r.is_error)
        self.assertAlmostEqual(r.duration_s, 30.0)

    def test_run_build_timeout_recovers_partial_cost_and_signal(self):
        # a killed build's captured stdout may still carry cost / a rate_limit_event — recover it
        # (keeps weekly spend + the oracle warm) while still flagging the run as errored/incomplete.
        partial = _stream(
            {"type": "rate_limit_event", "rate_limit_info":
                {"status": "allowed", "resetsAt": 1784900000, "rateLimitType": "five_hour"}},
            {"type": "result", "is_error": False, "total_cost_usd": 3.5},
        )
        def boom(*a, **k):
            raise subprocess.TimeoutExpired(cmd="claude", timeout=1, output=partial)
        clock = iter([100.0, 160.0]).__next__
        r = run_build(Path("/b/x.md"), Path("/r/x"), "opus",
                      Pace("high", 2, True, "opus"), Config(root="/tmp"), runner=boom, clock=clock)
        self.assertTrue(r.is_error)                 # timeout → incomplete → error, always
        self.assertTrue(r.raw.get("timeout"))
        self.assertAlmostEqual(r.cost_usd, 3.5)     # spend recovered from partial stream
        self.assertEqual(r.rate_status, "allowed")  # live signal recovered too
        self.assertAlmostEqual(r.duration_s, 60.0)

    def test_run_build_silences_build_stop_hook_via_env(self):
        captured = {}
        def spy(argv, **k):
            captured.update(k.get("env") or {})
            return _CP(stdout=json.dumps({"is_error": False, "total_cost_usd": 1.0}))
        run_build(Path("/b/x.md"), Path("/r/x"), "sonnet", Pace("low", 1, False, "sonnet"),
                  Config(root="/tmp"), runner=spy)
        self.assertEqual(captured.get("AUTOBUILD_NO_NOTIFY"), "1")  # build's own Stop hook stays quiet
        self.assertIn("PACE", captured)                            # existing env still passed

    def test_verify_repo_gates_on_exit_codes(self):
        self.assertTrue(verify_repo(Path("/r/x"), runner=lambda *a, **k: _CP(returncode=0)))
        self.assertFalse(verify_repo(Path("/r/x"), runner=lambda *a, **k: _CP(returncode=1)))

    def test_run_build_nonzero_exit_is_error(self):
        out = json.dumps({"total_cost_usd": 0.1})  # valid JSON, no is_error field
        r = run_build(Path("/b/x.md"), Path("/r/x"), "sonnet",
                      Pace("low", 1, False, "sonnet"), Config(root="/tmp"),
                      runner=lambda *a, **k: _CP(stdout=out, returncode=1))
        self.assertTrue(r.is_error)
        self.assertEqual(r.raw.get("returncode"), 1)

    def test_run_build_handles_missing_binary(self):
        def boom(*a, **k):
            raise FileNotFoundError("claude not found")
        r = run_build(Path("/b/x.md"), Path("/r/x"), "sonnet",
                      Pace("low", 1, False, "sonnet"), Config(root="/tmp"), runner=boom)
        self.assertTrue(r.is_error)

    def test_verify_repo_survives_missing_tool(self):
        def boom(*a, **k):
            raise FileNotFoundError("npm not found")
        self.assertFalse(verify_repo(Path("/r/x"), runner=boom))

    def test_verify_repo_survives_hang(self):
        def hang(*a, **k):
            raise subprocess.TimeoutExpired(cmd="npm", timeout=1)
        self.assertFalse(verify_repo(Path("/r/x"), runner=hang))

    # --- api_error_status: the structured limit signal from headless `claude -p` ---
    def test_parse_detects_rate_limit_via_api_error_status_dict(self):
        out = json.dumps({"is_error": True,
                          "api_error_status": {"status": 429, "message": "Too Many Requests"}})
        r = parse_result(out)
        self.assertTrue(r.rate_limited)
        self.assertEqual(r.api_error_status, {"status": 429, "message": "Too Many Requests"})

    def test_parse_detects_rate_limit_via_api_error_status_string(self):
        out = json.dumps({"is_error": True, "api_error_status": "429 rate_limit_error"})
        self.assertTrue(parse_result(out).rate_limited)

    def test_parse_reset_from_api_error_status(self):
        out = json.dumps({"is_error": True,
                          "api_error_status": {"code": 429, "resets_at": 1753305600}})
        r = parse_result(out)
        self.assertTrue(r.rate_limited)
        self.assertEqual(r.reset_at, 1753305600.0)

    def test_parse_success_with_limit_wording_not_flagged(self):
        # a SUCCESSFUL build whose own content mentions "rate limit" must NOT be flagged
        out = json.dumps({"is_error": False, "total_cost_usd": 0.2,
                          "result": "Implemented a token rate limit helper library.",
                          "api_error_status": None})
        self.assertFalse(parse_result(out).rate_limited)

    def test_parse_captures_api_error_status_none_on_success(self):
        out = json.dumps({"is_error": False, "api_error_status": None})
        self.assertIsNone(parse_result(out).api_error_status)

    def test_parse_error_without_api_status_falls_back_to_markers(self):
        # api_error_status absent but an error message carries a marker → still detected
        out = json.dumps({"is_error": True, "result": "Usage limit reached. reset_at:1753305600"})
        r = parse_result(out)
        self.assertTrue(r.rate_limited)
        self.assertEqual(r.reset_at, 1753305600.0)

    def test_parse_benign_id_containing_429_not_flagged(self):
        # an errored run whose api_error_status is a NON-limit error with an id that merely
        # contains "429" must NOT be read as rate-limited (would cost a needless long pause)
        out = json.dumps({"is_error": True,
                          "api_error_status": {"request_id": "req_4290xZ", "type": "overloaded_error"}})
        self.assertFalse(parse_result(out).rate_limited)

    # --- stream-json (per-build rate_limit_event capture) ---
    def test_argv_uses_stream_json_verbose(self):
        argv = build_argv(Path("/b/x.md"), Path("/r/x"), "opus", Config(root="/home/tenax/autobuild"))
        self.assertEqual(argv[argv.index("--output-format") + 1], "stream-json")
        self.assertIn("--verbose", argv)

    def test_parse_stream_json_captures_rate_limit_event(self):
        out = _stream(
            {"type": "system", "subtype": "init", "session_id": "s9"},
            {"type": "rate_limit_event", "rate_limit_info":
                {"status": "allowed", "resetsAt": 1784893800, "rateLimitType": "five_hour"}},
            {"type": "result", "is_error": False, "total_cost_usd": 0.3,
             "usage": {"output_tokens": 5}, "session_id": "s9"},
        )
        r = parse_result(out)
        self.assertFalse(r.is_error)
        self.assertAlmostEqual(r.cost_usd, 0.3)
        self.assertEqual(r.session_id, "s9")
        self.assertEqual(r.rate_status, "allowed")
        self.assertEqual(r.rate_reset_at, 1784893800.0)
        self.assertFalse(r.rate_limited)          # "allowed" is not a limit

    def test_parse_stream_json_rejected_is_rate_limited(self):
        out = _stream(
            {"type": "rate_limit_event", "rate_limit_info":
                {"status": "rejected", "resetsAt": 1784900000, "rateLimitType": "five_hour"}},
            {"type": "result", "is_error": True, "total_cost_usd": 0.0, "subtype": "error"},
        )
        r = parse_result(out)
        self.assertEqual(r.rate_status, "rejected")
        self.assertTrue(r.rate_limited)
        self.assertEqual(r.reset_at, 1784900000.0)

    def test_parse_batch_json_still_works_and_has_no_rate_status(self):
        out = json.dumps({"type": "result", "is_error": False, "total_cost_usd": 0.42})
        r = parse_result(out)
        self.assertAlmostEqual(r.cost_usd, 0.42)
        self.assertIsNone(r.rate_status)
        self.assertFalse(r.rate_limited)
