import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from book_translator.web_ui import (
    HTML,
    DashboardHandler,
    WorkflowStore,
    configured_model,
    normalize_config,
)


def test_normalize_config_populates_optional_defaults(tmp_path: Path) -> None:
    config = normalize_config({"source_pdf": str(tmp_path / "novel.pdf")})

    assert config["model"] == configured_model()
    assert config["pages_dir"] == str(Path("pages") / "original" / "novel")
    assert config["text_dir"] == str(Path("pages") / "text" / "novel")
    assert config["sample_pages"] == 3
    assert config["max_retries"] == 3
    assert config["request_delay"] == 0.25
    assert config["font_size"] == 11
    assert config["min_translated_characters"] == 100


def test_normalize_config_replaces_generic_paths_and_unknown_language(tmp_path: Path) -> None:
    config = normalize_config(
        {
            "source_pdf": str(tmp_path / "झुंज.pdf"),
            "source_language": "unknown",
            "pages_dir": "pages/original/book",
            "text_dir": "pages/text/book",
            "translated_text_dir": "pages/translated_text/book",
            "translated_pdf_dir": "pages/translated/book",
        }
    )

    assert config["source_language"] is None
    assert config["pages_dir"].endswith("pages\\original\\झुंज") or config["pages_dir"].endswith("pages/original/झुंज")
    assert config["translated_pdf_dir"].endswith("pages\\translated\\झुंज") or config["translated_pdf_dir"].endswith("pages/translated/झुंज")


def test_workflow_store_persists_and_resets_state(tmp_path: Path) -> None:
    state_path = tmp_path / "ui_state.json"
    store = WorkflowStore(state_path)
    store.update(config={"source_pdf": "book.pdf"})
    store.step("analyze", "running", "inspecting")
    started_at = store.snapshot()["steps"]["analyze"]["started_at"]
    store.step("analyze", "complete", "2 pages", page_count=2)
    store.message("Analyze complete")

    reloaded = WorkflowStore(state_path)
    assert reloaded.snapshot()["steps"]["analyze"]["page_count"] == 2
    assert reloaded.snapshot()["steps"]["analyze"]["started_at"] == started_at
    assert reloaded.snapshot()["steps"]["analyze"]["ended_at"]
    assert reloaded.snapshot()["messages"] == [reloaded.snapshot()["messages"][0]]
    reloaded.reset()
    assert reloaded.snapshot()["steps"] == {}
    assert not reloaded.snapshot()["messages"]


def test_dashboard_serves_html_and_state_api(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "ui_state.json")
    runner = type("Runner", (), {})()
    DashboardHandler.store = store
    DashboardHandler.runner = runner
    server = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        with urllib.request.urlopen(base, timeout=2) as response:
            html = response.read().decode("utf-8")
        with urllib.request.urlopen(base + "/api/state", timeout=2) as response:
            state = json.loads(response.read().decode("utf-8"))
        assert "Book Translator Control Room" in html
        assert 'id="languageCode"' in html
        assert "analyze.source_language" in html
        assert 'id="qualityReport"' in html
        assert "summary.failed" in html
        assert 'for="quality_report"' not in html
        assert state["running"] is False
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_dashboard_does_not_reapply_config_on_every_poll() -> None:
    assert "if(!hydrated){applyConfig(state.config||defaults);hydrated=true}" in HTML
    assert "if(state.config)applyConfig(state.config)" not in HTML


def test_quality_report_is_not_a_configuration_field() -> None:
    assert 'for="quality_report"' in HTML
