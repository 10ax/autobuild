"""The metered-autonomy guard.

A metered backend bills per token, so an unattended loop that starts building and then
runs all night is spending real money with nobody watching. The guard's job is to refuse
that by default and say why, rather than to build and hope.

It is deliberately a *startup* check: a daemon that starts, reports healthy and then
silently refuses every build would look identical to a broken one.
"""
import unittest
from pathlib import Path

from autobuild.config import Config, load_config
from autobuild.safety import metered_guard


def cfg(**kw) -> Config:
    base = dict(root=Path("/tmp/autobuild-safety-test"))
    base.update(kw)
    return Config(**base)


class TestMeteredGuard(unittest.TestCase):
    def test_seat_is_always_allowed(self):
        """The seat has a weekly quota, not a bill — nothing to protect."""
        self.assertIsNone(metered_guard(cfg(), metered=False, allow_metered=False))

    def test_guard_derives_metered_from_the_provider_when_not_given(self):
        """The guard must not depend on a caller having stamped cfg.metered: a Config that
        says provider=opencode but was never stamped still bills per token."""
        reason = metered_guard(cfg(provider="opencode"))
        self.assertIsNotNone(reason)
        self.assertIn("opencode", reason)

    def test_derived_guard_still_allows_the_seat(self):
        self.assertIsNone(metered_guard(cfg(provider="claude")))

    def test_metered_is_refused_by_default(self):
        reason = metered_guard(cfg(), metered=True, allow_metered=False)
        self.assertIsNotNone(reason)
        self.assertIn("metered", reason.lower())
        self.assertIn("allow_metered", reason)

    def test_metered_allowed_when_explicitly_opted_in(self):
        self.assertIsNone(metered_guard(cfg(), metered=True, allow_metered=True))

    def test_reason_names_the_provider_so_the_log_is_actionable(self):
        reason = metered_guard(cfg(), metered=True, allow_metered=False, provider="opencode")
        self.assertIn("opencode", reason)


class TestConfigDefaults(unittest.TestCase):
    """The safe value has to be the one you get without thinking about it."""

    def test_default_provider_is_the_seat(self):
        c = load_config(self._toml(""))
        self.assertEqual(c.provider, "claude")
        self.assertFalse(c.allow_metered)

    def test_runner_section_is_read(self):
        c = load_config(self._toml('[runner]\nprovider = "opencode"\n'))
        self.assertEqual(c.provider, "opencode")

    def test_safety_section_is_read(self):
        c = load_config(self._toml('[safety]\nallow_metered = true\n'))
        self.assertTrue(c.allow_metered)

    def test_unknown_provider_is_a_config_error(self):
        from autobuild.config import ConfigError
        with self.assertRaises(ConfigError):
            load_config(self._toml('[runner]\nprovider = "nope"\n'))

    def test_metered_cannot_be_set_from_the_config_file(self):
        """`metered` is stamped by the daemon from the resolved runner. A hand-written key
        could disagree with the backend actually in use, which is how a metered run would
        get past the guard."""
        c = load_config(self._toml("[runner]\nmetered = true\n[safety]\nmetered = true\n"))
        self.assertFalse(c.metered)

    def _toml(self, extra: str) -> Path:
        import tempfile
        d = Path(tempfile.mkdtemp())
        p = d / "runner.toml"
        p.write_text(extra or "[hours]\n")
        return p
