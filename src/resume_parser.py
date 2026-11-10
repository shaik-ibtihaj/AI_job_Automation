"""Local PDF text extraction. No OCR, network calls, or qualification inference."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
from io import BytesIO
import json
from pathlib import Path
import sys


class ResumeExtractionError(Exception):
    """A resume could not be extracted safely."""


class MissingDependencyError(ResumeExtractionError):
    """The local environment needs pypdf."""


class ResumeFileError(ResumeExtractionError):
    """The input file is missing or unreadable."""


class PDFParseError(ResumeExtractionError):
    """The PDF is malformed or its contents cannot be decoded."""


class EncryptedPDFError(ResumeExtractionError):
    """A supplied password is needed or is incorrect."""


class NoExtractableTextError(ResumeExtractionError):
    """The PDF has no usable text layer; it may be scanned."""


@dataclass(frozen=True)
class PageText:
    page_number: int
    text: str


@dataclass(frozen=True)
class ResumeExtraction:
    source_file: str
    sha256: str
    pages: tuple[PageText, ...]
    warnings: tuple[str, ...]

    @property
    def text(self) -> str:
        return "\n\n".join(page.text for page in self.pages if page.text)


def extract_resume(pdf_path: str | Path, *, password: str | None = None) -> ResumeExtraction:
    """Extract every page locally, failing rather than silently losing a broken page.

    Empty pages in a mixed PDF produce warnings. An entirely textless PDF fails.
    Extracted text is source evidence, not independent verification of its claims.
    """
    path = Path(pdf_path)
    if path.suffix.lower() != ".pdf":
        raise ResumeFileError("Expected a .pdf resume file.")
    try:
        contents = path.read_bytes()
    except OSError as exc:
        raise ResumeFileError(f"Cannot read PDF '{path}': {exc}") from exc
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise MissingDependencyError(
            "pypdf is unavailable. Install requirements.txt using the project's .venv Python."
        ) from exc

    pages: list[PageText] = []
    warnings: list[str] = []
    try:
        reader = PdfReader(BytesIO(contents), strict=True)
        if reader.is_encrypted:
            if not reader.decrypt(password if password is not None else ""):
                raise EncryptedPDFError("The PDF is encrypted; provide the correct password locally.")
        for number, page in enumerate(reader.pages, start=1):
            text = (page.extract_text() or "").strip()
            pages.append(PageText(number, text))
            if not text:
                warnings.append(
                    f"Page {number} has no extractable text. It may be blank or scanned; review manually."
                )
    except ResumeExtractionError:
        raise
    except Exception as exc:
        # Third-party parsers can raise many errors for malformed PDF structures.
        # Keep their cause while presenting one stable exception to callers.
        raise PDFParseError(f"PDF parsing failed ({type(exc).__name__}): {exc}") from exc
    if not any(any(char.isalnum() for char in page.text) for page in pages):
        raise NoExtractableTextError(
            "No usable text was extracted. The PDF may be scanned or blank. "
            "Provide a text-based PDF or manually verify a locally OCR-processed copy; no content was inferred."
        )
    return ResumeExtraction(str(path), hashlib.sha256(contents).hexdigest(), tuple(pages), tuple(warnings))


def extract_text_from_pdf(pdf_path: str | Path, *, password: str | None = None) -> str:
    """Convenience function for callers that only need text."""
    return extract_resume(pdf_path, password=password).text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", nargs="?", default="resumes/master_resume.pdf")
    parser.add_argument("--output", type=Path, default=Path("data/resume_extraction.json"))
    args = parser.parse_args()
    try:
        result = extract_resume(args.pdf)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(asdict(result), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except (ResumeExtractionError, OSError) as exc:
        print(f"Extraction failed: {exc}", file=sys.stderr)
        return 1
    print(f"Extracted {len(result.pages)} page(s) to {args.output}. Review before using as profile evidence.")
    for warning in result.warnings:
        print(f"WARNING: {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
