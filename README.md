# Adobe PDF Extract Viewer

Upload PDFs, run them through Adobe's PDF Extract API, and explore the
returned structure as togglable bounding-box overlays on a pdf.js viewer.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
# edit .env and fill in your Adobe PDF Services credentials
```

Get credentials from <https://acrobatservices.adobe.com/dc-integration-creation-app-cdn/main.html>
(free tier includes 500 document transactions/month).

## Run

```bash
.venv/bin/python app.py
```

Then open <http://127.0.0.1:5000>.

## How it works

- **Upload** a PDF via the left sidebar. The server stashes it under
  `data/<uuid>/` and kicks off an Adobe Extract job in a background thread.
- **List** of uploads with live status (processing / done / failed) lives in
  the left sidebar — click any item to switch PDFs.
- **Viewer** renders pages with pdf.js. Adobe's `Bounds` (PDF points,
  bottom-left origin) are reprojected into CSS pixels and overlaid on the
  page as colored rectangles.
- **Right sidebar** lists every element type (`H1`, `P`, `Table`, `Figure`,
  `LBody`, …) with counts and a checkbox to toggle that layer. Hover any
  box to see its `Path` and extracted text.

## Layout

```
app.py                Flask server (routes + background extract jobs)
adobe_extract.py      REST client: IMS token, asset upload, poll, download
templates/index.html  Single-page UI shell
static/app.js         pdf.js viewer + bbox overlay logic
static/styles.css     Dark three-column layout
data/<uuid>/          source.pdf + extract.json + meta.json per upload
```

## Keyboard

- `←` / `→` — prev / next page
- `+` / `-` — zoom in / out
# bbox-ocr-comparisons
