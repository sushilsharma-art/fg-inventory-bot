"""End-to-end unattended build for the FG Inventory Cloud Bot."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from crypto_payload import decrypt_payload, encrypt_payload
from config_bundle import restore_config
from inventory_pipeline import (
    build_freshness,
    build_inventory_frame,
    build_payload,
    write_summary_workbook,
)
from history_seed import restore_history
from secondary_sales import (
    attach_previous_secondary_metrics,
    attach_secondary_metrics,
    build_secondary_sales,
)
from sales_history import import_manual_history
from source_fetcher import fetch_current_sources, validate_local_sources
from tableau_downloader import download_tableau_exports
from tableau_history_refresh import refresh_history


IST = ZoneInfo("Asia/Kolkata")
ROOT = Path(__file__).resolve().parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="Report date in YYYY-MM-DD. Defaults to today in IST.")
    parser.add_argument("--fg-csv", type=Path, help="Validated local FG source for testing/backfill.")
    parser.add_argument(
        "--shelfwise-csv",
        type=Path,
        help="Validated local Shelfwise source for testing/backfill.",
    )
    parser.add_argument(
        "--sale-orders-csv",
        type=Path,
        help="Validated local Copy of Sale Orders source for the current-date gate.",
    )
    parser.add_argument(
        "--channel-sales-xlsx",
        type=Path,
        help="Current-date Channel Sales Tracker attachment from Anshul Bhatkar.",
    )
    parser.add_argument(
        "--require-channel-sales",
        action="store_true",
        help="Block publication until the current-date Channel Sales attachment is present.",
    )
    parser.add_argument(
        "--refresh-tableau",
        action="store_true",
        help="Download and import the approved Tableau quantity and value crosstabs.",
    )
    parser.add_argument(
        "--require-tableau",
        action="store_true",
        help="Block publication if the Tableau download or reconciliation fails.",
    )
    parser.add_argument(
        "--master",
        type=Path,
        default=ROOT / "config" / "Location master.xlsx",
    )
    parser.add_argument("--site-dir", type=Path, default=ROOT / "site")
    parser.add_argument("--work-dir", type=Path, default=ROOT / "work")
    parser.add_argument(
        "--sales-history-db",
        type=Path,
        default=ROOT / "data" / "secondary_sales_history.sqlite",
    )
    parser.add_argument(
        "--history-qty-csv",
        type=Path,
        default=ROOT / "data" / "history_sources" / "EComm Overall Sales Qty level.csv",
    )
    parser.add_argument(
        "--history-value-csv",
        type=Path,
        default=ROOT / "data" / "history_sources" / "EComm Overall Sales value level.csv",
    )
    parser.add_argument("--previous-data-url")
    parser.add_argument("--passcode-file", type=Path)
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def _passcode(args: argparse.Namespace) -> str:
    value = os.getenv("FG_BOT_PASSCODE", "").strip()
    if not value and args.passcode_file:
        value = args.passcode_file.read_text(encoding="utf-8").strip()
    if len(value) < 8:
        raise ValueError(
            "FG_BOT_PASSCODE is missing or too short. Use at least 8 characters."
        )
    return value


def _default_previous_url() -> str | None:
    repository = os.getenv("GITHUB_REPOSITORY", "").strip()
    if "/" not in repository:
        return None
    owner, repo = repository.split("/", 1)
    if repo.casefold() == f"{owner}.github.io".casefold():
        return f"https://{owner}.github.io/data.enc.json"
    return f"https://{owner}.github.io/{repo}/data.enc.json"


def _load_previous(
    url: str | None,
    passcode: str,
    target: Path,
) -> tuple[dict | None, bytes | None, str | None]:
    if not url:
        if target.exists():
            try:
                blob = target.read_bytes()
                envelope = json.loads(blob.decode("utf-8"))
                payload = decrypt_payload(envelope, passcode)
                return payload, blob, "Using the existing local payload as history."
            except Exception as exc:
                return None, None, f"Existing local payload could not be reused: {exc}"
        return None, None, "No previous data URL is configured; starting a new history."
    try:
        separator = "&" if "?" in url else "?"
        response = requests.get(
            f"{url}{separator}v={datetime.now().timestamp():.0f}",
            headers={"Cache-Control": "no-cache"},
            timeout=30,
        )
        if response.status_code == 404:
            return None, None, "No deployed payload exists yet; starting a new history."
        response.raise_for_status()
        blob = response.content
        envelope = json.loads(blob.decode("utf-8"))
        payload = decrypt_payload(envelope, passcode)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob)
        return payload, blob, None
    except Exception as exc:
        return None, None, f"Previous payload could not be reused: {exc}"


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=path.name + ".",
        suffix=".tmp",
        delete=False,
    ) as handle:
        json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
        temp_path = Path(handle.name)
    temp_path.replace(path)


def _status(
    *,
    state: str,
    run_date: date,
    generated_at: str,
    payload_hash: str | None,
    details: dict | None = None,
) -> dict:
    value = {
        "status": state,
        "report_date": run_date.isoformat(),
        "generated_at": generated_at,
        "timezone": "Asia/Kolkata",
        "payload_sha256": payload_hash,
    }
    if details:
        value["details"] = details
    return value


def main() -> int:
    args = parse_args()
    run_date = (
        date.fromisoformat(args.date)
        if args.date
        else datetime.now(IST).date()
    )
    local_inputs = [args.fg_csv, args.shelfwise_csv, args.sale_orders_csv]
    if any(local_inputs) and not all(local_inputs):
        raise ValueError(
            "--fg-csv, --shelfwise-csv, and --sale-orders-csv must be supplied together."
        )
    if args.channel_sales_xlsx and not all(local_inputs):
        raise ValueError(
            "--channel-sales-xlsx requires the three current-date daily CSV inputs."
        )
    if args.channel_sales_xlsx and (args.refresh_tableau or args.require_tableau):
        raise ValueError(
            "Use either --channel-sales-xlsx or the automated Tableau refresh, not both."
        )
    passcode = _passcode(args)
    restored_config = restore_config(
        ROOT / "config" / "config_bundle.enc.json",
        args.master.parent,
        passcode,
    )
    if restored_config:
        print("Restored the encrypted facility-mapping configuration.")
    if not args.master.exists():
        raise FileNotFoundError(f"Location master not found: {args.master}")
    restored_seed = restore_history(
        ROOT / "data" / "history_seed.enc.json",
        args.sales_history_db,
        passcode,
    )
    if restored_seed:
        print("Restored the encrypted secondary-sales history seed.")
    args.site_dir.mkdir(parents=True, exist_ok=True)
    dated_work = args.work_dir / run_date.isoformat()
    dated_work.mkdir(parents=True, exist_ok=True)

    previous_url = (
        args.previous_data_url
        or os.getenv("FG_BOT_DATA_URL", "").strip()
        or _default_previous_url()
    )
    deployed_path = args.site_dir / "data.enc.json"
    previous, previous_blob, previous_warning = _load_previous(
        previous_url,
        passcode,
        deployed_path,
    )
    if previous_warning:
        print(f"WARNING: {previous_warning}")
    previous_secondary = previous.get("secondarySales", {}) if previous else {}
    previous_has_current_sales = (
        previous_secondary.get("sourceFile")
        == f"Channel Sales Tracker Dump_{run_date:%Y-%m-%d}.xlsx"
    )
    if (
        previous
        and previous.get("dateKey") == run_date.isoformat()
        and not args.force
        and not args.refresh_tableau
        and not args.require_tableau
        and (not args.require_channel_sales or previous_has_current_sales)
    ):
        digest = hashlib.sha256(previous_blob or b"").hexdigest()
        status = _status(
            state="already-current",
            run_date=run_date,
            generated_at=datetime.now(IST).isoformat(),
            payload_hash=digest,
        )
        _atomic_json(args.site_dir / "status.json", status)
        _atomic_json(
            dated_work / "quality_report.json",
            {**status, "details": {"source": "existing deployed payload"}},
        )
        print(f"Current-date payload already exists for {run_date}; no rebuild needed.")
        return 0

    if args.fg_csv:
        sources, source_evidence = validate_local_sources(
            run_date,
            args.fg_csv.resolve(),
            args.shelfwise_csv.resolve(),
            args.sale_orders_csv.resolve(),
            args.channel_sales_xlsx.resolve() if args.channel_sales_xlsx else None,
        )
    else:
        sources, source_evidence = fetch_current_sources(
            run_date,
            dated_work / "sources",
            include_channel_sales=args.require_channel_sales,
        )

    tableau_quality = None
    if args.refresh_tableau or args.require_tableau:
        try:
            exports = download_tableau_exports(dated_work / "tableau_downloads")
            tableau_quality = refresh_history(
                Path(exports["quantity"]),
                Path(exports["value"]),
                history_db=args.sales_history_db,
                output_root=ROOT / "data" / "tableau_history",
                report_date=run_date,
            )
            channel_workbook = Path(tableau_quality["channel_workbook"])
            sources["channel_sales"] = channel_workbook
            source_evidence["channel_sales"] = {
                "source": "Tableau Cloud REST API",
                "file": channel_workbook.name,
                "data_through": tableau_quality["date_max"],
                "format_match": tableau_quality["format_match"],
                "quantity_view": exports["quantity_view"],
                "value_view": exports["value_view"],
            }
        except Exception as exc:
            if args.require_tableau:
                raise ValueError(f"Required Tableau refresh failed: {exc}") from exc
            print(f"WARNING: Tableau refresh failed; preserving prior Secondary metrics: {exc}")

    if args.require_channel_sales and "channel_sales" not in sources:
        raise ValueError("Current-date Channel Sales attachment is required.")

    frame, fg_quality = build_inventory_frame(sources["fg"], args.master)
    freshness, shelf_quality = build_freshness(
        sources["shelfwise"], args.master, run_date
    )
    secondary = None
    secondary_quality = None
    history_seed_quality = None
    if "channel_sales" in sources:
        if args.history_qty_csv.exists() and args.history_value_csv.exists():
            history_seed_quality = import_manual_history(
                args.sales_history_db,
                args.history_qty_csv,
                args.history_value_csv,
            )
        secondary, secondary_quality = build_secondary_sales(
            sources["channel_sales"],
            run_date,
            history_db=args.sales_history_db,
            previous_secondary=previous_secondary,
        )
        frame = attach_secondary_metrics(frame, secondary)
    elif previous_secondary and previous.get("skus"):
        frame = attach_previous_secondary_metrics(frame, previous["skus"])
        secondary_quality = {
            "carried_forward": True,
            "source_file": previous_secondary.get("sourceFile"),
            "data_through": previous_secondary.get("dataThrough"),
            "reason": (
                "No current-date Channel Sales attachment was available; "
                "the latest reviewed Secondary Sales metrics were preserved."
            ),
        }
    workbook_path = dated_work / f"FG_Inventory_Daily_{run_date:%d%m%Y}.xlsx"
    write_summary_workbook(frame, workbook_path, secondary)
    quality = {
        "publication_gate": "passed",
        "source_validation": source_evidence,
        "fg": fg_quality,
        "shelfwise": shelf_quality,
        "sale_orders": source_evidence["sale_orders"],
        "workbook": {
            "file": workbook_path.name,
            "size_bytes": workbook_path.stat().st_size,
        },
    }
    if secondary_quality:
        quality["secondary_sales"] = secondary_quality
    if history_seed_quality:
        quality["secondary_sales_history_seed"] = history_seed_quality
    if tableau_quality:
        quality["tableau_refresh"] = tableau_quality
    previous_history = previous.get("history") if previous else None
    payload = build_payload(
        frame,
        freshness,
        report_date=run_date,
        source_files={key: path.name for key, path in sources.items()},
        quality=quality,
        secondary=secondary,
        previous_secondary=previous_secondary,
        previous_history=previous_history,
    )
    if payload["dateKey"] != run_date.isoformat():
        raise ValueError("Payload date reconciliation failed.")
    if not payload["freshnessAvailable"]:
        raise ValueError("Freshness data is unavailable; publication blocked.")
    envelope = encrypt_payload(payload, passcode)
    _atomic_json(deployed_path, envelope)
    blob = deployed_path.read_bytes()
    digest = hashlib.sha256(blob).hexdigest()
    generated_at = datetime.now(IST).isoformat()
    status = _status(
        state="success",
        run_date=run_date,
        generated_at=generated_at,
        payload_hash=digest,
    )
    audit_details = {
            "skus": len(payload["skus"]),
            "rows": payload["rowCount"],
            "history_days": len(payload["history"]["days"]),
            "fg_source": sources["fg"].name,
            "shelfwise_source": sources["shelfwise"].name,
            "sale_orders_source": sources["sale_orders"].name,
            "channel_sales_source": sources.get("channel_sales", Path("")).name,
    }
    _atomic_json(args.site_dir / "status.json", status)
    _atomic_json(
        dated_work / "quality_report.json",
        {**status, "details": audit_details, "quality": quality},
    )
    print(
        f"SUCCESS: {run_date} | {len(payload['skus']):,} SKUs | "
        f"{payload['rowCount']:,} rows | {len(payload['history']['days'])} history days"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"PUBLICATION BLOCKED: {error}", file=sys.stderr)
        raise
