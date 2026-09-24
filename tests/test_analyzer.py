from pathlib import Path

import pymupdf
import pytest

from book_translator.analyzer import analyze_pdf, validate_pdf_path


def make_pdf(path: Path, text: str = "") -> Path:
    document = pymupdf.open()
    page = document.new_page()
    if text:
        page.insert_text((72, 72), text)
    document.save(path)
    document.close()
    return path


def test_analyze_pdf_reports_direct_text(tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "book.pdf", "This is enough extracted text to avoid OCR.")

    report = analyze_pdf(pdf, detect_language=False)

    assert report.page_count == 1
    assert report.pages[0].requires_ocr is False
    assert report.language_detection.method == "skipped"


def test_analyze_pdf_marks_blank_page_for_ocr(tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "scan.pdf")

    report = analyze_pdf(pdf, detect_language=False)

    assert report.pages[0].requires_ocr is True
    assert "OCR is required" in report.extraction_summary


def test_manual_language_override_avoids_api_call(tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "book.pdf", "This is enough extracted text to avoid OCR.")

    report = analyze_pdf(pdf, source_language="mr")

    assert report.language_detection.method == "manual"
    assert report.language_detection.language_code == "mr"


def test_validate_pdf_rejects_non_pdf(tmp_path: Path) -> None:
    text_file = tmp_path / "book.txt"
    text_file.write_text("not a PDF", encoding="utf-8")

    with pytest.raises(ValueError, match=".pdf"):
        validate_pdf_path(text_file)
