"""v2.2.9 — saving a manual FTP must be visible immediately.

Regression: /api/profile/update-ftp wrote athlete.json + the ftp_test_history
graph but left the running process's config.ATHLETE_FTP_W at its startup value.
The topbar + settings field read config.ATHLETE_FTP_W, so a saved FTP (e.g. 258)
appeared to "revert" to the old cached value (e.g. an eFTP of 243) until the app
was restarted — while the graph correctly showed the new value.

What changed, and why this file was rewritten: config no longer caches. Every
proxied name resolves through ProfileManager on each read (config.__getattr__,
config.py:118), so writing the profile IS the refresh. The endpoint's mirror
(`config.ATHLETE_FTP_W = new_ftp`) therefore stopped being a fix and became a
bug of its own: a real module attribute shadows __getattr__ permanently, so
after one save the profile stopped owning the FTP for the life of the process
and across profile switches (tests/test_one_ftp_owner.py holds that rule).

These tests kept the old behaviour alive in two ways, so they are expressed
through the proxy now: they ASSIGNED config.ATHLETE_FTP_W to set up the stale
value, which created the shadow the endpoint was then credited with fixing,
and their tearDown assigned it back, leaving a shadow behind for every later
test in the same worker.
"""
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

import app as app_module
import config


class TestFtpConfigRefresh(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app_module.app)
        # No assignment: a leftover shadow from anywhere else would make these
        # tests measure a stale module attribute instead of the profile.
        self._shadow = vars(config).pop("ATHLETE_FTP_W", None)

    def tearDown(self):
        vars(config).pop("ATHLETE_FTP_W", None)
        if self._shadow is not None:           # restore whatever we displaced
            setattr(config, "ATHLETE_FTP_W", self._shadow)

    def _pm(self, ftp: int) -> MagicMock:
        pm = MagicMock()
        pm.ftp = ftp                            # what the proxy will resolve to
        pm.ftp_source = "manual"
        pm.ftp_test_history = []
        return pm

    def test_update_ftp_is_visible_to_readers_immediately(self):
        """The rider saves 258; the topbar and settings field must say 258 on
        the next read, not after a restart."""
        fake_pm = self._pm(258)
        with patch("profile_manager.ProfileManager.get", return_value=fake_pm):
            r = self.client.post("/api/profile/update-ftp",
                                 json={"ftp": 258, "method": "manual", "applied": True})
            self.assertEqual(r.status_code, 200, r.text)
            fake_pm.update_ftp.assert_called_once()
            self.assertEqual(
                config.ATHLETE_FTP_W, 258,
                "readers do not see the saved FTP: the profile was written but "
                "config did not resolve through it")

    def test_unapplied_ftp_does_not_change_the_active_ftp(self):
        """applied=False records the test in the graph and leaves the FTP."""
        fake_pm = self._pm(243)
        with patch("profile_manager.ProfileManager.get", return_value=fake_pm):
            r = self.client.post("/api/profile/update-ftp",
                                 json={"ftp": 300, "method": "ramp", "applied": False})
            self.assertEqual(r.status_code, 200, r.text)
            fake_pm.update_ftp.assert_not_called()
            self.assertEqual(config.ATHLETE_FTP_W, 243)


if __name__ == "__main__":
    unittest.main()
