"""LightOnOCR-2-1B wrapper. Lazy-loads model on first call."""

from __future__ import annotations

import io
import threading
from typing import Any

_model = None
_processor = None
_device = None
_load_lock = threading.Lock()

MODEL_ID = "lightonai/LightOnOCR-2-1B"


def _load() -> tuple[Any, Any, str]:
    global _model, _processor, _device
    if _model is not None:
        assert _processor is not None and _device is not None
        return _model, _processor, _device
    with _load_lock:
        if _model is not None:
            assert _processor is not None and _device is not None
            return _model, _processor, _device
        import torch
        from transformers import AutoModelForImageTextToText, AutoProcessor

        device = "cuda" if torch.cuda.is_available() else "cpu"
        dtype = torch.bfloat16 if device == "cuda" else torch.float32
        processor = AutoProcessor.from_pretrained(MODEL_ID, trust_remote_code=True)
        model = AutoModelForImageTextToText.from_pretrained(
            MODEL_ID,
            torch_dtype=dtype,
            trust_remote_code=True,
        ).to(device)
        model.eval()
        _model, _processor, _device = model, processor, device
        return model, processor, device


def ocr_image(png_bytes: bytes, max_new_tokens: int = 1024) -> str:
    """Run LightOnOCR on a PNG image, return the model's text output."""
    import torch
    from PIL import Image

    model, processor, device = _load()
    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    conversation = [{"role": "user", "content": [{"type": "image", "image": img}]}]
    inputs = processor.apply_chat_template(
        conversation,
        add_generation_prompt=True,
        tokenize=True,
        return_dict=True,
        return_tensors="pt",
    ).to(device)
    with torch.inference_mode():
        out = model.generate(**inputs, max_new_tokens=max_new_tokens, do_sample=False)
    # Strip the prompt tokens from output
    input_len = inputs["input_ids"].shape[1]
    gen = out[0, input_len:]
    return processor.decode(gen, skip_special_tokens=True).strip()


def crop_pdf_region(pdf_path: str, page_num: int, bbox_pts: tuple[float, float, float, float],
                    dpi: int = 200, padding_pts: float = 4.0) -> bytes:
    """Crop a region of a PDF page to PNG bytes. bbox is in PDF points, bottom-left origin."""
    import fitz  # PyMuPDF
    x0, y0, x1, y1 = bbox_pts
    doc = fitz.open(pdf_path)
    try:
        page = doc[page_num]
        page_h = page.rect.height
        # Convert bottom-left PDF coords to top-left PyMuPDF coords, add padding
        rect = fitz.Rect(
            max(0, x0 - padding_pts),
            max(0, page_h - y1 - padding_pts),
            min(page.rect.width, x1 + padding_pts),
            min(page_h, page_h - y0 + padding_pts),
        )
        zoom = dpi / 72.0
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat, clip=rect, alpha=False)
        return pix.tobytes("png")
    finally:
        doc.close()
