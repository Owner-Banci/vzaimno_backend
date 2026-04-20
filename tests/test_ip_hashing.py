from __future__ import annotations

import os
import unittest

from app.config import get_secret
from app.pii import hash_ip, ip_hash_key


class IpHashingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_key = os.environ.get("IP_HASH_KEY")
        get_secret.cache_clear()
        ip_hash_key.cache_clear()

    def tearDown(self) -> None:
        if self._old_key is None:
            os.environ.pop("IP_HASH_KEY", None)
        else:
            os.environ["IP_HASH_KEY"] = self._old_key
        get_secret.cache_clear()
        ip_hash_key.cache_clear()

    def test_hash_ip_with_key_is_deterministic_hex(self) -> None:
        os.environ["IP_HASH_KEY"] = "test-ip-key"
        get_secret.cache_clear()
        ip_hash_key.cache_clear()

        one = hash_ip("1.2.3.4")
        two = hash_ip("1.2.3.4")
        self.assertEqual(one, two)
        self.assertIsNotNone(one)
        self.assertRegex(str(one), r"^[0-9a-f]{64}$")

    def test_hash_ip_without_key_returns_raw(self) -> None:
        os.environ.pop("IP_HASH_KEY", None)
        get_secret.cache_clear()
        ip_hash_key.cache_clear()

        with self.assertLogs("vzaimno", level="WARNING") as captured:
            value = hash_ip("1.2.3.4")

        self.assertEqual(value, "1.2.3.4")
        self.assertTrue(any("ip_hash_key_missing" in item for item in captured.output))


if __name__ == "__main__":
    unittest.main()
