"""Flask server: upload PDFs, run Adobe Extract, serve viewer."""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from flask import Flask, abort, jsonify, request, send_from_directory

from adobe_extract import ExtractError, extract_pdf
from roboflow_doclayout import RoboflowError, extract_pdf_layout
from mineru_extract import MinerUError, extract_pdf_mineru, normalize_cached as mineru_renormalize
from datalab_extract import DatalabError, extract_pdf_datalab, normalize_cached as datalab_renormalize
from llamaparse_extract import LlamaParseError, extract_pdf_llamaparse, normalize_cached as llamaparse_renormalize
from upstage_extract import UpstageError, extract_pdf_upstage, normalize_cached as upstage_renormalize

load_dotenv()

BASE_DIR = Path(__file__).parent.resolve()
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

CLIENT_ID = os.environ.get("PDF_SERVICES_CLIENT_ID")
CLIENT_SECRET = os.environ.get("PDF_SERVICES_CLIENT_SECRET")
ROBOFLOW_API_KEY = os.environ.get("ROBOFLOW_API_KEY", "").strip()
ROBOFLOW_MODEL = os.environ.get("ROBOFLOW_MODEL", "jaswanth-kwujz/doclayout-yolo/3").strip()
MINERU_API_TOKEN = os.environ.get("MINERU_API_TOKEN", "").strip()
DATALAB_API_KEY = os.environ.get("DATALAB_API_KEY", "").strip()
LLAMAPARSE_API_KEY = os.environ.get("LLAMAPARSE_API_KEY", "").strip()
UPSTAGE_API_KEY = os.environ.get("UPSTAGE_API_KEY", "").strip()

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # 100 MB

# In-memory job status (pdf_id -> {"status": ..., "error": ...})
_jobs: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.Lock()


def _safe_filename(name: str) -> str:
    name = os.path.basename(name or "document.pdf")
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    return name or "document.pdf"


def _set_job(pdf_id: str, status: str, error: str | None = None) -> None:
    with _jobs_lock:
        _jobs[pdf_id] = {"status": status, "error": error}


def _get_job(pdf_id: str) -> dict[str, Any]:
    with _jobs_lock:
        return dict(_jobs.get(pdf_id, {}))


def _run_extract(pdf_id: str, pdf_bytes: bytes) -> None:
    try:
        assert CLIENT_ID and CLIENT_SECRET, "Adobe credentials missing"
        result = extract_pdf(pdf_bytes, CLIENT_ID, CLIENT_SECRET)
        (DATA_DIR / pdf_id / "extract.json").write_text(json.dumps(result))
        _set_job(pdf_id, "done")
    except ExtractError as e:
        _set_job(pdf_id, "failed", str(e))
    except Exception as e:  # noqa: BLE001
        _set_job(pdf_id, "failed", f"{type(e).__name__}: {e}")


def _job_key(pdf_id: str, backend: str) -> str:
    return f"{pdf_id}:{backend}"


def _run_doclayout(pdf_id: str) -> None:
    key = _job_key(pdf_id, "doclayout")
    try:
        assert ROBOFLOW_API_KEY, "ROBOFLOW_API_KEY not set"
        pdf_path = DATA_DIR / pdf_id / "source.pdf"
        result = extract_pdf_layout(str(pdf_path), ROBOFLOW_API_KEY, ROBOFLOW_MODEL)
        (DATA_DIR / pdf_id / "doclayout.json").write_text(json.dumps(result))
        _set_job(key, "done")
    except (RoboflowError, AssertionError) as e:
        _set_job(key, "failed", str(e))
    except Exception as e:  # noqa: BLE001
        _set_job(key, "failed", f"{type(e).__name__}: {e}")


def _run_mineru(pdf_id: str) -> None:
    key = _job_key(pdf_id, "mineru")
    try:
        pdf_dir = DATA_DIR / pdf_id
        pdf_bytes = (pdf_dir / "source.pdf").read_bytes()
        raw_path = pdf_dir / "mineru_middle.json"
        if raw_path.exists():
            # Re-normalize from cached raw output, no MinerU call.
            result = mineru_renormalize(str(raw_path), pdf_bytes)
        else:
            assert MINERU_API_TOKEN, "MINERU_API_TOKEN not set"
            meta = json.loads((pdf_dir / "meta.json").read_text())
            filename = meta.get("filename") or "document.pdf"
            result = extract_pdf_mineru(
                pdf_bytes, filename, MINERU_API_TOKEN, raw_out_path=str(raw_path),
            )
        (pdf_dir / "mineru.json").write_text(json.dumps(result))
        _set_job(key, "done")
    except (MinerUError, AssertionError) as e:
        _set_job(key, "failed", str(e))
    except Exception as e:  # noqa: BLE001
        _set_job(key, "failed", f"{type(e).__name__}: {e}")


def _run_datalab(pdf_id: str) -> None:
    key = _job_key(pdf_id, "datalab")
    try:
        pdf_dir = DATA_DIR / pdf_id
        raw_path = pdf_dir / "datalab_raw.json"
        if raw_path.exists():
            result = datalab_renormalize(str(raw_path))
        else:
            assert DATALAB_API_KEY, "DATALAB_API_KEY not set"
            pdf_bytes = (pdf_dir / "source.pdf").read_bytes()
            meta = json.loads((pdf_dir / "meta.json").read_text())
            filename = meta.get("filename") or "document.pdf"
            result = extract_pdf_datalab(
                pdf_bytes, filename, DATALAB_API_KEY, raw_out_path=str(raw_path),
            )
        (pdf_dir / "datalab.json").write_text(json.dumps(result))
        _set_job(key, "done")
    except (DatalabError, AssertionError) as e:
        _set_job(key, "failed", str(e))
    except Exception as e:  # noqa: BLE001
        _set_job(key, "failed", f"{type(e).__name__}: {e}")


def _run_llamaparse(pdf_id: str) -> None:
    key = _job_key(pdf_id, "llamaparse")
    try:
        pdf_dir = DATA_DIR / pdf_id
        raw_path = pdf_dir / "llamaparse_raw.json"
        if raw_path.exists():
            result = llamaparse_renormalize(str(raw_path))
        else:
            assert LLAMAPARSE_API_KEY, "LLAMAPARSE_API_KEY not set"
            pdf_bytes = (pdf_dir / "source.pdf").read_bytes()
            meta = json.loads((pdf_dir / "meta.json").read_text())
            filename = meta.get("filename") or "document.pdf"
            result = extract_pdf_llamaparse(
                pdf_bytes, filename, LLAMAPARSE_API_KEY, raw_out_path=str(raw_path),
            )
        (pdf_dir / "llamaparse.json").write_text(json.dumps(result))
        _set_job(key, "done")
    except (LlamaParseError, AssertionError) as e:
        _set_job(key, "failed", str(e))
    except Exception as e:  # noqa: BLE001
        _set_job(key, "failed", f"{type(e).__name__}: {e}")


def _run_upstage(pdf_id: str) -> None:
    key = _job_key(pdf_id, "upstage")
    try:
        pdf_dir = DATA_DIR / pdf_id
        pdf_bytes = (pdf_dir / "source.pdf").read_bytes()
        raw_path = pdf_dir / "upstage_raw.json"
        if raw_path.exists():
            result = upstage_renormalize(str(raw_path), pdf_bytes)
        else:
            assert UPSTAGE_API_KEY, "UPSTAGE_API_KEY not set"
            meta = json.loads((pdf_dir / "meta.json").read_text())
            filename = meta.get("filename") or "document.pdf"
            result = extract_pdf_upstage(
                pdf_bytes, filename, UPSTAGE_API_KEY, raw_out_path=str(raw_path),
            )
        (pdf_dir / "upstage.json").write_text(json.dumps(result))
        _set_job(key, "done")
    except (UpstageError, AssertionError) as e:
        _set_job(key, "failed", str(e))
    except Exception as e:  # noqa: BLE001
        _set_job(key, "failed", f"{type(e).__name__}: {e}")


def _list_pdfs() -> list[dict[str, Any]]:
    out = []
    for d in sorted(DATA_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not d.is_dir():
            continue
        meta_path = d / "meta.json"
        if not meta_path.exists():
            continue
        meta = json.loads(meta_path.read_text())
        job = _get_job(d.name)
        status = job.get("status")
        if not status:
            status = "done" if (d / "extract.json").exists() else "unknown"
        dl_job = _get_job(_job_key(d.name, "doclayout"))
        dl_status = dl_job.get("status")
        if not dl_status:
            dl_status = "done" if (d / "doclayout.json").exists() else "none"
        mu_job = _get_job(_job_key(d.name, "mineru"))
        mu_status = mu_job.get("status")
        if not mu_status:
            mu_status = "done" if (d / "mineru.json").exists() else "none"
        dt_job = _get_job(_job_key(d.name, "datalab"))
        dt_status = dt_job.get("status")
        if not dt_status:
            dt_status = "done" if (d / "datalab.json").exists() else "none"
        lp_job = _get_job(_job_key(d.name, "llamaparse"))
        lp_status = lp_job.get("status")
        if not lp_status:
            lp_status = "done" if (d / "llamaparse.json").exists() else "none"
        up_job = _get_job(_job_key(d.name, "upstage"))
        up_status = up_job.get("status")
        if not up_status:
            up_status = "done" if (d / "upstage.json").exists() else "none"
        out.append({
            "id": d.name,
            "filename": meta.get("filename"),
            "status": status,
            "error": job.get("error"),
            "doclayout_status": dl_status,
            "doclayout_error": dl_job.get("error"),
            "mineru_status": mu_status,
            "mineru_error": mu_job.get("error"),
            "datalab_status": dt_status,
            "datalab_error": dt_job.get("error"),
            "llamaparse_status": lp_status,
            "llamaparse_error": lp_job.get("error"),
            "upstage_status": up_status,
            "upstage_error": up_job.get("error"),
        })
    return out


@app.route("/")
def index():
    return send_from_directory(app.template_folder, "index.html")


@app.route("/api/pdfs", methods=["GET"])
def list_pdfs():
    return jsonify(_list_pdfs())


@app.route("/api/pdfs", methods=["POST"])
def upload_pdf():
    if not CLIENT_ID or not CLIENT_SECRET:
        return jsonify({"error": "PDF_SERVICES_CLIENT_ID and PDF_SERVICES_CLIENT_SECRET must be set"}), 500
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "no file uploaded"}), 400
    pdf_bytes = f.read()
    if not pdf_bytes:
        return jsonify({"error": "empty file"}), 400

    pdf_id = uuid.uuid4().hex
    pdf_dir = DATA_DIR / pdf_id
    pdf_dir.mkdir()
    filename = _safe_filename(f.filename)
    (pdf_dir / "source.pdf").write_bytes(pdf_bytes)
    (pdf_dir / "meta.json").write_text(json.dumps({"filename": filename}))

    _set_job(pdf_id, "processing")
    threading.Thread(target=_run_extract, args=(pdf_id, pdf_bytes), daemon=True).start()

    return jsonify({"id": pdf_id, "filename": filename, "status": "processing"}), 201


@app.route("/api/pdfs/<pdf_id>", methods=["DELETE"])
def delete_pdf(pdf_id: str):
    pdf_dir = DATA_DIR / pdf_id
    if not pdf_dir.exists() or not pdf_dir.is_dir():
        abort(404)
    for child in pdf_dir.iterdir():
        child.unlink()
    pdf_dir.rmdir()
    with _jobs_lock:
        _jobs.pop(pdf_id, None)
    return jsonify({"ok": True})


@app.route("/api/pdfs/<pdf_id>/status")
def pdf_status(pdf_id: str):
    pdf_dir = DATA_DIR / pdf_id
    if not pdf_dir.exists():
        abort(404)
    job = _get_job(pdf_id)
    status = job.get("status") or ("done" if (pdf_dir / "extract.json").exists() else "unknown")
    return jsonify({"id": pdf_id, "status": status, "error": job.get("error")})


@app.route("/api/pdfs/<pdf_id>/file")
def pdf_file(pdf_id: str):
    pdf_dir = DATA_DIR / pdf_id
    if not (pdf_dir / "source.pdf").exists():
        abort(404)
    return send_from_directory(pdf_dir, "source.pdf", mimetype="application/pdf")


@app.route("/api/pdfs/<pdf_id>/extract")
def pdf_extract(pdf_id: str):
    pdf_dir = DATA_DIR / pdf_id
    extract_path = pdf_dir / "extract.json"
    if not extract_path.exists():
        abort(404)
    return send_from_directory(pdf_dir, "extract.json", mimetype="application/json")


@app.route("/api/pdfs/<pdf_id>/doclayout", methods=["POST"])
def doclayout_run(pdf_id: str):
    pdf_dir = DATA_DIR / pdf_id
    if not (pdf_dir / "source.pdf").exists():
        abort(404)
    if not ROBOFLOW_API_KEY:
        return jsonify({"error": "ROBOFLOW_API_KEY not set in .env"}), 500
    key = _job_key(pdf_id, "doclayout")
    if _get_job(key).get("status") == "processing":
        return jsonify({"status": "processing"}), 202
    _set_job(key, "processing")
    threading.Thread(target=_run_doclayout, args=(pdf_id,), daemon=True).start()
    return jsonify({"status": "processing"}), 202


@app.route("/api/pdfs/<pdf_id>/doclayout", methods=["GET"])
def doclayout_get(pdf_id: str):
    pdf_dir = DATA_DIR / pdf_id
    path = pdf_dir / "doclayout.json"
    if not path.exists():
        abort(404)
    return send_from_directory(pdf_dir, "doclayout.json", mimetype="application/json")


@app.route("/api/pdfs/<pdf_id>/doclayout/status")
def doclayout_status(pdf_id: str):
    pdf_dir = DATA_DIR / pdf_id
    if not pdf_dir.exists():
        abort(404)
    key = _job_key(pdf_id, "doclayout")
    job = _get_job(key)
    status = job.get("status") or ("done" if (pdf_dir / "doclayout.json").exists() else "none")
    return jsonify({"status": status, "error": job.get("error")})


@app.route("/api/pdfs/<pdf_id>/mineru", methods=["POST"])
def mineru_run(pdf_id: str):
    pdf_dir = DATA_DIR / pdf_id
    if not (pdf_dir / "source.pdf").exists():
        abort(404)
    if not MINERU_API_TOKEN:
        return jsonify({"error": "MINERU_API_TOKEN not set in .env (get one at https://mineru.net/apiManage/token)"}), 500
    key = _job_key(pdf_id, "mineru")
    if _get_job(key).get("status") == "processing":
        return jsonify({"status": "processing"}), 202
    _set_job(key, "processing")
    threading.Thread(target=_run_mineru, args=(pdf_id,), daemon=True).start()
    return jsonify({"status": "processing"}), 202


@app.route("/api/pdfs/<pdf_id>/mineru", methods=["GET"])
def mineru_get(pdf_id: str):
    pdf_dir = DATA_DIR / pdf_id
    path = pdf_dir / "mineru.json"
    if not path.exists():
        abort(404)
    return send_from_directory(pdf_dir, "mineru.json", mimetype="application/json")


@app.route("/api/pdfs/<pdf_id>/mineru/status")
def mineru_status_route(pdf_id: str):
    pdf_dir = DATA_DIR / pdf_id
    if not pdf_dir.exists():
        abort(404)
    key = _job_key(pdf_id, "mineru")
    job = _get_job(key)
    status = job.get("status") or ("done" if (pdf_dir / "mineru.json").exists() else "none")
    return jsonify({"status": status, "error": job.get("error")})


@app.route("/api/pdfs/<pdf_id>/datalab", methods=["POST"])
def datalab_run(pdf_id: str):
    pdf_dir = DATA_DIR / pdf_id
    if not (pdf_dir / "source.pdf").exists():
        abort(404)
    if not DATALAB_API_KEY and not (pdf_dir / "datalab_raw.json").exists():
        return jsonify({"error": "DATALAB_API_KEY not set in .env (get one at https://www.datalab.to/app/keys)"}), 500
    key = _job_key(pdf_id, "datalab")
    if _get_job(key).get("status") == "processing":
        return jsonify({"status": "processing"}), 202
    _set_job(key, "processing")
    threading.Thread(target=_run_datalab, args=(pdf_id,), daemon=True).start()
    return jsonify({"status": "processing"}), 202


@app.route("/api/pdfs/<pdf_id>/datalab", methods=["GET"])
def datalab_get(pdf_id: str):
    pdf_dir = DATA_DIR / pdf_id
    path = pdf_dir / "datalab.json"
    if not path.exists():
        abort(404)
    return send_from_directory(pdf_dir, "datalab.json", mimetype="application/json")


@app.route("/api/pdfs/<pdf_id>/datalab/status")
def datalab_status_route(pdf_id: str):
    pdf_dir = DATA_DIR / pdf_id
    if not pdf_dir.exists():
        abort(404)
    key = _job_key(pdf_id, "datalab")
    job = _get_job(key)
    status = job.get("status") or ("done" if (pdf_dir / "datalab.json").exists() else "none")
    return jsonify({"status": status, "error": job.get("error")})


@app.route("/api/pdfs/<pdf_id>/llamaparse", methods=["POST"])
def llamaparse_run(pdf_id: str):
    pdf_dir = DATA_DIR / pdf_id
    if not (pdf_dir / "source.pdf").exists():
        abort(404)
    if not LLAMAPARSE_API_KEY and not (pdf_dir / "llamaparse_raw.json").exists():
        return jsonify({"error": "LLAMAPARSE_API_KEY not set in .env (get one at https://cloud.llamaindex.ai/api-key)"}), 500
    key = _job_key(pdf_id, "llamaparse")
    if _get_job(key).get("status") == "processing":
        return jsonify({"status": "processing"}), 202
    _set_job(key, "processing")
    threading.Thread(target=_run_llamaparse, args=(pdf_id,), daemon=True).start()
    return jsonify({"status": "processing"}), 202


@app.route("/api/pdfs/<pdf_id>/llamaparse", methods=["GET"])
def llamaparse_get(pdf_id: str):
    pdf_dir = DATA_DIR / pdf_id
    path = pdf_dir / "llamaparse.json"
    if not path.exists():
        abort(404)
    return send_from_directory(pdf_dir, "llamaparse.json", mimetype="application/json")


@app.route("/api/pdfs/<pdf_id>/llamaparse/status")
def llamaparse_status_route(pdf_id: str):
    pdf_dir = DATA_DIR / pdf_id
    if not pdf_dir.exists():
        abort(404)
    key = _job_key(pdf_id, "llamaparse")
    job = _get_job(key)
    status = job.get("status") or ("done" if (pdf_dir / "llamaparse.json").exists() else "none")
    return jsonify({"status": status, "error": job.get("error")})


@app.route("/api/pdfs/<pdf_id>/upstage", methods=["POST"])
def upstage_run(pdf_id: str):
    pdf_dir = DATA_DIR / pdf_id
    if not (pdf_dir / "source.pdf").exists():
        abort(404)
    if not UPSTAGE_API_KEY and not (pdf_dir / "upstage_raw.json").exists():
        return jsonify({"error": "UPSTAGE_API_KEY not set in .env (get one at https://console.upstage.ai/api-keys)"}), 500
    key = _job_key(pdf_id, "upstage")
    if _get_job(key).get("status") == "processing":
        return jsonify({"status": "processing"}), 202
    _set_job(key, "processing")
    threading.Thread(target=_run_upstage, args=(pdf_id,), daemon=True).start()
    return jsonify({"status": "processing"}), 202


@app.route("/api/pdfs/<pdf_id>/upstage", methods=["GET"])
def upstage_get(pdf_id: str):
    pdf_dir = DATA_DIR / pdf_id
    path = pdf_dir / "upstage.json"
    if not path.exists():
        abort(404)
    return send_from_directory(pdf_dir, "upstage.json", mimetype="application/json")


@app.route("/api/pdfs/<pdf_id>/upstage/status")
def upstage_status_route(pdf_id: str):
    pdf_dir = DATA_DIR / pdf_id
    if not pdf_dir.exists():
        abort(404)
    key = _job_key(pdf_id, "upstage")
    job = _get_job(key)
    status = job.get("status") or ("done" if (pdf_dir / "upstage.json").exists() else "none")
    return jsonify({"status": status, "error": job.get("error")})


@app.route("/api/pdfs/<pdf_id>/ocr", methods=["POST"])
def pdf_ocr(pdf_id: str):
    pdf_dir = DATA_DIR / pdf_id
    pdf_path = pdf_dir / "source.pdf"
    if not pdf_path.exists():
        abort(404)
    body = request.get_json(silent=True) or {}
    try:
        page = int(body["page"])
        bbox = tuple(float(v) for v in body["bbox"])
        if len(bbox) != 4:
            raise ValueError("bbox must have 4 numbers")
    except (KeyError, TypeError, ValueError) as e:
        return jsonify({"error": f"bad request: {e}"}), 400
    try:
        import lightonocr
        png = lightonocr.crop_pdf_region(str(pdf_path), page, bbox)  # type: ignore[arg-type]
        text = lightonocr.ocr_image(png)
        return jsonify({"text": text})
    except ImportError as e:
        return jsonify({"error": f"OCR deps missing: pip install -r requirements-ocr.txt ({e})"}), 500
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
