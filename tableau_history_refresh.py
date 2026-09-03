"""Normalize Tableau channel-sales exports and import them into durable history.

The Tableau ``EComm Overall`` quantity sheet intentionally exposes fewer
dimension columns than the older manual history file.  The matching
``EComm Overall Sales`` value sheet remains the authoritative source for
subcategory, category, and brand.  This module reconciles both sheets at
SKU-channel grain, produces the approved seven-dimension wide files, and
imports the result without weakening the existing history validation.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
import sqlite3
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from sales_history import import_manual_history, load_history


ROOT = Path(__file__).resolve().parent
IST = ZoneInfo("Asia/Kolkata")
CANONICAL_DIMENSIONS = [
    "child_sku",
    "product_name",
    "sub_category",
    "category",
    "brand",
    "channel_name",
]
OUTPUT_HEADERS = [
    "child SKU",
    "product_name",
    "SubCategory_PM",
    "Category_PM",
    "Brand",
    "Channel name",
    "product_name",
]
ALIASES = {
    "child_sku": {"child_sku", "child_sku_code", "sku"},
    "product_name": {"product_name", "product"},
    "sub_category": {"sub_category", "subcategory_pm", "sub_category_pm"},
    "category": {"category", "category_pm"},
    "brand": {"brand"},
    "channel_name": {"channel_name", "channel"},
}


def _normal_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).strip().casefold()).strip("_")


def _read_table(path: Path) -> tuple[pd.DataFrame, list[str], str]:
    if path.suffix.casefold() in {".xlsx", ".xlsm", ".xls"}:
        try:
            frame = pd.read_excel(
                path,
                header=1,
                dtype=str,
                keep_default_na=False,
            )
            if len(frame.columns) < 5:
                raise ValueError("too few columns")
            frame.columns = [str(column).strip() for column in frame.columns]
            date_columns = [
                column
                for column in frame.columns
                if pd.notna(pd.to_datetime(column, dayfirst=True, errors="coerce"))
            ]
            if not date_columns:
                raise ValueError("no date columns")
            return frame, date_columns, "excel"
        except (ValueError, OSError) as exc:
            raise ValueError(f"Unable to read Tableau export {path.name}: {exc}") from exc

    last_error: Exception | None = None
    for encoding in ("utf-16", "utf-8-sig", "utf-8"):
        try:
            frame = pd.read_csv(
                path,
                header=1,
                dtype=str,
                keep_default_na=False,
                sep="\t",
                encoding=encoding,
            )
            if len(frame.columns) < 5:
                raise ValueError("too few columns")
            frame.columns = [str(column).strip() for column in frame.columns]
            date_columns = [
                column
                for column in frame.columns
                if pd.notna(pd.to_datetime(column, dayfirst=True, errors="coerce"))
            ]
            if not date_columns:
                raise ValueError("no date columns")
            return frame, date_columns, encoding
        except (UnicodeError, ValueError, pd.errors.ParserError) as exc:
            last_error = exc
    raise ValueError(f"Unable to read Tableau export {path.name}: {last_error}")


def _find_column(columns: list[str], dimension: str, *, required: bool) -> str | None:
    aliases = ALIASES[dimension]
    for column in columns:
        base = _normal_name(re.sub(r"\.\d+$", "", str(column)))
        if base in aliases:
            return column
    if required:
        raise ValueError(f"Missing {dimension} in Tableau export")
    return None


def _numbers(series: pd.Series) -> pd.Series:
    cleaned = series.fillna("").astype(str).str.strip()
    cleaned = cleaned.str.replace(",", "", regex=False).str.replace("₹", "", regex=False)
    cleaned = cleaned.str.replace(r"^\((.*)\)$", r"-\1", regex=True)
    return pd.to_numeric(cleaned, errors="coerce").fillna(0.0)


def _nullable_numbers(series: pd.Series) -> pd.Series:
    cleaned = series.fillna("").astype(str).str.strip()
    cleaned = cleaned.str.replace(",", "", regex=False).str.replace("₹", "", regex=False)
    cleaned = cleaned.str.replace(r"^\((.*)\)$", r"-\1", regex=True)
    cleaned = cleaned.mask(cleaned.eq(""))
    return pd.to_numeric(cleaned, errors="coerce")


def _entity_key(frame: pd.DataFrame) -> pd.Series:
    sku = frame["child_sku"].fillna("").astype(str).str.strip().str.casefold()
    product = frame["product_name"].fillna("").astype(str).str.strip().str.casefold()
    valid = sku.ne("") & sku.ne("unmapped")
    entity = sku.where(valid, "__unmapped__|" + product)
    channel = frame["channel_name"].fillna("").astype(str).str.strip().str.casefold()
    return entity + "|" + channel


def _extract(path: Path, measure: str) -> tuple[pd.DataFrame, list[str], dict[str, Any]]:
    raw, date_columns, encoding = _read_table(path)
    dimensions = [column for column in raw.columns if column not in date_columns]
    required = {"child_sku", "product_name", "channel_name"}
    mapping: dict[str, str] = {}
    for dimension in CANONICAL_DIMENSIONS:
        source = _find_column(dimensions, dimension, required=dimension in required)
        if source:
            mapping[dimension] = source

    first_dimension = mapping["child_sku"]
    total_mask = raw[first_dimension].astype(str).str.strip().str.casefold().eq("grand total")
    if not total_mask.any():
        raise ValueError(f"Grand Total row is missing from {path.name}")
    reported_row = raw.loc[total_mask].iloc[0]
    detail = raw.loc[~total_mask].copy()
    output = pd.DataFrame(index=detail.index)
    for dimension, source in mapping.items():
        output[dimension] = detail[source].fillna("").astype(str).str.strip()
    for dimension in CANONICAL_DIMENSIONS:
        if dimension not in output:
            output[dimension] = ""
    output = output[CANONICAL_DIMENSIONS]
    output["_entity_channel"] = _entity_key(output)
    if output["channel_name"].eq("").any():
        raise ValueError(f"Blank channel rows found in {path.name}")
    duplicates = output["_entity_channel"].duplicated(keep=False)
    if duplicates.any():
        sample = output.loc[duplicates, ["child_sku", "channel_name"]].head(5)
        raise ValueError(
            f"Duplicate SKU-channel rows found in {path.name}: "
            + sample.to_dict("records").__repr__()
        )
    for column in date_columns:
        output[column] = _nullable_numbers(detail[column])

    detail_totals = output[date_columns].sum(axis=0)
    reported = _numbers(reported_row[date_columns])
    difference = (detail_totals - reported).abs()
    quality = {
        "file": path.name,
        "encoding": encoding,
        "detail_rows": int(len(output)),
        "date_min": pd.to_datetime(date_columns, dayfirst=True).min().date().isoformat(),
        "date_max": pd.to_datetime(date_columns, dayfirst=True).max().date().isoformat(),
        "distinct_days": len(date_columns),
        "channels": int(output["channel_name"].nunique()),
        "skus": int(output["child_sku"].nunique()),
        "available_dimensions": sorted(mapping),
        "daily_total_mismatch_days": int(difference.gt(0.01).sum()),
        "max_abs_daily_total_difference": float(difference.max()),
        "negative_cells": int(output[date_columns].lt(0).sum().sum()),
        "measure": measure,
    }
    return output, date_columns, quality


def normalize_exports(
    quantity_path: Path,
    value_path: Path,
) -> tuple[pd.DataFrame, pd.DataFrame, list[str], dict[str, Any]]:
    quantity, quantity_dates, quantity_quality = _extract(quantity_path, "units")
    value, value_dates, value_quality = _extract(value_path, "sales_value")
    if quantity_dates != value_dates:
        raise ValueError("Quantity and value exports do not contain the same date columns")
    missing_value_dimensions = [
        dimension
        for dimension in CANONICAL_DIMENSIONS
        if dimension not in value_quality["available_dimensions"]
    ]
    if missing_value_dimensions:
        raise ValueError(
            "Value export is missing authoritative dimensions: "
            + ", ".join(missing_value_dimensions)
        )

    quantity_keys = set(quantity["_entity_channel"])
    value_keys = set(value["_entity_channel"])
    quantity_only = sorted(quantity_keys - value_keys)
    value_only = sorted(value_keys - quantity_keys)
    if quantity_only or value_only:
        raise ValueError(
            "Quantity and value exports do not reconcile at SKU-channel grain: "
            f"{len(quantity_only)} quantity-only / {len(value_only)} value-only"
        )

    metadata = value[["_entity_channel", *CANONICAL_DIMENSIONS]].copy()
    quantity_values = quantity[["_entity_channel", *quantity_dates]].copy()
    value_values = value[["_entity_channel", *value_dates]].copy()
    canonical_quantity = metadata.merge(
        quantity_values,
        on="_entity_channel",
        how="inner",
        validate="one_to_one",
    )
    canonical_value = metadata.merge(
        value_values,
        on="_entity_channel",
        how="inner",
        validate="one_to_one",
    )
    canonical_quantity.drop(columns="_entity_channel", inplace=True)
    canonical_value.drop(columns="_entity_channel", inplace=True)
    canonical_quantity = canonical_quantity.sort_values(
        ["channel_name", "child_sku", "product_name"]
    ).reset_index(drop=True)
    canonical_value = canonical_value.sort_values(
        ["channel_name", "child_sku", "product_name"]
    ).reset_index(drop=True)

    parsed_dates = pd.to_datetime(quantity_dates, dayfirst=True)
    quality = {
        "format_match": True,
        "normalization": (
            "Quantity category, subcategory, and brand were restored from the "
            "one-to-one matching value row."
        ),
        "quantity": quantity_quality,
        "value": value_quality,
        "matched_rows": int(len(canonical_quantity)),
        "date_min": parsed_dates.min().date().isoformat(),
        "date_max": parsed_dates.max().date().isoformat(),
        "distinct_days": int(len(quantity_dates)),
        "channels": int(canonical_quantity["channel_name"].nunique()),
        "skus": int(canonical_quantity["child_sku"].nunique()),
    }
    return canonical_quantity, canonical_value, quantity_dates, quality


def _format_number(value: Any) -> str:
    if pd.isna(value):
        return ""
    number = float(value)
    if abs(number - round(number)) < 1e-9:
        return str(int(round(number)))
    return f"{number:.6f}".rstrip("0").rstrip(".")


def _write_wide(path: Path, frame: pd.DataFrame, date_columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    totals = frame[date_columns].sum(axis=0)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-16",
        newline="",
        dir=path.parent,
        prefix=path.name + ".",
        suffix=".tmp",
        delete=False,
    ) as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow(["" for _ in OUTPUT_HEADERS] + ["Date Level"] * len(date_columns))
        writer.writerow(OUTPUT_HEADERS + date_columns)
        writer.writerow(
            ["Grand Total"] + ["Total"] * (len(OUTPUT_HEADERS) - 1)
            + [_format_number(totals[column]) for column in date_columns]
        )
        for _, row in frame.iterrows():
            dimensions = [
                row["child_sku"],
                row["product_name"],
                row["sub_category"],
                row["category"],
                row["brand"],
                row["channel_name"],
                row["product_name"],
            ]
            values = [_format_number(row[column]) for column in date_columns]
            writer.writerow(dimensions + values)
        temp_path = Path(handle.name)
    temp_path.replace(path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _replace_history_scope(
    history_db: Path,
    *,
    date_min: str,
    date_max: str,
    backup_path: Path,
) -> dict[str, Any]:
    if not history_db.exists():
        return {"deleted_rows": 0, "deleted_imports": 0, "backup": None}
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(history_db, backup_path)
    connection = sqlite3.connect(history_db)
    try:
        deleted_rows = connection.execute(
            """
            DELETE FROM secondary_sales_daily
            WHERE order_date BETWEEN ? AND ? AND source_priority <= 30
            """,
            (date_min, date_max),
        ).rowcount
        deleted_imports = connection.execute(
            """
            DELETE FROM source_imports
            WHERE source_type = 'manual_wide_backfill'
              AND date_min <= ? AND date_max >= ?
            """,
            (date_max, date_min),
        ).rowcount
        connection.commit()
        return {
            "deleted_rows": int(deleted_rows),
            "deleted_imports": int(deleted_imports),
            "backup": str(backup_path),
        }
    finally:
        connection.close()


def _build_tableau_channel_workbook(
    history_db: Path,
    *,
    report_date: date,
    output_dir: Path,
) -> Path:
    daily = load_history(history_db)
    if daily.empty:
        raise ValueError("Durable Tableau history is empty")
    usable = daily.loc[daily["order_date"].dt.date.le(report_date)].copy()
    latest = usable["order_date"].max()
    if pd.isna(latest) or latest.date() < report_date - pd.Timedelta(days=2):
        raise ValueError(
            f"Tableau history is stale for {report_date}: latest is {latest}"
        )
    raw_start = latest - pd.Timedelta(days=39)
    raw = usable.loc[usable["order_date"].ge(raw_start)].copy()
    raw = raw.rename(columns={"units": "qty", "sales_value": "sales"})
    raw = raw[
        [
            "order_date",
            "channel_name",
            "brand",
            "category",
            "sub_category",
            "product_name",
            "child_sku",
            "qty",
            "sales",
        ]
    ]
    if len(raw) < 1_000 or raw["order_date"].nunique() < 30:
        raise ValueError(
            "Tableau history does not have enough recent rows/dates to build the bot"
        )
    current_start = latest.replace(day=1)
    previous_start = current_start - pd.offsets.MonthBegin(1)
    previous = usable.loc[
        usable["order_date"].ge(previous_start)
        & usable["order_date"].lt(current_start)
    ].copy()
    previous = previous.rename(columns={"units": "qty"})
    historic = previous[
        [
            "child_sku",
            "product_name",
            "sub_category",
            "category",
            "brand",
            "channel_name",
            "order_date",
            "qty",
        ]
    ]
    output = output_dir / f"Tableau Channel Sales_{report_date.isoformat()}.xlsx"
    with tempfile.NamedTemporaryFile(
        dir=output_dir,
        prefix=output.name + ".",
        suffix=".tmp.xlsx",
        delete=False,
    ) as handle:
        temp_path = Path(handle.name)
    try:
        with pd.ExcelWriter(temp_path, engine="openpyxl") as writer:
            raw.to_excel(writer, sheet_name="raw_data", index=False)
            historic.to_excel(
                writer,
                sheet_name="Historic Jan26 onwards",
                index=False,
                startrow=1,
            )
        temp_path.replace(output)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    return output


def refresh_history(
    quantity_path: Path,
    value_path: Path,
    *,
    history_db: Path,
    output_root: Path,
    report_date: date,
) -> dict[str, Any]:
    quantity, value, date_columns, quality = normalize_exports(
        quantity_path, value_path
    )
    parsed_dates = pd.to_datetime(date_columns, dayfirst=True)
    months = sorted(parsed_dates.strftime("%Y-%m").unique())
    if len(months) != 1:
        raise ValueError("A scheduled Tableau refresh must contain exactly one month")
    output_dir = output_root / months[0]
    quantity_output = output_dir / "EComm Overall Sales Qty level.csv"
    value_output = output_dir / "EComm Overall Sales value level.csv"
    raw_dir = output_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(quantity_path, raw_dir / "EComm Overall.csv")
    shutil.copy2(value_path, raw_dir / "EComm Overall Sales.csv")
    _write_wide(quantity_output, quantity, date_columns)
    _write_wide(value_output, value, date_columns)
    date_min = parsed_dates.min().date().isoformat()
    date_max = parsed_dates.max().date().isoformat()
    backup_path = output_dir / "secondary_sales_history.pre_tableau.sqlite"
    replacement = _replace_history_scope(
        history_db,
        date_min=date_min,
        date_max=date_max,
        backup_path=backup_path,
    )
    try:
        imported = import_manual_history(history_db, quantity_output, value_output)
        if not imported.get("imported"):
            raise ValueError("The normalized Tableau history was not imported")
    except Exception:
        if replacement.get("backup"):
            shutil.copy2(backup_path, history_db)
        raise
    channel_workbook = _build_tableau_channel_workbook(
        history_db,
        report_date=report_date,
        output_dir=output_dir,
    )
    result = {
        **quality,
        "quantity_output": str(quantity_output),
        "value_output": str(value_output),
        "quantity_sha256": _sha256(quantity_output),
        "value_sha256": _sha256(value_output),
        "history_import": imported,
        "history_replacement": replacement,
        "channel_workbook": str(channel_workbook),
    }
    report_path = output_dir / "quality_report.json"
    report_path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    result["quality_report"] = str(report_path)
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quantity", type=Path, required=True)
    parser.add_argument("--value", type=Path, required=True)
    parser.add_argument(
        "--history-db",
        type=Path,
        default=ROOT / "data" / "secondary_sales_history.sqlite",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=ROOT / "data" / "tableau_history",
    )
    parser.add_argument(
        "--report-date",
        help="Bot report date in YYYY-MM-DD. Defaults to today in IST.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = refresh_history(
        args.quantity.resolve(),
        args.value.resolve(),
        history_db=args.history_db.resolve(),
        output_root=args.output_root.resolve(),
        report_date=(
            date.fromisoformat(args.report_date)
            if args.report_date
            else datetime.now(IST).date()
        ),
    )
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
