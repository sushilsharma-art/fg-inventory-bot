"""Read the approved GRN Rolling sheet and project post-connection DOI."""

from __future__ import annotations

import os
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests


ETA_SHEET_NAME = "1. GRN Rolling"
ETA_SPREADSHEET_ID = "1h03MU8bEJpIGQwdzaWRoge_F7dDb59XCY5mQT8qJvi8"
ETA_SHEET_GID = "376294268"
ETA_SOURCE_URL = (
    "https://docs.google.com/spreadsheets/d/"
    f"{ETA_SPREADSHEET_ID}/export?format=csv&gid={ETA_SHEET_GID}"
)
ETA_OUTPUT_COLUMNS = [
    "Next Connection Date",
    "Next Connection Units",
    "Post Connection Secondary Overall DOI",
    "Post Connection Primary Overall DOI",
]

_DATE_HEADER = re.compile(r"^\s*(\d{1,2})[- ]([A-Za-z]{3})(?:[- ][A-Za-z]{3})?\s*$")
_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


def _sku_key(value: Any) -> str:
    return str(value or "").strip().upper()


def _header_date(value: Any, report_date: date) -> date | None:
    match = _DATE_HEADER.match(str(value or ""))
    if not match:
        return None
    day = int(match.group(1))
    month = _MONTHS.get(match.group(2).casefold())
    if month is None:
        return None
    year = report_date.year
    # Support a rolling plan that crosses New Year without changing its layout.
    if report_date.month >= 10 and month <= 3:
        year += 1
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _quantity(value: Any) -> float:
    text = str(value or "").strip().replace(",", "")
    if not text:
        return 0.0
    number = pd.to_numeric(text, errors="coerce")
    return 0.0 if pd.isna(number) else float(number)


def parse_eta_plan(
    source_csv: Path,
    report_date: date,
    *,
    min_skus: int = 100,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Return the first positive future connection for every SKU.

    Duplicate source rows are aggregated at SKU/date grain before selecting the
    earliest date, so split manufacturing lines cannot under- or overstate ETA.
    """
    raw = pd.read_csv(source_csv, header=None, dtype=object, keep_default_na=False)
    if raw.shape[0] < 4 or raw.shape[1] < 12:
        raise ValueError(
            f"GRN Rolling export is unexpectedly small: {raw.shape[0]} rows x "
            f"{raw.shape[1]} columns."
        )

    header_row = None
    sku_column = None
    for row_index in range(min(10, len(raw))):
        for column_index, value in enumerate(raw.iloc[row_index].tolist()):
            if str(value).strip().casefold() == "sku code":
                header_row = row_index
                sku_column = column_index
                break
        if header_row is not None:
            break
    if header_row is None or sku_column is None:
        raise ValueError("GRN Rolling export does not contain the 'SKU Code' header.")

    dated_columns: list[tuple[int, date]] = []
    for column_index, value in enumerate(raw.iloc[header_row].tolist()):
        parsed = _header_date(value, report_date)
        if parsed and parsed > report_date:
            dated_columns.append((column_index, parsed))
    if not dated_columns:
        raise ValueError(
            f"GRN Rolling export has no future date columns after {report_date}."
        )

    rows = raw.iloc[header_row + 1 :].copy()
    rows[sku_column] = rows[sku_column].map(_sku_key)
    rows = rows.loc[rows[sku_column].ne("")]
    distinct_skus = int(rows[sku_column].nunique())
    if distinct_skus < min_skus:
        raise ValueError(
            f"GRN Rolling SKU volume is unexpectedly low: {distinct_skus:,}."
        )

    connections: list[dict[str, Any]] = []
    for column_index, connection_date in dated_columns:
        quantities = rows[column_index].map(_quantity)
        positive = quantities.gt(0)
        if not positive.any():
            continue
        daily = pd.DataFrame(
            {
                "SkuCode": rows.loc[positive, sku_column],
                "Next Connection Date": connection_date,
                "Next Connection Units": quantities.loc[positive],
            }
        )
        connections.extend(daily.to_dict("records"))

    if connections:
        daily_plan = (
            pd.DataFrame(connections)
            .groupby(["SkuCode", "Next Connection Date"], as_index=False)[
                "Next Connection Units"
            ]
            .sum()
        )
        first_dates = daily_plan.groupby("SkuCode")["Next Connection Date"].transform(
            "min"
        )
        plan = daily_plan.loc[daily_plan["Next Connection Date"].eq(first_dates)].copy()
        plan = plan.sort_values(["Next Connection Date", "SkuCode"]).reset_index(drop=True)
    else:
        plan = pd.DataFrame(
            columns=["SkuCode", "Next Connection Date", "Next Connection Units"]
        )

    quality = {
        "status": "fresh",
        "sheet": ETA_SHEET_NAME,
        "source_file": source_csv.name,
        "source_rows": int(len(rows)),
        "distinct_skus": distinct_skus,
        "duplicate_sku_rows": int(len(rows) - distinct_skus),
        "future_date_start": min(value for _, value in dated_columns).isoformat(),
        "future_date_end": max(value for _, value in dated_columns).isoformat(),
        "upcoming_skus": int(len(plan)),
        "upcoming_units": round(float(plan["Next Connection Units"].sum()), 2),
    }
    return plan, quality


def download_eta_plan(
    output_dir: Path,
    report_date: date,
    *,
    source_url: str | None = None,
) -> tuple[Path, pd.DataFrame, dict[str, Any]]:
    """Download and validate the approved public CSV export."""
    url = (
        source_url
        or os.getenv("FG_BOT_ETA_CSV_URL", "").strip()
        or ETA_SOURCE_URL
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"GRN_Rolling_{report_date:%Y-%m-%d}.csv"
    partial = target.with_suffix(".csv.part")
    response = requests.get(url, timeout=(15, 90))
    response.raise_for_status()
    if len(response.content) < 1_000:
        raise ValueError(
            f"GRN Rolling download is unexpectedly small: {len(response.content):,} bytes."
        )
    partial.write_bytes(response.content)
    partial.replace(target)
    plan, quality = parse_eta_plan(target, report_date)
    quality["source_file"] = target.name
    quality["source_url"] = url
    quality["fetched_at"] = datetime.now(timezone.utc).isoformat()
    return target, plan, quality


def _attach_plan(frame: pd.DataFrame, plan: pd.DataFrame) -> pd.DataFrame:
    output = frame.copy()
    keyed_plan = plan.copy()
    if not keyed_plan.empty:
        keyed_plan["_sku_key"] = keyed_plan["SkuCode"].map(_sku_key)
        keyed_plan = keyed_plan.drop_duplicates("_sku_key").set_index("_sku_key")
    key = output["SkuCode"].map(_sku_key)
    for column in ["Next Connection Date", "Next Connection Units"]:
        mapping = keyed_plan[column] if column in keyed_plan.columns else pd.Series(dtype=object)
        output[column] = key.map(mapping)

    inventory_check = (
        output["Inventory Check"].fillna("").astype(str).str.strip().str.casefold().eq("yes")
    )
    locations = output["Location Name"].fillna("").astype(str).str.strip().str.casefold()
    eligible = inventory_check & ~locations.str.contains("rtv", na=False)
    stock = output.loc[eligible].groupby("SkuCode")[
        ["Stock on Hand", "Stock In Transfer"]
    ].sum().sum(axis=1)
    output["_eta_network_stock"] = output["SkuCode"].map(stock).fillna(0.0)
    incoming = pd.to_numeric(output["Next Connection Units"], errors="coerce")
    projected_stock = output["_eta_network_stock"] + incoming

    if "Secondary DRR" in output.columns:
        secondary_drr = pd.to_numeric(output["Secondary DRR"], errors="coerce")
    else:
        secondary_drr = pd.Series(0.0, index=output.index)
    primary_drr = pd.to_numeric(output["Overall DRR"], errors="coerce")
    output["Post Connection Secondary Overall DOI"] = np.where(
        incoming.notna() & secondary_drr.gt(0),
        projected_stock / secondary_drr,
        np.nan,
    )
    output["Post Connection Primary Overall DOI"] = np.where(
        incoming.notna() & primary_drr.gt(0),
        projected_stock / primary_drr,
        np.nan,
    )
    output.drop(columns="_eta_network_stock", inplace=True)
    for column in [
        "Next Connection Units",
        "Post Connection Secondary Overall DOI",
        "Post Connection Primary Overall DOI",
    ]:
        output[column] = pd.to_numeric(output[column], errors="coerce").round(0)
    return output


def attach_eta_metrics(frame: pd.DataFrame, plan: pd.DataFrame) -> pd.DataFrame:
    return _attach_plan(frame, plan)


def attach_previous_eta_metrics(
    frame: pd.DataFrame,
    previous_skus: list[dict[str, Any]],
    report_date: date,
) -> tuple[pd.DataFrame, int]:
    """Keep still-future ETA quantities while recalculating DOI on today's stock."""
    rows = []
    for item in previous_skus:
        try:
            connection_date = date.fromisoformat(str(item.get("nextEtaDate", "")))
            units = float(item.get("nextEtaUnits") or 0)
        except (TypeError, ValueError):
            continue
        if connection_date > report_date and units > 0:
            rows.append(
                {
                    "SkuCode": _sku_key(item.get("code")),
                    "Next Connection Date": connection_date,
                    "Next Connection Units": units,
                }
            )
    plan = pd.DataFrame(
        rows,
        columns=["SkuCode", "Next Connection Date", "Next Connection Units"],
    )
    return _attach_plan(frame, plan), len(plan)
