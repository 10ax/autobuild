import tempfile, unittest
from pathlib import Path
from autobuild.ledger import (append_ledger, read_ledger, GovernorState,
                              load_governor_state, save_governor_state)

class TestLedger(unittest.TestCase):
    def test_ledger_roundtrip(self):
        p = Path(tempfile.mkdtemp()) / "ledger.jsonl"
        append_ledger(p, {"slug": "a", "cost_usd": 1.0})
        append_ledger(p, {"slug": "b", "cost_usd": 2.0})
        rows = read_ledger(p)
        self.assertEqual([r["slug"] for r in rows], ["a", "b"])

    def test_governor_state_defaults_and_roundtrip(self):
        p = Path(tempfile.mkdtemp()) / "governor.json"
        st = load_governor_state(p)                       # missing file -> defaults
        self.assertEqual(st.weekly_spend_usd, 0.0)
        self.assertIsNone(st.window_start)
        st.learned_ceiling_usd = 12.5
        save_governor_state(p, st)
        self.assertEqual(load_governor_state(p).learned_ceiling_usd, 12.5)

    def test_load_governor_state_tolerates_corruption(self):
        p = Path(tempfile.mkdtemp()) / "governor.json"
        p.write_text('{ this is not valid json')
        st = load_governor_state(p)          # must NOT raise
        self.assertEqual(st.weekly_spend_usd, 0.0)

    def test_read_ledger_skips_torn_line(self):
        p = Path(tempfile.mkdtemp()) / "ledger.jsonl"
        append_ledger(p, {"slug": "a"})
        with p.open("a") as f:
            f.write('{ torn')             # simulate crash mid-append
        rows = read_ledger(p)              # must NOT raise
        self.assertEqual([r["slug"] for r in rows], ["a"])
