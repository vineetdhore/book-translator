"""Merge page-level English PDFs into a translated book."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pymupdf

from book_translator.extractor import load_manifest


@dataclass(frozen=True)
class MergeResult:
    """Summary of a merged translated book."""

    output_path: Path
    source_page_count: int
    translated_source_pages: int
    output_page_count: int
    overflow_pages: int


def merge_translated_book(
    pages_dir: Path,
    *,
    translated_pdf_dir: Path | None = None,
    output_path: Path | None = None,
    overwrite: bool = False,
) -> MergeResult:
    """Merge all translated page PDFs in source order and validate page coverage."""
    source_dir = validate_pages_directory(pages_dir)
    manifest = load_manifest(source_dir / "job_manifest.json")
    records = sorted(
        (record for record in manifest["pages"] if isinstance(record, dict)),
        key=lambda record: record.get("page_number", 0),
    )
    source_page_count = manifest.get("page_count")
    if not isinstance(source_page_count, int) or source_page_count < 1:
        raise ValueError("Job manifest does not contain a valid source page count.")
    if len(records) != source_page_count:
        raise ValueError(
            f"Manifest page count ({len(records)}) does not match source page count ({source_page_count})."
        )

    pdf_dir = (translated_pdf_dir or Path("pages") / "translated" / source_dir.name).expanduser()
    output = (output_path or Path("output") / f"{source_dir.name}_translated_english.pdf").expanduser()
    if output.exists() and not overwrite:
        raise ValueError(f"Output already exists: {output}. Use --overwrite to replace it.")
    output.parent.mkdir(parents=True, exist_ok=True)

    page_paths: list[Path] = []
    overflow_pages = 0
    for expected_page_number, record in enumerate(records, start=1):
        page_number = record.get("page_number")
        if page_number != expected_page_number:
            raise ValueError("Manifest pages must contain consecutive page numbers in source order.")
        filename = record.get("translated_pdf") or f"page_{page_number:04d}_en.pdf"
        page_path = Path(filename)
        if not page_path.is_absolute():
            page_path = pdf_dir / page_path
        if not page_path.is_file():
            raise ValueError(f"Translated PDF missing for source page {page_number}: {page_path}")
        try:
            with pymupdf.open(page_path) as page_document:
                if page_document.page_count == 0:
                    raise ValueError(f"Translated PDF is empty for source page {page_number}: {page_path}")
                if page_document.page_count > 1:
                    overflow_pages += 1
        except (pymupdf.FileDataError, RuntimeError) as error:
            raise ValueError(f"Translated PDF is invalid for source page {page_number}: {page_path}") from error
        page_paths.append(page_path)

    source_language = manifest.get("source_language") or "unknown"
    temporary_path = output.with_suffix(".partial.pdf")
    merged = pymupdf.open()
    try:
        for page_path in page_paths:
            with pymupdf.open(page_path) as page_document:
                merged.insert_pdf(page_document)
        merged.set_metadata(
            {
                "format": "PDF 1.7",
                "title": f"{source_dir.name} - English Translation",
                "author": "Book Translator",
                "subject": "English translation",
                "keywords": f"translation, English, source-language:{source_language}",
                "creator": "Book Translator",
                "producer": "PyMuPDF",
            }
        )
        merged.save(temporary_path)
        output_page_count = merged.page_count
    finally:
        merged.close()
    temporary_path.replace(output)

    return MergeResult(
        output_path=output,
        source_page_count=source_page_count,
        translated_source_pages=len(page_paths),
        output_page_count=output_page_count,
        overflow_pages=overflow_pages,
    )


def validate_pages_directory(pages_dir: Path) -> Path:
    """Validate the split-page directory and its manifest."""
    directory = pages_dir.expanduser()
    if not directory.is_dir() or not (directory / "job_manifest.json").is_file():
        raise ValueError("A split-page directory containing job_manifest.json is required.")
    return directory