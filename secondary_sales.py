"""Secondary channel-sales parsing, validation, and DRR calculations."""

from __future__ import annotations

import calendar
import re
import zipfile
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from sales_history import build_monthly_history, import_current_attachment, load_history


RAW_SHEET = "raw_data"
HISTORIC_SHEET = "Historic Jan26 onwards"
REQUIRED_RAW_COLUMNS = [
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
HISTORIC_COLUMNS = [
    "child_sku",
    "product_name",
    "sub_category",
    "category",
    "brand",
    "channel_name",
    "order_date",
    "qty",
]
SECONDARY_OUTPUT_COLUMNS = [
    "Secondary 7 Day Sales Units",
    "Secondary DRR 7 Day",
    "Secondary 30 Day Sales Units",
    "Secondary DRR 30 Day",
    "Secondary DRR",
    "Secondary MTD Units",
    "Secondary MTD Sales Value",
    "Secondary Last Month Units",
    "Secondary Last Month Sales Value",
    "Secondary Overall DOI",
    "Secondary Mumbai DRR",
    "Secondary Mumbai DOI",
]


def _clean_text(series: pd.Series) -> pd.Series:
    return series.fillna("").astype(str).str.strip()


def _number(value: Any) -> float:
    try:
        number = float(value)
        return number if np.isfinite(number) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _month_bounds(anchor: pd.Timestamp) -> tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp]:
    current_start = anchor.replace(day=1)
    previous_start = current_start - pd.offsets.MonthBegin(1)
    return current_start, previous_start, current_start


def _expected_previous_month_days(anchor: pd.Timestamp) -> int:
    previous_month_end = anchor.replace(day=1) - pd.Timedelta(days=1)
    return calendar.monthrange(previous_month_end.year, previous_month_end.month)[1]


def validate_channel_sales_attachment(
    path: Path,
    report_date: date,
    *,
    min_rows: int = 1_000,
    min_distinct_dates: int = 30,
) -> dict[str, Any]:
    if path.suffix.lower() != ".xlsx":
        raise ValueError(f"Channel Sales source must be .xlsx: {path.name}")
    if path.name.lower().endswith(".part"):
        raise ValueError(f"Partial Channel Sales download cannot be used: {path.name}")
    match = re.fullmatch(
        r"(?:Channel Sales Tracker Dump|Tableau Channel Sales)_(\d{4}-\d{2}-\d{2})\.xlsx",
        path.name,
        flags=re.IGNORECASE,
    )
    if not match:
        raise ValueError(f"Unexpected Channel Sales filename: {path.name}")
    attachment_date = date.fromisoformat(match.group(1))
    if attachment_date != report_date:
        raise ValueError(
            f"Stale Channel Sales attachment: {attachment_date} != {report_date}"
        )
    if not zipfile.is_zipfile(path):
        raise ValueError(f"Channel Sales attachment is not a valid XLSX file: {path.name}")
    with zipfile.ZipFile(path) as archive:
        corrupt_member = archive.testzip()
        if corrupt_member:
            raise ValueError(
                f"Channel Sales attachment contains a corrupt member: {corrupt_member}"
            )

    raw = pd.read_excel(path, sheet_name=RAW_SHEET, usecols="A:I")
    raw.columns = [str(column).strip().casefold() for column in raw.columns]
    if list(raw.columns) != REQUIRED_RAW_COLUMNS:
        raise ValueError(
            "Channel Sales raw_data columns do not match the approved schema: "
            + ", ".join(raw.columns)
        )
    raw["order_date"] = pd.to_datetime(raw["order_date"], errors="coerce").dt.normalize()
    valid_dates = raw["order_date"].dropna()
    if len(raw) < min_rows or valid_dates.nunique() < min_distinct_dates:
        raise ValueError(
            "Channel Sales raw_data volume is unexpectedly low: "
            f"{len(raw):,} rows / {valid_dates.nunique():,} dates."
        )
    latest = valid_dates.max()
    if latest.date() > report_date:
        raise ValueError(
            f"Channel Sales contains a future order date: {latest.date()} > {report_date}"
        )
    if latest.date() < report_date - timedelta(days=2):
        raise ValueError(
            f"Channel Sales is stale: latest order date is {latest.date()} for {report_date}"
        )
    return {
        "file": path.name,
        "source_type": (
            "tableau_history"
            if path.name.casefold().startswith("tableau channel sales_")
            else "gmail_attachment"
        ),
        "attachment_date": attachment_date.isoformat(),
        "size_bytes": path.stat().st_size,
        "raw_rows": len(raw),
        "distinct_dates": int(valid_dates.nunique()),
        "earliest_order_date": valid_dates.min().date().isoformat(),
        "latest_order_date": latest.date().isoformat(),
    }


def _period_sums(
    frame: pd.DataFrame,
    group_columns: list[str],
    value_columns: list[str],
) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=[*group_columns, *value_columns]).set_index(
            group_columns
        )
    return frame.groupby(group_columns, dropna=False)[value_columns].sum()


def build_secondary_sales(
    path: Path,
    report_date: date,
    *,
    history_db: Path | None = None,
    previous_secondary: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    validation = validate_channel_sales_attachment(path, report_date)
    raw = pd.read_excel(path, sheet_name=RAW_SHEET, usecols="A:I")
    raw.columns = REQUIRED_RAW_COLUMNS
    for column in [
        "channel_name",
        "brand",
        "category",
        "sub_category",
        "product_name",
        "child_sku",
    ]:
        raw[column] = _clean_text(raw[column])
    raw["order_date"] = pd.to_datetime(raw["order_date"], errors="coerce").dt.normalize()
    raw["qty"] = pd.to_numeric(raw["qty"], errors="coerce").fillna(0.0)
    raw["sales"] = pd.to_numeric(raw["sales"], errors="coerce").fillna(0.0)
    raw = raw.loc[raw["order_date"].notna() & raw["channel_name"].ne("")].copy()

    history_import_quality: dict[str, Any] | None = None
    history_daily = pd.DataFrame()
    history_bundle: dict[str, Any] | None = None
    if history_db is not None:
        history_import_quality = import_current_attachment(history_db, path)
        history_daily = load_history(history_db)
    metric_raw = raw.copy()
    metric_source = "current attachment"
    if not history_daily.empty:
        metric_raw = history_daily.rename(
            columns={"units": "qty", "sales_value": "sales"}
        ).copy()
        metric_raw = metric_raw.loc[
            metric_raw["order_date"].notna()
            & metric_raw["order_date"].dt.date.le(report_date)
            & metric_raw["channel_name"].ne("")
        ].copy()
        metric_source = "durable channel-sales history"
    latest = metric_raw["order_date"].max()
    current_start, previous_start, previous_end = _month_bounds(latest)
    if not history_daily.empty:
        history_bundle = build_monthly_history(
            history_daily,
            through_date=latest,
            previous_secondary=previous_secondary,
        )
    channel_cutoff = metric_raw.groupby("channel_name")["order_date"].max()
    metric_raw["_channel_cutoff"] = metric_raw["channel_name"].map(channel_cutoff)
    metric_raw["_in7"] = metric_raw["order_date"].between(
        metric_raw["_channel_cutoff"] - pd.Timedelta(days=6),
        metric_raw["_channel_cutoff"],
    )
    metric_raw["_in30"] = metric_raw["order_date"].between(
        metric_raw["_channel_cutoff"] - pd.Timedelta(days=29),
        metric_raw["_channel_cutoff"],
    )
    metric_raw["_mtd"] = metric_raw["order_date"].between(
        current_start, metric_raw["_channel_cutoff"]
    )
    metric_raw["_latest"] = metric_raw["order_date"].eq(
        metric_raw["_channel_cutoff"]
    )

    historic = pd.read_excel(
        path,
        sheet_name=HISTORIC_SHEET,
        header=1,
        usecols="A:H",
    )
    historic.columns = HISTORIC_COLUMNS
    for column in [
        "child_sku",
        "product_name",
        "sub_category",
        "category",
        "brand",
        "channel_name",
    ]:
        historic[column] = _clean_text(historic[column])
    historic["order_date"] = pd.to_datetime(
        historic["order_date"], errors="coerce"
    ).dt.normalize()
    historic["qty"] = pd.to_numeric(historic["qty"], errors="coerce").fillna(0.0)
    historic = historic.loc[
        historic["order_date"].notna() & historic["channel_name"].ne("")
    ].copy()
    historic_previous = historic.loc[
        historic["order_date"].ge(previous_start)
        & historic["order_date"].lt(previous_end)
    ].copy()

    expected_previous_days = _expected_previous_month_days(latest)
    raw_previous = metric_raw.loc[
        metric_raw["order_date"].ge(previous_start)
        & metric_raw["order_date"].lt(previous_end)
    ].copy()
    history_previous = pd.DataFrame()
    if not history_daily.empty:
        history_previous = history_daily.loc[
            history_daily["order_date"].ge(previous_start)
            & history_daily["order_date"].lt(previous_end)
        ].copy()
    history_previous_days = (
        int(history_previous["order_date"].nunique())
        if not history_previous.empty
        else 0
    )
    history_previous_complete = history_previous_days == expected_previous_days
    if history_previous_complete:
        previous_units_frame = history_previous.rename(columns={"units": "qty"})
        previous_values_frame = history_previous.rename(columns={"sales_value": "sales"})
        previous_unit_days = history_previous_days
        previous_value_days = history_previous_days
        previous_units_complete = True
        previous_values_complete = True
        previous_source = "durable channel-sales history"
    else:
        previous_units_frame = historic_previous
        previous_values_frame = raw_previous
        previous_unit_days = int(historic_previous["order_date"].nunique())
        previous_value_days = int(raw_previous["order_date"].nunique())
        previous_units_complete = previous_unit_days == expected_previous_days
        previous_values_complete = previous_value_days == expected_previous_days
        previous_source = "current attachment fallback"

    sku_valid = metric_raw["child_sku"].ne("") & metric_raw[
        "child_sku"
    ].str.casefold().ne("unmapped")
    sku_raw = metric_raw.loc[sku_valid].copy()
    keys = ["child_sku", "channel_name"]
    base = (
        sku_raw.groupby(keys, dropna=False)
        .agg(
            product_name=("product_name", "first"),
            brand=("brand", "first"),
            category=("category", "first"),
            sub_category=("sub_category", "first"),
            data_through=("_channel_cutoff", "max"),
        )
        .sort_index()
    )
    for flag, prefix in [
        ("_in7", "sale7"),
        ("_in30", "sale30"),
        ("_mtd", "mtd"),
        ("_latest", "latest"),
    ]:
        sums = _period_sums(sku_raw.loc[sku_raw[flag]], keys, ["qty", "sales"])
        sums = sums.rename(columns={"qty": f"{prefix}_units", "sales": f"{prefix}_value"})
        base = base.join(sums, how="left")

    historic_sku_valid = (
        previous_units_frame["child_sku"].ne("")
        & previous_units_frame["child_sku"].str.casefold().ne("unmapped")
    )
    last_units = _period_sums(
        previous_units_frame.loc[historic_sku_valid], keys, ["qty"]
    ).rename(columns={"qty": "last_month_units"})
    base = base.join(last_units, how="left")
    if previous_values_complete:
        raw_previous_valid = (
            previous_values_frame["child_sku"].ne("")
            & previous_values_frame["child_sku"].str.casefold().ne("unmapped")
        )
        last_values = _period_sums(
            previous_values_frame.loc[raw_previous_valid], keys, ["sales"]
        ).rename(columns={"sales": "last_month_value"})
        base = base.join(last_values, how="left")
    else:
        base["last_month_value"] = np.nan

    numeric_columns = [
        "sale7_units",
        "sale7_value",
        "sale30_units",
        "sale30_value",
        "mtd_units",
        "mtd_value",
        "latest_units",
        "latest_value",
        "last_month_units",
    ]
    base[numeric_columns] = base[numeric_columns].fillna(0.0)
    base["drr7"] = base["sale7_units"] / 7.0
    base["drr30"] = base["sale30_units"] / 30.0
    base["drr"] = base[["drr7", "drr30"]].max(axis=1)
    sku_channel = base.reset_index()

    sku_summary = (
        sku_channel.groupby("child_sku", as_index=False)
        .agg(
            product_name=("product_name", "first"),
            brand=("brand", "first"),
            sale7_units=("sale7_units", "sum"),
            sale7_value=("sale7_value", "sum"),
            sale30_units=("sale30_units", "sum"),
            sale30_value=("sale30_value", "sum"),
            drr7=("drr7", "sum"),
            drr30=("drr30", "sum"),
            drr=("drr", "sum"),
            mtd_units=("mtd_units", "sum"),
            mtd_value=("mtd_value", "sum"),
            latest_units=("latest_units", "sum"),
            latest_value=("latest_value", "sum"),
            last_month_units=("last_month_units", "sum"),
            last_month_value=("last_month_value", "sum"),
        )
    )
    if not previous_values_complete:
        sku_summary["last_month_value"] = np.nan

    channel_base = (
        metric_raw.groupby("channel_name", as_index=False)
        .agg(data_through=("_channel_cutoff", "max"))
        .set_index("channel_name")
    )
    previous_channels = pd.Index(previous_units_frame["channel_name"].dropna().unique())
    channel_base = channel_base.reindex(channel_base.index.union(previous_channels))
    channel_base.index.name = "channel_name"
    previous_channel_through = previous_units_frame.groupby("channel_name")[
        "order_date"
    ].max()
    channel_base["data_through"] = channel_base["data_through"].fillna(
        previous_channel_through
    )
    for flag, prefix in [
        ("_in7", "sale7"),
        ("_in30", "sale30"),
        ("_mtd", "mtd"),
        ("_latest", "latest"),
    ]:
        sums = _period_sums(
            metric_raw.loc[metric_raw[flag]], ["channel_name"], ["qty", "sales"]
        )
        sums = sums.rename(columns={"qty": f"{prefix}_units", "sales": f"{prefix}_value"})
        channel_base = channel_base.join(sums, how="left")
    historic_channel = _period_sums(
        previous_units_frame, ["channel_name"], ["qty"]
    ).rename(columns={"qty": "last_month_units"})
    channel_base = channel_base.join(historic_channel, how="left")
    if previous_values_complete:
        previous_channel_values = _period_sums(
            previous_values_frame, ["channel_name"], ["sales"]
        ).rename(columns={"sales": "last_month_value"})
        channel_base = channel_base.join(previous_channel_values, how="left")
    else:
        channel_base["last_month_value"] = np.nan
    channel_numeric = [
        "sale7_units",
        "sale7_value",
        "sale30_units",
        "sale30_value",
        "mtd_units",
        "mtd_value",
        "latest_units",
        "latest_value",
        "last_month_units",
    ]
    channel_base[channel_numeric] = channel_base[channel_numeric].fillna(0.0)
    channel_base["drr7"] = channel_base["sale7_units"] / 7.0
    channel_base["drr30"] = channel_base["sale30_units"] / 30.0
    channel_base["drr"] = channel_base[["drr7", "drr30"]].max(axis=1)
    channel_summary = channel_base.reset_index().sort_values("drr", ascending=False)

    latest_global = metric_raw.loc[metric_raw["order_date"].eq(latest)]
    mtd_global = metric_raw.loc[metric_raw["order_date"].ge(current_start)]
    totals = {
        "dataThrough": latest.date().isoformat(),
        "latestUnits": round(_number(latest_global["qty"].sum()), 2),
        "latestValue": round(_number(latest_global["sales"].sum()), 2),
        "latestReportingChannels": int(latest_global["channel_name"].nunique()),
        "mtdUnits": round(_number(mtd_global["qty"].sum()), 2),
        "mtdValue": round(_number(mtd_global["sales"].sum()), 2),
        "lastMonthUnits": round(_number(previous_units_frame["qty"].sum()), 2),
        "lastMonthValue": (
            round(_number(previous_values_frame["sales"].sum()), 2)
            if previous_values_complete
            else None
        ),
        "lastMonth": previous_start.strftime("%Y-%m"),
        "lastMonthUnitsComplete": previous_units_complete,
        "lastMonthValueComplete": previous_values_complete,
    }
    quality = {
        **validation,
        "historic_rows": len(historic),
        "historic_latest_order_date": historic["order_date"].max().date().isoformat(),
        "channels": int(raw["channel_name"].nunique()),
        "skus": int(sku_summary["child_sku"].nunique()),
        "blank_sku_rows": int(raw["child_sku"].eq("").sum()),
        "unmapped_sku_rows": int(raw["child_sku"].str.casefold().eq("unmapped").sum()),
        "negative_qty_rows": int(raw["qty"].lt(0).sum()),
        "negative_sales_rows": int(raw["sales"].lt(0).sum()),
        "previous_month_unit_days": previous_unit_days,
        "previous_month_value_days": previous_value_days,
        "previous_month_units_complete": previous_units_complete,
        "previous_month_values_complete": previous_values_complete,
        "previous_month_source": previous_source,
        "metric_source": metric_source,
        "history_import": history_import_quality,
        "history": history_bundle["quality"] if history_bundle else {"available": False},
        "drr_rule": "sum by SKU of channel-level max(7-day units/7, 30-day units/30)",
    }
    result = {
        "source_file": path.name,
        "report_date": report_date,
        "data_through": latest.date(),
        "sku_summary": sku_summary,
        "sku_channel": sku_channel,
        "channel_summary": channel_summary,
        "totals": totals,
        "previous_values_complete": previous_values_complete,
        "history": history_bundle,
    }
    return result, quality


def attach_secondary_metrics(
    frame: pd.DataFrame,
    secondary: dict[str, Any],
) -> pd.DataFrame:
    output = frame.copy()
    summary = secondary["sku_summary"].set_index("child_sku")
    mapping = {
        "sale7_units": "Secondary 7 Day Sales Units",
        "drr7": "Secondary DRR 7 Day",
        "sale30_units": "Secondary 30 Day Sales Units",
        "drr30": "Secondary DRR 30 Day",
        "drr": "Secondary DRR",
        "mtd_units": "Secondary MTD Units",
        "mtd_value": "Secondary MTD Sales Value",
        "last_month_units": "Secondary Last Month Units",
        "last_month_value": "Secondary Last Month Sales Value",
    }
    for source, destination in mapping.items():
        output[destination] = output["SkuCode"].map(summary[source])
    fill_columns = [
        column
        for column in mapping.values()
        if column != "Secondary Last Month Sales Value"
    ]
    output[fill_columns] = output[fill_columns].fillna(0.0)

    sku_channel = secondary["sku_channel"].copy()
    channel_key = _clean_text(sku_channel["channel_name"]).str.casefold().str.replace(
        r"[\s_-]+", "", regex=True
    )
    non_web_drr = (
        sku_channel.loc[~channel_key.isin({"web", "webapp"})]
        .groupby("child_sku")["drr"]
        .sum()
    )
    output["Secondary Mumbai DRR"] = (
        output["SkuCode"].map(non_web_drr).fillna(0.0)
    )

    inventory_check = _clean_text(output["Inventory Check"]).str.casefold().eq("yes")
    location = _clean_text(output["Location Name"]).str.casefold()
    not_rtv = ~location.str.contains("rtv", na=False)
    network = output.loc[inventory_check & not_rtv].groupby("SkuCode")
    network_stock = network["Stock on Hand"].sum() + network["Stock In Transfer"].sum()
    output["_secondary_network_stock"] = output["SkuCode"].map(network_stock).fillna(0.0)
    denominator = output["Secondary DRR"].replace(0, np.nan)
    output["Secondary Overall DOI"] = (
        output["_secondary_network_stock"] / denominator
    ).fillna(0.0)
    output.drop(columns=["_secondary_network_stock"], inplace=True)

    plain_mumbai = location.eq("mumbai")
    mumbai_soh = output.loc[plain_mumbai].groupby("SkuCode")["Stock on Hand"].sum()
    output["_secondary_mumbai_soh"] = output["SkuCode"].map(mumbai_soh).fillna(0.0)
    mumbai_denominator = output["Secondary Mumbai DRR"].replace(0, np.nan)
    output["Secondary Mumbai DOI"] = (
        output["_secondary_mumbai_soh"] / mumbai_denominator
    ).fillna(0.0)
    output.drop(columns=["_secondary_mumbai_soh"], inplace=True)

    for column in [
        "Secondary 7 Day Sales Units",
        "Secondary 30 Day Sales Units",
        "Secondary MTD Units",
        "Secondary Last Month Units",
        "Secondary Overall DOI",
        "Secondary Mumbai DOI",
    ]:
        output[column] = output[column].round(0).astype("int64")
    for column in [
        "Secondary DRR 7 Day",
        "Secondary DRR 30 Day",
        "Secondary DRR",
        "Secondary Mumbai DRR",
    ]:
        output[column] = output[column].round(2)
    output["Secondary MTD Sales Value"] = output["Secondary MTD Sales Value"].round(2)
    output["Secondary Last Month Sales Value"] = output[
        "Secondary Last Month Sales Value"
    ].round(2)
    return output


def attach_previous_secondary_metrics(
    frame: pd.DataFrame,
    previous_skus: list[dict[str, Any]],
) -> pd.DataFrame:
    """Carry forward reviewed sales metrics while recalculating DOI on today's stock."""
    output = frame.copy()
    previous = {
        str(row.get("code", "")).strip(): row
        for row in previous_skus
        if str(row.get("code", "")).strip()
    }
    mapping = {
        "sec7": "Secondary 7 Day Sales Units",
        "secDrr7": "Secondary DRR 7 Day",
        "sec30": "Secondary 30 Day Sales Units",
        "secDrr30": "Secondary DRR 30 Day",
        "secDRR": "Secondary DRR",
        "secMTDUnits": "Secondary MTD Units",
        "secMTDValue": "Secondary MTD Sales Value",
        "secLastMonthUnits": "Secondary Last Month Units",
        "secLastMonthValue": "Secondary Last Month Sales Value",
        "secMumbaiDRR": "Secondary Mumbai DRR",
    }
    for source, destination in mapping.items():
        output[destination] = output["SkuCode"].map(
            lambda sku: previous.get(str(sku), {}).get(source)
        )
    fill_columns = [
        column
        for column in mapping.values()
        if column != "Secondary Last Month Sales Value"
    ]
    output[fill_columns] = output[fill_columns].fillna(0.0)

    inventory_check = _clean_text(output["Inventory Check"]).str.casefold().eq("yes")
    location = _clean_text(output["Location Name"]).str.casefold()
    not_rtv = ~location.str.contains("rtv", na=False)
    network = output.loc[inventory_check & not_rtv].groupby("SkuCode")
    network_stock = network["Stock on Hand"].sum() + network["Stock In Transfer"].sum()
    output["_secondary_network_stock"] = output["SkuCode"].map(network_stock).fillna(0.0)
    denominator = output["Secondary DRR"].replace(0, np.nan)
    output["Secondary Overall DOI"] = (
        output["_secondary_network_stock"] / denominator
    ).fillna(0.0)
    output.drop(columns=["_secondary_network_stock"], inplace=True)

    plain_mumbai = location.eq("mumbai")
    mumbai_soh = output.loc[plain_mumbai].groupby("SkuCode")["Stock on Hand"].sum()
    output["_secondary_mumbai_soh"] = output["SkuCode"].map(mumbai_soh).fillna(0.0)
    mumbai_denominator = output["Secondary Mumbai DRR"].replace(0, np.nan)
    output["Secondary Mumbai DOI"] = (
        output["_secondary_mumbai_soh"] / mumbai_denominator
    ).fillna(0.0)
    output.drop(columns=["_secondary_mumbai_soh"], inplace=True)

    for column in [
        "Secondary 7 Day Sales Units",
        "Secondary 30 Day Sales Units",
        "Secondary MTD Units",
        "Secondary Last Month Units",
        "Secondary Overall DOI",
        "Secondary Mumbai DOI",
    ]:
        output[column] = output[column].round(0).astype("int64")
    for column in [
        "Secondary DRR 7 Day",
        "Secondary DRR 30 Day",
        "Secondary DRR",
        "Secondary Mumbai DRR",
    ]:
        output[column] = output[column].round(2)
    output["Secondary MTD Sales Value"] = output["Secondary MTD Sales Value"].round(2)
    output["Secondary Last Month Sales Value"] = output[
        "Secondary Last Month Sales Value"
    ].round(2)
    return output


def secondary_payload(secondary: dict[str, Any]) -> dict[str, Any]:
    def value(number: Any, digits: int = 2) -> float | None:
        if number is None or pd.isna(number):
            return None
        return round(_number(number), digits)

    channels = []
    for row in secondary["channel_summary"].to_dict("records"):
        channels.append(
            {
                "name": str(row["channel_name"]),
                "through": pd.Timestamp(row["data_through"]).date().isoformat(),
                "latestUnits": value(row["latest_units"]),
                "latestValue": value(row["latest_value"]),
                "sale7Units": value(row["sale7_units"]),
                "sale30Units": value(row["sale30_units"]),
                "drr7": value(row["drr7"]),
                "drr30": value(row["drr30"]),
                "drr": value(row["drr"]),
                "mtdUnits": value(row["mtd_units"]),
                "mtdValue": value(row["mtd_value"]),
                "lastMonthUnits": value(row["last_month_units"]),
                "lastMonthValue": value(row["last_month_value"]),
            }
        )
    sku_channels: dict[str, list[dict[str, Any]]] = {}
    for row in secondary["sku_channel"].to_dict("records"):
        sku_channels.setdefault(str(row["child_sku"]), []).append(
            {
                "name": str(row["channel_name"]),
                "through": pd.Timestamp(row["data_through"]).date().isoformat(),
                "latestUnits": value(row["latest_units"]),
                "latestValue": value(row["latest_value"]),
                "sale7Units": value(row["sale7_units"]),
                "sale30Units": value(row["sale30_units"]),
                "drr7": value(row["drr7"]),
                "drr30": value(row["drr30"]),
                "drr": value(row["drr"]),
                "mtdUnits": value(row["mtd_units"]),
                "mtdValue": value(row["mtd_value"]),
                "lastMonthUnits": value(row["last_month_units"]),
                "lastMonthValue": value(row["last_month_value"]),
            }
        )
    history = secondary.get("history") or {}
    monthly_totals = []
    for row in history.get("monthly_total", pd.DataFrame()).to_dict("records"):
        monthly_totals.append(
            {
                "month": str(row["month"]),
                "units": value(row["units"]),
                "value": value(row["sales_value"]),
                "channels": int(row["channels"]),
            }
        )
    monthly_channels = []
    for row in history.get("monthly_channel", pd.DataFrame()).to_dict("records"):
        monthly_channels.append(
            {
                "month": str(row["month"]),
                "channel": str(row["channel_name"]),
                "units": value(row["units"]),
                "value": value(row["sales_value"]),
            }
        )
    monthly_sku_channels = []
    for row in history.get("monthly_sku_channel", pd.DataFrame()).to_dict("records"):
        monthly_sku_channels.append(
            {
                "month": str(row["month"]),
                "sku": str(row["child_sku"]),
                "product": str(row["product_name"]),
                "brand": str(row["brand"]),
                "channel": str(row["channel_name"]),
                "units": value(row["units"]),
                "value": value(row["sales_value"]),
            }
        )
    complete_months = history.get("complete_months", [])
    return {
        "sourceFile": secondary["source_file"],
        "dataThrough": secondary["data_through"].isoformat(),
        "totals": secondary["totals"],
        "channels": channels,
        "skuChannels": sku_channels,
        "monthlyHistory": {
            "months": monthly_totals,
            "channels": monthly_channels,
            "latestTwoMonths": complete_months[-2:],
            "latestThreeMonths": complete_months[-3:],
            "coverage": history.get("quality", {}),
        },
        "monthlySkuChannels": monthly_sku_channels,
        "drrRule": "SKU Secondary DRR = sum of channel max(7-day units/7, 30-day units/30)",
        "lastMonthValueComplete": secondary["previous_values_complete"],
    }
