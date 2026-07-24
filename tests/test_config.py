import tempfile, unittest
from pathlib import Path
from autobuild.config import load_config, ConfigError

def _write(text):
    d = Path(tempfile.mkdtemp())
    (d / "config").mkdir()
    p = d / "config" / "runner.toml"
    p.write_text(text)
    return p, d

class TestConfig(unittest.TestCase):
    def test_defaults_when_empty(self):
        p, d = _write("")
        cfg = load_config(p, root=d)
        self.assertEqual(cfg.timezone, "Europe/Rome")
        self.assertEqual(cfg.max_concurrency, 3)
        self.assertFalse(cfg.weekly_reserve_enabled)
        self.assertEqual(cfg.default_model, "sonnet")
        self.assertEqual(cfg.weekend_from, "fri 19:00")   # weekend-continuous default
        self.assertEqual(cfg.weekend_to, "mon 08:00")

    def test_weekend_overrides(self):
        p, d = _write('[hours]\nweekend_from = "sat 00:00"\nweekend_to = "sun 23:59"\n')
        cfg = load_config(p, root=d)
        self.assertEqual(cfg.weekend_from, "sat 00:00")
        self.assertEqual(cfg.weekend_to, "sun 23:59")

    def test_overrides(self):
        p, d = _write('[pace]\nmax_concurrency = 2\n[build]\ndefault_model = "opus"\n')
        cfg = load_config(p, root=d)
        self.assertEqual(cfg.max_concurrency, 2)
        self.assertEqual(cfg.default_model, "opus")

    def test_invalid_model_raises(self):
        p, d = _write('[build]\ndefault_model = "gpt"\n')
        with self.assertRaises(ConfigError):
            load_config(p, root=d)
