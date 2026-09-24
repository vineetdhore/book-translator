import json
from pathlib import Path

import pymupdf

from book_translator.pdf_generator import generate_translated_pdfs
from book_translator.quality_checker import check_translation_quality, translation_script_issues
from book_translator.splitter import split_pdf


def make_source_pdf(path: Path, pages: int = 2) -> Path:
    document = pymupdf.open()
    for page_number in range(pages):
        page = document.new_page(width=300, height=400)
        page.insert_text((40, 40), f"Source page {page_number + 1}")
    document.save(path)
    document.close()
    return path


def test_quality_check_reports_passed_ocr_and_selected_pages(tmp_path: Path) -> None:
    source = split_pdf(make_source_pdf(tmp_path / "book.pdf"), output_dir=tmp_path / "original")
    text_dir = tmp_path / "translated-text"
    text_dir.mkdir()
    pdf_dir = tmp_path / "translated"
    (text_dir / "page_0001_en.txt").write_text("A complete translated page with enough content.", encoding="utf-8")
    (text_dir / "page_0002_en.txt").write_text("Another complete translated page with enough content.", encoding="utf-8")
    generate_translated_pdfs(source.output_dir, translated_text_dir=text_dir, output_dir=pdf_dir)

    manifest = json.loads(source.manifest_path.read_text(encoding="utf-8"))
    manifest["pages"][1]["extraction_method"] = "ocr"
    source.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    report_path = tmp_path / "review.json"

    result = check_translation_quality(
        source.output_dir,
        translated_text_dir=text_dir,
        translated_pdf_dir=pdf_dir,
        report_path=report_path,
        pages="2",
        min_translated_characters=10,
    )

    assert result.checked_pages == 1
    assert result.passed_pages == 1
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["summary"]["ocr_derived"] == [2]
    assert report["selected_pages"] == [2]


def test_quality_check_detects_blank_short_overflow_and_missing(tmp_path: Path) -> None:
    source = split_pdf(make_source_pdf(tmp_path / "book.pdf", pages=4), output_dir=tmp_path / "original")
    text_dir = tmp_path / "translated-text"
    text_dir.mkdir()
    pdf_dir = tmp_path / "translated"
    pdf_dir.mkdir()
    (text_dir / "page_0001_en.txt").write_text("", encoding="utf-8")
    (text_dir / "page_0002_en.txt").write_text("Short.", encoding="utf-8")
    (text_dir / "page_0003_en.txt").write_text(
        "Short.\n" + "A long translated paragraph that wraps onto additional PDF pages.\n" * 100,
        encoding="utf-8",
    )
    generate_translated_pdfs(
        source.output_dir,
        translated_text_dir=text_dir,
        output_dir=pdf_dir,
        start_page=2,
        end_page=3,
    )

    report_path = tmp_path / "review.json"
    result = check_translation_quality(
        source.output_dir,
        translated_text_dir=text_dir,
        translated_pdf_dir=pdf_dir,
        report_path=report_path,
        min_translated_characters=10,
    )

    assert result.failed_pages == 4
    assert result.blank_pages == 1
    assert result.short_pages == 1
    assert result.overflow_pages == 1
    assert result.missing_output_pages == 2


def test_translation_script_issues_detect_mojibake_and_source_script() -> None:
    assert "mojibake_output" in translation_script_issues("à¤®à¤¶à¤ƒ translated", min_latin_letter_ratio=0.25)
    assert "non_english_output" in translation_script_issues("मराठी मजकूर इथे आहे", min_latin_letter_ratio=0.25)
    assert translation_script_issues("This is a natural English translation.", min_latin_letter_ratio=0.25) == []


def test_quality_check_reports_non_english_output(tmp_path: Path) -> None:
    source = split_pdf(make_source_pdf(tmp_path / "book.pdf", pages=1), output_dir=tmp_path / "original")
    text_dir = tmp_path / "translated-text"
    text_dir.mkdir()
    pdf_dir = tmp_path / "translated"
    (text_dir / "page_0001_en.txt").write_text("à¤®à¤¶à¤ƒ " * 80, encoding="utf-8")
    generate_translated_pdfs(source.output_dir, translated_text_dir=text_dir, output_dir=pdf_dir)

    result = check_translation_quality(
        source.output_dir,
        translated_text_dir=text_dir,
        translated_pdf_dir=pdf_dir,
        report_path=tmp_path / "quality.json",
    )

    assert result.failed_pages == 1
    assert result.non_english_pages == 1
    report = json.loads((tmp_path / "quality.json").read_text(encoding="utf-8"))
    assert report["summary"]["non_english"] == [1]
    assert "mojibake_output" in report["pages"][0]["issues"]