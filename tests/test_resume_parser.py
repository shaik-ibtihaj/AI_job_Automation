"""Synthetic PDF fixtures only: never embed private resume content in tests."""

import hashlib
import importlib.util
import builtins
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.resume_parser import (
    EncryptedPDFError, NoExtractableTextError, PDFParseError,
    MissingDependencyError, ResumeFileError, extract_resume, extract_text_from_pdf, main,
)


def make_pdf(texts: list[str]) -> bytes:
    """Build a small, valid PDF with deterministic ASCII text and exact xref offsets."""
    kids = " ".join(f"{4 + i * 2} 0 R" for i in range(len(texts)))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {len(texts)} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for i, text in enumerate(texts):
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("ascii") if text else b""
        objects.extend([
            (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
             f"/Resources << /Font << /F1 3 0 R >> >> /Contents {5 + i * 2} 0 R >>").encode(),
            f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream",
        ])
    data = b"%PDF-1.4\n"
    offsets = [0]
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(data))
        data += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_offset = len(data)
    data += f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode()
    data += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:])
    data += (f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\n"
             f"startxref\n{xref_offset}\n%%EOF\n").encode()
    return data


class InputValidationTests(unittest.TestCase):
    def test_missing_pdf_reports_file_error(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ResumeFileError):
                extract_resume(Path(directory) / "missing.pdf")

    def test_non_pdf_is_rejected(self):
        with self.assertRaises(ResumeFileError):
            extract_resume("resume.txt")

    def test_directory_is_rejected(self):
        with tempfile.TemporaryDirectory(suffix=".pdf") as directory:
            with self.assertRaises(ResumeFileError):
                extract_resume(directory)

    def test_missing_dependency_is_reported_without_inventing_text(self):
        original_import = builtins.__import__

        def without_pypdf(name, *args, **kwargs):
            if name == "pypdf":
                raise ImportError("Synthetic unavailable dependency")
            return original_import(name, *args, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "synthetic.pdf"
            pdf.write_bytes(make_pdf(["Synthetic candidate"]))
            with patch("builtins.__import__", side_effect=without_pypdf):
                with self.assertRaises(MissingDependencyError):
                    extract_resume(pdf)

    def test_cli_failure_has_nonzero_status_and_creates_no_output(self):
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "missing.pdf"
            output = Path(directory) / "output.json"
            errors = StringIO()
            with patch("sys.argv", ["resume_parser", str(pdf), "--output", str(output)]), redirect_stderr(errors):
                self.assertEqual(main(), 1)
            self.assertIn("Extraction failed", errors.getvalue())
            self.assertFalse(output.exists())


@unittest.skipUnless(importlib.util.find_spec("pypdf"), "pypdf is not installed; PDF extraction tests cannot run")
class PDFExtractionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.pdf = Path(self.temp.name) / "synthetic.pdf"

    def test_extracts_real_text_and_source_hash(self):
        content = make_pdf(["Synthetic candidate", "Python SQL"])
        self.pdf.write_bytes(content)
        result = extract_resume(self.pdf)
        self.assertEqual(result.text, "Synthetic candidate\n\nPython SQL")
        self.assertEqual([p.page_number for p in result.pages], [1, 2])
        self.assertEqual(result.sha256, hashlib.sha256(content).hexdigest())
        self.assertEqual(result.warnings, ())
        self.assertEqual(extract_text_from_pdf(self.pdf), result.text)

    def test_textless_pdf_reports_possible_scan(self):
        self.pdf.write_bytes(make_pdf([""]))
        with self.assertRaisesRegex(NoExtractableTextError, "scanned or blank"):
            extract_resume(self.pdf)

    def test_mixed_pages_preserve_page_numbers_and_warn(self):
        self.pdf.write_bytes(make_pdf(["", "Synthetic qualification"]))
        result = extract_resume(self.pdf)
        self.assertEqual(result.pages[1].page_number, 2)
        self.assertIn("Page 1", result.warnings[0])
        self.assertEqual(result.text, "Synthetic qualification")

    def test_corrupt_pdf_reports_error(self):
        self.pdf.write_bytes(b"%PDF-1.4\nnot a valid PDF")
        with self.assertRaises(PDFParseError):
            extract_resume(self.pdf)

    def test_empty_file_reports_error(self):
        self.pdf.write_bytes(b"")
        with self.assertRaises(PDFParseError):
            extract_resume(self.pdf)

    def test_encrypted_pdf_needs_correct_password(self):
        from pypdf import PdfReader, PdfWriter
        from io import BytesIO
        writer = PdfWriter()
        writer.append_pages_from_reader(PdfReader(BytesIO(make_pdf(["Private synthetic text"]))))
        writer.encrypt("test-password")
        with self.pdf.open("wb") as stream:
            writer.write(stream)
        with self.assertRaises(EncryptedPDFError):
            extract_resume(self.pdf)
        with self.assertRaises(EncryptedPDFError):
            extract_resume(self.pdf, password="wrong")
        self.assertEqual(extract_resume(self.pdf, password="test-password").text, "Private synthetic text")


if __name__ == "__main__":
    unittest.main()
