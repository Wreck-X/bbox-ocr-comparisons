"""MinerU Cloud API client.

Flow:
  1. POST /api/v4/file-urls/batch        -> batch_id + presigned PUT URL
  2. PUT <presigned_url>                 -> uploads the PDF
  3. GET /api/v4/extract-results/batch/{id} (poll until state == "done")
  4. GET <full_zip_url>                  -> download ZIP containing layout.json
                                            (== middle.json with bbox info)
  5. Parse middle.json -> normalize to {pages, elements} matching Adobe shape.

middle.json bbox format: [x0, y0, x1, y1] in pixel coords (top-left origin),
with each page object also carrying its rendered page_size in pixels.
We convert to PDF points (bottom-left origin) so the viewer renders correctly.
"""

from __future__ import annotations

import io
import json
import time
import uuid
import zipfile
from typing import Any

import fitz  # PyMuPDF
import requests


BASE = "https://mineru.net/api/v4"


class MinerUError(RuntimeError):
    pass


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _request_upload(token: str, filename: str, data_id: str) -> tuple[str, str]:
    resp = requests.post(
        f"{BASE}/file-urls/batch",
        headers={**_auth(token), "Content-Type": "application/json"},
        json={
            "files": [{"name": filename, "data_id": data_id}],
            "model_version": "vlm",
            "enable_table": True,
            "enable_formula": True,
            "language": "en",
            "is_ocr": False,
        },
        timeout=30,
    )
    if not resp.ok:
        raise MinerUError(f"file-urls/batch failed: {resp.status_code} {resp.text[:300]}")
    body = resp.json()
    data = body.get("data") or {}
    batch_id = data.get("batch_id")
    file_urls = data.get("file_urls") or []
    if not batch_id or not file_urls:
        raise MinerUError(f"unexpected file-urls response: {body}")
    return batch_id, file_urls[0]


def _put_pdf(upload_url: str, pdf_bytes: bytes) -> None:
    resp = requests.put(upload_url, data=pdf_bytes, timeout=120)
    if not resp.ok:
        raise MinerUError(f"upload failed: {resp.status_code} {resp.text[:300]}")


def _poll_batch(token: str, batch_id: str, timeout_s: int = 600) -> str:
    """Poll until done, return the result ZIP URL."""
    deadline = time.time() + timeout_s
    delay = 3.0
    last_body: Any = None
    while time.time() < deadline:
        resp = requests.get(
            f"{BASE}/extract-results/batch/{batch_id}",
            headers=_auth(token),
            timeout=30,
        )
        if not resp.ok:
            raise MinerUError(f"poll failed: {resp.status_code} {resp.text[:300]}")
        body = resp.json()
        last_body = body
        data = body.get("data") or {}
        results = data.get("extract_result") or []
        if results:
            r = results[0]
            state = r.get("state")
            if state == "done":
                zip_url = r.get("full_zip_url") or r.get("zip_url")
                if not zip_url:
                    raise MinerUError(f"done but no zip url: {r}")
                return zip_url
            if state == "failed":
                raise MinerUError(f"MinerU job failed: {r.get('err_msg') or r}")
        time.sleep(delay)
        delay = min(delay * 1.4, 15)
    raise MinerUError(f"timed out waiting for batch {batch_id}; last={last_body}")


def _download_middle_json(zip_url: str) -> dict[str, Any]:
    resp = requests.get(zip_url, timeout=120)
    if not resp.ok:
        raise MinerUError(f"zip download failed: {resp.status_code}")
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        names = zf.namelist()
        # Prefer middle.json; fall back to layout.json (alias).
        target = next((n for n in names if n.endswith("middle.json")), None) \
            or next((n for n in names if n.endswith("layout.json")), None)
        if not target:
            raise MinerUError(f"no middle.json/layout.json in zip: {names}")
        with zf.open(target) as f:
            return json.load(f)


def _block_text(block: dict[str, Any]) -> str:
    """Pull text out of a block, recursing into nested blocks (lists, tables)."""
    parts: list[str] = []
    for line in block.get("lines") or []:
        line_buf = ""
        for span in line.get("spans") or []:
            line_buf += span.get("content") or span.get("text") or ""
        if line_buf:
            parts.append(line_buf)
    for sub in block.get("blocks") or block.get("sub_blocks") or []:
        sub_text = _block_text(sub)
        if sub_text:
            parts.append(sub_text)
    return "\n".join(parts).strip()


def _emit_block(block: dict[str, Any], page_idx: int, scale_x: float, scale_y: float,
                pdf_h_pt: float, out: list[dict[str, Any]], parent_path: str = "") -> None:
    """Append this block (and any leaf children) as Adobe-shaped elements."""
    bbox = block.get("bbox")
    btype = block.get("type", "unknown")
    path = f"{parent_path}/{btype}" if parent_path else f"//MinerU/{btype}"

    children = block.get("blocks") or block.get("sub_blocks") or []
    if bbox and len(bbox) == 4:
        x0_px, y0_px, x1_px, y1_px = bbox
        x0_pt = x0_px * scale_x
        x1_pt = x1_px * scale_x
        y0_pt = pdf_h_pt - y1_px * scale_y
        y1_pt = pdf_h_pt - y0_px * scale_y
        out.append({
            "Page": page_idx,
            "Bounds": [x0_pt, y0_pt, x1_pt, y1_pt],
            "Path": path,
            "Text": _block_text(block),
        })

    # Emit children as separate elements so each list item / table cell shows up
    # with its own bbox + text. The parent gets auto-hidden by the "hide
    # nested containers" filter in the UI when children exist.
    for child in children:
        _emit_block(child, page_idx, scale_x, scale_y, pdf_h_pt, out, parent_path=path)


def _normalize(middle: dict[str, Any], pdf_bytes: bytes) -> dict[str, Any]:
    """Convert MinerU middle.json to the Adobe-shaped {pages, elements} format.

    MinerU bboxes are pixel coords with top-left origin; reprojected here to
    PDF points with bottom-left origin so the existing viewer renders them.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pdf_pages = [(p.rect.width, p.rect.height) for p in doc]
    doc.close()

    pages_meta: list[dict[str, float]] = []
    elements: list[dict[str, Any]] = []

    pdf_info = middle.get("pdf_info") or middle.get("page_info") or []
    for page_idx, page in enumerate(pdf_info):
        page_size = page.get("page_size") or [0, 0]
        img_w = float(page_size[0] or 1)
        img_h = float(page_size[1] or 1)
        if page_idx < len(pdf_pages):
            pdf_w_pt, pdf_h_pt = pdf_pages[page_idx]
        else:
            pdf_w_pt, pdf_h_pt = img_w, img_h
        pages_meta.append({"width": pdf_w_pt, "height": pdf_h_pt})

        scale_x = pdf_w_pt / img_w
        scale_y = pdf_h_pt / img_h

        for block in page.get("para_blocks") or page.get("preproc_blocks") or []:
            _emit_block(block, page_idx, scale_x, scale_y, pdf_h_pt, elements)

    return {"pages": pages_meta, "elements": elements, "source": "mineru-cloud"}


def extract_pdf_mineru(pdf_bytes: bytes, filename: str, token: str,
                       raw_out_path: str | None = None) -> dict[str, Any]:
    data_id = uuid.uuid4().hex
    batch_id, upload_url = _request_upload(token, filename, data_id)
    _put_pdf(upload_url, pdf_bytes)
    zip_url = _poll_batch(token, batch_id)
    middle = _download_middle_json(zip_url)
    if raw_out_path:
        with open(raw_out_path, "w") as f:
            json.dump(middle, f)
    return _normalize(middle, pdf_bytes)


def normalize_cached(middle_path: str, pdf_bytes: bytes) -> dict[str, Any]:
    """Re-normalize an existing middle.json without re-calling MinerU."""
    with open(middle_path) as f:
        middle = json.load(f)
    return _normalize(middle, pdf_bytes)
