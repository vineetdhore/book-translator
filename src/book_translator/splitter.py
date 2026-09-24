"""Split validated source PDFs into page-level PDFs and create job manifests."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pymupdf

from book_translator.analyzer import validate_pdf_path


@dataclass(frozen=True)
class SplitResult:
    """Locations and counts produced by a successful split operation."""

    source_path: Path
    output_dir: Path
    manifest_path: Path
    page_count: int


def split_pdf(
    pdf_path: Path,
    *,
    output_dir: Path | None = None,
    source_language: str | None = None,
    overwrite: bool = False,
) -> SplitResult:
    """Create an individual PDF for each page and a page-status job manifest."""
    source_path = validate_pdf_path(pdf_path)
    destination = output_dir or Path("pages") / "original" / safe_directory_name(source_path.stem)
    destination = destination.expanduser()
    if source_language:
        source_language = validate_language_code(source_language)

    try:
        source_document = pymupdf.open(source_path)
    except (pymupdf.FileDataError, RuntimeError) as error:
        raise ValueError(f"'{source_path.name}' is not a readable PDF: {error}") from error

    with source_document:
        if source_document.needs_pass:
            raise ValueError("The PDF is password-protected and cannot be split without a password.")
        if source_document.page_count == 0:
            raise ValueError("The PDF contains no pages.")

        destination.mkdir(parents=True, exist_ok=True)
        existing_pages = list(destination.glob("page_*.pdf"))
        manifest_path = destination / "job_manifest.json"
        if (existing_pages or manifest_path.exists()) and not overwrite:
            raise ValueError(
                f"Output already exists in '{destination}'. Use --overwrite to replace it, "
                "or choose --output-dir."
            )

        page_records: list[dict[str, object]] = []
        for page_index in range(source_document.page_count):
            page_number = page_index + 1
            page_filename = f"page_{page_number:04d}.pdf"
            page_path = destination / page_filename
            write_single_page_pdf(source_document, page_index, page_path)
            page_records.append(
                {
                    "page_number": page_number,
                    "original_pdf": page_filename,
                    "extraction_method": None,
                    "translation_status": "not_started",
                    "error": None,
                }
            )

    manifest = {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "source_pdf": str(source_path.resolve()),
        "source_file_size_bytes": source_path.stat().st_size,
        "source_language": source_language,
        "page_count": len(page_records),
        "page_directory": str(destination.resolve()),
        "pages": page_records,
    }
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return SplitResult(
        source_path=source_path,
        output_dir=destination,
        manifest_path=manifest_path,
        page_count=len(page_records),
    )


def write_single_page_pdf(
    source_document: pymupdf.Document, page_index: int, page_path: Path
) -> None:
    """Write one source page to its own PDF through an atomic file replacement."""
    temporary_path = page_path.with_suffix(".partial.pdf")
    one_page_document = pymupdf.open()
    try:
        one_page_document.insert_pdf(source_document, from_page=page_index, to_page=page_index)
        one_page_document.save(temporary_path)
    finally:
        one_page_document.close()
    temporary_path.replace(page_path)


def safe_directory_name(name: str) -> str:
    """Return a Windows-safe but readable output directory name."""
    sanitized = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(". ")
    return sanitized or "book"


def validate_language_code(value: str) -> str:
    """Validate a source-language code before persisting it in the manifest."""
    code = value.strip().lower()
    if not re.fullmatch(r"[a-z]{2,3}", code):
        raise ValueError("--source-language must be a two- or three-letter ISO language code.")
    return code
