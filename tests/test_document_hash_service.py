from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from app.services.document_hash_service import (
    DocumentHashError,
    verify_document_bytes,
    verify_document_bytes_sync,
)


class DocumentHashServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory(
            prefix="closing-hash-test-"
        )
        self.root = Path(self._temporary_directory.name) / "staging"
        self.root.mkdir()

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _write(self, name: str, payload: bytes) -> str:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return hashlib.sha256(payload).hexdigest()

    def test_pdf_bytes_match_expected_sha256(self) -> None:
        payload = b"%PDF-1.7 synthetic closing package"
        digest = self._write("package.pdf", payload)

        result = verify_document_bytes_sync("package.pdf", digest, root=self.root)

        self.assertTrue(result.verified)
        self.assertEqual(result.actual_sha256, hashlib.sha256(payload).hexdigest())

    def test_tiff_mismatch_returns_actual_sha256(self) -> None:
        payload = b"II*\x00synthetic-tiff"
        self._write("survey.tiff", payload)

        result = verify_document_bytes_sync(
            "survey.tiff", "0" * 64, root=self.root
        )

        self.assertFalse(result.verified)
        self.assertEqual(result.actual_sha256, hashlib.sha256(payload).hexdigest())

    def test_tif_suffix_is_supported(self) -> None:
        digest = self._write("plat.tif", b"MM\x00*synthetic-tif")

        result = verify_document_bytes_sync("plat.tif", digest, root=self.root)

        self.assertTrue(result.verified)

    def test_empty_pdf_is_hashed_not_skipped(self) -> None:
        digest = self._write("empty.pdf", b"")

        result = verify_document_bytes_sync("empty.pdf", digest, root=self.root)

        self.assertTrue(result.verified)
        self.assertEqual(
            result.actual_sha256,
            "e3b0c44298fc1c149afbf4c8996fb924"
            "27ae41e4649b934ca495991b7852b855",
        )

    def test_digest_depends_only_on_file_bytes(self) -> None:
        payload = b"%PDF-1.7 synthetic package bytes"
        digest = self._write("content-only.pdf", payload)
        unrelated_metadata = b"synthetic-metadata-never-hashed"

        result = verify_document_bytes_sync(
            "content-only.pdf", digest, root=self.root
        )

        self.assertEqual(result.actual_sha256, hashlib.sha256(payload).hexdigest())
        self.assertNotEqual(
            result.actual_sha256,
            hashlib.sha256(unrelated_metadata).hexdigest(),
        )

    async def test_async_verify_returns_same_result(self) -> None:
        digest = self._write("async.pdf", b"%PDF-async-synthetic")

        result = await verify_document_bytes("async.pdf", digest, root=self.root)

        self.assertTrue(result.verified)
        self.assertEqual(result.actual_sha256, digest)

    def test_rejects_invalid_digest(self) -> None:
        self._write("package.pdf", b"%PDF")

        with self.assertRaisesRegex(DocumentHashError, "invalid_expected_digest"):
            verify_document_bytes_sync("package.pdf", "not-a-digest", root=self.root)

    def test_rejects_unsupported_suffix(self) -> None:
        self._write("notes.txt", b"synthetic notes")

        with self.assertRaisesRegex(DocumentHashError, "unsupported_document_type"):
            verify_document_bytes_sync("notes.txt", "a" * 64, root=self.root)

    def test_rejects_missing_file(self) -> None:
        with self.assertRaisesRegex(DocumentHashError, "document_not_found"):
            verify_document_bytes_sync("missing.pdf", "a" * 64, root=self.root)

    def test_rejects_absolute_path(self) -> None:
        with self.assertRaisesRegex(DocumentHashError, "path_not_allowed"):
            verify_document_bytes_sync(
                str(self.root / "package.pdf"), "a" * 64, root=self.root
            )

    def test_rejects_parent_traversal(self) -> None:
        with self.assertRaisesRegex(DocumentHashError, "path_not_allowed"):
            verify_document_bytes_sync("../package.pdf", "a" * 64, root=self.root)

    def test_rejects_symlink_escape(self) -> None:
        outside = Path(self._temporary_directory.name) / "outside.pdf"
        outside.write_bytes(b"%PDF-outside")
        (self.root / "linked.pdf").symlink_to(outside)

        with self.assertRaisesRegex(DocumentHashError, "path_not_allowed"):
            verify_document_bytes_sync("linked.pdf", "a" * 64, root=self.root)


if __name__ == "__main__":
    unittest.main()
