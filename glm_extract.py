"""GLM (Zhipu AI) OCR backend.

GLM is a multimodal LLM, not a dedicated layout detector — we render each
page to PNG, send it through the chat completions endpoint with a JSON-mode
prompt, and parse the returned elements. Bbox quality is therefore noisier
than the other backends; treat results as approximate.

Flow per page:
  1. Render page to PNG with PyMuPDF.
  2. POST https://open.bigmodel.cn/api/paas/v4/chat/completions
     with image_url (data URL) + a strict JSON-schema prompt.
  3. Parse JSON; convert pixel bboxes (top-left origin) to PDF points
     (bottom-left origin) to match the viewer.

Env:
  GLM_API_KEY    required
  GLM_MODEL      optional, defaults to glm-4v-plus
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any

import fitz  # PyMuPDF
import requests


ENDPOINT = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
RENDER_DPI = 144  # balance: enough for OCR, small enough to upload fast

PROMPT = (
    "You are a document layout extractor. Look at the page image and return a "
    "JSON object with one key: \"elements\". Each element MUST have:\n"
    "  - \"category\": one of [\"title\", \"heading\", \"paragraph\", \"list_item\", "
    "\"table\", \"figure\", \"caption\", \"equation\", \"footer\", \"header\"]\n"
    "  - \"text\": the literal text content (empty string for figures)\n"
    "  - \"bbox\": [x1, y1, x2, y2] in pixel coordinates of the image, "
    "with (0,0) at the TOP-LEFT corner. x1<x2, y1<y2.\n"
    "Cover every visible text block and figure. Do not invent content. "
    "Return ONLY the JSON object, no prose."
)


class GLMError(RuntimeError):
    pass


def _render_page_png(page: fitz.Page, dpi: int) -> tuple[bytes, int, int]:
    zoom = dpi / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    return pix.tobytes("png"), pix.width, pix.height


def _call_glm(png: bytes, api_key: str, model: str) -> dict[str, Any]:
    b64 = base64.b64encode(png).decode("ascii")
    body = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                    {"type": "text", "text": PROMPT},
                ],
            }
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.1,
    }
    resp = requests.post(
        ENDPOINT,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=180,
    )
    if not resp.ok:
        raise GLMError(f"GLM {resp.status_code}: {resp.text[:300]}")
    return resp.json()


_CODE_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)


def _parse_content(text: str) -> dict[str, Any]:
    cleaned = _CODE_FENCE.sub("", text).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # Try to salvage the first {...} block.
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if m:
            return json.loads(m.group(0))
        raise


def _bbox_to_bounds(bbox: Any, img_w: int, img_h: int,
                    page_w_pt: float, page_h_pt: float) -> tuple[float, float, float, float] | None:
    if not isinstance(bbox, (list, tuple)) or len(bbox) < 4:
        return None
    try:
        x1, y1, x2, y2 = (float(v) for v in bbox[:4])
    except (TypeError, ValueError):
        return None
    if x2 <= x1 or y2 <= y1:
        return None
    px_to_pt_x = page_w_pt / img_w
    px_to_pt_y = page_h_pt / img_h
    x0 = x1 * px_to_pt_x
    x1p = x2 * px_to_pt_x
    # Y-flip: top-left pixels -> bottom-left points
    y0 = page_h_pt - (y2 * px_to_pt_y)
    y1p = page_h_pt - (y1 * px_to_pt_y)
    return x0, y0, x1p, y1p


def _normalize(per_page: list[dict[str, Any]]) -> dict[str, Any]:
    """per_page items: {"width_pt", "height_pt", "img_w", "img_h", "raw"}"""
    pages_meta: list[dict[str, float]] = []
    elements: list[dict[str, Any]] = []
    for idx, entry in enumerate(per_page):
        page_w = float(entry["width_pt"])
        page_h = float(entry["height_pt"])
        img_w = int(entry["img_w"])
        img_h = int(entry["img_h"])
        pages_meta.append({"width": page_w, "height": page_h})

        raw = entry.get("raw") or {}
        # Some responses wrap under "elements", some under "result", some plain list.
        items: list[Any] = []
        if isinstance(raw, dict):
            items = raw.get("elements") or raw.get("result") or raw.get("items") or []
        elif isinstance(raw, list):
            items = raw

        for it in items:
            if not isinstance(it, dict):
                continue
            bounds = _bbox_to_bounds(it.get("bbox") or it.get("bBox"),
                                     img_w, img_h, page_w, page_h)
            if not bounds:
                continue
            category = str(it.get("category") or it.get("type") or "Unknown")
            elements.append({
                "Page": idx,
                "Bounds": list(bounds),
                "Path": f"//GLM/{category}",
                "Text": str(it.get("text") or "").strip(),
            })

    return {"pages": pages_meta, "elements": elements, "source": "glm-ocr"}


def extract_pdf_glm(pdf_bytes: bytes, api_key: str, model: str = "glm-4v-plus",
                    raw_out_path: str | None = None) -> dict[str, Any]:
    per_page: list[dict[str, Any]] = []
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        for page in doc:
            page_w_pt = page.rect.width
            page_h_pt = page.rect.height
            png, img_w, img_h = _render_page_png(page, RENDER_DPI)
            resp = _call_glm(png, api_key, model)
            content = ""
            try:
                content = resp["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError):
                content = ""
            raw = _parse_content(content) if content else {}
            per_page.append({
                "width_pt": page_w_pt,
                "height_pt": page_h_pt,
                "img_w": img_w,
                "img_h": img_h,
                "raw": raw,
            })
    finally:
        doc.close()

    if raw_out_path:
        with open(raw_out_path, "w") as f:
            json.dump(per_page, f)
    return _normalize(per_page)


def normalize_cached(raw_path: str) -> dict[str, Any]:
    with open(raw_path) as f:
        return _normalize(json.load(f))
