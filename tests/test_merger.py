from pathlib import Path

import pymupdf
import pytest

from book_translator.merger import merge_translated_book
from book_translator.pdf_generator import generate_translated_pdfs
from book_translator.splitter import split_pdf


def make_source_pdf(path: Path, pages: int = 2) -> Path:
    document = pymupdf.open()
    for page_number in range(pages):
        page = document.new_page(width=300, height=400)
        page.insert_text((40, 40), f"Source page {page_number + 1}")
    document.save(path)
    document.close()
    return path


def test_merge_translated_book_preserves_order_and_metadata(tmp_path: Path) -> None:
    source = split_pdf(
        make_source_pdf(tmp_path / "book.pdf"),
        output_dir=tmp_path / "original",
        source_language="mr",
    )
    text_dir = tmp_path / "translated-text"
    text_dir.mkdir()
    for page_number in (1, 2):
        (text_dir / f"page_{page_number:04d}_en.txt").write_text(
            f"Translated page {page_number}.", encoding="utf-8"
        )
    translated_dir = tmp_path / "translated"
    generate_translated_pdfs(source.output_dir, translated_text_dir=text_dir, output_dir=translated_dir)

    result = merge_translated_book(source.output_dir, translated_pdf_dir=translated_dir, output_path=tmp_path / "book_en.pdf")

    assert result.source_page_count == 2
    assert result.translated_source_pages == 2
    assert result.output_page_count == 2
    with pymupdf.open(result.output_path) as document:
        assert document.metadata["subject"] == "English translation"
        assert "source-language:mr" in document.metadata["keywords"]
        assert "Translated page 1" in document[0].get_text().replace("\xa0", " ")
        assert "Translated page 2" in document[1].get_text().replace("\xa0", " ")


def test_merge_requires_every_translated_page(tmp_path: Path) -> None:
    source = split_pdf(make_source_pdf(tmp_path / "book.pdf"), output_dir=tmp_path / "original")
    translated_dir = tmp_path / "translated"
    translated_dir.mkdir()
    (translated_dir / "page_0001_en.pdf").write_bytes(
        (source.output_dir / "page_0001.pdf").read_bytes()
    )

    with pytest.raises(ValueError, match="Translated PDF missing for source page 2"):
        merge_translated_book(source.output_dir, translated_pdf_dir=translated_dir, output_path=tmp_path / "book_en.pdf")


def test_merge_counts_overflow_pages(tmp_path: Path) -> None:
    source = split_pdf(make_source_pdf(tmp_path / "book.pdf", pages=1), output_dir=tmp_path / "original")
    text_dir = tmp_path / "translated-text"
    text_dir.mkdir()
    (text_dir / "page_0001_en.txt").write_text(
        "Long translated content.\n" * 100, encoding="utf-8"
    )
    translated_dir = tmp_path / "translated"
    generate_translated_pdfs(source.output_dir, translated_text_dir=text_dir, output_dir=translated_dir)

    result = merge_translated_book(source.output_dir, translated_pdf_dir=translated_dir, output_path=tmp_path / "book_en.pdf")

    assert result.translated_source_pages == 1
    assert result.overflow_pages == 1
    assert result.output_page_count > result.source_page_count