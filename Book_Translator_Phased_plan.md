# End-to-End Book Translator — Phased Plan

The project will run entirely on a local machine. A Google API key will be supplied through a `.env` file and used for language detection and translation.

## Phase 1: Project setup

- Create a Python virtual environment and dependency definition (`requirements.txt` or `pyproject.toml`).
- Store credentials in `.env`; add `.env` to `.gitignore` so the key is never committed.
- Create a predictable working layout:

```text
input/
pages/original/
pages/translated/
output/
temp/
logs/
```

- Recommended libraries:
  - `PyMuPDF` or `pypdf` for PDF operations
  - `Pillow` and OpenCV for image processing
  - `pytesseract` or EasyOCR for scanned-page OCR
  - Google GenAI SDK for language detection and translation
  - `python-dotenv` for loading `.env`

## Phase 2: Input validation and book analysis

- Accept a local PDF path through a Python CLI.
- Validate that the PDF is readable and non-empty.
- Inspect its page count, metadata, dimensions, and orientation.
- Determine whether its pages have selectable text or require OCR.
- Extract samples from multiple pages and detect the source language through the Google API.
- Allow a source-language override when the detection result is uncertain.

## Phase 3: Split the source PDF into individual pages

- Produce one valid PDF per original page.
- Use sequential, stable filenames:

```text
pages/original/page_0001.pdf
pages/original/page_0002.pdf
```

- Save page metadata in JSON or SQLite, including page number, source path, extraction method, detected language, translation status, and errors.
- Make the workflow resumable, so completed pages are not reprocessed after an interruption.

## Phase 4: Extract text page by page

For each individual page:

1. Attempt direct text extraction.
2. If text is missing or sparse, render the page to an image and run OCR.
3. Clean common OCR defects such as broken words, malformed line breaks, and repeated headers or footers.
4. Retain structural cues where practical: headings, paragraphs, lists, quotations, footnotes, and page numbers.

Extraction uses validated direct text first, OCR only for image regions not covered by valid text, and full-page OCR as a fallback for image-only or corrupt text-layer pages. Mixed direct/OCR pages are recorded separately for review.

## Phase 5: Translate to English

- Send one page, or safely sized chunks of a page, to the Google API.
- Use a consistent translation instruction that requires natural English while preserving headings, paragraph breaks, lists, quotations, notes, names, and technical terms.
- Do not summarize or omit content.
- Save source and translated text separately for traceability.
- Add retry logic, rate limiting, exponential backoff, and clear error reporting.
- Cache successful translations locally to prevent duplicate API calls and costs.
- On Google quota exhaustion, try the configured Gemini fallback chain before using the optional local Ollama fallback.

## Phase 6: Generate translated PDFs per page

Implemented with the `book-translator generate-pdfs` command.

- Create one English PDF for each source page:

```text
pages/translated/page_0001_en.pdf
pages/translated/page_0002_en.pdf
```

- Prioritize legible English output in the initial version.
- Retain source-page dimensions, margins, and orientation where feasible.
- Apply word wrapping, sensible typography, and overflow handling.
- Optionally include the original page number in a footer for cross-reference.
- Treat high-fidelity recreation of complex layouts, tables, columns, and illustrations as a later enhancement.

## Phase 7: Quality checks

Implemented with the reusable `book-translator quality-check` command.

- Confirm each source page has a translated output page.
- Detect blank results, excessively short translations, overflow, and failed pages.
- Detect likely source-script output, UTF-8 mojibake, and other non-English translation results.
- Generate a review report listing translated, OCR-derived, skipped, and failed pages.
- Support reprocessing selected page numbers only.

## Phase 8: Merge the translated book

Implemented with the `book-translator merge-book` command.

- Merge translated page PDFs in original page order.
- Create a final output such as:

```text
output/book_translated_english.pdf
```

- Verify the final page count against the original PDF.
- Add PDF metadata identifying the document as an English translation and recording the detected source language.

## Phase 9: Local CLI and usability

Implemented with the `book-translator` CLI and local browser dashboard, including `retry`, `merge`, page selection, aliases, visible step progress, persisted state, and resumable behavior.

Provide commands similar to:

```bash
python main.py translate input/book.pdf
python main.py translate input/book.pdf --language hi
python main.py retry --pages 12,13,14
python main.py merge
```

Useful options include:

- `--language` to override automatic detection
- `--start-page` and `--end-page` to translate a range
- `--resume` to continue a previous job
- `--ocr-engine` to select the OCR engine
- `--output` to choose the destination
- `--keep-intermediate-files` for debugging and review

## Phase 10: Testing and packaging

- Unit-test splitting, sequential naming, text extraction, OCR fallback, job tracking, and merging.
- Test selectable-text books, scanned books, non-English scripts, mixed-language pages, tables, and image-heavy pages.
- Provide a README covering setup, `.env` configuration, commands, supported inputs, and limitations.
- Package the project as a local Python application.

## Recommended delivery order

1. PDF validation, splitting, and merging.
2. Text extraction with OCR fallback.
3. Language detection.
4. Page-level translation and job persistence.
5. Basic translated-page PDF generation.
6. CLI, retries, quality reporting, and automated tests.
7. Advanced layout preservation and formatting.

The first release should focus on accurate text translation and reliable page-by-page processing. Near pixel-perfect replication of original pages is a separate, later stage because it requires robust layout analysis and reconstruction.
