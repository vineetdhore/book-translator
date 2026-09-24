import json
from pathlib import Path

import pymupdf

from book_translator.pdf_generator import generate_translated_pdfs, write_translated_pdf
from book_translator.splitter import split_pdf


def make_source_pdf(path: Path) -> Path:
    document = pymupdf.open()
    page = document.new_page(width=300, height=400)
    page.insert_text((40, 40), "Source page")
    document.save(path)
    document.close()
    return path


def test_generate_translated_pdf_preserves_size_and_manifest(tmp_path: Path) -> None:
    source = split_pdf(make_source_pdf(tmp_path / "book.pdf"), output_dir=tmp_path / "original")
    text_dir = tmp_path / "translated-text"
    text_dir.mkdir()
    (text_dir / "page_0001_en.txt").write_text("Faithful English translation.", encoding="utf-8")

    result = generate_translated_pdfs(
        source.output_dir,
        translated_text_dir=text_dir,
        output_dir=tmp_path / "translated",
    )

    assert result.generated_pages == 1
    with pymupdf.open(result.output_dir / "page_0001_en.pdf") as document:
        assert document.page_count == 1
        assert document[0].rect.width == 300
        assert document[0].rect.height == 400
        assert "Faithful English translation." in document[0].get_text().replace("\xa0", " ")
    manifest = json.loads(source.manifest_path.read_text(encoding="utf-8"))
    assert manifest["pages"][0]["pdf_status"] == "complete"


def test_generate_translated_pdf_keeps_overflow_in_same_file(tmp_path: Path) -> None:
    source = split_pdf(make_source_pdf(tmp_path / "book.pdf"), output_dir=tmp_path / "original")
    text_dir = tmp_path / "translated-text"
    text_dir.mkdir()
    long_text = "\n".join(["A translated paragraph with enough words to wrap."] * 100)
    (text_dir / "page_0001_en.txt").write_text(long_text, encoding="utf-8")

    result = generate_translated_pdfs(
        source.output_dir,
        translated_text_dir=text_dir,
        output_dir=tmp_path / "translated",
    )

    with pymupdf.open(result.output_dir / "page_0001_en.pdf") as document:
        assert document.page_count > 1
        assert "A translated paragraph" in document[0].get_text().replace("\xa0", " ")


def test_generate_translated_pdf_preserves_unicode_quotes_and_apostrophes(tmp_path: Path) -> None:
    source = split_pdf(make_source_pdf(tmp_path / "book.pdf"), output_dir=tmp_path / "original")
    output = tmp_path / "translated.pdf"

    write_translated_pdf(
        source.output_dir / "page_0001.pdf",
        output,
        "He said “hello” and it’s fine — really.",
    )

    with pymupdf.open(output) as document:
        text = document[0].get_text()
        assert "“hello”" in text
        assert "it’s" in text