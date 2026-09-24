"""Generate readable English PDFs from translated page text."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pymupdf

from book_translator.extractor import load_manifest, page_records_by_number, write_manifest

DEFAULT_FONT_SIZE = 11
DEFAULT_MARGIN = 54
DEFAULT_LINE_SPACING = 1.35
UNICODE_FONT_CANDIDATES = (
    Path("C:/Windows/Fonts/arial.ttf"),
    Path("C:/Windows/Fonts/segoeui.ttf"),
)


@dataclass(frozen=True)
class PdfGenerationResult:
    """Summary of one translated-PDF generation run."""

    output_dir: Path
    generated_pages: int
    skipped_pages: int
    failed_pages: int


def generate_translated_pdfs(
    pages_dir: Path,
    *,
    translated_text_dir: Path | None = None,
    output_dir: Path | None = None,
    start_page: int | None = None,
    end_page: int | None = None,
    overwrite: bool = False,
    font_size: float = DEFAULT_FONT_SIZE,
) -> PdfGenerationResult:
    """Create one English PDF per translated source page and update its manifest."""
    source_dir = validate_pages_directory(pages_dir)
    manifest_path = source_dir / "job_manifest.json"
    manifest = load_manifest(manifest_path)
    if font_size <= 0:
        raise ValueError("--font-size must be greater than zero.")

    text_dir = (translated_text_dir or Path("pages") / "translated_text" / source_dir.name).expanduser()
    if not text_dir.is_dir():
        raise ValueError(f"Translated-text directory not found: {text_dir}")
    destination = (output_dir or Path("pages") / "translated" / source_dir.name).expanduser()
    destination.mkdir(parents=True, exist_ok=True)
    selected_records = select_page_records(manifest["pages"], start_page, end_page)
    records = page_records_by_number(manifest)
    counts = {"generated": 0, "skipped": 0, "failed": 0}

    for record in selected_records:
        page_number = record["page_number"]
        output_path = destination / f"page_{page_number:04d}_en.pdf"
        text_path = text_dir / f"page_{page_number:04d}_en.txt"
        source_path = source_dir / str(record.get("original_pdf", f"page_{page_number:04d}.pdf"))
        manifest_record = records[page_number]

        if output_path.exists() and not overwrite:
            manifest_record.update(
                {"translated_pdf": str(output_path.resolve()), "pdf_status": "complete", "pdf_error": None}
            )
            counts["skipped"] += 1
            write_current_manifest(manifest_path, manifest, records)
            continue

        try:
            if not text_path.is_file():
                raise ValueError(f"Translated text not found: {text_path}")
            translated_text = text_path.read_text(encoding="utf-8").strip()
            if not translated_text:
                raise ValueError("Translated text is empty.")
            write_translated_pdf(source_path, output_path, translated_text, font_size=font_size)
            manifest_record.update(
                {"translated_pdf": str(output_path.resolve()), "pdf_status": "complete", "pdf_error": None}
            )
            counts["generated"] += 1
        except (OSError, RuntimeError, ValueError) as error:
            manifest_record.update(
                {"pdf_status": "failed", "pdf_error": f"{type(error).__name__}: {error}"}
            )
            counts["failed"] += 1
        finally:
            write_current_manifest(manifest_path, manifest, records)

    return PdfGenerationResult(
        output_dir=destination,
        generated_pages=counts["generated"],
        skipped_pages=counts["skipped"],
        failed_pages=counts["failed"],
    )


def write_translated_pdf(
    source_path: Path,
    output_path: Path,
    translated_text: str,
    *,
    font_size: float = DEFAULT_FONT_SIZE,
) -> None:
    """Write translated text using the source page size, adding overflow pages as needed."""
    with pymupdf.open(source_path) as source_document:
        if source_document.page_count != 1:
            raise ValueError(f"Expected one-page source PDF: {source_path}")
        source_rect = source_document[0].rect

    font_path = unicode_font_path()
    lines = wrap_text(translated_text, source_rect.width - 2 * DEFAULT_MARGIN, font_size, font_path)
    line_height = font_size * DEFAULT_LINE_SPACING
    usable_height = source_rect.height - 2 * DEFAULT_MARGIN
    lines_per_page = max(1, int(usable_height // line_height))

    temporary_path = output_path.with_suffix(".partial.pdf")
    document = pymupdf.open()
    try:
        for start in range(0, len(lines), lines_per_page):
            page = document.new_page(width=source_rect.width, height=source_rect.height)
            text_options = {
                "fontname": "bookfont" if font_path else "helv",
                "fontsize": font_size,
                "lineheight": DEFAULT_LINE_SPACING,
                "color": (0, 0, 0),
            }
            if font_path:
                text_options["fontfile"] = str(font_path)
            page.insert_textbox(
                pymupdf.Rect(
                    DEFAULT_MARGIN,
                    DEFAULT_MARGIN,
                    source_rect.width - DEFAULT_MARGIN,
                    source_rect.height - DEFAULT_MARGIN,
                ),
                "\n".join(lines[start : start + lines_per_page]),
                **text_options,
            )
        document.save(temporary_path)
    finally:
        document.close()
    temporary_path.replace(output_path)


def wrap_text(text: str, max_width: float, font_size: float, font_path: Path | None = None) -> list[str]:
    """Wrap paragraphs without losing blank lines or explicit line boundaries."""
    font = pymupdf.Font(fontfile=str(font_path)) if font_path else pymupdf.Font("helv")
    wrapped: list[str] = []
    for paragraph in text.replace("\r\n", "\n").split("\n"):
        if not paragraph.strip():
            wrapped.append("")
            continue
        current = ""
        for word in paragraph.split():
            candidate = f"{current} {word}".strip()
            if current and font.text_length(candidate, fontsize=font_size) > max_width:
                wrapped.append(current)
                current = word
            else:
                current = candidate
        wrapped.append(current)
    return wrapped or [""]


def unicode_font_path() -> Path | None:
    """Return an installed Unicode font suitable for embedding in generated PDFs."""
    return next((path for path in UNICODE_FONT_CANDIDATES if path.is_file()), None)


def validate_pages_directory(pages_dir: Path) -> Path:
    """Validate the split-page directory and its manifest."""
    directory = pages_dir.expanduser()
    if not directory.is_dir() or not (directory / "job_manifest.json").is_file():
        raise ValueError("A split-page directory containing job_manifest.json is required.")
    return directory


def select_page_records(
    records: list[dict[str, Any]], start_page: int | None, end_page: int | None
) -> list[dict[str, Any]]:
    """Select an inclusive page range."""
    if start_page is not None and start_page < 1:
        raise ValueError("--start-page must be at least 1.")
    if end_page is not None and end_page < 1:
        raise ValueError("--end-page must be at least 1.")
    if start_page is not None and end_page is not None and start_page > end_page:
        raise ValueError("--start-page cannot exceed --end-page.")
    selected = [
        record
        for record in records
        if isinstance(record, dict)
        and isinstance(record.get("page_number"), int)
        and (start_page is None or record["page_number"] >= start_page)
        and (end_page is None or record["page_number"] <= end_page)
    ]
    if not selected:
        raise ValueError("No pages match the requested page range.")
    return selected


def write_current_manifest(
    manifest_path: Path, manifest: dict[str, Any], records: dict[int, dict[str, Any]]
) -> None:
    """Persist PDF-generation status after every page."""
    manifest["pages"] = [records[number] for number in sorted(records)]
    write_manifest(manifest_path, manifest)