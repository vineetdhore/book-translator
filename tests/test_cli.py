from pathlib import Path

import pytest

from book_translator.cli import build_parser, contiguous_ranges, parse_page_list


def test_translate_parser_supports_phase_nine_options() -> None:
    args = build_parser().parse_args(
        [
            "translate",
            "pages/original/book",
            "--language",
            "hi",
            "--pages",
            "12,13,14",
            "--output",
            "custom-output",
            "--resume",
            "--keep-intermediate-files",
        ]
    )

    assert args.source_language_alias == "hi"
    assert args.pages == "12,13,14"
    assert args.output_dir == Path("custom-output")
    assert args.resume is True
    assert args.keep_intermediate_files is True


def test_retry_and_merge_aliases_parse() -> None:
    retry = build_parser().parse_args(["retry", "pages/original/book", "--pages", "12,14"])
    merge = build_parser().parse_args(["merge", "pages/original/book", "--output", "book.pdf"])

    assert retry.pages == "12,14"
    assert merge.command == "merge"
    assert merge.output == Path("book.pdf")


def test_page_selection_helpers_validate_and_group_pages() -> None:
    assert parse_page_list("14,12,13,13") == [12, 13, 14]
    assert contiguous_ranges([2, 3, 7, 10, 11]) == [(2, 3), (7, 7), (10, 11)]
    with pytest.raises(ValueError, match="positive page numbers"):
        parse_page_list("0,2")