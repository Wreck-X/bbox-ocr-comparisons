"""Upstage Document Parse client.

POST https://api.upstage.ai/v1/document-digitization
  Authorization: Bearer <api_key>
  Multipart: document=<pdf>, model=document-parse

Synchronous; response shape:
  {
    "elements": [
      {
        "category": "paragraph"|"heading1"|"table"|"figure"|"list"
                    |"caption"|"equation"|"footer"|"header"|...,
        "content": {"html": "...", "markdown": "...", "text": "..."},
        "coordinates": [{"x": 0.12, "y": 0.34}, ... 4 corners ...],
        "page": 1,
        "id": <int>
      },
      ...
    ],
    "usage": {"pages": <int>}, ...
  }

Coordinates are NORMALIZED (0..1) with TOP-LEFT origin, relative to the page.
Upstage's response doesn't carry page dimensions, so we read them with PyMuPDF
and emit bounds in PDF points / bottom-left origin to match the viewer.
"""

from __future__ import annotations

import json
from typing import Any

import fitz  # PyMuPDF
import requests


ENDPOINT = "https://api.upstage.ai/v1/document-digitization"


class UpstageError(RuntimeError):
    pass


def _call(pdf_bytes: bytes, filename: str, api_key: str) -> dict[str, Any]:
    files = {"document": (filename, pdf_bytes, "application/pdf")}
    data = {
        "model": "document-parse",
        "ocr": "auto",
        "output_formats": "['text']",
        "coordinates": "true",
    }
    resp = requests.post(
        ENDPOINT,
        headers={"Authorization": f"Bearer {api_key}"},
        files=files,
        data=data,
        timeout=300,
    )
    if not resp.ok:
        raise UpstageError(f"Upstage {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def _page_dims(pdf_bytes: bytes) -> list[tuple[float, float]]:
    dims: list[tuple[float, float]] = []
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        for page in doc:
            dims.append((page.rect.width, page.rect.height))
    finally:
        doc.close()
    return dims


def _bounds(coords: list[dict[str, Any]], page_w: float, page_h: float) -> tuple[float, float, float, float] | None:
    xs: list[float] = []
    ys: list[float] = []
    for c in coords or []:
        try:
            xs.append(float(c["x"]))
            ys.append(float(c["y"]))
        except (KeyError, TypeError, ValueError):
            return None
    if len(xs) < 3 or len(ys) < 3:
        return None
    x0_n, x1_n = min(xs), max(xs)
    y0_n_top, y1_n_top = min(ys), max(ys)
    x0 = x0_n * page_w
    x1 = x1_n * page_w
    # Y-flip from normalized top-left to point bottom-left.
    y0 = (1.0 - y1_n_top) * page_h
    y1 = (1.0 - y0_n_top) * page_h
    return x0, y0, x1, y1


def _text(content: Any) -> str:
    if isinstance(content, dict):
        return str(content.get("text") or content.get("markdown") or content.get("html") or "").strip()
    return str(content or "").strip()


def _normalize(payload: dict[str, Any], pdf_bytes: bytes) -> dict[str, Any]:
    dims = _page_dims(pdf_bytes)
    pages_meta = [{"width": w, "height": h} for (w, h) in dims]

    elements: list[dict[str, Any]] = []
    for el in payload.get("elements") or []:
        page_1based = int(el.get("page") or 1)
        page_idx = page_1based - 1
        if page_idx < 0 or page_idx >= len(dims):
            continue
        page_w, page_h = dims[page_idx]
        bounds = _bounds(el.get("coordinates") or [], page_w, page_h)
        if not bounds:
            continue
        category = str(el.get("category") or "Unknown")
        elements.append({
            "Page": page_idx,
            "Bounds": list(bounds),
            "Path": f"//Upstage/{category}",
            "Text": _text(el.get("content")),
        })

    return {"pages": pages_meta, "elements": elements, "source": "upstage-document-parse"}


def extract_pdf_upstage(pdf_bytes: bytes, filename: str, api_key: str,
                       raw_out_path: str | None = None) -> dict[str, Any]:
    payload = _call(pdf_bytes, filename, api_key)
    if raw_out_path:
        with open(raw_out_path, "w") as f:
            json.dump(payload, f)
    return _normalize(payload, pdf_bytes)


def normalize_cached(raw_path: str, pdf_bytes: bytes) -> dict[str, Any]:
    with open(raw_path) as f:
        return _normalize(json.load(f), pdf_bytes)
