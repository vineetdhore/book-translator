import json
from pathlib import Path

import pymupdf

from book_translator.extractor import extract_pages
from book_translator.splitter import split_pdf


def make_source_pdf(path: Path, text: str) -> Path:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), text)
    document.save(path)
    document.close()
    return path


def test_extract_pages_saves_direct_text_and_updates_manifest(tmp_path: Path) -> None:
    source = make_source_pdf(tmp_path / "book.pdf", "Extract this selectable text from the page.")
    split = split_pdf(source, output_dir=tmp_path / "original")

    result = extract_pages(split.output_dir, output_dir=tmp_path / "text")

    assert result.direct_text_pages == 1
    assert result.ocr_pages == 0
    assert (tmp_path / "text" / "page_0001.txt").read_text(encoding="utf-8").startswith(
        "Extract this selectable text"
    )
    manifest = json.loads(split.manifest_path.read_text(encoding="utf-8"))
    assert manifest["pages"][0]["extraction_method"] == "direct_text"
    assert manifest["pages"][0]["extraction_status"] == "complete"


def test_extract_pages_resumes_existing_text(tmp_path: Path) -> None:
    source = make_source_pdf(tmp_path / "book.pdf", "Extract this selectable text from the page.")
    split = split_pdf(source, output_dir=tmp_path / "original")
    output_dir = tmp_path / "text"
    extract_pages(split.output_dir, output_dir=output_dir)

    result = extract_pages(split.output_dir, output_dir=output_dir)

    assert result.skipped_pages == 1
