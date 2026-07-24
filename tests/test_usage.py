import json, tempfile, unittest
from datetime import datetime, timezone
from pathlib import Path
from autobuild.usage import read_signal, write_oracle, UsageSignal

NOW = datetime(2026, 7, 24, 2, 0, tzinfo=timezone.utc)
NOW_TS = NOW.timestamp()


def _write(p: Path, obj):
    p.write_text(json.dumps(obj))


class TestUsage(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.oracle = self.d / "usage-oracle.json"
        self.snap = self.d / "usage-snapshot.json"

    def test_missing_files_empty_signal(self):
        s = read_signal(self.oracle, self.snap, NOW)
        self.assertEqual(s, UsageSignal())

    def test_fresh_oracle_status_and_reset(self):
        _write(self.oracle, {"status": "allowed", "reset_at": 1784893800,
                             "rate_limit_type": "five_hour", "written_at": NOW_TS - 60})
        s = read_signal(self.oracle, self.snap, NOW)
        self.assertEqual(s.status, "allowed")
        self.assertEqual(s.reset_at, 1784893800.0)

    def test_stale_oracle_ignored(self):
        _write(self.oracle, {"status": "rejected", "reset_at": 1784893800,
                             "written_at": NOW_TS - 4000})     # older than 900s
        s = read_signal(self.oracle, self.snap, NOW)
        self.assertIsNone(s.status)
        self.assertIsNone(s.reset_at)

    def test_corrupt_file_yields_empty(self):
        self.oracle.write_text("{ this is not json")
        self.assertEqual(read_signal(self.oracle, self.snap, NOW), UsageSignal())

    def test_snapshot_percentages_and_oracle_reset_preferred(self):
        _write(self.oracle, {"status": "allowed", "reset_at": 111_1111111, "written_at": NOW_TS - 30})
        _write(self.snap, {"five_hour": {"used_percentage": 23.5, "reset_at": 999_9999999},
                           "seven_day": {"used_percentage": 41.2, "reset_at": 222_2222222},
                           "written_at": NOW_TS - 30})
        s = read_signal(self.oracle, self.snap, NOW)
        self.assertEqual(s.used_pct_5h, 23.5)
        self.assertEqual(s.used_pct_7d, 41.2)
        self.assertEqual(s.reset_at, 1111111111.0)      # oracle reset wins over snapshot's
        self.assertEqual(s.reset_at_7d, 2222222222.0)

    def test_snapshot_reset_fills_when_oracle_absent(self):
        _write(self.snap, {"five_hour": {"used_percentage": 10.0, "reset_at": 1784893800},
                           "written_at": NOW_TS - 30})
        s = read_signal(self.oracle, self.snap, NOW)
        self.assertEqual(s.used_pct_5h, 10.0)
        self.assertEqual(s.reset_at, 1784893800.0)      # filled from snapshot when no oracle

    def test_write_oracle_roundtrips(self):
        write_oracle(self.oracle, "rejected", 1784900000, "five_hour", NOW)
        s = read_signal(self.oracle, self.snap, NOW)
        self.assertEqual(s.status, "rejected")
        self.assertEqual(s.reset_at, 1784900000.0)
