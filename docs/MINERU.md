# MinerU Cloud backend

How the MinerU Cloud (opendatalab) integration in this viewer works, end to end.

## Why MinerU
- Official opendatalab pipeline (the same team behind DocLayout-YOLO + MinerU2.5-Pro)
- Returns per-element bounding boxes **and** rendered content (text, table HTML, equation LaTeX) in one call
- Currently free during beta — 600 pages/file, 2000 pages/day at highest priority, 10K files/day
- No multipart upload: you request a presigned URL, then `PUT` the PDF to it

## Files
| Path | Role |
|---|---|
| `mineru_extract.py` | Standalone REST client + normalizer (no Flask deps) |
| `app.py` | Flask wiring: routes, background thread, caching |
| `static/app.js` | Frontend: backend dropdown, Run button, polling, renderer |
| `data/<uuid>/source.pdf` | The uploaded PDF |
| `data/<uuid>/mineru_middle.json` | Raw MinerU response (cached) |
| `data/<uuid>/mineru.json` | Normalized output the viewer consumes |

## Configuration

Set in `.env` (loaded via `python-dotenv`):

```bash
MINERU_API_TOKEN=eyJ0eXBlIjoiSldUIi...     # get one at https://mineru.net/apiManage/token
```

If unset, the **Run MinerU** button returns a 500 with a helpful error pointing to the token URL.

## End-to-end flow

```
User clicks "Run MinerU"
        │
        ▼
POST /api/pdfs/<id>/mineru   ──>  background thread (app.py: _run_mineru)
        │
        ▼
mineru_extract.extract_pdf_mineru(pdf_bytes, filename, token)
        │
        ├─ 1. POST /api/v4/file-urls/batch   ──>  batch_id + presigned PUT URL
        ├─ 2. PUT  <presigned_url>           ──>  uploads the PDF bytes
        ├─ 3. GET  /api/v4/extract-results/batch/{batch_id} (poll loop)
        │        until state == "done"        ──>  zip download URL
        └─ 4. GET  <zip_url>                  ──>  extract middle.json from ZIP
        │
        ▼
_normalize(middle, pdf_bytes) → {pages, elements} (Adobe-shaped)
        │
        ├─ writes raw middle.json to data/<uuid>/mineru_middle.json
        └─ writes normalized output to data/<uuid>/mineru.json
        │
        ▼
Frontend polls GET /api/pdfs/<id>/mineru/status
        when status == "done", fetches /api/pdfs/<id>/mineru
        switches the backend dropdown to "mineru"
        renders the bboxes
```

## Step-by-step details

### 1. Request upload URL — `_request_upload`
```http
POST https://mineru.net/api/v4/file-urls/batch
Authorization: Bearer <MINERU_API_TOKEN>
Content-Type: application/json

{
  "files": [{ "name": "doc.pdf", "data_id": "<uuid hex>" }],
  "model_version": "vlm",
  "enable_table": true,
  "enable_formula": true,
  "language": "en",
  "is_ocr": false
}
```

Returns:
```json
{
  "data": {
    "batch_id": "...",
    "file_urls": ["https://s3.../presigned-put-url"]
  }
}
```

`model_version: "vlm"` selects MinerU2.5-Pro (the VLM pipeline). The other option is `"pipeline"` (legacy, faster, lower quality).

### 2. Upload — `_put_pdf`
```http
PUT <presigned_url>
<raw PDF bytes>
```
No auth header, no content-type — the presigned URL embeds everything. 120s timeout for large uploads.

### 3. Poll for completion — `_poll_batch`
```http
GET https://mineru.net/api/v4/extract-results/batch/{batch_id}
Authorization: Bearer <MINERU_API_TOKEN>
```

Response when running:
```json
{ "data": { "extract_result": [{ "state": "running" }] } }
```

Response when done:
```json
{
  "data": {
    "extract_result": [{
      "state": "done",
      "full_zip_url": "https://cdn.../result.zip"
    }]
  }
}
```

Polling uses an exponential backoff from 3s up to 15s, capped at 600s total. State values: `pending` | `running` | `done` | `failed`.

### 4. Download + parse — `_download_middle_json`
The ZIP contains multiple files (`full.md`, `images/`, etc.). We pull `middle.json` (alias: `layout.json`) — that's the only file we need.

## `middle.json` structure

```jsonc
{
  "pdf_info": [
    {
      "page_size": [width_px, height_px],   // rendered page size in pixels
      "para_blocks": [                       // top-level layout blocks
        {
          "type": "text" | "title" | "list" | "image" | "table" | "equation",
          "bbox": [x0, y0, x1, y1],          // pixel coords, TOP-LEFT origin
          "lines": [
            { "spans": [{ "content": "the actual text" }] }
          ],
          "blocks": [ ... ]                  // nested children (list items, table cells)
        }
      ]
    }
  ]
}
```

**Key facts:**
- Coordinates are in **rendered pixels**, **top-left origin**. We need both scale + Y-flip to land in PDF points / bottom-left.
- `list` and `table` blocks are **containers** — their actual content lives in `blocks` (the child array). Each child has its own bbox + text.
- `equation` blocks store the LaTeX as `span.content`.
- `image` blocks have an `image_path` field pointing to a file in the ZIP (we don't extract image renditions — bbox is enough for the viewer).

## Normalization — `_normalize` + `_emit_block`

The viewer's renderer expects every backend to emit this shape:
```json
{
  "pages": [{ "width": pt, "height": pt }, ...],
  "elements": [
    {
      "Page": 0,
      "Bounds": [x0, y0, x1, y1],   // PDF points, bottom-left origin
      "Path":   "//MinerU/list/text",
      "Text":   "the rendered text"
    }
  ]
}
```

### Per-page setup
```python
pdf_w_pt, pdf_h_pt = pdf_pages[page_idx]   # source PDF dims, from PyMuPDF
img_w, img_h = page["page_size"]           # MinerU's rendered pixel size
scale_x = pdf_w_pt / img_w
scale_y = pdf_h_pt / img_h
```

Reading the PDF directly with PyMuPDF (not trusting `page_size` alone) protects against MinerU's renderer using a different page size than the source PDF expects.

### Per-block bbox conversion
```python
x0_pt = x0_px * scale_x
x1_pt = x1_px * scale_x
y0_pt = pdf_h_pt - y1_px * scale_y   # Y-flip
y1_pt = pdf_h_pt - y0_px * scale_y
```

### Recursive emission
```python
def _emit_block(block, page_idx, scale_x, scale_y, pdf_h_pt, out, parent_path=""):
    btype = block["type"]
    path  = f"{parent_path}/{btype}" if parent_path else f"//MinerU/{btype}"

    # emit THIS block
    out.append({
        "Page":   page_idx,
        "Bounds": [...],
        "Path":   path,
        "Text":   _block_text(block),
    })

    # recurse into children — each list item / table cell becomes its own element
    for child in block.get("blocks") or block.get("sub_blocks") or []:
        _emit_block(child, ..., parent_path=path)
```

A `list` block becomes:
- One element with `Path: //MinerU/list` (the envelope)
- Plus one element per item with `Path: //MinerU/list/text`

The parent envelope gets auto-hidden in the UI by the **"Hide nested containers"** filter, which suppresses any bbox that fully encloses ≥ 2 other bboxes. Result: the user sees only the leaf items.

### Text extraction
```python
def _block_text(block):
    parts = []
    for line in block.get("lines") or []:
        parts.append("".join(
            span.get("content") or span.get("text") or ""
            for span in line.get("spans") or []
        ))
    for sub in block.get("blocks") or block.get("sub_blocks") or []:
        parts.append(_block_text(sub))   # recurse for container blocks
    return "\n".join(p for p in parts if p).strip()
```

For `equation` blocks, `span.content` is the LaTeX (e.g. `\frac{a}{b}`), which is what shows in the right-side hover panel.

## Caching strategy

```python
raw_path = pdf_dir / "mineru_middle.json"
if raw_path.exists():
    # re-normalize from cached raw, no API call
    result = normalize_cached(str(raw_path), pdf_bytes)
else:
    # call MinerU, persist raw output
    result = extract_pdf_mineru(pdf_bytes, filename, token, raw_out_path=str(raw_path))
```

Why: the normalizer is the part most likely to change as you tune the viewer. Caching the raw response means iterating on `_emit_block` / `_block_text` costs zero credits — re-click **Run MinerU** and you get the new output instantly.

To force a fresh API call: delete `data/<uuid>/mineru_middle.json` and click Run MinerU again.

## Flask routes

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/pdfs/<id>/mineru` | Kick off the background job (idempotent — returns 202 if already running) |
| `GET` | `/api/pdfs/<id>/mineru` | Fetch the normalized JSON (404 if not done yet) |
| `GET` | `/api/pdfs/<id>/mineru/status` | `{ "status": "processing" \| "done" \| "failed" \| "none", "error": ... }` |

Job status lives in an in-process dict keyed by `"<pdf_id>:mineru"`. On server restart, status is recomputed from disk (`done` if `mineru.json` exists, else `none`).

## Frontend integration (`static/app.js`)

1. **Backend dropdown** has `mineru` as an option:
   ```js
   const backendPaths = { adobe: "extract", doclayout: "doclayout", mineru: "mineru", datalab: "datalab" };
   ```
2. **Run MinerU button** (`#run-mineru`) POSTs to `/api/pdfs/<id>/mineru`, then `pollBackend(id, "mineru")` starts a 2.5s-interval status poll.
3. When status flips to `done`, the dropdown auto-switches to `mineru` and the viewer re-fetches `/api/pdfs/<id>/mineru`.
4. The same `renderBoxes()` function renders the result — no MinerU-specific render code. Everything plugs into the existing pipeline because the shape matches Adobe's.

## Known limitations / things to watch
- **No retry on transient failure.** A single 5xx during polling kills the job — user has to click Run MinerU again.
- **600-page limit per file** is MinerU's, not ours. Larger PDFs return an error from `/file-urls/batch`.
- **`page_size` in middle.json is sometimes missing** for very simple docs — we fall back to PyMuPDF page dimensions.
- **Discarded blocks** (page headers/footers/numbers) are not included in `para_blocks` by default. Switch to `preproc_blocks` if you want to see everything.
- **Images aren't rendered** — we extract bboxes but not the cropped PNGs. Add ZIP-walking logic to `_download_middle_json` if you need the images.

## Quick test

```bash
.venv/bin/python -c "
from mineru_extract import extract_pdf_mineru
import os
pdf = open('data/<uuid>/source.pdf', 'rb').read()
result = extract_pdf_mineru(pdf, 'test.pdf', os.environ['MINERU_API_TOKEN'])
print(f\"{len(result['pages'])} pages, {len(result['elements'])} elements\")
print('types:', set(e['Path'].rsplit('/', 1)[-1] for e in result['elements']))
"
```
