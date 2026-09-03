"""Durable secondary-sales history for channel and SKU-channel queries."""

from __future__ import annotations

import calendar
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


DIMENSIONS = [
    "child_sku",
    "product_name",
    "sub_category",
    "category",
    "brand",
    "channel_name",
]
DAILY_COLUMNS = [
    "entity_key",
    *DIMENSIONS,
    "order_date",
    "units",
    "sales_value",
]


def _clean_text(series: pd.Series) -> pd.Series:
    return series.fillna("").astype(str).str.strip()


def _entity_keys(frame: pd.DataFrame) -> pd.Series:
    sku = _clean_text(frame["child_sku"])
    product = _clean_text(frame["product_name"]).str.casefold()
    valid = sku.ne("") & sku.str.casefold().ne("unmapped")
    return sku.str.casefold().where(valid, "__unmapped__|" + product)


def _numeric(series: pd.Series) -> pd.Series:
    text = _clean_text(series)
    text = text.str.replace(",", "", regex=False).str.replace("₹", "", regex=False)
    text = text.str.replace(r"^\((.*)\)$", r"-\1", regex=True)
    return pd.to_numeric(text, errors="coerce").fillna(0.0)


def _numeric_nullable(series: pd.Series) -> pd.Series:
    text = _clean_text(series)
    text = text.str.replace(",", "", regex=False).str.replace("₹", "", regex=False)
    text = text.str.replace(r"^\((.*)\)$", r"-\1", regex=True)
    text = text.mask(text.eq(""))
    return pd.to_numeric(text, errors="coerce")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_wide(path: Path, measure: str) -> tuple[pd.DataFrame, dict[str, Any]]:
    wide = pd.read_csv(
        path,
        header=1,
        dtype=str,
        keep_default_na=False,
        encoding="utf-16",
        sep="\t",
    )
    wide.columns = [str(column).strip() for column in wide.columns]
    if len(wide.columns) < 8:
        raise ValueError(f"Historical sales file has too few columns: {path.name}")
    original_dimensions = list(wide.columns[:7])
    date_columns = list(wide.columns[7:])
    parsed_dates = pd.to_datetime(date_columns, dayfirst=True, errors="coerce")
    invalid_dates = [date_columns[i] for i, value in enumerate(parsed_dates) if pd.isna(value)]
    if invalid_dates:
        raise ValueError(
            f"Historical sales file contains invalid date columns: {', '.join(invalid_dates[:5])}"
        )

    total_row = wide.iloc[0]
    detail = wide.iloc[1:].copy()
    detail = detail.rename(
        columns={
            original_dimensions[0]: "child_sku",
            original_dimensions[1]: "product_name",
            original_dimensions[2]: "sub_category",
            original_dimensions[3]: "category",
            original_dimensions[4]: "brand",
            original_dimensions[5]: "channel_name",
        }
    )
    detail = detail[[*DIMENSIONS, *date_columns]]
    for column in DIMENSIONS:
        detail[column] = _clean_text(detail[column])
    if detail.duplicated(DIMENSIONS).any():
        raise ValueError(f"Duplicate historical dimension rows found in {path.name}")

    long = detail.melt(
        id_vars=DIMENSIONS,
        value_vars=date_columns,
        var_name="order_date",
        value_name=measure,
    )
    long["order_date"] = pd.to_datetime(
        long["order_date"], dayfirst=True, errors="coerce"
    ).dt.normalize()
    long[measure] = _numeric_nullable(long[measure])
    long = long.loc[long[measure].notna()].copy()

    daily_detail = long.groupby("order_date", as_index=False)[measure].sum()
    reported = pd.DataFrame(
        {
            "order_date": parsed_dates,
            "reported": _numeric(total_row[date_columns]).to_numpy(),
        }
    )
    reconciliation = daily_detail.merge(reported, on="order_date", how="outer")
    reconciliation["difference"] = reconciliation[measure] - reconciliation["reported"]
    quality = {
        "file": path.name,
        "sha256": _sha256(path),
        "detail_rows": int(len(detail)),
        "daily_grain_rows": int(len(long)),
        "date_min": parsed_dates.min().date().isoformat(),
        "date_max": parsed_dates.max().date().isoformat(),
        "distinct_days": int(len(date_columns)),
        "channels": int(detail["channel_name"].nunique()),
        "skus": int(detail["child_sku"].nunique()),
        "unmapped_dimension_rows": int(
            detail["child_sku"].str.casefold().isin(["", "unmapped"]).sum()
        ),
        "negative_cells": int(long[measure].lt(0).sum()),
        "daily_total_mismatch_days": int(reconciliation["difference"].abs().gt(0.01).sum()),
        "max_abs_daily_total_difference": float(
            reconciliation["difference"].abs().max()
        ),
    }
    return long, quality


def read_manual_history(
    qty_path: Path, value_path: Path
) -> tuple[pd.DataFrame, dict[str, Any]]:
    qty, qty_quality = _read_wide(qty_path, "units")
    value, value_quality = _read_wide(value_path, "sales_value")
    keys = [*DIMENSIONS, "order_date"]
    combined = qty.merge(value, on=keys, how="outer", indicator=True, validate="one_to_one")
    qty_only = int(combined["_merge"].eq("left_only").sum())
    value_only = int(combined["_merge"].eq("right_only").sum())
    if qty_only or value_only:
        raise ValueError(
            "Quantity and value history do not reconcile at SKU-channel-day grain: "
            f"{qty_only} quantity-only / {value_only} value-only rows."
        )
    combined.drop(columns="_merge", inplace=True)
    combined["entity_key"] = _entity_keys(combined)
    combined = combined[DAILY_COLUMNS].sort_values(
        ["order_date", "channel_name", "entity_key"]
    )
    quality = {
        "quantity": qty_quality,
        "value": value_quality,
        "matched_daily_grain_rows": int(len(combined)),
        "channels": int(combined["channel_name"].nunique()),
        "skus": int(combined["child_sku"].nunique()),
        "months": sorted(combined["order_date"].dt.strftime("%Y-%m").unique()),
    }
    return combined, quality


def read_current_attachment(path: Path) -> pd.DataFrame:
    raw = pd.read_excel(path, sheet_name="raw_data", usecols="A:I")
    raw.columns = [
        "order_date",
        "channel_name",
        "brand",
        "category",
        "sub_category",
        "product_name",
        "child_sku",
        "units",
        "sales_value",
    ]
    for column in DIMENSIONS:
        raw[column] = _clean_text(raw[column])
    raw["order_date"] = pd.to_datetime(raw["order_date"], errors="coerce").dt.normalize()
    raw["units"] = pd.to_numeric(raw["units"], errors="coerce").fillna(0.0)
    raw["sales_value"] = pd.to_numeric(raw["sales_value"], errors="coerce").fillna(0.0)
    raw = raw.loc[raw["order_date"].notna() & raw["channel_name"].ne("")].copy()
    raw["entity_key"] = _entity_keys(raw)
    grouped = (
        raw.groupby(["entity_key", "channel_name", "order_date"], as_index=False)
        .agg(
            child_sku=("child_sku", "first"),
            product_name=("product_name", "first"),
            sub_category=("sub_category", "first"),
            category=("category", "first"),
            brand=("brand", "first"),
            units=("units", "sum"),
            sales_value=("sales_value", "sum"),
        )
    )
    return grouped[DAILY_COLUMNS]


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS secondary_sales_daily (
            entity_key TEXT NOT NULL,
            child_sku TEXT NOT NULL,
            product_name TEXT NOT NULL,
            sub_category TEXT NOT NULL,
            category TEXT NOT NULL,
            brand TEXT NOT NULL,
            channel_name TEXT NOT NULL,
            order_date TEXT NOT NULL,
            units REAL NOT NULL,
            sales_value REAL NOT NULL,
            source_file TEXT NOT NULL,
            source_priority INTEGER NOT NULL,
            imported_at TEXT NOT NULL,
            PRIMARY KEY (entity_key, channel_name, order_date)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS source_imports (
            sha256 TEXT PRIMARY KEY,
            source_file TEXT NOT NULL,
            source_type TEXT NOT NULL,
            imported_at TEXT NOT NULL,
            row_count INTEGER NOT NULL,
            date_min TEXT NOT NULL,
            date_max TEXT NOT NULL,
            quality_json TEXT NOT NULL
        )
        """
    )
    return connection


def _upsert(
    db_path: Path,
    frame: pd.DataFrame,
    *,
    source_file: str,
    source_type: str,
    source_hash: str,
    source_priority: int,
    quality: dict[str, Any],
) -> bool:
    imported_at = datetime.now(timezone.utc).isoformat()
    connection = _connect(db_path)
    try:
        existing = connection.execute(
            "SELECT 1 FROM source_imports WHERE sha256 = ?", (source_hash,)
        ).fetchone()
        if existing:
            return False
        records = []
        for row in frame.itertuples(index=False):
            records.append(
                (
                    str(row.entity_key),
                    str(row.child_sku),
                    str(row.product_name),
                    str(row.sub_category),
                    str(row.category),
                    str(row.brand),
                    str(row.channel_name),
                    pd.Timestamp(row.order_date).date().isoformat(),
                    float(row.units),
                    float(row.sales_value),
                    source_file,
                    source_priority,
                    imported_at,
                )
            )
        connection.executemany(
            """
            INSERT INTO secondary_sales_daily (
                entity_key, child_sku, product_name, sub_category, category, brand,
                channel_name, order_date, units, sales_value, source_file,
                source_priority, imported_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(entity_key, channel_name, order_date) DO UPDATE SET
                child_sku = excluded.child_sku,
                product_name = excluded.product_name,
                sub_category = excluded.sub_category,
                category = excluded.category,
                brand = excluded.brand,
                units = excluded.units,
                sales_value = excluded.sales_value,
                source_file = excluded.source_file,
                source_priority = excluded.source_priority,
                imported_at = excluded.imported_at
            WHERE excluded.source_priority >= secondary_sales_daily.source_priority
            """,
            records,
        )
        connection.execute(
            """
            INSERT INTO source_imports (
                sha256, source_file, source_type, imported_at, row_count,
                date_min, date_max, quality_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_hash,
                source_file,
                source_type,
                imported_at,
                len(frame),
                pd.Timestamp(frame["order_date"].min()).date().isoformat(),
                pd.Timestamp(frame["order_date"].max()).date().isoformat(),
                json.dumps(quality, ensure_ascii=False, default=str),
            ),
        )
        connection.commit()
        return True
    finally:
        connection.close()


def import_manual_history(
    db_path: Path, qty_path: Path, value_path: Path
) -> dict[str, Any]:
    frame, quality = read_manual_history(qty_path, value_path)
    combined_hash = hashlib.sha256(
        (_sha256(qty_path) + _sha256(value_path) + "|approved-manual-v2").encode("ascii")
    ).hexdigest()
    imported = _upsert(
        db_path,
        frame,
        source_file=f"{qty_path.name} + {value_path.name}",
        source_type="manual_wide_backfill",
        source_hash=combined_hash,
        source_priority=30,
        quality=quality,
    )
    return {**quality, "imported": imported, "database": str(db_path)}


def import_current_attachment(db_path: Path, path: Path) -> dict[str, Any]:
    frame = read_current_attachment(path)
    quality = {
        "file": path.name,
        "daily_grain_rows": int(len(frame)),
        "date_min": frame["order_date"].min().date().isoformat(),
        "date_max": frame["order_date"].max().date().isoformat(),
        "channels": int(frame["channel_name"].nunique()),
        "skus": int(frame["child_sku"].nunique()),
    }
    imported = _upsert(
        db_path,
        frame,
        source_file=path.name,
        source_type="daily_email_attachment",
        source_hash=_sha256(path),
        source_priority=20,
        quality=quality,
    )
    return {**quality, "imported": imported, "database": str(db_path)}


def load_history(db_path: Path) -> pd.DataFrame:
    if not db_path.exists():
        return pd.DataFrame(columns=DAILY_COLUMNS)
    connection = _connect(db_path)
    try:
        frame = pd.read_sql_query(
            """
            SELECT entity_key, child_sku, product_name, sub_category, category,
                   brand, channel_name, order_date, units, sales_value
            FROM secondary_sales_daily
            """,
            connection,
        )
    finally:
        connection.close()
    frame["order_date"] = pd.to_datetime(frame["order_date"], errors="coerce").dt.normalize()
    return frame


def _records_from_previous(previous_secondary: dict[str, Any] | None) -> pd.DataFrame:
    records = (previous_secondary or {}).get("monthlySkuChannels", [])
    rows = []
    for record in records:
        month = str(record.get("month", ""))
        if not month:
            continue
        rows.append(
            {
                "month": month,
                "child_sku": str(record.get("sku", "")),
                "product_name": str(record.get("product", "")),
                "brand": str(record.get("brand", "")),
                "channel_name": str(record.get("channel", "")),
                "units": float(record.get("units", 0) or 0),
                "sales_value": float(record.get("value", 0) or 0),
            }
        )
    return pd.DataFrame(rows)


def build_monthly_history(
    daily: pd.DataFrame,
    *,
    through_date: pd.Timestamp,
    previous_secondary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    through = pd.Timestamp(through_date).normalize()
    usable = daily.loc[daily["order_date"].le(through)].copy()
    if usable.empty:
        return {
            "monthly_sku_channel": pd.DataFrame(),
            "monthly_channel": pd.DataFrame(),
            "monthly_total": pd.DataFrame(),
            "daily_channel": pd.DataFrame(),
            "complete_months": [],
            "quality": {"available": False},
        }
    usable["month"] = usable["order_date"].dt.strftime("%Y-%m")
    days = usable.groupby("month")["order_date"].nunique()
    complete_months = []
    for month, count in days.items():
        year, month_number = map(int, month.split("-"))
        expected = calendar.monthrange(year, month_number)[1]
        if int(count) == expected:
            complete_months.append(month)

    complete_daily = usable.loc[usable["month"].isin(complete_months)].copy()
    monthly_sku = (
        complete_daily.groupby(["month", "child_sku", "channel_name"], as_index=False)
        .agg(
            product_name=("product_name", "first"),
            brand=("brand", "first"),
            units=("units", "sum"),
            sales_value=("sales_value", "sum"),
        )
    )
    previous = _records_from_previous(previous_secondary)
    if not previous.empty:
        previous = previous.loc[~previous["month"].isin(complete_months)].copy()
        monthly_sku = pd.concat([monthly_sku, previous], ignore_index=True)
    if not monthly_sku.empty:
        monthly_sku = (
            monthly_sku.groupby(["month", "child_sku", "channel_name"], as_index=False)
            .agg(
                product_name=("product_name", "first"),
                brand=("brand", "first"),
                units=("units", "sum"),
                sales_value=("sales_value", "sum"),
            )
            .sort_values(["month", "channel_name", "child_sku"])
        )
    all_complete_months = sorted(monthly_sku["month"].unique()) if not monthly_sku.empty else []
    monthly_channel = (
        monthly_sku.groupby(["month", "channel_name"], as_index=False)
        .agg(units=("units", "sum"), sales_value=("sales_value", "sum"))
        .sort_values(["month", "sales_value"], ascending=[True, False])
    )
    monthly_total = (
        monthly_sku.groupby("month", as_index=False)
        .agg(
            units=("units", "sum"),
            sales_value=("sales_value", "sum"),
            channels=("channel_name", "nunique"),
        )
        .sort_values("month")
    )
    daily_channel = (
        usable.groupby(["order_date", "channel_name"], as_index=False)
        .agg(units=("units", "sum"), sales_value=("sales_value", "sum"))
        .sort_values(["order_date", "channel_name"])
    )
    quality = {
        "available": True,
        "date_min": usable["order_date"].min().date().isoformat(),
        "date_max": usable["order_date"].max().date().isoformat(),
        "daily_rows": int(len(usable)),
        "channels": int(usable["channel_name"].nunique()),
        "skus": int(usable["child_sku"].nunique()),
        "complete_months": all_complete_months,
    }
    return {
        "monthly_sku_channel": monthly_sku,
        "monthly_channel": monthly_channel,
        "monthly_total": monthly_total,
        "daily_channel": daily_channel,
        "complete_months": all_complete_months,
        "quality": quality,
    }
