from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

from eta_plan import attach_eta_metrics, attach_previous_eta_metrics, parse_eta_plan


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
            [["SKU1", date(2026, 9, 9), 320]],
            columns=["SkuCode", "Next Connection Date", "Next Connection Units"],
        )
        output = attach_eta_metrics(frame, plan)
        row = output.iloc[0]
        # Eligible stock is 600+120+300=1,020; RTV stock is excluded.
        self.assertEqual(int(row["Post Connection Secondary Overall DOI"]), 84)
        self.assertEqual(int(row["Post Connection Primary Overall DOI"]), 9)

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
