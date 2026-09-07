"""Secure, short-lived transfer of reviewed Secondary Sales calculations."""

from __future__ import annotations

import base64
import json
import os
import zlib
from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd


ENV_PREFIX = "FG_BOT_SECONDARY_OVERRIDE_B64"
FRAME_KEYS = ("sku_summary", "sku_channel", "channel_summary")
HISTORY_FRAME_KEYS = (
    "monthly_sku_channel",
    "monthly_channel",
    "monthly_total",
    "daily_channel",
)


def _json_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (pd.Timestamp, date)):
        return value.isoformat()
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and np.isnan(value):
        return None
    return value


def _records(frame: pd.DataFrame | None) -> list[dict[str, Any]]:
    if frame is None or frame.empty:
        return []
    return [
        {key: _json_value(value) for key, value in row.items()}
        for row in frame.to_dict("records")
    ]


def pack_secondary_override(
    secondary: dict[str, Any],
    quality: dict[str, Any],
    report_date: date,
) -> str:
    """Create a compressed transfer value without including the raw workbook."""
    history = secondary.get("history") or {}
    packed_secondary = {
        "source_file": secondary["source_file"],
        "report_date": report_date.isoformat(),
        "data_through": secondary["data_through"].isoformat(),
        "totals": secondary["totals"],
        "previous_values_complete": secondary["previous_values_complete"],
        **{key: _records(secondary.get(key)) for key in FRAME_KEYS},
        "history": {
            **{key: _records(history.get(key)) for key in HISTORY_FRAME_KEYS},
            "complete_months": history.get("complete_months", []),
            "quality": history.get("quality", {}),
        },
    }
    document = {
        "schema": 1,
        "report_date": report_date.isoformat(),
        "secondary": packed_secondary,
        "quality": quality,
    }
    raw = json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
        default=_json_value,
    ).encode("utf-8")
    return base64.b64encode(zlib.compress(raw, level=9)).decode("ascii")


def _read_environment() -> str | None:
    direct = os.getenv(ENV_PREFIX, "").strip()
    if direct:
        return direct
    parts = []
    for index in range(1, 17):
        value = os.getenv(f"{ENV_PREFIX}_{index}", "").strip()
        if value:
            parts.append(value)
        elif parts:
            break
    return "".join(parts) or None


def load_secondary_override(run_date: date) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Load and strictly validate a current-report-date override from Actions secrets."""
    encoded = _read_environment()
    if not encoded:
        return None
    try:
        compressed = base64.b64decode(encoded, validate=True)
        raw = zlib.decompress(compressed)
        if len(raw) > 25_000_000:
            raise ValueError("Secondary override exceeds the safe decoded size.")
        document = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ValueError("Secondary override could not be decoded safely.") from exc

    if document.get("schema") != 1:
        raise ValueError("Unsupported Secondary override schema.")
    if document.get("report_date") != run_date.isoformat():
        print(
            "WARNING: Ignoring Secondary override for a different report date: "
            f"{document.get('report_date')}"
        )
        return None

    packed = document.get("secondary") or {}
    expected_source = f"Channel Sales Tracker Dump_{run_date:%Y-%m-%d}.xlsx"
    if packed.get("source_file") != expected_source:
        raise ValueError("Secondary override attachment name does not match the report date.")
    through = date.fromisoformat(str(packed.get("data_through")))
    if through > run_date or through < run_date - timedelta(days=2):
        raise ValueError(
            f"Secondary override through date {through} is not current for {run_date}."
        )

    secondary = {
        "source_file": packed["source_file"],
        "report_date": run_date,
        "data_through": through,
        "totals": packed.get("totals", {}),
        "previous_values_complete": bool(packed.get("previous_values_complete")),
        **{key: pd.DataFrame(packed.get(key, [])) for key in FRAME_KEYS},
        "history": {
            **{
                key: pd.DataFrame((packed.get("history") or {}).get(key, []))
                for key in HISTORY_FRAME_KEYS
            },
            "complete_months": (packed.get("history") or {}).get(
                "complete_months", []
            ),
            "quality": (packed.get("history") or {}).get("quality", {}),
        },
    }
    if secondary["sku_summary"].empty or secondary["sku_channel"].empty:
        raise ValueError("Secondary override contains no SKU metrics.")
    quality = dict(document.get("quality") or {})
    quality.update(
        {
            "secure_override": True,
            "carried_forward": False,
            "source_file": packed["source_file"],
            "data_through": through.isoformat(),
        }
    )
    return secondary, quality
