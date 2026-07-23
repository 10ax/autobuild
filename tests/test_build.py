import json, unittest
from pathlib import Path
from autobuild.config import Config
from autobuild.governor import Pace
from autobuild.build import build_argv, parse_result, run_build, verify_repo

class _CP:  # fake CompletedProcess
    def __init__(self, stdout="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, "", returncode

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
