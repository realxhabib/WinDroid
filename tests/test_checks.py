import unittest

from androiddesktop.checks import BLOCKING, GB, OK, WARNING, decide_disk, decide_hypervisor, decide_memory, gb, parse_accel


class Hypervisor(unittest.TestCase):
    def test_emulator_says_yes(self):
        c = decide_hypervisor(True, True, (0, "WHPX(10.0.26200) is installed and usable."))
        self.assertEqual(c.level, OK)
        self.assertIn("usable", c.detail)

    def test_emulator_verdict_wins_over_a_failed_platform_query(self):
        self.assertEqual(decide_hypervisor(True, None, (0, "usable")).level, OK)

    def test_feature_off_offers_to_turn_it_on(self):
        c = decide_hypervisor(False, None, None)
        self.assertEqual((c.level, c.fix), (BLOCKING, "enable_hypervisor"))
        self.assertEqual(decide_hypervisor(False, None, (1, "WHPX is not installed")).fix, "enable_hypervisor")

    def test_feature_on_but_not_running_needs_a_restart(self):
        c = decide_hypervisor(True, False, None)
        self.assertEqual(c.level, BLOCKING)
        self.assertIn("Restart", c.title)
        self.assertEqual(c.fix, "")

    def test_emulator_refuses_for_another_reason(self):
        c = decide_hypervisor(True, True, (2, "Another hypervisor is in the way"))
        self.assertEqual(c.level, BLOCKING)
        self.assertEqual(c.detail, "Another hypervisor is in the way")

    def test_unknown_before_install_does_not_block(self):
        self.assertEqual(decide_hypervisor(True, None, None).level, OK)

    def test_parse_accel(self):
        out = "accel:\n0\nWHPX(10.0.26200) is installed and usable.\naccel\n"
        self.assertEqual(parse_accel(out), "WHPX(10.0.26200) is installed and usable.")


class Memory(unittest.TestCase):
    def test_eight_gb_pc_reporting_a_bit_less_is_fine(self):
        self.assertEqual(decide_memory(int(7.8 * GB), 6 * GB, False).level, OK)

    def test_small_pc_warns(self):
        c = decide_memory(6 * GB, 3 * GB, False)
        self.assertEqual(c.level, WARNING)
        self.assertIn("6 GB", c.title)

    def test_low_free_memory_only_matters_before_android_starts(self):
        self.assertEqual(decide_memory(16 * GB, 2 * GB, False).level, WARNING)
        self.assertEqual(decide_memory(16 * GB, 2 * GB, True).level, OK)


class Disk(unittest.TestCase):
    def test_setup_needs_room(self):
        c = decide_disk(5 * GB, False, int(7.8 * GB), "C:")
        self.assertEqual((c.level, c.fix), (BLOCKING, "open_storage"))
        self.assertIn("7.8 GB", c.detail)
        self.assertIn("5 GB is free", c.detail)

    def test_setup_with_room(self):
        self.assertEqual(decide_disk(20 * GB, False, int(7.8 * GB), "C:").level, OK)

    def test_nearly_full_after_install_warns_but_does_not_block(self):
        self.assertEqual(decide_disk(1 * GB, True, 0, "C:").level, WARNING)
        self.assertEqual(decide_disk(3 * GB, True, 0, "C:").level, OK)

    def test_gb(self):
        self.assertEqual([gb(53 * GB), gb(7.8 * GB), gb(0.25 * GB)], ["53", "7.8", "0.2"])


if __name__ == "__main__":
    unittest.main()
