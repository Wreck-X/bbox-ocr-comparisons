"""Datalab Marker API client.

Flow:
  1. POST /api/v1/marker (multipart: file, output_format=json)  -> request_check_url
  2. GET <request_check_url> (poll until status == "complete")
  3. Walk the nested JSON tree and emit each block as an element.

Datalab returns a nested tree: Document -> Page -> (Group | Text | Equation | ...).
Each block carries:
  - block_type:  e.g. "ListItem", "Equation", "TableGroup", "Picture", "Text"
  - polygon:     4 corner points in PDF-point units, TOP-LEFT origin
                 (e.g. a letter page is [[0,0],[612,0],[612,792],[0,792]])
  - html:        HTML representation of this block's content
  - children:    nested child blocks (lists, tables, figures)

We normalize to the same {pages, elements} shape used by Adobe / MinerU,
with bounds in PDF points / bottom-left origin so the viewer renders them.
"""

from __future__ import annotations

import html as html_lib
import json
import re
import time
from typing import Any

import requests


BASE = "https://www.datalab.to/api/v1/marker"


class DatalabError(RuntimeError):
    pass


def _polygon_to_bbox(polygon: list) -> tuple[float, float, float, float]:
    xs = [p[0] for p in polygon]
    ys = [p[1] for p in polygon]
    return min(xs), min(ys), max(xs), max(ys)


_TAG_RE = re.compile(r"<[^>]+>")
_REF_RE = re.compile(r"<content-ref[^>]*/?>", re.IGNORECASE)


def _strip_html(s: str | None) -> str:
    if not s:
        return ""
    # Strip content-ref placeholder tags and any other HTML tags.
    s = _REF_RE.sub("", s)
    s = _TAG_RE.sub("", s)
    return html_lib.unescape(s).strip()


def _submit(pdf_bytes: bytes, filename: str, api_key: str) -> str:
    files = {"file": (filename, pdf_bytes, "application/pdf")}
    data = {
        "output_format": "json",
        "disable_image_extraction": "true",
    }
    resp = requests.post(
        BASE,
        headers={"X-API-Key": api_key},
        files=files,
        data=data,
        timeout=120,
    )
    if not resp.ok:
        raise DatalabError(f"submit failed: {resp.status_code} {resp.text[:300]}")
    body = resp.json()
    check_url = body.get("request_check_url")
    if not check_url:
        raise DatalabError(f"no request_check_url in response: {body}")
    return check_url


def _poll(check_url: str, api_key: str, timeout_s: int = 600) -> dict[str, Any]:
    deadline = time.time() + timeout_s
    delay = 3.0
    last: Any = None
    while time.time() < deadline:
        resp = requests.get(check_url, headers={"X-API-Key": api_key}, timeout=30)
        if not resp.ok:
            raise DatalabError(f"poll failed: {resp.status_code} {resp.text[:300]}")
        body = resp.json()
        last = body
        status = body.get("status")
        if status == "complete":
            if not body.get("success", True):
                raise DatalabError(f"Datalab returned failure: {body.get('error') or body}")
            return body
        if status in {"failed", "error"}:
            raise DatalabError(f"Datalab job failed: {body.get('error') or body}")
        time.sleep(delay)
        delay = min(delay * 1.4, 15)
    raise DatalabError(f"poll timed out; last={last}")


def _emit(block: dict[str, Any], page_idx: int, page_h_pt: float,
          out: list[dict[str, Any]], parent_path: str = "") -> None:
    btype = block.get("block_type", "Unknown")
    path = f"{parent_path}/{btype}" if parent_path else f"//Datalab/{btype}"
    polygon = block.get("polygon")
    if polygon and len(polygon) >= 3:
        x0_pt, y0_top_pt, x1_pt, y1_top_pt = _polygon_to_bbox(polygon)
        # Y-flip: top-left point space -> bottom-left point space
        y0_pt = page_h_pt - y1_top_pt
        y1_pt = page_h_pt - y0_top_pt
        out.append({
            "Page": page_idx,
            "Bounds": [x0_pt, y0_pt, x1_pt, y1_pt],
            "Path": path,
            "Text": _strip_html(block.get("html")),
        })

    for child in block.get("children") or []:
        _emit(child, page_idx, page_h_pt, out, parent_path=path)


def _normalize(payload: dict[str, Any]) -> dict[str, Any]:
    """Walk the Document -> Page -> ... tree into our {pages, elements} shape."""
    pages_meta: list[dict[str, float]] = []
    elements: list[dict[str, Any]] = []

    # The "json" output_format puts the document tree under `json` (or sometimes
    # the body itself is the tree). Be defensive.
    tree = payload.get("json") or payload
    if isinstance(tree, str):
        tree = json.loads(tree)

    # Top level should be a Document with Page children.
    pages = []
    if isinstance(tree, dict):
        if tree.get("block_type") == "Document":
            pages = tree.get("children") or []
        elif tree.get("block_type") == "Page":
            pages = [tree]
        elif "children" in tree:
            # Some shapes wrap with a doc-level object
            pages = [c for c in (tree.get("children") or []) if c.get("block_type") == "Page"]

    for page_idx, page in enumerate(pages):
        polygon = page.get("polygon") or [[0, 0], [612, 0], [612, 792], [0, 792]]
        x0, y0, x1, y1 = _polygon_to_bbox(polygon)
        page_w_pt = x1 - x0
        page_h_pt = y1 - y0
        pages_meta.append({"width": page_w_pt, "height": page_h_pt})

        for child in page.get("children") or []:
            _emit(child, page_idx, page_h_pt, elements)

    return {"pages": pages_meta, "elements": elements, "source": "datalab-marker"}


def extract_pdf_datalab(pdf_bytes: bytes, filename: str, api_key: str,
                        raw_out_path: str | None = None) -> dict[str, Any]:
    check_url = _submit(pdf_bytes, filename, api_key)
    payload = _poll(check_url, api_key)
    if raw_out_path:
        with open(raw_out_path, "w") as f:
            json.dump(payload, f)
    return _normalize(payload)


def normalize_cached(raw_path: str) -> dict[str, Any]:
    with open(raw_path) as f:
        return _normalize(json.load(f))
