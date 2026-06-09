"""Roboflow-hosted DocLayout-YOLO inference.

Renders each PDF page to PNG, sends to Roboflow's hosted detect endpoint,
and normalizes the response into the same shape the viewer uses for Adobe
Extract: {pages: [{width, height}], elements: [{Page, Bounds, Path, ...}]}.

Bounds are in PDF points with bottom-left origin to match Adobe's format,
so the existing frontend renderer works unchanged.
"""

from __future__ import annotations

import base64
from typing import Any

import fitz  # PyMuPDF
import requests


ROBOFLOW_DETECT_URL = "https://detect.roboflow.com"
RENDER_DPI = 200  # higher = sharper detection, more bytes uploaded
CONFIDENCE = 15   # percent (Roboflow uses 0-100). Lower = more boxes, more noise.
OVERLAP = 50      # NMS IoU threshold, percent.


class RoboflowError(RuntimeError):
    pass


def _render_page_png(page: fitz.Page, dpi: int) -> bytes:
    zoom = dpi / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    return pix.tobytes("png")


def _normalize_model(model: str) -> str:
    # Roboflow's hosted detect endpoint expects "<project>/<version>".
    # Accept "<workspace>/<project>/<version>" too and strip the workspace.
    parts = [p for p in model.strip("/").split("/") if p]
    if len(parts) == 3:
        parts = parts[1:]
    return "/".join(parts)


def _call_roboflow(png_bytes: bytes, model: str, api_key: str) -> dict[str, Any]:
    url = f"{ROBOFLOW_DETECT_URL}/{_normalize_model(model)}"
    b64 = base64.b64encode(png_bytes).decode("ascii")
    resp = requests.post(
        url,
        params={
            "api_key": api_key,
            "confidence": CONFIDENCE,
            "overlap": OVERLAP,
        },
        data=b64,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=60,
    )
    if not resp.ok:
        raise RoboflowError(f"Roboflow {resp.status_code} for {url}: {resp.text[:300]}")
    return resp.json()


def extract_pdf_layout(pdf_path: str, api_key: str, model: str) -> dict[str, Any]:
    """Run DocLayout-YOLO over every page; return Adobe-shaped JSON."""
    doc = fitz.open(pdf_path)
    pages_meta: list[dict[str, float]] = []
    elements: list[dict[str, Any]] = []
    try:
        for page_idx, page in enumerate(doc):
            page_w_pt = page.rect.width
            page_h_pt = page.rect.height
            pages_meta.append({"width": page_w_pt, "height": page_h_pt})

            png = _render_page_png(page, RENDER_DPI)
            result = _call_roboflow(png, model, api_key)

            img_w = result.get("image", {}).get("width") or page_w_pt * RENDER_DPI / 72
            img_h = result.get("image", {}).get("height") or page_h_pt * RENDER_DPI / 72
            # Conversion factors from rendered pixels to PDF points
            px_to_pt_x = page_w_pt / img_w
            px_to_pt_y = page_h_pt / img_h

            for pred in result.get("predictions", []):
                # Roboflow gives center-x, center-y, width, height in pixel coords
                # with top-left origin.
                cx = float(pred["x"]) * px_to_pt_x
                cy = float(pred["y"]) * px_to_pt_y
                w = float(pred["width"]) * px_to_pt_x
                h = float(pred["height"]) * px_to_pt_y
                x0_pt = cx - w / 2
                x1_pt = cx + w / 2
                # Flip Y: top-left pixel origin -> bottom-left point origin
                y_top_pt = cy - h / 2
                y_bot_pt = cy + h / 2
                y0_pt = page_h_pt - y_bot_pt  # lower edge in PDF coords
                y1_pt = page_h_pt - y_top_pt  # upper edge

                cls = str(pred.get("class", "unknown"))
                elements.append({
                    "Page": page_idx,
                    "Bounds": [x0_pt, y0_pt, x1_pt, y1_pt],
                    "Path": f"//DocLayout/{cls}",
                    "Text": "",
                    "Confidence": float(pred.get("confidence", 0)),
                })
    finally:
        doc.close()

    return {"pages": pages_meta, "elements": elements, "source": "doclayout-yolo"}
