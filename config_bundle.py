"""Encrypt and restore private facility-mapping files for public deployment."""

from __future__ import annotations

import argparse
import base64
import json
import os
import tempfile
from pathlib import Path

from crypto_payload import decrypt_payload, encrypt_payload


BUNDLE_KIND = "fg-inventory-private-config"


def pack_config(master: Path, overrides: Path, output: Path, passcode: str) -> None:
    if not master.exists():
        raise FileNotFoundError(f"Location master not found: {master}")
    if not overrides.exists():
        raise FileNotFoundError(f"Facility overrides not found: {overrides}")
    envelope = encrypt_payload(
        {
            "kind": BUNDLE_KIND,
            "files": {
                "Location master.xlsx": base64.b64encode(master.read_bytes()).decode("ascii"),
                "facility_overrides.csv": base64.b64encode(overrides.read_bytes()).decode("ascii"),
            },
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


def restore_config(bundle: Path, config_dir: Path, passcode: str) -> bool:
    master = config_dir / "Location master.xlsx"
    overrides = config_dir / "facility_overrides.csv"
    if master.exists() and overrides.exists():
        return False
    if not bundle.exists():
        return False
    envelope = json.loads(bundle.read_text(encoding="utf-8"))
    payload = decrypt_payload(envelope, passcode)
    if payload.get("kind") != BUNDLE_KIND:
        raise ValueError("The encrypted configuration bundle has an unexpected type.")
    files = payload.get("files")
    if not isinstance(files, dict):
        raise ValueError("The encrypted configuration bundle has no files.")
    config_dir.mkdir(parents=True, exist_ok=True)
    for name in ("Location master.xlsx", "facility_overrides.csv"):
        encoded = files.get(name)
        if not isinstance(encoded, str) or not encoded:
            raise ValueError(f"The encrypted configuration bundle is missing {name}.")
        raw = base64.b64decode(encoded, validate=True)
        with tempfile.NamedTemporaryFile(
            "wb",
            dir=config_dir,
            prefix=name + ".",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(raw)
            staged = Path(handle.name)
        staged.replace(config_dir / name)
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
    pack.add_argument("--master", type=Path, required=True)
    pack.add_argument("--overrides", type=Path, required=True)
    pack.add_argument("--output", type=Path, required=True)
    pack.add_argument("--passcode-file", type=Path)
    restore = subparsers.add_parser("restore")
    restore.add_argument("--bundle", type=Path, required=True)
    restore.add_argument("--config-dir", type=Path, required=True)
    restore.add_argument("--passcode-file", type=Path)
    args = parser.parse_args()
    if args.command == "pack":
        pack_config(args.master, args.overrides, args.output, _passcode(args.passcode_file))
    else:
        restore_config(args.bundle, args.config_dir, _passcode(args.passcode_file))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
