from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

from eta_plan import (
    attach_eta_metrics,
    attach_previous_eta_metrics,
    audit_eta_payload,
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

    def test_previous_eta_is_discarded_after_its_connection_date(self) -> None:
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
        output, count = attach_previous_eta_metrics(
            frame,
            [{"code": "SKU1", "nextEtaDate": "2026-09-08", "nextEtaUnits": 50}],
            date(2026, 9, 8),
        )
        self.assertEqual(count, 0)
        self.assertTrue(pd.isna(output.iloc[0]["Next Connection Date"]))


if __name__ == "__main__":
    unittest.main()
