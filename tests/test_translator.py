import json
from pathlib import Path

import pymupdf
import pytest

from book_translator.extractor import extract_pages
from book_translator.splitter import split_pdf
from book_translator.translator import (
    DEFAULT_GOOGLE_FALLBACK_MODELS,
    QuotaExceededError,
    build_translation_prompt,
    call_with_retries,
    configured_google_fallback_models,
    translate_pages,
    translate_text,
)


def make_source_pdf(path: Path, text: str) -> Path:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), text)
    document.save(path)
    document.close()
    return path


def test_translate_pages_saves_english_text_and_updates_manifest(tmp_path: Path) -> None:
    source = make_source_pdf(tmp_path / "book.pdf", "Source text long enough for extraction.")
    split = split_pdf(source, output_dir=tmp_path / "original", source_language="mr")
    extract_pages(split.output_dir, output_dir=tmp_path / "text")

    result = translate_pages(
        split.output_dir,
        text_dir=tmp_path / "text",
        output_dir=tmp_path / "english",
        request_delay=0,
        translation_function=lambda text, language, model: "Faithful English translation.",
    )

    assert result.translated_pages == 1
    assert (tmp_path / "english" / "page_0001_en.txt").read_text(encoding="utf-8").strip() == (
        "Faithful English translation."
    )
    manifest = json.loads(split.manifest_path.read_text(encoding="utf-8"))
    assert manifest["pages"][0]["translation_status"] == "complete"


def test_translation_retries_transient_failure(monkeypatch) -> None:
    outcomes = iter([RuntimeError("temporary"), "Translated"])
    monkeypatch.setattr("book_translator.translator.time.sleep", lambda _: None)

    def operation() -> str:
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    assert call_with_retries(operation, max_retries=1) == "Translated"


def test_translation_stops_immediately_for_quota_exhaustion(monkeypatch) -> None:
    attempts = 0
    monkeypatch.setattr("book_translator.translator.time.sleep", lambda _: None)

    def quota_error() -> str:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("429 RESOURCE_EXHAUSTED: quota")

    with pytest.raises(QuotaExceededError):
        call_with_retries(quota_error, max_retries=3)
    assert attempts == 1


def test_translation_uses_google_fallback_after_primary_quota(monkeypatch) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    monkeypatch.setenv("GOOGLE_FALLBACK_MODELS", "fallback-model")
    attempted_models: list[str] = []

    def fake_google_request(prompt: str, api_key: str, model: str) -> str:
        attempted_models.append(model)
        if model == "primary-model":
            raise RuntimeError("429 RESOURCE_EXHAUSTED")
        return "Faithful English."

    monkeypatch.setattr("book_translator.translator.request_google_translation", fake_google_request)

    output = translate_text("मजकूर", "mr", "primary-model")

    assert output.text == "Faithful English."
    assert output.model == "fallback-model"
    assert attempted_models == ["primary-model", "fallback-model"]


def test_translation_uses_all_configured_google_fallback_models(monkeypatch) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    monkeypatch.setenv(
        "GOOGLE_FALLBACK_MODELS",
        "gemini-3.5-flash,gemini-3.5-flash-lite,gemini-2.5-flash,gemini-2.5-flash-lite",
    )
    attempted_models: list[str] = []

    def fake_google_request(prompt: str, api_key: str, model: str) -> str:
        attempted_models.append(model)
        if model != "gemini-2.5-flash-lite":
            raise RuntimeError("429 RESOURCE_EXHAUSTED")
        return "Faithful English."

    monkeypatch.setattr("book_translator.translator.request_google_translation", fake_google_request)

    output = translate_text("मजकूर", "mr", "gemini-2.5-flash")

    assert output.model == "gemini-2.5-flash-lite"
    assert attempted_models == [
        "gemini-2.5-flash",
        "gemini-3.5-flash",
        "gemini-3.5-flash-lite",
        "gemini-2.5-flash-lite",
    ]


def test_default_google_fallback_models_include_requested_models(monkeypatch) -> None:
    monkeypatch.delenv("GOOGLE_FALLBACK_MODELS", raising=False)

    assert configured_google_fallback_models() == list(DEFAULT_GOOGLE_FALLBACK_MODELS)


def test_translation_prompt_contains_canonical_book_terminology() -> None:
    prompt = build_translation_prompt("Ravji Commander", "mr")

    assert '"Ravji" (never "Rawji")' in prompt
    assert '"Commander" (never "Commandant")' in prompt
