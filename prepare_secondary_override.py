"""Prepare chunked GitHub Actions secret values from a reviewed Gmail attachment."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from secondary_override import pack_secondary_override
from secondary_sales import build_secondary_sales


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("attachment", type=Path)
    parser.add_argument("--date", required=True)
    parser.add_argument(
        "--history-db", type=Path, default=Path("data/secondary_sales_history.sqlite")
    )
    parser.add_argument("--output-dir", type=Path, default=Path("work/secondary_override"))
    parser.add_argument("--chunk-size", type=int, default=40_000)
    args = parser.parse_args()

    report_date = date.fromisoformat(args.date)
    secondary, quality = build_secondary_sales(
        args.attachment,
        report_date,
        history_db=args.history_db,
    )
    encoded = pack_secondary_override(secondary, quality, report_date)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for old in args.output_dir.glob("part-*.txt"):
        old.unlink()
    parts = [
        encoded[index : index + args.chunk_size]
        for index in range(0, len(encoded), args.chunk_size)
    ]
    for index, part in enumerate(parts, start=1):
        (args.output_dir / f"part-{index:02d}.txt").write_text(part, encoding="ascii")
    print(
        f"Prepared {len(parts)} confidential secret chunk(s); "
        f"source={secondary['source_file']}; through={secondary['data_through']}; "
        f"encoded_bytes={len(encoded)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
