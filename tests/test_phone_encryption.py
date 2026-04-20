from __future__ import annotations

import os
import unittest

from app.config import get_secret
from app.pii import decrypt_phone_expr, hash_phone, phone_hash_key, pii_encryption_key


class PhoneEncryptionHelpersTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_phone_hash_key = os.environ.get("PHONE_HASH_KEY")
        self._old_pii_key = os.environ.get("PII_ENCRYPTION_KEY")
        get_secret.cache_clear()
        phone_hash_key.cache_clear()
        pii_encryption_key.cache_clear()

    def tearDown(self) -> None:
        if self._old_phone_hash_key is None:
            os.environ.pop("PHONE_HASH_KEY", None)
        else:
            os.environ["PHONE_HASH_KEY"] = self._old_phone_hash_key

        if self._old_pii_key is None:
            os.environ.pop("PII_ENCRYPTION_KEY", None)
        else:
            os.environ["PII_ENCRYPTION_KEY"] = self._old_pii_key

        get_secret.cache_clear()
        phone_hash_key.cache_clear()
        pii_encryption_key.cache_clear()

    def test_hash_phone_with_key(self) -> None:
        os.environ["PHONE_HASH_KEY"] = "phone-hash-test-key"
        get_secret.cache_clear()
        phone_hash_key.cache_clear()

        hashed = hash_phone("+79161234567")
        self.assertIsNotNone(hashed)
        self.assertRegex(str(hashed), r"^[0-9a-f]{64}$")

    def test_hash_phone_without_key_returns_none(self) -> None:
        os.environ.pop("PHONE_HASH_KEY", None)
        get_secret.cache_clear()
        phone_hash_key.cache_clear()

        with self.assertLogs("vzaimno", level="WARNING") as captured:
            hashed = hash_phone("+79161234567")

        self.assertIsNone(hashed)
        self.assertTrue(any("phone_hash_key_missing" in item for item in captured.output))

    def test_decrypt_phone_expr_requires_key_for_pgp(self) -> None:
        os.environ["PII_ENCRYPTION_KEY"] = "pii-test-key"
        get_secret.cache_clear()
        pii_encryption_key.cache_clear()

        frag, params = decrypt_phone_expr("u.phone_enc")
        self.assertIn("pgp_sym_decrypt", frag)
        self.assertEqual(params, ("pii-test-key",))


if __name__ == "__main__":
    unittest.main()
