from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from app.config import get_env
from app.storage import LocalFSStorage


class LocalStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self._old_uploads_dir = os.environ.get("UPLOADS_DIR")
        os.environ["UPLOADS_DIR"] = self.tmpdir.name
        get_env.cache_clear()

    def tearDown(self) -> None:
        self.tmpdir.cleanup()
        if self._old_uploads_dir is None:
            os.environ.pop("UPLOADS_DIR", None)
        else:
            os.environ["UPLOADS_DIR"] = self._old_uploads_dir
        get_env.cache_clear()

    def test_put_get_exists_delete_cycle(self) -> None:
        storage = LocalFSStorage()
        key = storage.put("ann-1/file.txt", b"hello", content_type="text/plain")

        self.assertEqual(key, "ann-1/file.txt")
        self.assertTrue(storage.exists(key))
        self.assertEqual(storage.get_url(key), "/uploads/ann-1/file.txt")

        path = Path(self.tmpdir.name) / "ann-1" / "file.txt"
        self.assertTrue(path.exists())
        self.assertEqual(path.read_bytes(), b"hello")

        storage.delete(key)
        self.assertFalse(storage.exists(key))

    def test_invalid_key_rejected(self) -> None:
        storage = LocalFSStorage()
        with self.assertRaises(ValueError):
            storage.put("../escape.txt", b"x", content_type="text/plain")


if __name__ == "__main__":
    unittest.main()
