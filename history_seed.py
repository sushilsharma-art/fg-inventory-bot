"""Create and restore a passcode-encrypted SQLite history seed."""

from __future__ import annotations

import argparse
import base64
import json
import os
import tempfile
import zlib
from pathlib import Path
from typing import Any

from crypto_payload import decrypt_payload, encrypt_payload


def pack_history(database: Path, output: Path, passcode: str) -> None:
    if not database.exists() or database.stat().st_size < 1_000:
        raise ValueError(f"History database is missing or empty: {database}")
    compressed = zlib.compress(database.read_bytes(), level=9)
    envelope = encrypt_payload(
        {
            "kind": "secondary-sales-sqlite-seed",
            "sqlite_zlib_b64": base64.b64encode(compressed).decode("ascii"),
        },
        passcode,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=output.parent,
        prefix=output.name + ".",
        suffix=".tmp",
        delete=False,
    ) as handle:
        json.dump(envelope, handle, ensure_ascii=False, separators=(",", ":"))
        staged = Path(handle.name)
    staged.replace(output)


def restore_history(seed: Path, database: Path, passcode: str) -> bool:
    if database.exists():
        return False
    if not seed.exists():
        return False
    envelope: dict[str, Any] = json.loads(seed.read_text(encoding="utf-8"))
    payload = decrypt_payload(envelope, passcode)
    if payload.get("kind") != "secondary-sales-sqlite-seed":
        raise ValueError("The encrypted history seed has an unexpected type.")
    raw = zlib.decompress(base64.b64decode(payload["sqlite_zlib_b64"]))
    if not raw.startswith(b"SQLite format 3\x00"):
        raise ValueError("The decrypted history seed is not a SQLite database.")
    database.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "wb",
        dir=database.parent,
        prefix=database.name + ".",
        suffix=".tmp",
        delete=False,
    ) as handle:
        handle.write(raw)
        staged = Path(handle.name)
    staged.replace(database)
    return True


def _passcode(path: Path | None) -> str:
    value = os.getenv("FG_BOT_PASSCODE", "").strip()
    if not value and path:
        value = path.read_text(encoding="utf-8").strip()
    if len(value) < 8:
        raise ValueError("FG_BOT_PASSCODE is missing or too short.")
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    pack = subparsers.add_parser("pack")
    pack.add_argument("--database", type=Path, required=True)
    pack.add_argument("--output", type=Path, required=True)
    pack.add_argument("--passcode-file", type=Path)
    restore = subparsers.add_parser("restore")
    restore.add_argument("--seed", type=Path, required=True)
    restore.add_argument("--database", type=Path, required=True)
    restore.add_argument("--passcode-file", type=Path)
    args = parser.parse_args()
    if args.command == "pack":
        pack_history(args.database, args.output, _passcode(args.passcode_file))
    else:
        restore_history(args.seed, args.database, _passcode(args.passcode_file))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
