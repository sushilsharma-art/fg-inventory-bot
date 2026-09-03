from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from cloud_runner import _decrypt_with_passcodes, _login_passcode
from crypto_payload import encrypt_payload


class CloudRunnerPasscodeTests(unittest.TestCase):
    def test_login_passcode_falls_back_to_private_key(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_login_passcode("private-key"), "private-key")

    def test_login_passcode_uses_separate_shareable_key(self) -> None:
        with patch.dict(
            os.environ,
            {"FG_BOT_LOGIN_PASSCODE": "shared-key"},
            clear=True,
        ):
            self.assertEqual(_login_passcode("private-key"), "shared-key")

    def test_previous_payload_accepts_rotation_fallback(self) -> None:
        payload = {"dateKey": "2026-09-03", "skus": []}
        envelope = encrypt_payload(payload, "private-key")
        restored = _decrypt_with_passcodes(
            envelope,
            ["new-login-key", "private-key"],
        )
        self.assertEqual(restored, payload)


if __name__ == "__main__":
    unittest.main()
