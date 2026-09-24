"""Translate extracted page text to English through the configured Google model."""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from google import genai
from google.genai import types

from book_translator.extractor import load_manifest, page_records_by_number, write_manifest

DEFAULT_MODEL = "gemini-2.5-flash"
DEFAULT_GOOGLE_FALLBACK_MODELS = (
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
)


@dataclass(frozen=True)
class TranslationResult:
    """Summary of one page-level translation run."""

    output_dir: Path
    translated_pages: int
    skipped_pages: int
    failed_pages: int
    stopped_for_quota: bool


class QuotaExceededError(RuntimeError):
    """Raised when Google reports that the configured account quota is exhausted."""


class EmptyTranslationError(RuntimeError):
    """Raised when a provider returns no usable English translation text."""


@dataclass(frozen=True)
class TranslationOutput:
    """Translated content together with the provider and model that produced it."""

    text: str
    provider: str
    model: str


TranslationFunction = Callable[[str, str | None, str], str | TranslationOutput]


def translate_pages(
    pages_dir: Path,
    *,
    text_dir: Path | None = None,
    output_dir: Path | None = None,
    source_language: str | None = None,
    model: str | None = None,
    start_page: int | None = None,
    end_page: int | None = None,
    max_retries: int = 3,
    request_delay: float = 0.25,
    overwrite: bool = False,
    translation_function: TranslationFunction | None = None,
) -> TranslationResult:
    """Translate extracted pages, saving results and status after every page."""
    source_dir = validate_manifest_directory(pages_dir)
    manifest_path = source_dir / "job_manifest.json"
    manifest = load_manifest(manifest_path)
    configured_language = source_language or manifest.get("source_language")
    language_code = validate_language_code(configured_language) if configured_language else None
    if max_retries < 0:
        raise ValueError("--max-retries cannot be negative.")
    if request_delay < 0:
        raise ValueError("--request-delay cannot be negative.")

    source_text_dir = text_dir or Path("pages") / "text" / source_dir.name
    if not source_text_dir.is_dir():
        raise ValueError(f"Extracted-text directory not found: {source_text_dir}")
    destination = output_dir or Path("pages") / "translated_text" / source_dir.name
    destination.mkdir(parents=True, exist_ok=True)
    selected_records = select_page_records(manifest["pages"], start_page, end_page)
    records = page_records_by_number(manifest)
    translate = translation_function or translate_text
    counts = {"translated": 0, "skipped": 0, "failed": 0}
    quota_exhausted = False
    active_google_model = model or configured_model()

    for record in selected_records:
        page_number = record["page_number"]
        source_text_path = source_text_dir / f"page_{page_number:04d}.txt"
        translated_path = destination / f"page_{page_number:04d}_en.txt"
        manifest_record = records[page_number]

        if translated_path.exists() and not overwrite:
            manifest_record.update(
                {
                    "translated_text": str(translated_path.resolve()),
                    "translation_status": "complete",
                    "error": None,
                }
            )
            counts["skipped"] += 1
            write_current_manifest(manifest_path, manifest, records)
            continue

        try:
            source_text = source_text_path.read_text(encoding="utf-8").strip()
            if not source_text:
                raise ValueError("Extracted source text is empty.")
            selected_model = active_google_model
            output = call_with_retries(
                lambda source_text=source_text, language_code=language_code, selected_model=selected_model: require_translation_text(
                    coerce_translation_output(
                        translate(source_text, language_code, selected_model), selected_model
                    )
                ),
                max_retries=max_retries,
            )
            write_text_file(translated_path, output.text)
            if output.provider == "google-genai":
                active_google_model = output.model
            manifest_record.update(
                {
                    "translated_text": str(translated_path.resolve()),
                    "translation_status": "complete",
                    "translation_provider": output.provider,
                    "translation_model": output.model,
                    "error": None,
                }
            )
            counts["translated"] += 1
            if request_delay:
                time.sleep(request_delay)
        except QuotaExceededError as error:
            manifest_record.update(
                {
                    "translation_status": "failed",
                    "error": f"{type(error).__name__}: {safe_error_message(error)}",
                }
            )
            counts["failed"] += 1
            quota_exhausted = True
        except Exception as error:  # noqa: BLE001 - preserve completed pages if one request fails.
            manifest_record.update(
                {
                    "translation_status": "failed",
                    "error": f"{type(error).__name__}: {safe_error_message(error)}",
                }
            )
            counts["failed"] += 1
        finally:
            write_current_manifest(manifest_path, manifest, records)
        if quota_exhausted:
            break

    return TranslationResult(
        output_dir=destination,
        translated_pages=counts["translated"],
        skipped_pages=counts["skipped"],
        failed_pages=counts["failed"],
        stopped_for_quota=quota_exhausted,
    )


def translate_text(
    source_text: str, source_language: str | None, model: str
) -> TranslationOutput:
    """Translate with Google first, then configured free fallbacks after quota exhaustion."""
    load_dotenv()
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key or api_key == "replace_with_your_google_api_key":
        raise ValueError("GOOGLE_API_KEY is not configured.")

    prompt = build_translation_prompt(source_text, source_language)
    last_quota_error: Exception | None = None
    last_empty_response: EmptyTranslationError | None = None
    for google_model in unique_models([model, *configured_google_fallback_models()]):
        try:
            translated_text = request_google_translation(prompt, api_key, google_model)
            if not translated_text.strip():
                raise EmptyTranslationError(f"{google_model} returned an empty response.")
            return TranslationOutput(
                text=translated_text,
                provider="google-genai",
                model=google_model,
            )
        except Exception as error:
            if isinstance(error, EmptyTranslationError):
                last_empty_response = error
                continue
            if not is_quota_error(error):
                raise
            last_quota_error = error

    ollama_model = os.getenv("OLLAMA_FALLBACK_MODEL", "").strip()
    if ollama_model:
        try:
            return TranslationOutput(
                text=request_ollama_translation(prompt, ollama_model),
                provider="ollama",
                model=ollama_model,
            )
        except (OSError, RuntimeError, ValueError, URLError):
            pass

    if last_quota_error is not None:
        raise QuotaExceededError("Google quota is exhausted and no local fallback is available.") from last_quota_error
    if last_empty_response is not None:
        raise last_empty_response
    raise RuntimeError("No translation model is configured.")


def build_translation_prompt(source_text: str, source_language: str | None) -> str:
    """Build a provider-neutral brief for faithful, natural translation."""
    language_label = source_language or "the detected source language"
    return f"""You are an expert literary translator. Translate this {language_label} book page into natural, fluent English.

Preserve the full meaning, context, intent, tone, imagery, and factual details of every sentence and paragraph. Translate idiomatically for an English reader; do not perform a word-for-word translation. Preserve the original reading order, headings, paragraph breaks, lists, quotations, dialogue, and emphasis where represented in plain text. Do not summarize, simplify, censor, add commentary, or invent details.

When the source repeats an English term only as a redundant explanatory gloss, you may retain it once and omit later purely redundant copies. Do not remove repetition that is meaningful, narrative, stylistic, instructional, or needed for clarity.

Terminology consistency is mandatory across all pages. Preserve established character names and titles exactly once chosen. Do not vary spellings or titles between pages.

For this book, transliterate the character name as "Ravji" (never "Rawji") and translate the military title as "Commander" (never "Commandant"), unless the source clearly refers to a different name or rank.

Output only the translated English page text. Do not add a title, notes, labels, or an explanation.

SOURCE TEXT:
{source_text}"""


def request_google_translation(prompt: str, api_key: str, model: str) -> str:
    """Send one translation request to a specific Google model."""
    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        ),
    )
    return (response.text or "").strip()


def request_ollama_translation(prompt: str, model: str) -> str:
    """Use a locally running Ollama model without transmitting book text externally."""
    host = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
    payload = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "options": {"temperature": 0.2},
        }
    ).encode("utf-8")
    request = Request(
        f"{host}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=300) as response:
            data = json.loads(response.read().decode("utf-8"))
    except URLError as error:
        raise RuntimeError(
            "Ollama is unavailable. Install and run Ollama, then pull the configured fallback model."
        ) from error
    text = data.get("message", {}).get("content")
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError("Ollama returned an empty translation response.")
    return text.strip()


def call_with_retries(
    operation: Callable[[], str | TranslationOutput], *, max_retries: int
) -> str | TranslationOutput:
    """Retry a failing API operation with exponential backoff."""
    for attempt in range(max_retries + 1):
        try:
            return operation()
        except QuotaExceededError:
            raise
        except Exception as error:
            if is_quota_error(error):
                raise QuotaExceededError("Google API quota is exhausted.") from error
            if attempt == max_retries:
                raise
            time.sleep(2**attempt)
    raise RuntimeError("Translation retry loop ended unexpectedly.")


def configured_model() -> str:
    """Load the configured model name without exposing the API key."""
    load_dotenv()
    return os.getenv("GOOGLE_GENAI_MODEL", DEFAULT_MODEL)


def configured_google_fallback_models() -> list[str]:
    """Read comma-separated Google alternatives, ordered by preference."""
    default_models = ",".join(DEFAULT_GOOGLE_FALLBACK_MODELS)
    return [
        model.strip()
        for model in os.getenv("GOOGLE_FALLBACK_MODELS", default_models).split(",")
        if model.strip()
    ]


def unique_models(models: list[str]) -> list[str]:
    """Remove empty or duplicate model names while preserving the requested order."""
    return list(dict.fromkeys(model.strip() for model in models if model.strip()))


def coerce_translation_output(
    value: str | TranslationOutput, default_model: str
) -> TranslationOutput:
    """Normalize test or custom translators that return only translated text."""
    if isinstance(value, TranslationOutput):
        return value
    return TranslationOutput(text=value, provider="custom", model=default_model)


def require_translation_text(output: TranslationOutput) -> TranslationOutput:
    """Treat an empty response as retryable rather than a completed API request."""
    if not output.text.strip():
        raise EmptyTranslationError("The translation response was empty.")
    return output


def validate_manifest_directory(pages_dir: Path) -> Path:
    """Validate the page directory produced by the split phase."""
    directory = pages_dir.expanduser()
    if not directory.is_dir() or not (directory / "job_manifest.json").is_file():
        raise ValueError("A split-page directory containing job_manifest.json is required.")
    return directory


def validate_language_code(value: object) -> str:
    """Validate a manifest or command-line source language code."""
    if not isinstance(value, str) or not re.fullmatch(r"[a-z]{2,3}", value.strip().lower()):
        raise ValueError("A two- or three-letter source language code is required.")
    return value.strip().lower()


def select_page_records(
    records: list[dict[str, Any]], start_page: int | None, end_page: int | None
) -> list[dict[str, Any]]:
    """Select a validated inclusive page range from manifest records."""
    if start_page is not None and start_page < 1:
        raise ValueError("--start-page must be at least 1.")
    if end_page is not None and end_page < 1:
        raise ValueError("--end-page must be at least 1.")
    if start_page is not None and end_page is not None and start_page > end_page:
        raise ValueError("--start-page cannot exceed --end-page.")
    selected = [
        record
        for record in records
        if isinstance(record, dict)
        and isinstance(record.get("page_number"), int)
        and (start_page is None or record["page_number"] >= start_page)
        and (end_page is None or record["page_number"] <= end_page)
    ]
    if not selected:
        raise ValueError("No pages match the requested page range.")
    return selected


def write_current_manifest(
    manifest_path: Path, manifest: dict[str, Any], records: dict[int, dict[str, Any]]
) -> None:
    """Persist manifest updates in page order after each translation attempt."""
    manifest["pages"] = [records[number] for number in sorted(records)]
    write_manifest(manifest_path, manifest)


def write_text_file(path: Path, text: str) -> None:
    """Atomically write a translated page as UTF-8 text."""
    temporary_path = path.with_suffix(".partial.txt")
    temporary_path.write_text(text.strip() + "\n", encoding="utf-8")
    temporary_path.replace(path)


def safe_error_message(error: Exception) -> str:
    """Provide a concise operational error without retaining request content."""
    return str(error).replace("\n", " ").strip()[:240] or "no additional detail"


def is_quota_error(error: Exception) -> bool:
    """Identify quota exhaustion responses that should halt, not retry, a job."""
    message = str(error).upper()
    return "429" in message and ("RESOURCE_EXHAUSTED" in message or "QUOTA" in message)
