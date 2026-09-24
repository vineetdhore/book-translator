"""Local browser dashboard for the book translation workflow."""

from __future__ import annotations

import json
import threading
import traceback
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from book_translator.analyzer import analyze_pdf
from book_translator.extractor import extract_pages
from book_translator.merger import merge_translated_book
from book_translator.pdf_generator import generate_translated_pdfs
from book_translator.quality_checker import check_translation_quality
from book_translator.splitter import split_pdf
from book_translator.translator import configured_model, translate_pages

DEFAULT_UI_STATE_PATH = Path("logs") / "ui_state.json"


@dataclass
class WorkflowState:
    """Persisted dashboard state and the latest workflow progress."""

    config: dict[str, Any] = field(default_factory=dict)
    steps: dict[str, dict[str, Any]] = field(default_factory=dict)
    messages: list[str] = field(default_factory=list)
    running: bool = False
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None


class WorkflowStore:
    """Thread-safe JSON-backed state store for the local dashboard."""

    def __init__(self, path: Path = DEFAULT_UI_STATE_PATH) -> None:
        self.path = path.expanduser()
        self.lock = threading.RLock()
        self.state = self.load()

    def load(self) -> WorkflowState:
        if not self.path.is_file():
            return WorkflowState()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            return WorkflowState(**payload)
        except (OSError, TypeError, ValueError):
            return WorkflowState()

    def save(self) -> None:
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".partial.json")
            temporary.write_text(
                json.dumps(asdict(self.state), indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            temporary.replace(self.path)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return asdict(self.state)

    def reset(self) -> None:
        with self.lock:
            self.state = WorkflowState()
            self.save()

    def update(self, **values: Any) -> None:
        with self.lock:
            for key, value in values.items():
                setattr(self.state, key, value)
            self.save()

    def message(self, text: str) -> None:
        with self.lock:
            timestamp = datetime.now(UTC).strftime("%H:%M:%S")
            self.state.messages.append(f"{timestamp}  {text}")
            self.state.messages = self.state.messages[-80:]
            self.save()

    def step(self, name: str, status: str, detail: str = "", **extra: Any) -> None:
        with self.lock:
            now = datetime.now(UTC).isoformat()
            previous = self.state.steps.get(name, {})
            started_at = now if status == "running" or not previous.get("started_at") else previous["started_at"]
            ended_at = now if status in {"complete", "warning", "failed"} else None
            self.state.steps[name] = {
                "status": status,
                "detail": detail,
                "started_at": started_at,
                "ended_at": ended_at,
                "updated_at": now,
                **extra,
            }
            self.save()


class WorkflowRunner:
    """Run the existing pipeline one phase at a time in a background thread."""

    def __init__(self, store: WorkflowStore) -> None:
        self.store = store
        self.thread: threading.Thread | None = None

    def start(self, config: dict[str, Any]) -> None:
        with self.store.lock:
            if self.store.state.running:
                raise ValueError("A translation workflow is already running.")
            self.store.state.config = config
            self.store.state.running = True
            self.store.state.started_at = datetime.now(UTC).isoformat()
            self.store.state.finished_at = None
            self.store.state.error = None
            self.store.state.messages = []
            self.store.save()
        self.thread = threading.Thread(target=self.run, args=(config,), daemon=True)
        self.thread.start()

    def run(self, config: dict[str, Any]) -> None:
        try:
            source_pdf = Path(config["source_pdf"]).expanduser()
            pages_dir = Path(config["pages_dir"]).expanduser()
            text_dir = Path(config["text_dir"]).expanduser()
            translated_text_dir = Path(config["translated_text_dir"]).expanduser()
            translated_pdf_dir = Path(config["translated_pdf_dir"]).expanduser()
            output_pdf = Path(config["output_pdf"]).expanduser()
            language = config.get("source_language") or None
            start_page = config.get("start_page")
            end_page = config.get("end_page")
            overwrite = bool(config.get("overwrite"))

            self.store.step("analyze", "running", "Inspecting source PDF")
            analysis = analyze_pdf(
                source_pdf,
                detect_language=not bool(language),
                sample_pages=int(config["sample_pages"]),
                source_language=language,
            )
            detected_language = analysis.language_detection.language_code or language or None
            detected_language_name = analysis.language_detection.language or ""
            display_language = (
                f"{detected_language_name} ({detected_language})"
                if detected_language_name and detected_language
                else detected_language_name or detected_language or "not detected"
            )
            self.store.update(config={**config, "source_language": detected_language or ""})
            self.store.step(
                "analyze",
                "complete",
                f"{analysis.page_count} pages; {analysis.extraction_summary}",
                page_count=analysis.page_count,
                source_language=display_language,
                source_language_name=detected_language_name,
                source_language_code=detected_language or "",
            )
            self.store.message(f"Analyze complete: {analysis.page_count} pages, language {display_language}.")

            self.store.step("split", "running", "Creating or resuming page files")
            if pages_dir.is_dir() and (pages_dir / "job_manifest.json").is_file() and not overwrite:
                manifest = json.loads((pages_dir / "job_manifest.json").read_text(encoding="utf-8"))
                split_count = int(manifest.get("page_count", analysis.page_count))
                self.store.step("split", "complete", f"Resumed existing split job with {split_count} pages.", page_count=split_count)
            else:
                split = split_pdf(source_pdf, output_dir=pages_dir, source_language=detected_language, overwrite=overwrite)
                self.store.step("split", "complete", f"Created {split.page_count} page PDFs.", page_count=split.page_count)
            self.store.message("Split complete.")

            self.store.step("extract", "running", "Extracting text with OCR fallback")
            extraction = extract_pages(
                pages_dir,
                output_dir=text_dir,
                force_ocr=bool(config["force_ocr"]),
                tesseract_language=config["tesseract_language"],
                overwrite=overwrite,
            )
            self.store.step(
                "extract",
                "complete" if extraction.failed_pages == 0 else "warning",
                f"{extraction.direct_text_pages} direct, {extraction.ocr_pages} OCR, {extraction.failed_pages} failed.",
                direct_text_pages=extraction.direct_text_pages,
                ocr_pages=extraction.ocr_pages,
                failed_pages=extraction.failed_pages,
            )
            self.store.message("Text extraction complete.")

            self.store.step("translate", "running", "Translating pages; completed pages are skipped")
            translation = translate_pages(
                pages_dir,
                text_dir=text_dir,
                output_dir=translated_text_dir,
                source_language=detected_language,
                model=config["model"],
                start_page=start_page,
                end_page=end_page,
                max_retries=int(config["max_retries"]),
                request_delay=float(config["request_delay"]),
                overwrite=overwrite,
            )
            translation_status = "complete" if translation.failed_pages == 0 else "warning"
            self.store.step(
                "translate",
                translation_status,
                f"{translation.translated_pages} translated, {translation.skipped_pages} skipped, {translation.failed_pages} failed.",
                translated_pages=translation.translated_pages,
                skipped_pages=translation.skipped_pages,
                failed_pages=translation.failed_pages,
            )
            self.store.message("Translation step complete.")

            self.store.step("pdf", "running", "Rendering translated page PDFs")
            pdfs = generate_translated_pdfs(
                pages_dir,
                translated_text_dir=translated_text_dir,
                output_dir=translated_pdf_dir,
                start_page=start_page,
                end_page=end_page,
                overwrite=overwrite,
                font_size=float(config["font_size"]),
            )
            self.store.step(
                "pdf",
                "complete" if pdfs.failed_pages == 0 else "warning",
                f"{pdfs.generated_pages} generated, {pdfs.skipped_pages} skipped, {pdfs.failed_pages} failed.",
                generated_pages=pdfs.generated_pages,
                skipped_pages=pdfs.skipped_pages,
                failed_pages=pdfs.failed_pages,
            )
            self.store.message("Translated page PDFs complete.")

            self.store.step("quality", "running", "Checking translated outputs")
            quality = check_translation_quality(
                pages_dir,
                translated_text_dir=translated_text_dir,
                translated_pdf_dir=translated_pdf_dir,
                report_path=Path(config["quality_report"]),
                start_page=start_page,
                end_page=end_page,
                min_translated_characters=int(config["min_translated_characters"]),
            )
            quality_summary: dict[str, Any] = {}
            if quality.report_path.is_file():
                quality_payload = json.loads(quality.report_path.read_text(encoding="utf-8"))
                quality_summary = quality_payload.get("summary", {})
            self.store.step(
                "quality",
                "complete" if quality.failed_pages == 0 else "warning",
                f"{quality.passed_pages} passed, {quality.failed_pages} failed; report saved.",
                report_path=str(quality.report_path),
                failed_pages=quality.failed_pages,
                summary=quality_summary,
            )
            self.store.message("Quality check complete.")

            self.store.step("merge", "running", "Merging translated book")
            merge = merge_translated_book(
                pages_dir,
                translated_pdf_dir=translated_pdf_dir,
                output_path=output_pdf,
                overwrite=overwrite,
            )
            self.store.step(
                "merge",
                "complete",
                f"{merge.output_page_count} output pages from {merge.source_page_count} source pages.",
                output_path=str(merge.output_path),
                output_page_count=merge.output_page_count,
            )
            self.store.message(f"Workflow complete: {merge.output_path}")
            self.store.update(running=False, finished_at=datetime.now(UTC).isoformat())
        except Exception as error:  # noqa: BLE001 - surface workflow failures in the dashboard.
            self.store.step("current", "failed", str(error))
            self.store.update(
                running=False,
                finished_at=datetime.now(UTC).isoformat(),
                error=f"{type(error).__name__}: {error}",
            )
            self.store.message("Workflow stopped with an error.")
            traceback.print_exc()


HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Book Translator Control Room</title>
<style>
:root{--ink:#17202a;--muted:#69737d;--paper:#f5f1e8;--panel:#fffdf8;--line:#ddd5c7;--accent:#c64b32;--teal:#176b67;--gold:#d69e2e;--ok:#287a52;--bad:#a83b36}*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 10% 0,#fff8df 0,transparent 34%),linear-gradient(135deg,#f6f1e7,#e8eeea);color:var(--ink);font-family:Georgia,serif}button,input,select{font:inherit}button{cursor:pointer}.shell{max-width:1440px;margin:auto;padding:28px}.mast{display:flex;justify-content:space-between;gap:24px;align-items:end;margin-bottom:24px}.eyebrow{font:700 11px Arial,sans-serif;letter-spacing:2px;text-transform:uppercase;color:var(--accent)}h1{font-size:clamp(32px,5vw,68px);line-height:.94;margin:8px 0 0;max-width:760px}.lede{max-width:450px;color:var(--muted);line-height:1.5}.layout{display:grid;grid-template-columns:minmax(340px,1fr) minmax(420px,1.25fr);gap:18px}.panel{background:color-mix(in srgb,var(--panel) 92%,transparent);border:1px solid var(--line);box-shadow:0 12px 32px #4a3b2412;padding:22px}.panel h2{font-size:22px;margin:0 0 16px}.formgrid{display:grid;grid-template-columns:1fr 1fr;gap:13px}.field{display:flex;flex-direction:column;gap:6px}.field.full{grid-column:1/-1}.field label{font:700 11px Arial,sans-serif;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}input,select{width:100%;border:1px solid var(--line);background:#fffefa;padding:10px;color:var(--ink);border-radius:3px}input:focus,select:focus{outline:2px solid #d69e2e66;border-color:var(--gold)}.checks{display:flex;gap:18px;flex-wrap:wrap;margin-top:16px;font:14px Arial,sans-serif}.checks label{display:flex;gap:7px;align-items:center}.actions{display:flex;gap:10px;margin-top:20px}.primary,.secondary{border:0;padding:12px 18px;font:700 13px Arial,sans-serif;text-transform:uppercase;letter-spacing:.08em}.primary{background:var(--accent);color:white}.secondary{background:#e6e0d3;color:var(--ink)}button:disabled{opacity:.45;cursor:not-allowed}.notice{margin-top:14px;min-height:22px;color:var(--muted);font:13px Arial,sans-serif}.metrics{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin-bottom:18px}.metric{border-top:3px solid var(--teal);padding:11px;background:#f4f0e7}.metric strong{display:block;font-size:27px}.metric span{font:11px Arial,sans-serif;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}.steps{display:grid;gap:9px}.step{display:grid;grid-template-columns:30px 1fr auto;gap:10px;align-items:center;border:1px solid var(--line);padding:11px;background:#fffefa}.stepnum{width:27px;height:27px;border-radius:50%;display:grid;place-items:center;background:#e9e4d8;font:700 12px Arial,sans-serif}.step.running{border-color:var(--gold);background:#fff8de}.step.complete{border-color:#b7d8c5}.step.warning{border-color:#e2c783}.step.failed{border-color:#e4aaa3}.step.complete .stepnum{background:var(--ok);color:#fff}.step.failed .stepnum{background:var(--bad);color:#fff}.stepname{font-weight:700}.detail{font:13px Arial,sans-serif;color:var(--muted);margin-top:3px}.timing{font:11px/1.45 Arial,sans-serif;color:var(--muted);margin-top:5px}.badge{font:700 10px Arial,sans-serif;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}.complete .badge{color:var(--ok)}.running .badge{color:var(--gold)}.failed .badge{color:var(--bad)}.log{margin-top:18px;background:#202b2d;color:#dce9dd;padding:14px;height:180px;overflow:auto;font:12px/1.6 Consolas,monospace;white-space:pre-wrap}.resume{margin-top:18px;padding:12px;background:#eef5ee;border-left:4px solid var(--teal);font:13px/1.5 Arial,sans-serif;color:#315a4d}@media(max-width:900px){.layout{grid-template-columns:1fr}.mast{display:block}.lede{margin-top:15px}.metrics{grid-template-columns:repeat(2,1fr)}}@media(max-width:540px){.shell{padding:16px}.formgrid{grid-template-columns:1fr}.field.full{grid-column:auto}}
</style></head>
<body><main class="shell"><header class="mast"><div><div class="eyebrow">Local workflow / control room</div><h1>Turn a book into English.</h1></div><p class="lede">Configure the run once. Watch each phase report back. Come back later and resume from the artifacts already on disk.</p></header><div class="layout"><section class="panel"><h2>Run configuration</h2><form id="config"><div class="formgrid"><div class="field full"><label for="source_pdf">Source PDF</label><input id="source_pdf" name="source_pdf" placeholder="input\\book.pdf" required></div><div class="field"><label for="source_language">Source language</label><input id="source_language" name="source_language" placeholder="Auto-detect"></div><div class="field"><label for="model">Primary model</label><input id="model" name="model"></div><div class="field"><label for="pages_dir">Page workspace</label><input id="pages_dir" name="pages_dir"></div><div class="field"><label for="text_dir">Extracted text</label><input id="text_dir" name="text_dir"></div><div class="field"><label for="translated_text_dir">English text</label><input id="translated_text_dir" name="translated_text_dir"></div><div class="field"><label for="translated_pdf_dir">English page PDFs</label><input id="translated_pdf_dir" name="translated_pdf_dir"></div><div class="field full"><label for="output_pdf">Final merged PDF</label><input id="output_pdf" name="output_pdf"></div><div class="field"><label for="start_page">Start page</label><input id="start_page" name="start_page" type="number" min="1" placeholder="All"></div><div class="field"><label for="end_page">End page</label><input id="end_page" name="end_page" type="number" min="1" placeholder="All"></div><div class="field"><label for="sample_pages">Language samples</label><input id="sample_pages" name="sample_pages" type="number" min="1"></div><div class="field"><label for="tesseract_language">OCR language</label><input id="tesseract_language" name="tesseract_language"></div><div class="field"><label for="max_retries">Max retries</label><input id="max_retries" name="max_retries" type="number" min="0"></div><div class="field"><label for="request_delay">Request delay (sec)</label><input id="request_delay" name="request_delay" type="number" min="0" step="0.05"></div><div class="field"><label for="font_size">PDF font size</label><input id="font_size" name="font_size" type="number" min="1" step="0.5"></div><div class="field"><label for="min_translated_characters">Minimum translation chars</label><input id="min_translated_characters" name="min_translated_characters" type="number" min="1"></div><div class="field full"><label for="quality_report">Quality report</label><input id="quality_report" name="quality_report"></div></div><div class="checks"><label><input id="force_ocr" name="force_ocr" type="checkbox"> Force OCR</label><label><input id="overwrite" name="overwrite" type="checkbox"> Overwrite existing files</label></div><div class="actions"><button class="primary" id="start" type="submit">Start workflow</button><button class="secondary" id="reset" type="button">Reset</button></div><div class="notice" id="notice"></div></form><div class="resume">Existing page files are treated as checkpoints. Leave overwrite off to resume safely after an interruption.</div></section><section class="panel"><h2>Translation dashboard</h2><div class="metrics"><div class="metric"><strong id="pageCount">--</strong><span>Source pages</span></div><div class="metric"><strong id="translatedCount">--</strong><span>Translated</span></div><div class="metric"><strong id="failedCount">--</strong><span>Needs attention</span></div><div class="metric"><strong id="progress">0%</strong><span>Workflow</span></div></div><div class="steps" id="steps"></div><div class="log" id="log">Waiting for a run.</div></section></div></main><script>
const defaults={source_pdf:'',source_language:'',model:'__CONFIGURED_MODEL__',pages_dir:'pages/original/book',text_dir:'pages/text/book',translated_text_dir:'pages/translated_text/book',translated_pdf_dir:'pages/translated/book',output_pdf:'output/book_translated_english.pdf',sample_pages:3,tesseract_language:'eng',max_retries:3,request_delay:.25,font_size:11,min_translated_characters:100,force_ocr:false,overwrite:false};
const names=['analyze','split','extract','translate','pdf','quality','merge'];const labels={analyze:'Analyze source',split:'Split pages',extract:'Extract text',translate:'Translate pages',pdf:'Generate page PDFs',quality:'Quality checks',merge:'Merge book'};let state={};
function applyConfig(config){for(const [key,value] of Object.entries({...defaults,...config})){const el=document.getElementById(key);if(!el)continue;if(el.type==='checkbox')el.checked=Boolean(value);else el.value=value??''}}
function readConfig(){const data={};for(const key of Object.keys(defaults)){const el=document.getElementById(key);if(el.type==='checkbox')data[key]=el.checked;else if(el.type==='number')data[key]=el.value===''?null:Number(el.value);else data[key]=el.value}return data}
function clock(value){return value?new Date(value).toLocaleString(): '—'}
function render(){const steps=document.getElementById('steps');steps.innerHTML=names.map((name,index)=>{const item=state.steps?.[name]||{status:'pending',detail:'Waiting'};return `<div class="step ${item.status}"><div class="stepnum">${index+1}</div><div><div class="stepname">${labels[name]}</div><div class="detail">${item.detail||'Waiting'}</div><div class="timing">Start: ${clock(item.started_at)} &nbsp; End: ${clock(item.ended_at)}</div></div><div class="badge">${item.status}</div></div>`}).join('');const analyze=state.steps?.analyze||{};const translate=state.steps?.translate||{};const quality=state.steps?.quality||{};const summary=quality.summary||{};document.getElementById('pageCount').textContent=analyze.page_count??'--';document.getElementById('translatedCount').textContent=translate.translated_pages??'--';document.getElementById('failedCount').textContent=(translate.failed_pages??0)+(quality.failed_pages??0);const done=names.filter(n=>state.steps?.[n]?.status==='complete').length;document.getElementById('progress').textContent=Math.round(done/names.length*100)+'%';const reportPath=quality.report_path?`Report: ${quality.report_path}`:'Quality report: waiting for a run.';document.getElementById('qualityReport').textContent=`${reportPath} | Passed: ${summary.translated?.length??0} | Failed: ${summary.failed?.length??0} | Blank: ${summary.blank?.length??0} | Short: ${summary.excessively_short?.length??0} | Overflow: ${summary.overflow?.length??0} | Missing: ${summary.missing_output?.length??0}`;document.getElementById('log').textContent=(state.messages||[]).join('\n')||'Waiting for a run.';document.getElementById('start').disabled=Boolean(state.running);document.getElementById('notice').textContent=state.error?state.error:(state.running?'Workflow running...':state.finished_at?'Last run finished.':'Ready.');}
let hydrated=false;
async function refresh(){const response=await fetch('/api/state');state=await response.json();if(!hydrated){applyConfig(state.config||defaults);hydrated=true}render();}
document.getElementById('config').addEventListener('submit',async event=>{event.preventDefault();const response=await fetch('/api/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(readConfig())});const result=await response.json();if(!response.ok)alert(result.error||'Could not start workflow');await refresh()});document.getElementById('reset').addEventListener('click',async()=>{if(!confirm('Reset dashboard fields and progress? Existing files will remain on disk.'))return;const response=await fetch('/api/reset',{method:'POST'});if(!response.ok){const result=await response.json();alert(result.error||'Could not reset workflow');return}applyConfig(defaults);await refresh()});refresh();setInterval(refresh,1200);
</script></body></html>"""


class DashboardHandler(BaseHTTPRequestHandler):
    """HTTP handlers for the dashboard and workflow API."""

    store: WorkflowStore
    runner: WorkflowRunner

    def do_GET(self) -> None:
        if urlparse(self.path).path == "/api/state":
            self.json(self.store.snapshot())
            return
        if urlparse(self.path).path == "/":
            html = HTML.replace("__CONFIGURED_MODEL__", configured_model())
            html = html.replace(
                '<div class="field full"><label for="quality_report">Quality report</label><input id="quality_report" name="quality_report"></div>',
                "",
            )
            html = html.replace(
                '<div class="metric"><strong id="translatedCount"',
                '<div class="metric"><strong id="languageCode">--</strong><span>Detected language</span></div><div class="metric"><strong id="translatedCount"',
            )
            html = html.replace(
                '<div class="log" id="log">Waiting for a run.</div>',
                '<div class="quality-report" id="qualityReport">Quality report: waiting for a run.</div><div class="log" id="log">Waiting for a run.</div>',
            )
            html = html.replace(
                "document.getElementById('translatedCount').textContent=translate.translated_pages??'--';",
                "document.getElementById('languageCode').textContent=(analyze.source_language_name ? `${analyze.source_language_name} (${analyze.source_language_code||'--'})` : (analyze.source_language_code||analyze.source_language||'--'));document.getElementById('translatedCount').textContent=translate.translated_pages??'--';",
            )
            payload = html.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/api/start":
                length = int(self.headers.get("Content-Length", "0"))
                config = json.loads(self.rfile.read(length).decode("utf-8"))
                config = normalize_config(config)
                self.runner.start(config)
                self.json({"ok": True})
                return
            if path == "/api/reset":
                if self.store.state.running:
                    raise ValueError("Cannot reset while a workflow is running.")
                self.store.reset()
                self.json({"ok": True})
                return
            self.send_error(HTTPStatus.NOT_FOUND)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            self.json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)

    def json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


def normalize_config(raw: dict[str, Any]) -> dict[str, Any]:
    """Fill optional fields with project defaults and validate the minimum config."""
    source_pdf = str(raw.get("source_pdf", "")).strip()
    if not source_pdf:
        raise ValueError("Source PDF is required.")
    source_path = Path(source_pdf).expanduser()
    book_name = source_path.stem or "book"
    source_language = str(raw.get("source_language", "")).strip().lower()
    if source_language in {"unknown", "not detected", "auto-detect"}:
        source_language = ""

    def default_path(key: str, path: Path) -> str:
        value = str(raw.get(key) or "").strip()
        normalized = value.replace("\\", "/")
        return str(path) if not value or normalized.endswith("/book") else value

    values = {
        "source_pdf": source_pdf,
        "source_language": source_language or None,
        "model": str(raw.get("model") or configured_model()),
        "pages_dir": default_path("pages_dir", Path("pages") / "original" / book_name),
        "text_dir": default_path("text_dir", Path("pages") / "text" / book_name),
        "translated_text_dir": default_path("translated_text_dir", Path("pages") / "translated_text" / book_name),
        "translated_pdf_dir": default_path("translated_pdf_dir", Path("pages") / "translated" / book_name),
        "output_pdf": str(raw.get("output_pdf") or Path("output") / f"{book_name}_translated_english.pdf"),
        "quality_report": str(raw.get("quality_report") or Path("logs") / f"quality_{book_name}.json"),
        "sample_pages": int(raw.get("sample_pages") or 3),
        "tesseract_language": str(raw.get("tesseract_language") or "eng"),
        "max_retries": int(raw.get("max_retries") if raw.get("max_retries") is not None else 3),
        "request_delay": float(raw.get("request_delay") if raw.get("request_delay") is not None else 0.25),
        "font_size": float(raw.get("font_size") if raw.get("font_size") is not None else 11),
        "min_translated_characters": int(raw.get("min_translated_characters") if raw.get("min_translated_characters") is not None else 100),
        "start_page": raw.get("start_page"),
        "end_page": raw.get("end_page"),
        "force_ocr": bool(raw.get("force_ocr")),
        "overwrite": bool(raw.get("overwrite")),
    }
    if values["sample_pages"] < 1 or values["max_retries"] < 0 or values["request_delay"] < 0:
        raise ValueError("Sample pages must be positive; retries and delay cannot be negative.")
    return values


def serve_dashboard(host: str = "127.0.0.1", port: int = 8765, state_path: Path = DEFAULT_UI_STATE_PATH) -> None:
    """Start the local dashboard server."""
    store = WorkflowStore(state_path)
    runner = WorkflowRunner(store)
    DashboardHandler.store = store
    DashboardHandler.runner = runner
    server = ThreadingHTTPServer((host, port), DashboardHandler)
    print(f"Book Translator dashboard: http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
