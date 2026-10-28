import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from autobuild.backlog import scan_backlog
from autobuild.build import BuildResult
from autobuild.config import Config, ConfigError, load_config
from autobuild.daemon import run_once
from autobuild.ledger import GovernorState, read_ledger
from autobuild.review import opposite_tier, parse_verdict, review_build, review_tier

TZ = ZoneInfo("Europe/Rome")
NIGHT = datetime(2026, 7, 23, 23, tzinfo=TZ)
BRIEF = '''+++
spec_version = "1.0"
slug = "alpha"
title = "alpha"
tier = "script"
priority = 5
status = "pending"
+++
## Intent
x
## Acceptance Criteria
A1. x
'''


def _root():
    d = Path(tempfile.mkdtemp())
    (d / "backlog").mkdir()
    (d / "projects").mkdir()
    (d / "state").mkdir()
    (d / "backlog" / "a.md").write_text(BRIEF)
    return d


def _sig(root):
    return {"oracle_path": root / "state" / "usage-oracle.json",
            "snapshot_path": root / "state" / "usage-snapshot.json"}


class TestVerdict(unittest.TestCase):
    def test_opposite_tier(self):
        self.assertEqual(opposite_tier("opus"), "sonnet")
        self.assertEqual(opposite_tier("sonnet"), "opus")

    def test_the_checker_is_cheap_by_default(self):
        cfg = Config(root=Path("."))
        self.assertEqual(review_tier(cfg, "opus"), "sonnet")   # cheap tier, either way
        self.assertEqual(review_tier(cfg, "sonnet"), "sonnet")

    def test_auto_picks_a_different_model_and_a_pin_obeys(self):
        auto = Config(root=Path("."), reviewer_tier="auto")
        self.assertEqual(review_tier(auto, "sonnet"), "opus")
        pinned = Config(root=Path("."), reviewer_tier="opus")
        self.assertEqual(review_tier(pinned, "sonnet"), "opus")

    def test_accept(self):
        self.assertEqual(parse_verdict('{"verdict":"accept","reasons":[]}'),
                         ("accept", []))

    def test_revise_keeps_the_reasons(self):
        verdict, reasons = parse_verdict(
            'text\n```json\n{"verdict":"revise","reasons":["no test for A2","scope creep"]}\n```')
        self.assertEqual(verdict, "revise")
        self.assertEqual(reasons, ["no test for A2", "scope creep"])

    def test_last_verdict_wins(self):
        verdict, _ = parse_verdict('{"verdict":"revise","reasons":["x"]}\n'
                                   '{"verdict":"accept","reasons":[]}')
        self.assertEqual(verdict, "accept")

    def test_garbage_and_unknown_verdicts_are_no_verdict(self):
        self.assertEqual(parse_verdict("looks good to me"), (None, []))
        self.assertEqual(parse_verdict('{"verdict":"maybe"}'), (None, []))

    def test_missing_text_is_no_verdict(self):
        self.assertEqual(parse_verdict(None), (None, []))


class TestReviewBuild(unittest.TestCase):
    def test_reads_the_verdict_out_of_the_agent_output(self):
        seen = {}

        def builder(brief, repo, model, pace, cfg, **kw):
            seen["model"] = model
            seen["mode"] = kw.get("mode")
            seen["timeout"] = kw.get("timeout_min")
            return BuildResult(is_error=False, cost_usd=0.2,
                               raw={"output": 'long text\n{"verdict":"accept","reasons":[]}'})

        cfg = Config(root=Path("."), reviewer_timeout_min=15)
        review = review_build(Path("b.md"), Path("."), "opus", None, cfg, builder=builder)
        self.assertTrue(review.accepted)
        self.assertEqual(review.tier, "sonnet")
        self.assertEqual(seen, {"model": "sonnet", "mode": "review", "timeout": 15})
        self.assertEqual(review.result.cost_usd, 0.2)

    def test_an_errored_run_is_never_an_accept(self):
        def builder(*a, **kw):
            return BuildResult(is_error=True, cost_usd=0.0,
                               raw={"output": '{"verdict":"accept","reasons":[]}'})

        review = review_build(Path("b.md"), Path("."), "sonnet",
                              None, Config(root=Path(".")), builder=builder)
        self.assertFalse(review.accepted)
        self.assertIsNone(review.verdict)

    def test_a_refusal_says_why(self):
        def builder(*a, **kw):
            return BuildResult(is_error=False, cost_usd=0.1,
                               raw={"output": '{"verdict":"revise","reasons":["A2 unproven"]}'})

        review = review_build(Path("b.md"), Path("."), "sonnet",
                              None, Config(root=Path(".")), builder=builder)
        self.assertFalse(review.accepted)
        self.assertEqual(review.reason, "A2 unproven")


class TestDaemonGate(unittest.TestCase):
    def _run(self, root, cfg, builder):
        notes = []
        res = run_once(cfg, NIGHT, GovernorState(), root / "state",
                       builder=builder, verifier=lambda *a, **k: True,
                       notifier=lambda cfg, event, runner=None, **k: notes.append((event, k)),
                       **_sig(root))
        return res, notes

    def test_a_green_build_is_held_back_by_a_revise(self):
        root = _root()
        res, notes = self._run(root, Config(root=root, max_concurrency=1, weekly_guard_enabled=False,
                                            reviewer_enabled=True),
                               builder=lambda *a, **k: BuildResult(
                                   is_error=False, cost_usd=0.5,
                                   raw={"output": '{"verdict":"revise","reasons":["A1 unproven"]}'}))
        self.assertEqual(res["action"], "built")
        item = next(i for i in scan_backlog(root / "backlog") if i.meta["slug"] == "alpha")
        self.assertEqual(item.meta["status"], "needs-review")
        ledger = read_ledger(root / "state" / "ledger.jsonl")
        self.assertEqual(ledger[0]["review"], "revise")
        self.assertEqual(notes[-1][0], "needs-review")
        self.assertEqual(notes[-1][1]["reason"], "A1 unproven")

    def test_an_accept_is_what_makes_it_done(self):
        root = _root()
        res, notes = self._run(root, Config(root=root, max_concurrency=1, weekly_guard_enabled=False,
                                            reviewer_enabled=True),
                               builder=lambda *a, **k: BuildResult(
                                   is_error=False, cost_usd=0.5,
                                   raw={"output": '{"verdict":"accept","reasons":[]}'}))
        item = next(i for i in scan_backlog(root / "backlog") if i.meta["slug"] == "alpha")
        self.assertEqual(item.meta["status"], "done")
        self.assertEqual(read_ledger(root / "state" / "ledger.jsonl")[0]["review"], "accept")
        self.assertEqual(notes[-1][0], "done")

    def test_a_silent_checker_does_not_promote(self):
        root = _root()
        self._run(root, Config(root=root, max_concurrency=1, weekly_guard_enabled=False,
                               reviewer_enabled=True),
                  builder=lambda *a, **k: BuildResult(is_error=False, cost_usd=0.5,
                                                      raw={"output": "looks good"}))
        item = next(i for i in scan_backlog(root / "backlog") if i.meta["slug"] == "alpha")
        self.assertEqual(item.meta["status"], "needs-review")

    def test_a_red_suite_never_reaches_the_checker(self):
        root = _root()
        calls = []

        def builder(*a, **k):
            calls.append(k.get("mode"))
            return BuildResult(is_error=False, cost_usd=0.5)

        notes = []
        run_once(Config(root=root, max_concurrency=1, weekly_guard_enabled=False,
                 reviewer_enabled=True), NIGHT,
                 GovernorState(), root / "state", builder=builder,
                 verifier=lambda *a, **k: False,
                 notifier=lambda cfg, event, runner=None, **k: notes.append(event),
                 **_sig(root))
        self.assertEqual(calls, [None])          # one build, no review run
        item = next(i for i in scan_backlog(root / "backlog") if i.meta["slug"] == "alpha")
        self.assertEqual(item.meta["status"], "needs-review")


class TestConfig(unittest.TestCase):
    def test_reviewer_defaults_to_off(self):
        self.assertFalse(Config(root=Path(".")).reviewer_enabled)

    def test_the_toml_section_is_read(self):
        d = Path(tempfile.mkdtemp())
        (d / "cfg").mkdir()
        (d / "cfg" / "runner.toml").write_text(
            '[reviewer]\nenabled = true\ntier = "opus"\ntimeout_min = 9\n')
        cfg = load_config(d / "cfg" / "runner.toml", root=d)
        self.assertTrue(cfg.reviewer_enabled)
        self.assertEqual((cfg.reviewer_tier, cfg.reviewer_timeout_min), ("opus", 9))

    def test_a_bad_tier_is_a_config_error(self):
        d = Path(tempfile.mkdtemp())
        (d / "runner.toml").write_text('[reviewer]\nenabled = true\ntier = "cheap"\n')
        with self.assertRaises(ConfigError):
            load_config(d / "runner.toml", root=d)
