"""LlamaParse (LlamaIndex Cloud) client.

Flow:
  1. POST /api/v1/parsing/upload (multipart: file)        -> job id
  2. GET  /api/v1/parsing/job/{id}                        -> poll until SUCCESS
  3. GET  /api/v1/parsing/job/{id}/result/json            -> structured JSON

The JSON result has shape:
  {
    "pages": [
      {
        "page": 1,
        "width": <pt>, "height": <pt>,
        "items": [
          {"type": "heading"|"text"|"table"|"image"|...,
           "lvl": <int?>, "value": "...", "md": "...",
           "bBox": {"x": <pt>, "y": <pt>, "w": <pt>, "h": <pt>}}
        ]
      }
    ]
  }

bBox is in PDF points with TOP-LEFT origin. We Y-flip into the bottom-left
origin used by the viewer, mirroring datalab_extract / mineru_extract.
"""

from __future__ import annotations

import json
import time
from typing import Any

import requests


BASE = "https://api.cloud.llamaindex.ai/api/v1/parsing"


class LlamaParseError(RuntimeError):
    pass


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _submit(pdf_bytes: bytes, filename: str, api_key: str) -> str:
    files = {"file": (filename, pdf_bytes, "application/pdf")}
    # `balanced` is the default tier; explicit so behavior is stable.
    data = {"parse_mode": "parse_page_with_layout_agent"}
    resp = requests.post(
        f"{BASE}/upload",
        headers=_auth(api_key),
        files=files,
        data=data,
        timeout=120,
    )
    if not resp.ok:
        raise LlamaParseError(f"submit failed: {resp.status_code} {resp.text[:300]}")
    body = resp.json()
    job_id = body.get("id") or body.get("job_id")
    if not job_id:
        raise LlamaParseError(f"no job id in response: {body}")
    return str(job_id)


def _poll(job_id: str, api_key: str, timeout_s: int = 600) -> None:
    deadline = time.time() + timeout_s
    delay = 3.0
    last: Any = None
    while time.time() < deadline:
        resp = requests.get(f"{BASE}/job/{job_id}", headers=_auth(api_key), timeout=30)
        if not resp.ok:
            raise LlamaParseError(f"poll failed: {resp.status_code} {resp.text[:300]}")
        body = resp.json()
        last = body
        status = (body.get("status") or "").upper()
        if status in {"SUCCESS", "COMPLETED", "DONE"}:
            return
        if status in {"ERROR", "FAILED", "CANCELLED"}:
            raise LlamaParseError(f"job {status.lower()}: {body.get('error') or body}")
        time.sleep(delay)
        delay = min(delay * 1.4, 15)
    raise LlamaParseError(f"poll timed out; last={last}")


def _fetch_json(job_id: str, api_key: str) -> dict[str, Any]:
    resp = requests.get(
        f"{BASE}/job/{job_id}/result/json",
        headers=_auth(api_key),
        timeout=60,
    )
    if not resp.ok:
        raise LlamaParseError(f"result fetch failed: {resp.status_code} {resp.text[:300]}")
    return resp.json()


def _bbox_to_bounds(bbox: dict[str, Any], page_h_pt: float) -> tuple[float, float, float, float] | None:
    try:
        x = float(bbox["x"])
        y_top = float(bbox["y"])
        w = float(bbox["w"])
        h = float(bbox["h"])
    except (KeyError, TypeError, ValueError):
        return None
    x0 = x
    x1 = x + w
    y0 = page_h_pt - (y_top + h)  # lower edge in bottom-left coords
    y1 = page_h_pt - y_top        # upper edge
    return x0, y0, x1, y1


def _normalize(payload: dict[str, Any]) -> dict[str, Any]:
    pages_meta: list[dict[str, float]] = []
    elements: list[dict[str, Any]] = []

    # The JSON endpoint returns either {"pages": [...]} directly, or a wrapper
    # with a `result` / `data` key. Be defensive.
    pages = payload.get("pages")
    if pages is None and isinstance(payload.get("result"), dict):
        pages = payload["result"].get("pages")
    if pages is None and isinstance(payload.get("data"), dict):
        pages = payload["data"].get("pages")
    pages = pages or []

    for idx, page in enumerate(pages):
        page_w = float(page.get("width") or 612)
        page_h = float(page.get("height") or 792)
        pages_meta.append({"width": page_w, "height": page_h})

        for item in page.get("items") or []:
            bbox = item.get("bBox") or item.get("bbox")
            if not bbox:
                continue
            bounds = _bbox_to_bounds(bbox, page_h)
            if not bounds:
                continue
            itype = str(item.get("type") or "Unknown")
            lvl = item.get("lvl")
            label = f"{itype}{lvl}" if itype == "heading" and lvl else itype
            text = item.get("value") or item.get("md") or ""
            elements.append({
                "Page": idx,
                "Bounds": list(bounds),
                "Path": f"//LlamaParse/{label}",
                "Text": str(text).strip(),
            })

    return {"pages": pages_meta, "elements": elements, "source": "llamaparse"}


def extract_pdf_llamaparse(pdf_bytes: bytes, filename: str, api_key: str,
                           raw_out_path: str | None = None) -> dict[str, Any]:
    job_id = _submit(pdf_bytes, filename, api_key)
    _poll(job_id, api_key)
    payload = _fetch_json(job_id, api_key)
    if raw_out_path:
        with open(raw_out_path, "w") as f:
            json.dump(payload, f)
    return _normalize(payload)


def normalize_cached(raw_path: str) -> dict[str, Any]:
    with open(raw_path) as f:
        return _normalize(json.load(f))
