"""Download the two approved Tableau Cloud crosstabs with a PAT.

The PAT is read only from environment variables.  Its secret is never written
to disk or included in logs.  Downloads are staged as ``.part`` files and are
promoted only after Tableau returns a valid Excel workbook.
"""

from __future__ import annotations

import argparse
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from xml.etree import ElementTree

import requests


DEFAULT_SERVER = "https://prod-apnortheast-a.online.tableau.com"
DEFAULT_SITE = "manmatters"
DEFAULT_WORKBOOK = "OverallSalesTracker"
DEFAULT_API_VERSION = "3.29"
XML_NAMESPACE = "http://tableau.com/api"


@dataclass(frozen=True)
class TableauView:
    id: str
    name: str
    content_url: str


def _normal(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).casefold())


def _xml(response: requests.Response) -> ElementTree.Element:
    response.raise_for_status()
    try:
        return ElementTree.fromstring(response.content)
    except ElementTree.ParseError as exc:
        raise ValueError(
            f"Tableau returned an unreadable response ({response.status_code})."
        ) from exc


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required for the Tableau refresh.")
    return value


def _select_workbook(items: Iterable[ElementTree.Element], wanted: str) -> str:
    target = _normal(wanted)
    matches = [
        item
        for item in items
        if target in {_normal(item.get("name", "")), _normal(item.get("contentUrl", ""))}
    ]
    if len(matches) != 1:
        names = sorted({item.get("name", "") for item in items})
        raise ValueError(
            f"Unable to uniquely resolve Tableau workbook {wanted!r}; "
            f"matches={len(matches)}, available={names[:25]}"
        )
    return str(matches[0].get("id"))


def _select_view(views: list[TableauView], candidates: Iterable[str]) -> TableauView:
    targets = {_normal(value) for value in candidates}
    exact = [
        view
        for view in views
        if _normal(view.name) in targets
        or _normal(view.content_url.rsplit("/", 1)[-1]) in targets
    ]
    if len(exact) != 1:
        available = sorted({f"{view.name} ({view.content_url})" for view in views})
        raise ValueError(
            "Unable to uniquely resolve Tableau view "
            f"{sorted(candidates)!r}; matches={len(exact)}, available={available[:40]}"
        )
    return exact[0]


def _assert_excel(path: Path) -> None:
    if path.stat().st_size < 10_000:
        raise ValueError(f"Tableau export is unexpectedly small: {path.name}")
    with path.open("rb") as handle:
        if handle.read(4) != b"PK\x03\x04":
            raise ValueError(f"Tableau did not return an Excel workbook: {path.name}")


def download_tableau_exports(output_dir: Path) -> dict[str, object]:
    server = (os.getenv("TABLEAU_SERVER_URL") or DEFAULT_SERVER).strip().rstrip("/")
    site = (os.getenv("TABLEAU_SITE_CONTENT_URL") or DEFAULT_SITE).strip()
    workbook_name = (
        os.getenv("TABLEAU_WORKBOOK_CONTENT_URL") or DEFAULT_WORKBOOK
    ).strip()
    api_version = (os.getenv("TABLEAU_API_VERSION") or DEFAULT_API_VERSION).strip()
    pat_name = _required_env("TABLEAU_PAT_NAME")
    pat_secret = _required_env("TABLEAU_PAT_SECRET")

    session = requests.Session()
    session.headers.update({"Accept": "application/xml", "User-Agent": "fg-inventory-bot/1.0"})
    token = ""
    site_id = ""
    try:
        signin = session.post(
            f"{server}/api/{api_version}/auth/signin",
            json={
                "credentials": {
                    "personalAccessTokenName": pat_name,
                    "personalAccessTokenSecret": pat_secret,
                    "site": {"contentUrl": site},
                }
            },
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=45,
        )
        signin.raise_for_status()
        credentials = signin.json()["credentials"]
        token = str(credentials["token"])
        site_id = str(credentials["site"]["id"])
        session.headers.update({"X-Tableau-Auth": token})

        workbook_response = session.get(
            f"{server}/api/{api_version}/sites/{site_id}/workbooks",
            params={"pageSize": 1000},
            timeout=60,
        )
        workbook_root = _xml(workbook_response)
        workbooks = list(workbook_root.findall(f".//{{{XML_NAMESPACE}}}workbook"))
        workbook_id = _select_workbook(workbooks, workbook_name)

        views_response = session.get(
            f"{server}/api/{api_version}/sites/{site_id}/workbooks/{workbook_id}/views",
            params={"pageSize": 1000},
            timeout=60,
        )
        views_root = _xml(views_response)
        views = [
            TableauView(
                id=str(item.get("id", "")),
                name=str(item.get("name", "")),
                content_url=str(item.get("contentUrl", "")),
            )
            for item in views_root.findall(f".//{{{XML_NAMESPACE}}}view")
        ]
        quantity_view = _select_view(
            views,
            [os.getenv("TABLEAU_QUANTITY_VIEW", "ECommOverall"), "EComm Overall"],
        )
        value_view = _select_view(
            views,
            [os.getenv("TABLEAU_VALUE_VIEW", "ECommOverallSales"), "EComm Overall Sales"],
        )

        output_dir.mkdir(parents=True, exist_ok=True)
        downloaded: dict[str, Path] = {}
        for key, view, filename in (
            ("quantity", quantity_view, "EComm Overall.xlsx"),
            ("value", value_view, "EComm Overall Sales.xlsx"),
        ):
            destination = output_dir / filename
            with tempfile.NamedTemporaryFile(
                dir=output_dir,
                prefix=destination.name + ".",
                suffix=".part",
                delete=False,
            ) as handle:
                staged = Path(handle.name)
            try:
                response = session.get(
                    f"{server}/api/{api_version}/sites/{site_id}/views/{view.id}/crosstab/excel",
                    params={"maxAge": 1},
                    timeout=180,
                )
                response.raise_for_status()
                staged.write_bytes(response.content)
                _assert_excel(staged)
                staged.replace(destination)
            except Exception:
                staged.unlink(missing_ok=True)
                raise
            downloaded[key] = destination

        return {
            "quantity": downloaded["quantity"],
            "value": downloaded["value"],
            "workbook": workbook_name,
            "quantity_view": quantity_view.name,
            "value_view": value_view.name,
            "site": site,
        }
    finally:
        if token and site_id:
            try:
                session.post(
                    f"{server}/api/{api_version}/auth/signout",
                    timeout=20,
                )
            except requests.RequestException:
                pass
        session.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    result = download_tableau_exports(args.output_dir.resolve())
    print(f"Downloaded Tableau quantity: {Path(result['quantity']).name}")
    print(f"Downloaded Tableau value: {Path(result['value']).name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
