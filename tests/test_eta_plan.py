from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd
import requests

from eta_plan import (
    ETA_SOURCE_URL,
    attach_eta_metrics,
    audit_eta_payload,
    download_eta_plan,
    eta_snapshot_matches,
    load_eta_snapshot,
    parse_eta_plan,
)


class EtaPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_first_future_connection_aggregates_duplicate_sku_rows(self) -> None:
        path = self.root / "rolling.csv"
        blank = [""] * 12
        header = ["Brand", "SKU Code", "Name", *([""] * 7), "09-Sep-Wed", "10-Sep-Thu"]
        pd.DataFrame(
            [
                ["Rolling Day ->", *([""] * 11)],
                blank,
                header,
                ["MM", "SKU1", "One A", *([""] * 7), "1,000", "500"],
                ["MM", "SKU1", "One B", *([""] * 7), "250", "700"],
                ["LJ", "SKU2", "Two", *([""] * 7), "", "900"],
            ]
        ).to_csv(path, index=False, header=False)

        plan, quality = parse_eta_plan(path, date(2026, 9, 8), min_skus=1)
        rows = plan.set_index("SkuCode")
        self.assertEqual(rows.loc["SKU1", "Next Connection Date"], date(2026, 9, 9))
        self.assertEqual(float(rows.loc["SKU1", "Next Connection Units"]), 1250.0)
        self.assertEqual(rows.loc["SKU2", "Next Connection Date"], date(2026, 9, 10))
        self.assertEqual(quality["duplicate_sku_rows"], 1)
        self.assertEqual(quality["upcoming_skus"], 2)

    def test_mixed_headers_preserve_cutoff_duplicates_and_year_rollover(self) -> None:
        path = self.root / "rolling.csv"
        prefix = ["Brand", "SKU Code", "Name", "", ""]
        dates = ["26-11", "27-11", "27-Nov-Fri", "02/12", "01-01", "31-11", "12-13"]
        pd.DataFrame(
            [
                prefix + dates,
                ["MM", "SKU1", "One A", "", "", 900, 100, 200, 500, 0, 999, 999],
                ["MM", "sku1", "One B", "", "", 300, 30, 0, 0, 0, 0, 0],
                ["LJ", "SKU2", "Two", "", "", 50, 0, 0, 80, 0, 0, 0],
                ["BB", "SKU3", "Three", "", "", 0, 0, 0, 0, 90, 999, 999],
            ]
        ).to_csv(path, index=False, header=False)

        plan, quality = parse_eta_plan(path, date(2026, 11, 26), min_skus=1)
        rows = plan.set_index("SkuCode")
        self.assertEqual(rows.loc["SKU1", "Next Connection Date"], date(2026, 11, 27))
        self.assertEqual(rows.loc["SKU1", "Next Connection Units"], 330)
        self.assertEqual(rows.loc["SKU2", "Next Connection Date"], date(2026, 12, 2))
        self.assertEqual(rows.loc["SKU3", "Next Connection Date"], date(2027, 1, 1))
        self.assertEqual(quality["future_date_end"], "2027-01-01")

    def test_post_connection_doi_uses_same_eligible_network_stock(self) -> None:
        frame = pd.DataFrame(
            [
                ["SKU1", "Yes", "Mumbai", 600, 120, 150, 16],
                ["SKU1", "Yes", "Dark Store", 300, 0, 150, 16],
                ["SKU1", "Yes", "Mumbai RTV", 999, 999, 150, 16],
            ],
            columns=[
                "SkuCode",
                "Inventory Check",
                "Location Name",
                "Stock on Hand",
                "Stock In Transfer",
                "Overall DRR",
                "Secondary DRR",
            ],
        )
        plan = pd.DataFrame(
            [["SKU1", date(2026, 9, 13), 320]],
            columns=["SkuCode", "Next Connection Date", "Next Connection Units"],
        )
        output = attach_eta_metrics(frame, plan, date(2026, 9, 8))
        row = output.iloc[0]
        # Eligible stock is 1,020 and RTV is excluded. Before the connection,
        # five days of normal sales are consumed independently for each DRR.
        self.assertEqual(int(row["Days Until Connection"]), 5)
        self.assertEqual(int(row["Post Connection Secondary Overall DOI"]), 79)
        self.assertEqual(int(row["Post Connection Primary Overall DOI"]), 4)

    def test_publication_audit_verifies_connections_and_nullable_doi(self) -> None:
        report_date = date(2026, 11, 26)
        connection_date = date(2026, 11, 27)
        frame = pd.DataFrame(
            [["SKU1", "Yes", "Mumbai", 40, 0, 0, 5]],
            columns=["SkuCode", "Inventory Check", "Location Name", "Stock on Hand",
                     "Stock In Transfer", "Overall DRR", "Secondary DRR"],
        )
        plan = pd.DataFrame(
            [["SKU1", connection_date, 15], ["SKU2", connection_date, 10]],
            columns=["SkuCode", "Next Connection Date", "Next Connection Units"],
        )
        records = [{"code": "SKU1", "nextEtaDate": "2026-11-27", "nextEtaUnits": 15,
                    "postEtaPrimaryOverallDOI": None, "postEtaSecOverallDOI": 10}]
        calculated = attach_eta_metrics(frame, plan, report_date)
        report = audit_eta_payload(calculated, plan, records)
        self.assertEqual(report["published_connection_skus"], 1)
        self.assertEqual(report["verified_date_and_units_skus"], 1)
        self.assertEqual(report["verified_projection_skus"], 1)
        self.assertEqual(report["published_primary_doi_skus"], 0)
        self.assertEqual(report["published_secondary_doi_skus"], 1)
        self.assertEqual(report["published_both_doi_skus"], 0)
        records[0]["postEtaPrimaryOverallDOI"] = 0
        self.assertEqual(audit_eta_payload(calculated, plan, records)["verified_projection_skus"], 0)
        records[0]["nextEtaUnits"] = 5
        self.assertEqual(audit_eta_payload(calculated, plan, records)["verified_date_and_units_skus"], 0)

    def test_download_uses_only_the_approved_tab_and_fingerprints_changes(self) -> None:
        report_date = date(2026, 11, 27)
        headers = ["Brand", "SKU Code", "Name", *([""] * 8), "28-11"]
        rows = [["MM", f"SKU{i}", "Test", *([""] * 8), 25] for i in range(105)]
        received = Mock(content=pd.DataFrame([headers, *rows]).to_csv(index=False, header=False).encode())
        with patch.dict("os.environ", {"FG_BOT_ETA_CSV_URL": "https://example.invalid/other.csv"}):
            with patch("eta_plan.requests.get", return_value=received) as fetch:
                _, plan, first = download_eta_plan(self.root, report_date)
                fetch.assert_called_once_with(ETA_SOURCE_URL, timeout=(15, 90))
                self.assertEqual(len(plan), 105)
                rows[0][-1] = 30
                received.content = pd.DataFrame([headers, *rows]).to_csv(index=False, header=False).encode()
                _, _, edited = download_eta_plan(self.root, report_date)
        previous = {
            "status": "fresh",
            "sourceUrl": first["source_url"],
            "planFingerprint": first["plan_fingerprint"],
        }
        self.assertTrue(eta_snapshot_matches(previous, first))
        self.assertFalse(eta_snapshot_matches(previous, edited))
        self.assertFalse(eta_snapshot_matches({**previous, "sourceUrl": "old.csv"}, first))

    def test_failed_approved_tab_clears_connection_fields(self) -> None:
        frame = pd.DataFrame(
            [["SKU1", "Yes", "Mumbai", 100, 0, 10, 5]],
            columns=[
                "SkuCode",
                "Inventory Check",
                "Location Name",
                "Stock on Hand",
                "Stock In Transfer",
                "Overall DRR",
                "Secondary DRR",
            ],
        )
        frame["Next Connection Date"] = date(2026, 11, 29)
        frame["Next Connection Units"] = 50
        report_date = date(2026, 11, 27)
        with patch("eta_plan.requests.get", side_effect=requests.Timeout("test timeout")):
            path, plan, quality = load_eta_snapshot(self.root, report_date)
        self.assertIsNone(path)
        self.assertEqual(quality["status"], "unavailable")
        self.assertFalse(eta_snapshot_matches({"status": "fresh", "sourceUrl": ETA_SOURCE_URL}, quality))
        output = attach_eta_metrics(frame, plan, report_date)
        self.assertTrue(pd.isna(output.iloc[0]["Next Connection Date"]))
        self.assertTrue(pd.isna(output.iloc[0]["Next Connection Units"]))


if __name__ == "__main__":
    unittest.main()
