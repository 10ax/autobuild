"""The provider seam: one runner per backend, same BuildResult out.

The daemon used to shell out to `claude -p ... --output-format stream-json` directly and
parse its events inline. That made the seat the only possible backend, so the parser could
not be tested apart from the CLI and nothing else could be plugged in. Here each backend
owns its argv and its parse; `run_build` only knows the protocol.
"""
import json
import unittest

from autobuild.config import Config
from autobuild.runner import (ClaudeSeatRunner, OpenCodeRunner, build_runner,
                              METERED, SEAT)


def cfg(**kw) -> Config:
    from pathlib import Path
    base = dict(root=Path("/tmp/autobuild-runner-test"))
    base.update(kw)
    return Config(**base)


class TestMeteredClassification(unittest.TestCase):
    """Which backends are billed per token decides whether the weekly guard means anything."""

    def test_seat_is_not_metered(self):
        self.assertFalse(build_runner("claude", cfg()).metered)

    def test_opencode_is_metered(self):
        self.assertTrue(build_runner("opencode", cfg()).metered)

    def test_unknown_provider_is_rejected(self):
        with self.assertRaises(ValueError):
            build_runner("something-else", cfg())

    def test_classification_constants(self):
        self.assertIn("claude", SEAT)
        self.assertIn("opencode", METERED)


class TestClaudeSeatRunner(unittest.TestCase):
    """The seat runner must keep emitting exactly what the daemon consumes today."""

    def test_argv_shape(self):
        r = ClaudeSeatRunner(cfg())
        argv = r.argv("PROMPT", "/repo", "sonnet", work_dir="/wt")
        self.assertEqual(argv[0], "claude")
        self.assertIn("--output-format", argv)
        self.assertIn("stream-json", argv)
        self.assertIn("--permission-mode", argv)
        self.assertIn("bypassPermissions", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "sonnet")

    def test_work_dir_adds_a_second_add_dir(self):
        argv = ClaudeSeatRunner(cfg()).argv("P", "/repo", "sonnet", work_dir="/wt")
        dirs = [argv[i + 1] for i, a in enumerate(argv) if a == "--add-dir"]
        self.assertEqual(dirs, ["/repo", "/wt"])

    def test_parses_stream_json_result(self):
        stdout = json.dumps({"type": "result", "is_error": False, "total_cost_usd": 1.25,
                             "session_id": "s1"})
        res = ClaudeSeatRunner(cfg()).parse(stdout)
        self.assertFalse(res.is_error)
        self.assertEqual(res.cost_usd, 1.25)
        self.assertEqual(res.session_id, "s1")

    def test_parses_rate_limit_event(self):
        lines = [json.dumps({"type": "rate_limit_event", "rate_limit_info": {
            "rateLimitType": "five_hour", "status": "rejected", "resetsAt": 1790000000}}),
            json.dumps({"type": "result", "is_error": True})]
        res = ClaudeSeatRunner(cfg()).parse("\n".join(lines))
        self.assertTrue(res.rate_limited)
        self.assertEqual(res.rate_status, "rejected")
        self.assertEqual(res.rate_reset_at, 1790000000.0)


class TestOpenCodeRunner(unittest.TestCase):
    def test_argv_shape(self):
        r = OpenCodeRunner(cfg())
        argv = r.argv("PROMPT", "/repo", "opencode-go/deepseek-v4.1-flash", work_dir="/wt")
        self.assertEqual(argv[0], "opencode")
        self.assertEqual(argv[1], "run")
        self.assertIn("--format", argv)
        self.assertIn("json", argv)
        self.assertIn("--auto", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "opencode-go/deepseek-v4.1-flash")
        self.assertIn("PROMPT", argv)

    def test_parses_ndjson_with_session_id(self):
        lines = [json.dumps({"type": "step_start", "sessionID": "ses_abc"}),
                 json.dumps({"type": "text", "sessionID": "ses_abc",
                             "part": {"type": "text", "text": "done"}})]
        res = OpenCodeRunner(cfg()).parse("\n".join(lines))
        self.assertFalse(res.is_error)
        self.assertEqual(res.session_id, "ses_abc")

    def test_never_reports_rate_limited(self):
        """A metered provider has no weekly quota, so a 429-shaped body must not
        be read as the seat's rate_limit_event."""
        res = OpenCodeRunner(cfg()).parse(json.dumps({"type": "text", "text": "rate limit 429"}))
        self.assertFalse(res.rate_limited)
        self.assertIsNone(res.rate_status)

    def test_empty_output_is_an_error(self):
        self.assertTrue(OpenCodeRunner(cfg()).parse("").is_error)

    def test_cost_from_usage_document(self):
        """opencode run prints no cost, so the runner reads it back from the session."""
        r = OpenCodeRunner(cfg())
        res = r.parse(json.dumps({"type": "text", "sessionID": "ses_x"}))
        r.apply_usage({"cost": 0.42, "tokens": {"input": 10, "output": 2}})
        self.assertEqual(r.last_usage["cost_usd"], 0.42)
        self.assertEqual(r.last_usage["input"], 10)
        self.assertEqual(res.session_id, "ses_x")

    def test_missing_usage_leaves_no_cost(self):
        r = OpenCodeRunner(cfg())
        r.parse(json.dumps({"type": "text", "sessionID": "ses_x"}))
        r.apply_usage(None)
        self.assertIsNone(getattr(r, "last_usage", None))
