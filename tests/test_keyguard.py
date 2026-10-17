import unittest

from androiddesktop import adb

POLICY = """WINDOW MANAGER POLICY STATE (dumpsys window policy)
    mKeyguardOccluded=false mKeyguardOccludedChanged=false mPendingKeyguardOccluded=false
    KeyguardServiceDelegate
      showing={delegate}
      occluded=false
      deviceHasKeyguard=true
      KeyguardStateMonitor
        mIsShowing={monitor}
        mSimSecure=false
"""


class KeyguardShowingTest(unittest.TestCase):
    def test_unlocked(self):
        self.assertIs(adb.parse_keyguard_showing(POLICY.format(delegate="false", monitor="false")), False)

    def test_lock_screen_up(self):
        self.assertIs(adb.parse_keyguard_showing(POLICY.format(delegate="true", monitor="true")), True)

    def test_monitor_wins_over_the_delegate(self):
        self.assertIs(adb.parse_keyguard_showing(POLICY.format(delegate="true", monitor="false")), False)

    def test_delegate_alone(self):
        text = "    KeyguardServiceDelegate\n      showing=true\n"
        self.assertIs(adb.parse_keyguard_showing(text), True)

    def test_no_answer(self):
        self.assertIsNone(adb.parse_keyguard_showing("Can't find service: window"))


if __name__ == "__main__":
    unittest.main()
