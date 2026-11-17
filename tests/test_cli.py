import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from google.auth.exceptions import RefreshError, TransportError

from tododolist import cli
from tododolist.auth import google_session
from tododolist.clients import AuthError, BridgeError


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name) / "sync.sqlite3"
        self.env = patch.dict(os.environ, {"POLL_INTERVAL_SECONDS": "60"}, clear=False)
        self.env.start()
        os.environ.pop("HEARTBEAT_FILE", None)
        os.environ.pop("HEALTH_MAX_AGE_SECONDS", None)

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_unhealthy_before_first_pass(self):
        self.assertEqual(cli.health(self.state), 1)

    def test_healthy_after_recent_pass(self):
        cli.write_heartbeat(cli.heartbeat_file(self.state))
        self.assertEqual(cli.health(self.state), 0)

    def test_unhealthy_when_passes_stop(self):
        path = cli.heartbeat_file(self.state)
        cli.write_heartbeat(path)
        old = time.time() - 601
        os.utime(path, (old, old))
        self.assertEqual(cli.health(self.state), 1)

    def test_limit_can_be_configured(self):
        os.environ["HEALTH_MAX_AGE_SECONDS"] = "30"
        path = cli.heartbeat_file(self.state)
        cli.write_heartbeat(path)
        old = time.time() - 31
        os.utime(path, (old, old))
        self.assertEqual(cli.health(self.state), 1)


class AuthErrorTests(unittest.TestCase):
    def test_missing_token_is_an_auth_error(self):
        with self.assertRaises(AuthError):
            google_session(Path("/nonexistent/token.json"))

    def _expired_token(self, directory):
        path = Path(directory) / "token.json"
        path.write_text(
            '{"refresh_token": "r", "client_id": "c", "client_secret": "s", '
            '"token_uri": "https://oauth2.googleapis.com/token"}'
        )
        return path

    def test_revoked_refresh_is_an_auth_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._expired_token(directory)
            refresh = patch("tododolist.auth.Credentials.refresh", side_effect=RefreshError("bad"))
            with refresh, self.assertRaises(AuthError):
                google_session(path)

    def test_network_failure_is_not_an_auth_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._expired_token(directory)
            with patch("tododolist.auth.Credentials.refresh", side_effect=TransportError("down")):
                with self.assertRaises(BridgeError) as raised:
                    google_session(path)
                self.assertNotIsInstance(raised.exception, AuthError)


if __name__ == "__main__":
    unittest.main()
