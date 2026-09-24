import json
from pathlib import Path

import pymupdf
import pytest

from book_translator.splitter import split_pdf


def make_multipage_pdf(path: Path, pages: int = 2) -> Path:
    document = pymupdf.open()
    for page_number in range(pages):
        page = document.new_page()
        page.insert_text((72, 72), f"Page {page_number + 1}")
    document.save(path)
    document.close()
    return path


def test_split_pdf_creates_one_valid_pdf_per_page_and_manifest(tmp_path: Path) -> None:
    source_pdf = make_multipage_pdf(tmp_path / "book.pdf", pages=2)
    output_dir = tmp_path / "split-pages"

    result = split_pdf(source_pdf, output_dir=output_dir, source_language="mr")

    assert result.page_count == 2
    assert (output_dir / "page_0001.pdf").exists()
    assert (output_dir / "page_0002.pdf").exists()
    with pymupdf.open(output_dir / "page_0001.pdf") as first_page:
        assert first_page.page_count == 1
        assert "Page 1" in first_page[0].get_text()

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["source_language"] == "mr"
    assert manifest["pages"][1]["original_pdf"] == "page_0002.pdf"
    assert manifest["pages"][0]["translation_status"] == "not_started"


def test_split_pdf_refuses_to_overwrite_existing_work(tmp_path: Path) -> None:
    source_pdf = make_multipage_pdf(tmp_path / "book.pdf")
    output_dir = tmp_path / "split-pages"
    split_pdf(source_pdf, output_dir=output_dir)

    with pytest.raises(ValueError, match="Output already exists"):
        split_pdf(source_pdf, output_dir=output_dir)
