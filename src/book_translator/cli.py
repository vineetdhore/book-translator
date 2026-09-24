"""Command-line interface for the local Book Translator workflow."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from book_translator.analyzer import analyze_pdf
from book_translator.extractor import extract_pages
from book_translator.merger import merge_translated_book
from book_translator.pdf_generator import generate_translated_pdfs
from book_translator.quality_checker import check_translation_quality
from book_translator.splitter import split_pdf
from book_translator.translator import translate_pages
from book_translator.web_ui import serve_dashboard


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(
        prog="book-translator",
        description="Analyze a PDF before page-by-page translation.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    analyze_parser = subparsers.add_parser(
        "analyze", help="validate a PDF and inspect its pages"
    )
    analyze_parser.add_argument("pdf", type=Path, help="path to the source PDF")
    analyze_parser.add_argument(
        "--skip-language-detection",
        action="store_true",
        help="do not call the Google API to detect the source language",
    )
    analyze_parser.add_argument(
        "--sample-pages",
        type=int,
        default=3,
        help="maximum number of pages used for language detection (default: 3)",
    )
    analyze_parser.add_argument(
        "--source-language",
        metavar="ISO_CODE",
        help="manual ISO 639-1 source-language override, for example mr or hi",
    )
    analyze_parser.add_argument(
        "--report",
        type=Path,
        help="optional path for a JSON analysis report",
    )

    split_parser = subparsers.add_parser(
        "split", help="split a source PDF into one PDF per page"
    )
    split_parser.add_argument("pdf", type=Path, help="path to the source PDF")
    split_parser.add_argument(
        "--output-dir",
        type=Path,
        help="directory for one-page PDFs (default: pages/original/<book name>)",
    )
    split_parser.add_argument(
        "--source-language",
        metavar="ISO_CODE",
        help="optional known source-language code for the job manifest, for example mr",
    )
    split_parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace existing one-page PDFs and the job manifest in the output directory",
    )

    extract_parser = subparsers.add_parser(
        "extract", help="extract source text from one-page PDFs, using OCR when needed"
    )
    extract_parser.add_argument(
        "pages_dir", type=Path, help="directory containing page_*.pdf and job_manifest.json"
    )
    extract_parser.add_argument(
        "--output-dir",
        type=Path,
        help="directory for extracted page text (default: pages/text/<book name>)",
    )
    extract_parser.add_argument(
        "--force-ocr",
        action="store_true",
        help="use OCR even when a page contains selectable text",
    )
    extract_parser.add_argument(
        "--tesseract-language",
        default="eng",
        help="Tesseract language pack for OCR fallback (default: eng)",
    )
    extract_parser.add_argument(
        "--ocr-engine",
        choices=("tesseract",),
        default="tesseract",
        help="OCR engine to use (currently: tesseract)",
    )
    extract_parser.add_argument(
        "--overwrite",
        action="store_true",
        help="re-extract pages whose text files already exist",
    )

    translate_parser = subparsers.add_parser(
        "translate", help="translate extracted page text into natural English"
    )
    translate_parser.add_argument(
        "pages_dir", type=Path, help="directory containing job_manifest.json"
    )
    translate_parser.add_argument(
        "--text-dir",
        type=Path,
        help="directory containing source page text (default: pages/text/<book name>)",
    )
    translate_parser.add_argument(
        "--output-dir",
        "--output",
        dest="output_dir",
        type=Path,
        help="directory for translated page text (default: pages/translated_text/<book name>)",
    )
    translate_parser.add_argument(
        "--source-language",
        metavar="ISO_CODE",
        help="override the source language recorded in the job manifest",
    )
    translate_parser.add_argument(
        "--language",
        dest="source_language_alias",
        metavar="ISO_CODE",
        help="alias for --source-language",
    )
    translate_parser.add_argument(
        "--model",
        help="override GOOGLE_GENAI_MODEL for this translation run",
    )
    translate_parser.add_argument(
        "--start-page", type=int, help="first page to translate, inclusive"
    )
    translate_parser.add_argument(
        "--end-page", type=int, help="last page to translate, inclusive"
    )
    translate_parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help="retries after a failed API request (default: 3)",
    )
    translate_parser.add_argument(
        "--request-delay",
        type=float,
        default=0.25,
        help="seconds to wait between page requests (default: 0.25)",
    )
    translate_parser.add_argument(
        "--overwrite",
        action="store_true",
        help="retranslate pages whose English text files already exist",
    )
    translate_parser.add_argument(
        "--resume",
        action="store_true",
        help="resume by skipping completed page translations (default behavior)",
    )
    translate_parser.add_argument(
        "--keep-intermediate-files",
        action="store_true",
        help="retain intermediate files (default behavior)",
    )
    translate_parser.add_argument(
        "--pages",
        help="comma-separated page numbers to translate, for example 12,13,14",
    )

    pdf_parser = subparsers.add_parser(
        "generate-pdfs", help="generate one English PDF per translated page"
    )
    pdf_parser.add_argument("pages_dir", type=Path, help="directory containing job_manifest.json")
    pdf_parser.add_argument(
        "--translated-text-dir",
        type=Path,
        help="directory containing translated page text (default: pages/translated_text/<book name>)",
    )
    pdf_parser.add_argument(
        "--output-dir",
        type=Path,
        help="directory for translated PDFs (default: pages/translated/<book name>)",
    )
    pdf_parser.add_argument("--start-page", type=int, help="first page to generate, inclusive")
    pdf_parser.add_argument("--end-page", type=int, help="last page to generate, inclusive")
    pdf_parser.add_argument(
        "--font-size", type=float, default=11, help="English PDF font size (default: 11)"
    )
    pdf_parser.add_argument(
        "--overwrite", action="store_true", help="replace existing translated PDFs"
    )

    quality_parser = subparsers.add_parser(
        "quality-check", help="check translated text and PDFs and write a review report"
    )
    quality_parser.add_argument("pages_dir", type=Path, help="directory containing job_manifest.json")
    quality_parser.add_argument("--translated-text-dir", type=Path)
    quality_parser.add_argument("--translated-pdf-dir", type=Path)
    quality_parser.add_argument(
        "--report", type=Path, help="JSON report path (default: logs/quality_<book name>.json)"
    )
    quality_parser.add_argument(
        "--pages", help="comma-separated page numbers to check, for example 12,13,14"
    )
    quality_parser.add_argument("--start-page", type=int)
    quality_parser.add_argument("--end-page", type=int)
    quality_parser.add_argument(
        "--min-translated-characters",
        type=int,
        default=100,
        help="minimum non-blank translated characters (default: 100)",
    )
    quality_parser.add_argument(
        "--min-latin-letter-ratio",
        type=float,
        default=0.25,
        help="minimum Latin-letter ratio for non-short output (default: 0.25)",
    )

    merge_parser = subparsers.add_parser(
        "merge", aliases=["merge-book"], help="merge translated page PDFs into one English book"
    )
    merge_parser.add_argument("pages_dir", type=Path, help="directory containing job_manifest.json")
    merge_parser.add_argument("--translated-pdf-dir", type=Path)
    merge_parser.add_argument(
        "--output",
        type=Path,
        help="output PDF path (default: output/<book name>_translated_english.pdf)",
    )
    merge_parser.add_argument(
        "--overwrite", action="store_true", help="replace an existing merged PDF"
    )
    retry_parser = subparsers.add_parser(
        "retry", help="retry translation for selected failed or pending pages"
    )
    retry_parser.add_argument("pages_dir", type=Path, help="directory containing job_manifest.json")
    retry_parser.add_argument(
        "--pages", required=True, help="comma-separated page numbers, for example 12,13,14"
    )
    retry_parser.add_argument("--text-dir", type=Path)
    retry_parser.add_argument("--output", "--output-dir", dest="output_dir", type=Path)
    retry_parser.add_argument("--language", dest="source_language", metavar="ISO_CODE")
    retry_parser.add_argument("--model")
    retry_parser.add_argument("--max-retries", type=int, default=3)
    retry_parser.add_argument("--request-delay", type=float, default=0.25)
    retry_parser.add_argument("--overwrite", action="store_true")

    ui_parser = subparsers.add_parser(
        "ui", help="open the local browser dashboard for the full translation workflow"
    )
    ui_parser.add_argument("--host", default="127.0.0.1")
    ui_parser.add_argument("--port", type=int, default=8765)
    ui_parser.add_argument(
        "--state-file",
        type=Path,
        default=Path("logs") / "ui_state.json",
        help="persisted dashboard state file",
    )
    return parser


def main() -> None:
    """Run a Book Translator command."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args()

    if args.command == "analyze":
        try:
            report = analyze_pdf(
                args.pdf,
                detect_language=not args.skip_language_detection,
                sample_pages=args.sample_pages,
                source_language=args.source_language,
            )
        except (OSError, ValueError) as error:
            raise SystemExit(f"Analysis failed: {error}") from error

        serialized_report = json.dumps(report.to_dict(), indent=2, ensure_ascii=False)
        print(serialized_report)
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(serialized_report + "\n", encoding="utf-8")
            print(f"Analysis report saved to: {args.report}")
        return

    if args.command == "split":
        try:
            result = split_pdf(
                args.pdf,
                output_dir=args.output_dir,
                source_language=args.source_language,
                overwrite=args.overwrite,
            )
        except (OSError, ValueError) as error:
            raise SystemExit(f"Split failed: {error}") from error

        print(f"Created {result.page_count} one-page PDFs in: {result.output_dir}")
        print(f"Job manifest saved to: {result.manifest_path}")
        return

    if args.command == "extract":
        try:
            result = extract_pages(
                args.pages_dir,
                output_dir=args.output_dir,
                force_ocr=args.force_ocr,
                tesseract_language=args.tesseract_language,
                overwrite=args.overwrite,
            )
        except (OSError, ValueError) as error:
            raise SystemExit(f"Extraction failed: {error}") from error

        print(
            f"Extraction complete: {result.direct_text_pages} direct-text, "
                f"{result.ocr_pages} OCR, {result.mixed_pages} mixed direct/OCR, {result.skipped_pages} skipped, "
            f"{result.failed_pages} failed."
        )
        print(f"Extracted text saved in: {result.output_dir}")
        return

    if args.command == "translate":
        source_language = args.source_language or args.source_language_alias
        selected_pages = parse_page_list(args.pages) if args.pages else None
        try:
            results = run_translation_selections(
                args.pages_dir,
                selected_pages=selected_pages,
                text_dir=args.text_dir,
                output_dir=args.output_dir,
                source_language=source_language,
                model=args.model,
                start_page=args.start_page,
                end_page=args.end_page,
                max_retries=args.max_retries,
                request_delay=args.request_delay,
                overwrite=args.overwrite,
            )
        except (OSError, ValueError) as error:
            raise SystemExit(f"Translation failed: {error}") from error

        result = combine_translation_results(results)
        print(
            f"Translation complete: {result.translated_pages} translated, "
            f"{result.skipped_pages} skipped, {result.failed_pages} failed."
        )
        if result.stopped_for_quota:
            print("Translation stopped because Google reported exhausted API quota. Resume after quota is available.")
        print(f"English page text saved in: {result.output_dir}")
        return

    if args.command == "generate-pdfs":
        try:
            result = generate_translated_pdfs(
                args.pages_dir,
                translated_text_dir=args.translated_text_dir,
                output_dir=args.output_dir,
                start_page=args.start_page,
                end_page=args.end_page,
                overwrite=args.overwrite,
                font_size=args.font_size,
            )
        except (OSError, ValueError, RuntimeError) as error:
            raise SystemExit(f"PDF generation failed: {error}") from error

        print(
            f"PDF generation complete: {result.generated_pages} generated, "
            f"{result.skipped_pages} skipped, {result.failed_pages} failed."
        )
        print(f"Translated PDFs saved in: {result.output_dir}")
        return

    if args.command == "quality-check":
        try:
            result = check_translation_quality(
                args.pages_dir,
                translated_text_dir=args.translated_text_dir,
                translated_pdf_dir=args.translated_pdf_dir,
                report_path=args.report,
                pages=args.pages,
                start_page=args.start_page,
                end_page=args.end_page,
                min_translated_characters=args.min_translated_characters,
                min_latin_letter_ratio=args.min_latin_letter_ratio,
            )
        except (OSError, ValueError, RuntimeError) as error:
            raise SystemExit(f"Quality check failed: {error}") from error

        print(
            f"Quality check complete: {result.passed_pages} passed, {result.failed_pages} failed, "
            f"{result.blank_pages} blank, {result.short_pages} short, "
            f"{result.overflow_pages} overflow, {result.missing_output_pages} missing output, "
            f"{result.non_english_pages} non-English."
        )
        print(f"Review report saved to: {result.report_path}")
        return

    if args.command in {"merge", "merge-book"}:
        try:
            result = merge_translated_book(
                args.pages_dir,
                translated_pdf_dir=args.translated_pdf_dir,
                output_path=args.output,
                overwrite=args.overwrite,
            )
        except (OSError, ValueError, RuntimeError) as error:
            raise SystemExit(f"Book merge failed: {error}") from error

        print(
            f"Book merge complete: {result.translated_source_pages} source pages merged, "
            f"{result.output_page_count} output pages ({result.overflow_pages} overflow source pages)."
        )
        print(f"Merged English book saved to: {result.output_path}")
        return

    if args.command == "retry":
        try:
            results = run_translation_selections(
                args.pages_dir,
                selected_pages=parse_page_list(args.pages),
                text_dir=args.text_dir,
                output_dir=args.output_dir,
                source_language=args.source_language,
                model=args.model,
                max_retries=args.max_retries,
                request_delay=args.request_delay,
                overwrite=args.overwrite,
            )
        except (OSError, ValueError) as error:
            raise SystemExit(f"Retry failed: {error}") from error

        result = combine_translation_results(results)
        print(
            f"Retry complete: {result.translated_pages} translated, "
            f"{result.skipped_pages} skipped, {result.failed_pages} failed."
        )
        print(f"English page text saved in: {result.output_dir}")
        return

    if args.command == "ui":
        serve_dashboard(host=args.host, port=args.port, state_path=args.state_file)


def parse_page_list(value: str) -> list[int]:
    """Parse a positive comma-separated page list."""
    try:
        pages = sorted({int(item.strip()) for item in value.split(",") if item.strip()})
    except ValueError as error:
        raise ValueError("--pages must be a comma-separated list of positive page numbers.") from error
    if not pages or any(page < 1 for page in pages):
        raise ValueError("--pages must contain positive page numbers.")
    return pages


def contiguous_ranges(pages: Sequence[int]) -> list[tuple[int, int]]:
    """Group selected page numbers into inclusive contiguous ranges."""
    ranges: list[tuple[int, int]] = []
    start = previous = pages[0]
    for page in pages[1:]:
        if page != previous + 1:
            ranges.append((start, previous))
            start = page
        previous = page
    ranges.append((start, previous))
    return ranges


def run_translation_selections(
    pages_dir: Path,
    *,
    selected_pages: list[int] | None = None,
    text_dir: Path | None = None,
    output_dir: Path | None = None,
    source_language: str | None = None,
    model: str | None = None,
    start_page: int | None = None,
    end_page: int | None = None,
    max_retries: int = 3,
    request_delay: float = 0.25,
    overwrite: bool = False,
) -> list:
    """Run translation once per selected contiguous range."""
    ranges = contiguous_ranges(selected_pages) if selected_pages else [(start_page, end_page)]
    return [
        translate_pages(
            pages_dir,
            text_dir=text_dir,
            output_dir=output_dir,
            source_language=source_language,
            model=model,
            start_page=range_start,
            end_page=range_end,
            max_retries=max_retries,
            request_delay=request_delay,
            overwrite=overwrite,
        )
        for range_start, range_end in ranges
    ]


def combine_translation_results(results: list):
    """Combine results from one or more selected translation ranges."""
    first = results[0]
    result_type = type(first)
    return result_type(
        output_dir=first.output_dir,
        translated_pages=sum(result.translated_pages for result in results),
        skipped_pages=sum(result.skipped_pages for result in results),
        failed_pages=sum(result.failed_pages for result in results),
        stopped_for_quota=any(result.stopped_for_quota for result in results),
    )
