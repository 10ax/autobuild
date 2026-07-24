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
