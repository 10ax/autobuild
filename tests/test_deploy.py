"""Which systemd units belong to which provider.

The rate-limit oracle and the usage feed exist for one reason: to feed the governor a live
reading of the subscription's weekly quota. On a metered provider there is no such quota, so
running them would refresh a number nothing reads — while the usage-feed unit keeps an
interactive agent session alive around the clock. This decides what to install.
"""
import unittest

from autobuild.deploy import seat_units, units_for_provider, unit_files

# Units that only make sense against the seat's weekly quota.
SEAT_METERING = ("usage-feed.service", "ratelimit-oracle.service", "ratelimit-oracle.timer")
ALWAYS = ("autobuild.service", "autobuild-web.service")


class TestDeployUnits(unittest.TestCase):
    def test_seat_units_are_the_metering_ones(self):
        self.assertEqual(set(seat_units()), set(SEAT_METERING))

    def test_seat_provider_installs_the_metering_units(self):
        units = units_for_provider("claude")
        for u in SEAT_METERING:
            self.assertIn(u, units)
        for u in ALWAYS:
            self.assertIn(u, units)

    def test_metered_provider_omits_the_metering_units(self):
        units = units_for_provider("opencode")
        for u in SEAT_METERING:
            self.assertNotIn(u, units)
        for u in ALWAYS:
            self.assertIn(u, units)

    def test_every_returned_unit_exists_on_disk(self):
        for provider in ("claude", "opencode"):
            for u in units_for_provider(provider):
                self.assertTrue(unit_files(u).exists(), f"{u} missing for {provider}")

    def test_unknown_provider_is_rejected(self):
        with self.assertRaises(ValueError):
            units_for_provider("nope")
