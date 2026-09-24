"""Validate translated text and PDFs and produce a reusable review report."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pymupdf

from book_translator.extractor import load_manifest, page_records_by_number, write_manifest

DEFAULT_MIN_TRANSLATED_CHARACTERS = 100
DEFAULT_MIN_LATIN_LETTER_RATIO = 0.25


@dataclass(frozen=True)
class QualityCheckResult:
    """Summary and report location for one quality-check run."""

    report_path: Path
    checked_pages: int
    passed_pages: int
    failed_pages: int
    blank_pages: int
    short_pages: int
    overflow_pages: int
    missing_output_pages: int
    non_english_pages: int


def check_translation_quality(
    pages_dir: Path,
    *,
    translated_text_dir: Path | None = None,
    translated_pdf_dir: Path | None = None,
    report_path: Path | None = None,
    pages: str | None = None,
    start_page: int | None = None,
    end_page: int | None = None,
    min_translated_characters: int = DEFAULT_MIN_TRANSLATED_CHARACTERS,
    min_latin_letter_ratio: float = DEFAULT_MIN_LATIN_LETTER_RATIO,
) -> QualityCheckResult:
    """Check selected translated pages and save a JSON review report."""
    source_dir = validate_pages_directory(pages_dir)
    manifest_path = source_dir / "job_manifest.json"
    manifest = load_manifest(manifest_path)
    if min_translated_characters < 1:
        raise ValueError("--min-translated-characters must be at least 1.")
    if not 0 <= min_latin_letter_ratio <= 1:
        raise ValueError("--min-latin-letter-ratio must be between 0 and 1.")
    selected_numbers = select_page_numbers(
        manifest["pages"], pages=pages, start_page=start_page, end_page=end_page
    )

    text_dir = (translated_text_dir or Path("pages") / "translated_text" / source_dir.name).expanduser()
    pdf_dir = (translated_pdf_dir or Path("pages") / "translated" / source_dir.name).expanduser()
    records = page_records_by_number(manifest)
    page_results: list[dict[str, Any]] = []
    summary = {
        "translated": [],
        "ocr_derived": [],
        "skipped": [],
        "failed": [],
        "blank": [],
        "excessively_short": [],
        "overflow": [],
        "missing_output": [],
        "non_english": [],
    }

    for page_number in selected_numbers:
        record = records[page_number]
        text_path = text_dir / f"page_{page_number:04d}_en.txt"
        pdf_path = pdf_dir / f"page_{page_number:04d}_en.pdf"
        issues: list[str] = []
        translated_text = ""
        pdf_text = ""
        pdf_page_count = 0

        if text_path.is_file():
            translated_text = text_path.read_text(encoding="utf-8").strip()
            if not translated_text:
                issues.append("blank_translation")
            elif len(translated_text) < min_translated_characters:
                issues.append("excessively_short_translation")
            script_issues = translation_script_issues(
                translated_text, min_latin_letter_ratio=min_latin_letter_ratio
            )
            issues.extend(script_issues)
        else:
            issues.append("missing_translated_text")

        if pdf_path.is_file():
            try:
                with pymupdf.open(pdf_path) as document:
                    pdf_page_count = document.page_count
                    pdf_text = "\n".join(page.get_text() for page in document).strip()
                if pdf_page_count > 1:
                    issues.append("overflow")
                if not pdf_text:
                    issues.append("blank_pdf")
            except (pymupdf.FileDataError, RuntimeError) as error:
                issues.append(f"invalid_pdf: {type(error).__name__}")
        else:
            issues.append("missing_translated_pdf")

        if any(issue in issues for issue in ("missing_translated_text", "missing_translated_pdf")):
            summary["missing_output"].append(page_number)
        if "blank_translation" in issues or "blank_pdf" in issues:
            summary["blank"].append(page_number)
        if "excessively_short_translation" in issues:
            summary["excessively_short"].append(page_number)
        if "overflow" in issues:
            summary["overflow"].append(page_number)
        if any(issue in issues for issue in ("mojibake_output", "non_english_output")):
            summary["non_english"].append(page_number)

        if record.get("extraction_method") == "ocr":
            summary["ocr_derived"].append(page_number)
        if record.get("translation_status") == "complete" and record.get("pdf_status") == "complete":
            summary["translated"].append(page_number)
        elif not issues:
            summary["skipped"].append(page_number)
        if issues or record.get("translation_status") == "failed" or record.get("pdf_status") == "failed":
            summary["failed"].append(page_number)

        page_results.append(
            {
                "page_number": page_number,
                "extraction_method": record.get("extraction_method"),
                "translation_status": record.get("translation_status"),
                "pdf_status": record.get("pdf_status"),
                "translated_text_characters": len(translated_text),
                "latin_letter_ratio": latin_letter_ratio(translated_text),
                "pdf_page_count": pdf_page_count,
                "pdf_text_characters": len(pdf_text),
                "issues": issues,
            }
        )
        record.update(
            {
                "quality_status": "passed" if not issues else "failed",
                "quality_issues": issues,
            }
        )

    report = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "source_directory": str(source_dir.resolve()),
        "translated_text_directory": str(text_dir.resolve()),
        "translated_pdf_directory": str(pdf_dir.resolve()),
        "minimum_translated_characters": min_translated_characters,
        "minimum_latin_letter_ratio": min_latin_letter_ratio,
        "selected_pages": selected_numbers,
        "summary": {name: values for name, values in summary.items()},
        "pages": page_results,
    }
    destination = (report_path or Path("logs") / f"quality_{source_dir.name}.json").expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    manifest["pages"] = [records[number] for number in sorted(records)]
    write_manifest(manifest_path, manifest)

    failed_pages = len(summary["failed"])
    return QualityCheckResult(
        report_path=destination,
        checked_pages=len(selected_numbers),
        passed_pages=len(selected_numbers) - failed_pages,
        failed_pages=failed_pages,
        blank_pages=len(summary["blank"]),
        short_pages=len(summary["excessively_short"]),
        overflow_pages=len(summary["overflow"]),
        missing_output_pages=len(summary["missing_output"]),
        non_english_pages=len(summary["non_english"]),
    )


def latin_letter_ratio(text: str) -> float:
    """Return the proportion of alphabetic characters that are Latin."""
    letters = [character for character in text if character.isalpha()]
    if not letters:
        return 0.0
    return sum("A" <= character <= "Z" or "a" <= character <= "z" for character in letters) / len(letters)


def translation_script_issues(
    text: str, *, min_latin_letter_ratio: float
) -> list[str]:
    """Detect output that is likely source-script text or UTF-8 mojibake."""
    if not text:
        return []
    issues: list[str] = []
    if any(marker in text for marker in ("à¤", "Ã", "Â", "ï¿½", "�")):
        issues.append("mojibake_output")
    devanagari_letters = len(re.findall(r"[\u0900-\u097F]", text))
    alphabetic_characters = sum(character.isalpha() for character in text)
    if devanagari_letters >= 5 or (
        alphabetic_characters >= 20 and latin_letter_ratio(text) < min_latin_letter_ratio
    ):
        issues.append("non_english_output")
    return issues


def select_page_numbers(
    records: list[dict[str, Any]],
    *,
    pages: str | None,
    start_page: int | None,
    end_page: int | None,
) -> list[int]:
    """Select page numbers from a comma-separated list and/or inclusive range."""
    if start_page is not None and start_page < 1:
        raise ValueError("--start-page must be at least 1.")
    if end_page is not None and end_page < 1:
        raise ValueError("--end-page must be at least 1.")
    if start_page is not None and end_page is not None and start_page > end_page:
        raise ValueError("--start-page cannot exceed --end-page.")
    available = {
        record["page_number"]
        for record in records
        if isinstance(record, dict) and isinstance(record.get("page_number"), int)
    }
    selected = {
        number
        for number in available
        if (start_page is None or number >= start_page)
        and (end_page is None or number <= end_page)
    }
    if pages:
        try:
            selected &= {int(value.strip()) for value in pages.split(",") if value.strip()}
        except ValueError as error:
            raise ValueError("--pages must be a comma-separated list of page numbers.") from error
    if not selected:
        raise ValueError("No pages match the requested selection.")
    return sorted(selected)


def validate_pages_directory(pages_dir: Path) -> Path:
    """Validate the split-page directory and its manifest."""
    directory = pages_dir.expanduser()
    if not directory.is_dir() or not (directory / "job_manifest.json").is_file():
        raise ValueError("A split-page directory containing job_manifest.json is required.")
    return directory