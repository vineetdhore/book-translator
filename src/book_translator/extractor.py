"""Extract text from page-level PDFs, with a local OCR fallback."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pymupdf
import pytesseract
from PIL import Image, ImageOps

from book_translator.analyzer import MIN_SELECTABLE_TEXT_CHARACTERS


@dataclass(frozen=True)
class ExtractionResult:
    """Summary of one page-text extraction run."""

    output_dir: Path
    direct_text_pages: int
    ocr_pages: int
    mixed_pages: int
    skipped_pages: int
    failed_pages: int


def extract_pages(
    pages_dir: Path,
    *,
    output_dir: Path | None = None,
    force_ocr: bool = False,
    tesseract_language: str = "eng",
    overwrite: bool = False,
) -> ExtractionResult:
    """Extract text from every split PDF and persist statuses in its manifest."""
    source_dir = validate_pages_directory(pages_dir)
    manifest_path = source_dir / "job_manifest.json"
    manifest = load_manifest(manifest_path)
    page_paths = sorted(source_dir.glob("page_*.pdf"), key=page_number_from_path)
    if not page_paths:
        raise ValueError(f"No page_*.pdf files found in '{source_dir}'.")

    destination = output_dir or Path("pages") / "text" / source_dir.name
    destination = destination.expanduser()
    destination.mkdir(parents=True, exist_ok=True)
    page_records = page_records_by_number(manifest)
    counts = {"direct": 0, "ocr": 0, "mixed": 0, "skipped": 0, "failed": 0}

    for page_path in page_paths:
        page_number = page_number_from_path(page_path)
        text_path = destination / f"page_{page_number:04d}.txt"
        record = page_records.setdefault(page_number, {"page_number": page_number})

        if text_path.exists() and not overwrite:
            record.update(
                {
                    "source_text": str(text_path.resolve()),
                    "extraction_status": "complete",
                    "error": None,
                }
            )
            counts["skipped"] += 1
            continue

        try:
            text, method = extract_page_text(
                page_path,
                force_ocr=force_ocr,
                tesseract_language=tesseract_language,
            )
            if not text:
                raise ValueError("No text could be extracted from this page.")
            write_text_file(text_path, text)
            record.update(
                {
                    "source_text": str(text_path.resolve()),
                    "extraction_method": method,
                    "extraction_status": "complete",
                    "error": None,
                }
            )
            if method == "ocr":
                counts["ocr"] += 1
            elif method == "mixed_direct_and_ocr":
                counts["mixed"] += 1
            else:
                counts["direct"] += 1
        except (OSError, RuntimeError, ValueError, pytesseract.TesseractError) as error:
            record.update(
                {
                    "extraction_status": "failed",
                    "error": f"{type(error).__name__}: {error}",
                }
            )
            counts["failed"] += 1
        finally:
            manifest["pages"] = [page_records[number] for number in sorted(page_records)]
            write_manifest(manifest_path, manifest)

    return ExtractionResult(
        output_dir=destination,
        direct_text_pages=counts["direct"],
        ocr_pages=counts["ocr"],
        mixed_pages=counts["mixed"],
        skipped_pages=counts["skipped"],
        failed_pages=counts["failed"],
    )


def extract_page_text(
    page_path: Path, *, force_ocr: bool, tesseract_language: str
) -> tuple[str, str]:
    """Extract valid direct text, image-region text, or full-page OCR."""
    with pymupdf.open(page_path) as document:
        if document.needs_pass:
            raise ValueError("Page PDF is password-protected.")
        if document.page_count != 1:
            raise ValueError("Expected a one-page PDF.")
        page = document[0]
        direct_text = clean_text(page.get_text("text"))
        text_blocks = page.get_text("blocks")
        image_rects = image_rectangles(page)
        direct_is_valid = (
            not force_ocr
            and len(direct_text) >= MIN_SELECTABLE_TEXT_CHARACTERS
            and is_valid_direct_text(direct_text)
        )
        if direct_is_valid:
            region_text = extract_image_region_text(
                page, image_rects, text_blocks, tesseract_language
            )
            if region_text:
                return f"{direct_text}\n\n{region_text}", "mixed_direct_and_ocr"
            return direct_text, "direct_text"
        image = render_page_for_ocr(page)

    ocr_text = clean_text(pytesseract.image_to_string(image, lang=tesseract_language))
    return ocr_text, "ocr"


def is_valid_direct_text(text: str) -> bool:
    """Reject text with signs of a broken PDF font-to-Unicode mapping."""
    controls = sum(ord(character) < 32 and character not in "\n\r\t" for character in text)
    mojibake = sum(text.count(marker) for marker in ("�", "ï¿½", "à¤"))
    return controls < 3 and mojibake < 2


def image_rectangles(page: pymupdf.Page) -> list[pymupdf.Rect]:
    """Return distinct visible image rectangles on a page."""
    rectangles: list[pymupdf.Rect] = []
    for image in page.get_images(full=True):
        for rectangle in page.get_image_rects(image):
            if rectangle.width > 20 and rectangle.height > 20 and rectangle not in rectangles:
                rectangles.append(rectangle)
    return rectangles


def extract_image_region_text(
    page: pymupdf.Page,
    image_rects: list[pymupdf.Rect],
    text_blocks: list[tuple[Any, ...]],
    tesseract_language: str,
) -> str:
    """OCR image regions that are not already covered by selectable text."""
    if not image_rects or not hasattr(pytesseract, "image_to_data"):
        return ""
    direct_rects = [pymupdf.Rect(block[:4]) for block in text_blocks if block[4].strip()]
    region_results: list[str] = []
    for rectangle in image_rects:
        if any(rectangle.intersects(text_rect) for text_rect in direct_rects):
            continue
        try:
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(300 / 72, 300 / 72), clip=rectangle, alpha=False)
            image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
            data = pytesseract.image_to_data(image, lang=tesseract_language, output_type=pytesseract.Output.DICT)
            words = [word for word, confidence in zip(data["text"], data["conf"]) if word.strip() and float(confidence) >= 35]
            if len(words) >= 3:
                region_results.append(" ".join(words))
        except (OSError, RuntimeError, ValueError, pytesseract.TesseractError):
            continue
    return "\n\n".join(region_results)


def render_page_for_ocr(page: pymupdf.Page) -> Image.Image:
    """Render a PDF page at 300 DPI and increase contrast for OCR."""
    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(300 / 72, 300 / 72), alpha=False)
    image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
    grayscale = ImageOps.grayscale(image)
    return ImageOps.autocontrast(grayscale)


def clean_text(text: str) -> str:
    """Normalize text without removing paragraph boundaries or script characters."""
    normalized_lines = [line.rstrip() for line in text.replace("\r\n", "\n").split("\n")]
    return "\n".join(normalized_lines).strip()


def validate_pages_directory(pages_dir: Path) -> Path:
    """Validate a split-page directory and its required job manifest."""
    directory = pages_dir.expanduser()
    if not directory.is_dir():
        raise ValueError(f"Page directory not found: {directory}")
    if not (directory / "job_manifest.json").is_file():
        raise ValueError(f"job_manifest.json not found in '{directory}'. Run the split command first.")
    return directory


def load_manifest(path: Path) -> dict[str, Any]:
    """Load and validate the JSON manifest generated by the split phase."""
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"Invalid job manifest: {error}") from error
    if not isinstance(manifest, dict) or not isinstance(manifest.get("pages"), list):
        raise ValueError("Job manifest does not contain a valid pages list.")  # noqa: TRY004
    return manifest


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    """Atomically persist a manifest after each processed page for safe resumption."""
    temporary_path = path.with_suffix(".partial.json")
    temporary_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary_path.replace(path)


def write_text_file(path: Path, text: str) -> None:
    """Atomically save a page's extracted text as UTF-8."""
    temporary_path = path.with_suffix(".partial.txt")
    temporary_path.write_text(text + "\n", encoding="utf-8")
    temporary_path.replace(path)


def page_number_from_path(path: Path) -> int:
    """Return the numeric value in page_0001.pdf-style filenames."""
    match = re.fullmatch(r"page_(\d+)\.pdf", path.name)
    if not match:
        raise ValueError(f"Unexpected page filename: {path.name}")
    return int(match.group(1))


def page_records_by_number(manifest: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """Index manifest records for efficient, stable page-level updates."""
    records: dict[int, dict[str, Any]] = {}
    for record in manifest["pages"]:
        if isinstance(record, dict) and isinstance(record.get("page_number"), int):
            records[record["page_number"]] = record
    return records
