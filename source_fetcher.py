"""Discover, download, and validate current-date UniCommerce exports.

Exact Gmail URLs are used when optional OAuth credentials are configured.
Otherwise the known UniCommerce CloudFront timestamp windows are scanned.
All downloads are staged as .part files and promoted only after validation.
"""

from __future__ import annotations

import base64
import html
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests

from secondary_sales import validate_channel_sales_attachment


@dataclass(frozen=True)
class ReportSpec:
    key: str
    label: str
    base_urls: tuple[str, ...]
    encoded_prefix: str
    filename_prefix: str
    expected_header: str
    min_size: int
    min_rows: int
    scan_windows: tuple[tuple[time, time], ...]


REPORTS = (
    ReportSpec(
        key="fg",
        label="FG INVENTORY REPORT",
        base_urls=(
            "https://dawxwb4zstkgp.cloudfront.net/mosaicwellnesspvtlmt/68b536bc0c49eb100391e690/",
        ),
        encoded_prefix="FG%20INVENTORY%20REPORT_",
        filename_prefix="FG INVENTORY REPORT_",
        expected_header="Category",
        min_size=5_000_000,
        min_rows=50_000,
        scan_windows=((time(10, 15), time(11, 35)),),
    ),
    ReportSpec(
        key="shelfwise",
        label="Shelfwise Inventory",
        base_urls=(
            "https://dawxwb4zstkgp.cloudfront.net/mosaicwellnesspvtlmt/6a7d81674802e2705fbcac26/",
            "https://dawxwb4zstkgp.cloudfront.net/mosaicwellnesspvtlmt/6a7d5c8dbc51c639a98291e2/",
            "https://dawxwb4zstkgp.cloudfront.net/mosaicwellnesspvtlmt/68b53674df802e6cada98132/",
        ),
        encoded_prefix="Shelfwise%20Inventory_",
        filename_prefix="Shelfwise Inventory_",
        expected_header="Facility",
        min_size=10_000_000,
        min_rows=80_000,
        scan_windows=(
            (time(9, 45), time(10, 10)),
            (time(10, 15), time(11, 35)),
        ),
    ),
    ReportSpec(
        key="sale_orders",
        label="Copy of Sale Orders",
        base_urls=(
            "https://dawxwb4zstkgp.cloudfront.net/mosaicwellnesspvtlmt/6a7db62ea3db7d0e7020c599/",
            "https://dawxwb4zstkgp.cloudfront.net/mosaicwellnesspvtlmt/6a67443a34c0486d4e59acb0/",
            "https://dawxwb4zstkgp.cloudfront.net/mosaicwellnesspvtlmt/6a5bade540eac204cbabb319/",
            "https://dawxwb4zstkgp.cloudfront.net/mosaicwellnesspvtlmt/6a58a54cbced20668b5e3a08/",
            "https://dawxwb4zstkgp.cloudfront.net/mosaicwellnesspvtlmt/69400f452b43af563cc7e64a/",
            "https://dawxwb4zstkgp.cloudfront.net/mosaicwellnesspvtlmt/6a67432f7644251ef45b5279/",
        ),
        encoded_prefix="Copy%20of%20Sale%20Orders_",
        filename_prefix="Copy of Sale Orders_",
        expected_header="Display Order Code",
        min_size=5_000_000,
        min_rows=10_000,
        scan_windows=(
            (time(9, 45), time(10, 10)),
            (time(10, 45), time(11, 5)),
        ),
    ),
)


def _stamps(
    run_date: date,
    windows: tuple[tuple[time, time], ...],
) -> list[str]:
    output: list[str] = []
    for start_time, end_time in windows:
        current = datetime.combine(run_date, start_time)
        end = datetime.combine(run_date, end_time)
        while current <= end:
            output.append(current.strftime("%d%m%Y%H%M%S"))
            current += timedelta(seconds=1)
    return output


def _url_exists(url: str) -> bool:
    try:
        response = requests.head(
            url,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "*/*"},
            allow_redirects=True,
            timeout=(3, 5),
        )
        return 200 <= response.status_code < 400
    except requests.RequestException:
        return False


def scan_cloudfront(spec: ReportSpec, run_date: date) -> str:
    stamps = _stamps(run_date, spec.scan_windows)
    for base_url in spec.base_urls:
        candidates = [
            (stamp, f"{base_url}{spec.encoded_prefix}{stamp}.csv")
            for stamp in stamps
        ]
        hits: list[tuple[str, str]] = []
        with ThreadPoolExecutor(max_workers=24) as pool:
            futures = {
                pool.submit(_url_exists, url): (stamp, url)
                for stamp, url in candidates
            }
            for future in as_completed(futures):
                stamp, url = futures[future]
                if future.result():
                    hits.append((stamp, url))
        if hits:
            return sorted(hits)[-1][1]
    raise RuntimeError(
        f"No current-date {spec.label} export was found in the approved windows."
    )


def verify_csv(path: Path, spec: ReportSpec, run_date: date) -> dict[str, object]:
    if path.suffix.lower() == ".part" or path.name.lower().endswith(".part"):
        raise ValueError(f"Partial download cannot be used: {path.name}")
    stamp_match = re.fullmatch(
        rf"{re.escape(spec.filename_prefix)}(\d{{14}})\.csv",
        path.name,
        flags=re.IGNORECASE,
    )
    if not stamp_match:
        raise ValueError(f"Unexpected {spec.label} filename: {path.name}")
    source_stamp = datetime.strptime(stamp_match.group(1), "%d%m%Y%H%M%S")
    if source_stamp.date() != run_date:
        raise ValueError(
            f"Stale {spec.label} source: {source_stamp.date()} != {run_date}"
        )
    with path.open("rb") as handle:
        header = handle.readline().decode("utf-8-sig", errors="replace").strip()
        rows = 1 + sum(
            chunk.count(b"\n")
            for chunk in iter(lambda: handle.read(1024 * 1024), b"")
        )
    size = path.stat().st_size
    failures = []
    if size < spec.min_size:
        failures.append(f"{size:,} bytes < {spec.min_size:,}")
    if rows < spec.min_rows:
        failures.append(f"{rows:,} rows < {spec.min_rows:,}")
    if not header.startswith(spec.expected_header):
        failures.append(
            f"header starts {header[:80]!r}, expected {spec.expected_header!r}"
        )
    if failures:
        raise ValueError(f"Invalid {spec.label} file {path.name}: " + "; ".join(failures))
    return {
        "file": path.name,
        "source_timestamp": source_stamp.isoformat(),
        "size_bytes": size,
        "rows": rows,
        "header": header[:160],
    }


def download_and_verify(
    url: str,
    spec: ReportSpec,
    output_dir: Path,
    run_date: date,
) -> tuple[Path, dict[str, object]]:
    filename = unquote(Path(urlparse(url).path).name)
    destination = output_dir / filename
    output_dir.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        try:
            return destination, verify_csv(destination, spec, run_date)
        except ValueError:
            pass
    partial = destination.with_suffix(destination.suffix + ".part")
    partial.unlink(missing_ok=True)
    try:
        with requests.get(
            url,
            stream=True,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=(10, 240),
        ) as response:
            response.raise_for_status()
            with partial.open("wb") as handle:
                for chunk in response.iter_content(1024 * 1024):
                    if chunk:
                        handle.write(chunk)
        partial.replace(destination)
        return destination, verify_csv(destination, spec, run_date)
    except Exception:
        partial.unlink(missing_ok=True)
        raise


def _decode_part(value: str | None) -> str:
    if not value:
        return ""
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding).decode("utf-8", errors="replace")


def _gmail_text(payload: dict) -> str:
    parts = payload.get("parts") or []
    body = payload.get("body", {}).get("data")
    chunks = [_decode_part(body)] if body else []
    for part in parts:
        chunks.append(_gmail_text(part))
    return "\n".join(chunks)


def _gmail_access_token() -> str | None:
    client_id = os.getenv("GMAIL_CLIENT_ID", "").strip()
    client_secret = os.getenv("GMAIL_CLIENT_SECRET", "").strip()
    refresh_token = os.getenv("GMAIL_REFRESH_TOKEN", "").strip()
    if not all((client_id, client_secret, refresh_token)):
        return None
    token_response = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=20,
    )
    token_response.raise_for_status()
    return str(token_response.json()["access_token"])


def gmail_export_urls(run_date: date) -> dict[str, str]:
    """Return exact current-date URLs when optional Gmail OAuth is configured."""
    token = _gmail_access_token()
    if not token:
        return {}
    headers = {"Authorization": f"Bearer {token}"}
    next_date = run_date + timedelta(days=1)
    query = (
        f"after:{run_date:%Y/%m/%d} before:{next_date:%Y/%m/%d} "
        "from:(noreply@e.unicommerce.com)"
    )
    listing = requests.get(
        "https://gmail.googleapis.com/gmail/v1/users/me/messages",
        headers=headers,
        params={"q": query, "maxResults": 50},
        timeout=30,
    )
    listing.raise_for_status()
    urls: dict[str, str] = {}
    url_pattern = re.compile(r"https://dawxwb4zstkgp\.cloudfront\.net/[^\s\"'<>]+")
    for message in listing.json().get("messages", []):
        detail = requests.get(
            f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{message['id']}",
            headers=headers,
            params={"format": "full"},
            timeout=30,
        )
        detail.raise_for_status()
        content = html.unescape(_gmail_text(detail.json().get("payload", {})))
        for raw_url in url_pattern.findall(content):
            url = raw_url.rstrip(").,;")
            filename = unquote(Path(urlparse(url).path).name)
            for spec in REPORTS:
                match = re.fullmatch(
                    rf"{re.escape(spec.filename_prefix)}(\d{{14}})\.csv",
                    filename,
                    flags=re.IGNORECASE,
                )
                if not match:
                    continue
                stamp = datetime.strptime(match.group(1), "%d%m%Y%H%M%S")
                if stamp.date() == run_date:
                    existing = urls.get(spec.key)
                    if not existing or filename > unquote(Path(urlparse(existing).path).name):
                        urls[spec.key] = url
    return urls


def _gmail_parts(payload: dict) -> list[dict]:
    output = [payload]
    for part in payload.get("parts") or []:
        output.extend(_gmail_parts(part))
    return output


def fetch_channel_sales_attachment(
    run_date: date,
    output_dir: Path,
) -> tuple[Path, dict[str, object]]:
    token = _gmail_access_token()
    if not token:
        raise RuntimeError(
            "Gmail OAuth is required for the Channel Sales Tracker attachment."
        )
    headers = {"Authorization": f"Bearer {token}"}
    next_date = run_date + timedelta(days=1)
    sender = "anshul.bhatkar@mosaicwellness.in"
    query = (
        f"after:{run_date:%Y/%m/%d} before:{next_date:%Y/%m/%d} "
        f"from:{sender} subject:(Channel Sales Tracker Dump) has:attachment"
    )
    listing = requests.get(
        "https://gmail.googleapis.com/gmail/v1/users/me/messages",
        headers=headers,
        params={"q": query, "maxResults": 20},
        timeout=30,
    )
    listing.raise_for_status()
    expected_filename = f"Channel Sales Tracker Dump_{run_date:%Y-%m-%d}.xlsx"
    candidates: list[tuple[int, str, str]] = []
    for message in listing.json().get("messages", []):
        detail = requests.get(
            f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{message['id']}",
            headers=headers,
            params={"format": "full"},
            timeout=30,
        )
        detail.raise_for_status()
        body = detail.json()
        payload = body.get("payload", {})
        message_headers = {
            str(item.get("name", "")).casefold(): str(item.get("value", ""))
            for item in payload.get("headers") or []
        }
        if sender not in message_headers.get("from", "").casefold():
            continue
        if run_date.isoformat() not in message_headers.get("subject", ""):
            continue
        for part in _gmail_parts(payload):
            filename = str(part.get("filename") or "")
            attachment_id = str((part.get("body") or {}).get("attachmentId") or "")
            if filename == expected_filename and attachment_id:
                candidates.append(
                    (int(body.get("internalDate") or 0), message["id"], attachment_id)
                )
    if not candidates:
        raise RuntimeError(
            f"No current-date {expected_filename} attachment was found from {sender}."
        )
    _, message_id, attachment_id = sorted(candidates)[-1]
    attachment = requests.get(
        "https://gmail.googleapis.com/gmail/v1/users/me/messages/"
        f"{message_id}/attachments/{attachment_id}",
        headers=headers,
        timeout=120,
    )
    attachment.raise_for_status()
    encoded = str(attachment.json().get("data") or "")
    padding = "=" * (-len(encoded) % 4)
    content = base64.urlsafe_b64decode(encoded + padding)
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / expected_filename
    partial = destination.with_suffix(destination.suffix + ".part")
    partial.unlink(missing_ok=True)
    try:
        partial.write_bytes(content)
        partial.replace(destination)
        checks = validate_channel_sales_attachment(destination, run_date)
        checks["sender"] = sender
        checks["message_id"] = message_id
        checks["discovery"] = "Gmail exact attachment"
        return destination, checks
    except Exception:
        partial.unlink(missing_ok=True)
        destination.unlink(missing_ok=True)
        raise


def fetch_current_sources(
    run_date: date,
    output_dir: Path,
    *,
    include_channel_sales: bool = False,
) -> tuple[dict[str, Path], dict[str, dict[str, object]]]:
    gmail_urls = gmail_export_urls(run_date)
    paths: dict[str, Path] = {}
    evidence: dict[str, dict[str, object]] = {}
    for spec in REPORTS:
        url = gmail_urls.get(spec.key)
        discovery = "Gmail exact export URL"
        if not url:
            url = scan_cloudfront(spec, run_date)
            discovery = "CloudFront timestamp scan"
        path, checks = download_and_verify(url, spec, output_dir, run_date)
        checks["discovery"] = discovery
        paths[spec.key] = path
        evidence[spec.key] = checks
    if include_channel_sales:
        path, checks = fetch_channel_sales_attachment(run_date, output_dir)
        paths["channel_sales"] = path
        evidence["channel_sales"] = checks
    return paths, evidence


def validate_local_sources(
    run_date: date,
    fg_path: Path,
    shelfwise_path: Path,
    sale_orders_path: Path,
    channel_sales_path: Path | None = None,
) -> tuple[dict[str, Path], dict[str, dict[str, object]]]:
    supplied = {
        "fg": fg_path,
        "shelfwise": shelfwise_path,
        "sale_orders": sale_orders_path,
    }
    evidence = {
        spec.key: verify_csv(supplied[spec.key], spec, run_date)
        for spec in REPORTS
    }
    for checks in evidence.values():
        checks["discovery"] = "Explicit local source"
    if channel_sales_path:
        supplied["channel_sales"] = channel_sales_path
        checks = validate_channel_sales_attachment(channel_sales_path, run_date)
        checks["discovery"] = "Explicit local source"
        evidence["channel_sales"] = checks
    return supplied, evidence
