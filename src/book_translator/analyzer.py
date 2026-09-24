"""PDF validation, content inspection, and source-language detection."""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pymupdf
from dotenv import load_dotenv
from google import genai
from google.genai import types

MIN_SELECTABLE_TEXT_CHARACTERS = 30
MAX_LANGUAGE_SAMPLE_CHARACTERS = 6_000


@dataclass(frozen=True)
class PageAnalysis:
    """Extraction characteristics of one PDF page."""

    page_number: int
    width_points: float
    height_points: float
    orientation: str
    extracted_characters: int
    extracted_words: int
    image_count: int
    requires_ocr: bool


@dataclass(frozen=True)
class LanguageDetection:
    """Result returned by source-language detection."""

    language: str | None
    language_code: str | None
    confidence: str | None
    method: str
    note: str | None = None


@dataclass(frozen=True)
class BookAnalysis:
    """Validation and analysis report for one input PDF."""

    source_path: str
    file_size_bytes: int
    page_count: int
    metadata: dict[str, str]
    pages: list[PageAnalysis]
    extraction_summary: str
    language_detection: LanguageDetection

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable report."""
        return asdict(self)


def analyze_pdf(
    pdf_path: Path,
    *,
    detect_language: bool = True,
    sample_pages: int = 3,
    source_language: str | None = None,
) -> BookAnalysis:
    """Validate and inspect a PDF, optionally detecting its source language."""
    path = validate_pdf_path(pdf_path)
    if sample_pages < 1:
        raise ValueError("--sample-pages must be at least 1.")

    try:
        document = pymupdf.open(path)
    except (pymupdf.FileDataError, RuntimeError) as error:
        raise ValueError(f"'{path.name}' is not a readable PDF: {error}") from error

    with document:
        if document.needs_pass:
            raise ValueError("The PDF is password-protected and cannot be analyzed without a password.")
        if document.page_count == 0:
            raise ValueError("The PDF contains no pages.")

        pages, samples = inspect_pages(document, sample_pages)
        metadata = {
            key: value
            for key, value in document.metadata.items()
            if value and isinstance(value, str)
        }

    extraction_summary = summarize_extraction(pages)
    language = resolve_language_detection(
        samples,
        detect_language=detect_language,
        source_language=source_language,
    )
    return BookAnalysis(
        source_path=str(path.resolve()),
        file_size_bytes=path.stat().st_size,
        page_count=len(pages),
        metadata=metadata,
        pages=pages,
        extraction_summary=extraction_summary,
        language_detection=language,
    )


def resolve_language_detection(
    samples: list[str], *, detect_language: bool, source_language: str | None
) -> LanguageDetection:
    """Prefer an explicitly supplied language code over automatic detection."""
    if source_language:
        language_code = source_language.strip().lower()
        if not re.fullmatch(r"[a-z]{2,3}", language_code):
            raise ValueError("--source-language must be a two- or three-letter ISO language code.")
        return LanguageDetection(
            language=None,
            language_code=language_code,
            confidence="manual",
            method="manual",
            note="Source language was supplied by the user.",
        )
    if detect_language:
        return detect_source_language(samples)
    return LanguageDetection(
        language=None,
        language_code=None,
        confidence=None,
        method="skipped",
        note="Language detection was skipped by command-line option.",
    )


def validate_pdf_path(pdf_path: Path) -> Path:
    """Validate an existing, non-empty file with a PDF extension."""
    path = pdf_path.expanduser()
    if path.suffix.lower() != ".pdf":
        raise ValueError("Input must be a .pdf file.")
    if not path.exists():
        raise ValueError(f"PDF not found: {path}")
    if not path.is_file():
        raise ValueError(f"Input is not a file: {path}")
    if path.stat().st_size == 0:
        raise ValueError(f"PDF is empty: {path}")
    return path


def inspect_pages(document: pymupdf.Document, sample_pages: int) -> tuple[list[PageAnalysis], list[str]]:
    """Inspect all pages and select representative text samples."""
    pages: list[PageAnalysis] = []
    text_by_page: list[str] = []
    for page_number, page in enumerate(document, start=1):
        text = page.get_text("text").strip()
        word_count = len(re.findall(r"\S+", text))
        width, height = page.rect.width, page.rect.height
        pages.append(
            PageAnalysis(
                page_number=page_number,
                width_points=round(width, 2),
                height_points=round(height, 2),
                orientation="landscape" if width > height else "portrait",
                extracted_characters=len(text),
                extracted_words=word_count,
                image_count=len(page.get_images(full=True)),
                requires_ocr=len(text) < MIN_SELECTABLE_TEXT_CHARACTERS,
            )
        )
        text_by_page.append(text)

    candidate_indexes = [0, len(text_by_page) // 2, len(text_by_page) - 1]
    selected_samples: list[str] = []
    for index in dict.fromkeys(candidate_indexes):
        text = text_by_page[index]
        if text:
            selected_samples.append(text)
        if len(selected_samples) >= sample_pages:
            break
    return pages, selected_samples


def summarize_extraction(pages: list[PageAnalysis]) -> str:
    """Summarize whether direct extraction or OCR is expected."""
    ocr_pages = sum(page.requires_ocr for page in pages)
    if ocr_pages == 0:
        return "All pages contain enough selectable text for direct extraction."
    if ocr_pages == len(pages):
        return "No page contains enough selectable text; OCR is required for the whole document."
    return f"Direct extraction is usable on {len(pages) - ocr_pages} of {len(pages)} pages; OCR is required for {ocr_pages} pages."


def detect_source_language(samples: list[str]) -> LanguageDetection:
    """Detect the language represented by extracted PDF text using Google GenAI."""
    source_text = "\n\n".join(samples).strip()[:MAX_LANGUAGE_SAMPLE_CHARACTERS]
    if not source_text:
        return LanguageDetection(
            language=None,
            language_code=None,
            confidence=None,
            method="unavailable",
            note="No selectable text was available. Run OCR before detecting the language.",
        )

    load_dotenv()
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key or api_key == "replace_with_your_google_api_key":
        return LanguageDetection(
            language=None,
            language_code=None,
            confidence=None,
            method="unavailable",
            note="GOOGLE_API_KEY is not configured; language detection was not sent to Google.",
        )

    prompt = (
        "Identify the primary language of the following book excerpt. Return JSON only with "
        'the keys "language", "language_code" (ISO 639-1 when available), and "confidence" '
        '(high, medium, or low). Do not translate the text.\n\nEXCERPT:\n'
        f"{source_text}"
    )
    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=os.getenv("GOOGLE_GENAI_MODEL", "gemini-2.5-flash"),
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            ),
        )
        payload = json.loads(response.text or "{}")
        return LanguageDetection(
            language=coerce_string(payload.get("language")),
            language_code=coerce_string(payload.get("language_code")),
            confidence=coerce_string(payload.get("confidence")),
            method="google-genai",
        )
    except Exception as error:  # noqa: BLE001 - API errors must not prevent local PDF inspection.
        return LanguageDetection(
            language=None,
            language_code=None,
            confidence=None,
            method="failed",
            note=(
                f"Google language detection failed: {type(error).__name__} "
                f"({sanitize_error_message(error, api_key)})."
            ),
        )


def coerce_string(value: object) -> str | None:
    """Convert a JSON scalar to a stripped string, or return None."""
    return value.strip() if isinstance(value, str) and value.strip() else None


def sanitize_error_message(error: Exception, api_key: str) -> str:
    """Return a compact diagnostic without exposing the configured API key."""
    message = str(error).replace(api_key, "[redacted]").replace("\n", " ").strip()
    return message[:240] or "no additional detail"
