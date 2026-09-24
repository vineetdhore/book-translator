# Book Translator

A local Python workflow for splitting a book PDF into pages, detecting its source language, translating each page to English, and combining the translated pages into one PDF.

## Prerequisites

- Python 3.11 or newer
- A Google API key
- Tesseract OCR installed and available on your system `PATH` (needed for scanned PDFs)

## Setup

Create and activate a virtual environment in PowerShell:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Install the project and development tools:

```powershell
python -m pip install --upgrade pip
pip install -e ".[dev]"
```

Configure the Google API key:

```powershell
Copy-Item .env.example .env
```

Open `.env` and replace `replace_with_your_google_api_key` with the provided key. The `.env` file is excluded from Git.

## Working directories

| Directory | Purpose |
| --- | --- |
| `input/` | Source book PDFs |
| `pages/original/` | One-page PDFs split from a source book |
| `pages/translated/` | One-page English translation PDFs |
| `output/` | Final merged translated PDFs |
| `temp/` | Temporary rendered images and processing data |
| `logs/` | Runtime logs and job reports |

## Status

## Analyze a source PDF

Run the Phase 2 analysis command before processing a book:

```powershell
book-translator analyze "input\झुंज.pdf" --report "logs\analysis.json"
```

The command validates the PDF, inspects every page, identifies pages likely to need OCR, and detects the source language from representative selectable-text samples. Add `--skip-language-detection` to perform only local inspection. Use `--source-language mr` (or another ISO 639 code) to override automatic detection.

## Split a source PDF into individual pages

Create a one-page PDF for every page in a source book:

```powershell
book-translator split "input\झुंज.pdf" --source-language mr
```

The output is stored in `pages/original/<book name>/` as `page_0001.pdf`, `page_0002.pdf`, and so on. A `job_manifest.json` in the same directory records each page's processing and translation status, allowing subsequent phases to resume page by page. Existing output is protected; pass `--overwrite` only when replacing it is intended.

## Extract text from split pages

Extract selectable text from every split page, with local OCR fallback for sparse or image-only pages:

```powershell
book-translator extract "pages\original\झुंज"
```

Text is saved as UTF-8 files in `pages/text/<book name>/`. The job manifest is updated after every page, so rerunning the command skips completed pages. For scanned books, install Tesseract and use an installed language pack such as `--tesseract-language mar` for Marathi OCR.

## Translate extracted text to English

Translate every extracted page with the configured Google model:

```powershell
book-translator translate "pages\original\झुंज"
```

English page text is saved in `pages/translated_text/<book name>/`. Translation is page-by-page and resumable: completed outputs are skipped by default, while failures are recorded in the job manifest. The translation instruction preserves meaning, context, tone, sentence relationships, and document structure; it asks for fluent English rather than word-for-word substitution. Use `--start-page` and `--end-page` to retry a range, or `--overwrite` to deliberately translate existing output again.

If Google reports exhausted API quota, the command stops immediately after that page instead of consuming further requests. When quota is available again, rerun the same command; completed pages are skipped and failed or pending pages resume.

## Generate translated PDFs

Generate one English PDF for each translated page:

```powershell
book-translator generate-pdfs "pages\original\झुंज"
```

PDFs are saved in `pages/translated/<book name>/` as `page_0001_en.pdf`, `page_0002_en.pdf`, and so on. Each PDF uses the corresponding source page dimensions and records its PDF-generation status in `job_manifest.json`. Long translations flow onto additional pages in the same output PDF instead of being truncated. Existing PDFs are skipped; use `--overwrite` to regenerate them. Use `--start-page` and `--end-page` to generate a selected range.

## Free-model fallbacks

When the primary Google model returns a quota-exhaustion response, the translator tries each distinct model in the comma-separated `GOOGLE_FALLBACK_MODELS` list from `.env`. The recommended default chain is `gemini-3.5-flash`, `gemini-3.5-flash-lite`, `gemini-2.5-flash`, and `gemini-2.5-flash-lite`. The primary model is automatically skipped if it appears again in the fallback list. Each model has separate availability and rate limits determined by the Google account; if all Google models are exhausted, the optional Ollama fallback is attempted.

For a true no-API-quota fallback, install [Ollama](https://ollama.com/) and download its local translation model:

```powershell
ollama pull translategemma:4b
```

Then add this to `.env`:

```text
OLLAMA_FALLBACK_MODEL=translategemma:4b
```

`translategemma:4b` is the recommended local option because it is purpose-built for translation. Other free local choices are `gemma3:4b` for broad multilingual coverage and `qwen2.5:7b` when more local hardware is available. Ollama keeps source text on the local machine.

## Run quality checks

Run the reusable Phase 7 checker for a book:

```powershell
book-translator quality-check "pages\original\झुंज"
```

The report is saved by default to `logs/quality_<book name>.json`. It checks that translated text and PDFs exist, detects blank or excessively short translations, invalid or empty PDFs, overflow PDFs, failed pages, missing outputs, UTF-8 mojibake, and likely source-script/non-English output. It also lists translated, OCR-derived, skipped, failed, and non-English page numbers and records per-page quality status in `job_manifest.json`.

Check only selected pages when reprocessing or reviewing a subset:

```powershell
book-translator quality-check "pages\original\झुंज" --pages 12,13,14
book-translator quality-check "pages\original\झुंज" --start-page 60 --end-page 70
```

Use `--min-translated-characters` to change the short-translation threshold, `--min-latin-letter-ratio` to change the English-script threshold, and `--report` to choose a different JSON report path.

## Merge the translated book

After all translated page PDFs pass quality checks, merge them in source order:

```powershell
book-translator merge-book "pages\original\झुंज"
```

The merged PDF is saved by default to `output/झुंज_translated_english.pdf`. The command requires one valid translated PDF for every source page, verifies the manifest page count, preserves overflow pages, and writes PDF metadata identifying the result as an English translation with the recorded source language. Use `--output` to choose another path or `--overwrite` to replace an existing output.

## CLI usability

The workflow is resumable and can be run phase by phase:

```powershell
book-translator analyze "input\book.pdf" --source-language mr
book-translator split "input\book.pdf" --source-language mr
book-translator extract "pages\original\book" --ocr-engine tesseract
book-translator translate "pages\original\book" --language mr --start-page 1 --end-page 10 --resume
book-translator generate-pdfs "pages\original\book" --start-page 1 --end-page 10
book-translator quality-check "pages\original\book"
book-translator merge "pages\original\book" --output "output\book_translated_english.pdf"
```

Completed translation and extraction files are skipped by default; `--resume` makes that behavior explicit. Retry selected pages with:

```powershell
book-translator retry "pages\original\book" --pages 12,13,14 --language mr
```

`--output` is accepted as a shortcut for translation output directories and merged PDF paths. Intermediate files are retained by default; `--keep-intermediate-files` is accepted for scripts that make retention policy explicit. The current OCR engine is Tesseract, selected with `--ocr-engine tesseract`.

## Browser dashboard

Launch the local control room:

```powershell
book-translator ui
```

Open `http://127.0.0.1:8765/` in a browser. The dashboard connects the analyze, split, extract, translate, PDF, quality-check, and merge phases in one Start workflow. Optional fields are populated with the project defaults, including the configured model, sample-page count, OCR language, retry count, request delay, PDF font size, and quality threshold.

The dashboard displays the detected page count and source language after analysis, per-step status and details, translated/skipped/failed counts, a progress percentage, and a live event log. State is saved to `logs/ui_state.json`; completed files remain on disk as checkpoints, so starting again after an interruption resumes completed phases and pages. Reset clears the dashboard state and fields but intentionally leaves existing page artifacts untouched so they can be resumed.

Use `--port` to choose another local port or `--state-file` to keep separate workflow dashboards:

```powershell
book-translator ui --port 8766 --state-file logs\another_book_ui.json
```
