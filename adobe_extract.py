"""Minimal REST client for Adobe PDF Extract API.

Flow: IMS token -> create asset -> PUT PDF -> submit extractpdf job ->
poll for completion -> download result ZIP -> read structuredData.json.
"""

from __future__ import annotations

import io
import time
import zipfile
import json
from typing import Any

import requests


IMS_TOKEN_URL = "https://ims-na1.adobelogin.com/ims/token/v3"
PDF_SERVICES_BASE = "https://pdf-services.adobe.io"


class ExtractError(RuntimeError):
    pass


def _get_access_token(client_id: str, client_secret: str) -> str:
    resp = requests.post(
        IMS_TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
            "scope": "openid,AdobeID,read_organizations",
        },
        timeout=30,
    )
    if not resp.ok:
        raise ExtractError(f"IMS token failed: {resp.status_code} {resp.text}")
    return resp.json()["access_token"]


def _auth_headers(token: str, client_id: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "x-api-key": client_id,
    }


def _create_asset(token: str, client_id: str) -> tuple[str, str]:
    resp = requests.post(
        f"{PDF_SERVICES_BASE}/assets",
        headers={**_auth_headers(token, client_id), "Content-Type": "application/json"},
        json={"mediaType": "application/pdf"},
        timeout=30,
    )
    if not resp.ok:
        raise ExtractError(f"Asset create failed: {resp.status_code} {resp.text}")
    data = resp.json()
    return data["assetID"], data["uploadUri"]


def _upload_pdf(upload_uri: str, pdf_bytes: bytes) -> None:
    resp = requests.put(
        upload_uri,
        data=pdf_bytes,
        headers={"Content-Type": "application/pdf"},
        timeout=120,
    )
    if not resp.ok:
        raise ExtractError(f"Asset upload failed: {resp.status_code} {resp.text}")


def _submit_extract_job(token: str, client_id: str, asset_id: str) -> str:
    resp = requests.post(
        f"{PDF_SERVICES_BASE}/operation/extractpdf",
        headers={**_auth_headers(token, client_id), "Content-Type": "application/json"},
        json={
            "assetID": asset_id,
            "elementsToExtract": ["text", "tables"],
            "getCharBounds": True,
            "includeStyling": True,
        },
        timeout=30,
    )
    if resp.status_code != 201:
        raise ExtractError(f"Extract submit failed: {resp.status_code} {resp.text}")
    location = resp.headers.get("location")
    if not location:
        raise ExtractError("Extract submit returned no Location header")
    return location


def _poll_job(token: str, client_id: str, status_url: str, timeout_s: int = 300) -> str:
    deadline = time.time() + timeout_s
    delay = 2
    while time.time() < deadline:
        resp = requests.get(status_url, headers=_auth_headers(token, client_id), timeout=30)
        if not resp.ok:
            raise ExtractError(f"Poll failed: {resp.status_code} {resp.text}")
        body = resp.json()
        status = body.get("status")
        if status == "done":
            download_uri = body.get("asset", {}).get("downloadUri") or body.get("content", {}).get("downloadUri")
            if not download_uri:
                raise ExtractError(f"Job done but no downloadUri: {body}")
            return download_uri
        if status == "failed":
            raise ExtractError(f"Extract job failed: {body}")
        time.sleep(delay)
        delay = min(delay * 1.5, 10)
    raise ExtractError("Extract job timed out")


def _download_structured_data(download_uri: str) -> dict[str, Any]:
    resp = requests.get(download_uri, timeout=120)
    if not resp.ok:
        raise ExtractError(f"Download failed: {resp.status_code}")
    content = resp.content
    # Adobe returns either a ZIP (with structuredData.json + renditions) or
    # the JSON directly, depending on the operation.
    if content[:4] == b"PK\x03\x04":
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            names = zf.namelist()
            target = next((n for n in names if n.endswith("structuredData.json")), None)
            if not target:
                raise ExtractError(f"structuredData.json not in result ZIP: {names}")
            with zf.open(target) as f:
                return json.load(f)
    # Fallback: assume raw JSON
    try:
        return json.loads(content)
    except json.JSONDecodeError as e:
        head = content[:200]
        raise ExtractError(f"Result is neither ZIP nor JSON: {e}; first bytes: {head!r}") from e


def extract_pdf(pdf_bytes: bytes, client_id: str, client_secret: str) -> dict[str, Any]:
    """Run the full extract pipeline and return the parsed structuredData.json."""
    token = _get_access_token(client_id, client_secret)
    asset_id, upload_uri = _create_asset(token, client_id)
    _upload_pdf(upload_uri, pdf_bytes)
    status_url = _submit_extract_job(token, client_id, asset_id)
    download_uri = _poll_job(token, client_id, status_url)
    return _download_structured_data(download_uri)
