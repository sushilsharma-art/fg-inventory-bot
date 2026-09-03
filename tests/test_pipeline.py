from __future__ import annotations

import json
import tempfile
import unittest
import csv
from datetime import date
from pathlib import Path

import pandas as pd

from config_bundle import pack_config, restore_config
from crypto_payload import decrypt_payload, encrypt_payload
from inventory_pipeline import (
    FG_SOURCE_COLUMNS,
    build_freshness,
    build_inventory_frame,
    build_payload,
)
from secondary_sales import (
    attach_previous_secondary_metrics,
    attach_secondary_metrics,
    build_secondary_sales,
)
from sales_history import read_manual_history
from tableau_history_refresh import normalize_exports


class InventoryPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.master = self.root / "master.csv"
        pd.DataFrame(
            [
                ["Mumbai Hub", "Mumbai", "Non 3PL", "YES", "YES"],
                ["Channel Demand FC", "Mumbai", "Non 3PL", "No", "YES"],
                ["Dark Hub", "Dark Store", "Non 3PL", "No", "YES"],
                ["BLR Hub", "Bangalore", "3PL", "YES", "YES"],
            ],
            columns=[
                "Depot Name",
                "New Tagiing for Sale",
                "3PL",
                "For Sale Check1",
                "To be considered?",
            ],
        ).to_csv(self.master, index=False)
        pd.DataFrame(
            [["Channel Demand FC", "B2B Mumbai", "Non 3PL", "No", "Yes"]],
            columns=[
                "Depot Name",
                "New Tagiing for Sale",
                "3PL",
                "For Sale Check1",
                "To be considered?",
            ],
        ).to_csv(self.root / "facility_overrides.csv", index=False)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _fg_source(self) -> Path:
        path = self.root / "FG INVENTORY REPORT_07082026102314.csv"
        rows = [
            ["C", "ManMatters", "M", "Mumbai Hub", "SKU1", "One", 100, 50, 30000, 600, 0, 120, 0, 3000, 0, 840, 0],
            ["C", "ManMatters", "D", "Dark Hub", "SKU1", "One", 100, 50, 15000, 300, 0, 0, 0, 900, 0, 210, 0],
            ["C", "LittleJoys", "B", "BLR Hub", "SKU2", "Two", 200, 80, 16000, 200, 0, 40, 0, 600, 0, 210, 0],
        ]
        pd.DataFrame(rows, columns=FG_SOURCE_COLUMNS).to_csv(path, index=False)
        return path

    def _shelf_source(self) -> Path:
        path = self.root / "Shelfwise Inventory_07082026102242.csv"
        pd.DataFrame(
            [
                ["Mumbai Hub", "SKU1", "GOOD_INVENTORY", True, 100, "2027-07-01", "2026-07-01"],
                ["Dark Hub", "SKU1", "GOOD_INVENTORY", True, 30, "2026-08-20", "2026-05-01"],
                ["BLR Hub", "SKU2", "GOOD_INVENTORY", True, 50, "2026-08-01", "2026-01-01"],
            ],
            columns=[
                "Facility", "Item Type SKU Code", "Inventory Type",
                "Inventory Allocation", "Quantity", "Expiry", "Manufacturing",
            ],
        ).to_csv(path, index=False)
        return path

    def _channel_sales_source(self) -> Path:
        path = self.root / "Channel Sales Tracker Dump_2026-08-07.xlsx"
        rows = []
        for day in pd.date_range("2026-07-06", "2026-08-06", freq="D"):
            for channel in ["WebApp", "Blinkit"]:
                for sku in ["SKU1", "SKU2"]:
                    for _ in range(8):
                        rows.append(
                            [
                                day,
                                channel,
                                "MM",
                                "Category",
                                "Subcategory",
                                f"Product {sku}",
                                sku,
                                1.0,
                                100.0,
                            ]
                        )
        raw = pd.DataFrame(
            rows,
            columns=[
                "order_date",
                "channel_name",
                "brand",
                "category",
                "sub_category",
                "product_name",
                "child_sku",
                "qty",
                "sales",
            ],
        )
        historic_rows = []
        for day in pd.date_range("2026-07-01", "2026-07-31", freq="D"):
            for channel in ["WebApp", "Blinkit"]:
                for sku in ["SKU1", "SKU2"]:
                    historic_rows.append(
                        [sku, f"Product {sku}", "Subcategory", "Category", "MM", channel, day, 1.0]
                    )
        historic = pd.DataFrame(
            historic_rows,
            columns=[
                "child_sku",
                "product_name",
                "sub_category",
                "category",
                "brand",
                "channel_name",
                "order_date",
                "qty",
            ],
        )
        with pd.ExcelWriter(path, engine="openpyxl") as writer:
            raw.to_excel(writer, sheet_name="raw_data", index=False)
            historic.to_excel(
                writer,
                sheet_name="Historic Jan26 onwards",
                index=False,
                startrow=1,
            )
        return path

    def test_calculations_and_dark_store_derivation(self) -> None:
        frame, quality = build_inventory_frame(
            self._fg_source(), self.master, min_rows=1, min_skus=1
        )
        sku1 = frame.loc[frame["SkuCode"].eq("SKU1")].iloc[0]
        self.assertEqual(int(sku1["Overall DRR"]), 150)
        self.assertEqual(int(sku1["DRR without DS"]), 120)
        self.assertEqual(int(sku1["Overall DOI"]), 7)
        self.assertEqual(int(sku1["DOI without DS"]), 6)
        self.assertEqual(quality["unmapped_facilities"], 0)

        freshness, _ = build_freshness(
            self._shelf_source(), self.master, date(2026, 8, 7), min_rows=1, min_skus=1
        )
        payload = build_payload(
            frame,
            freshness,
            report_date=date(2026, 8, 7),
            source_files={"fg": "fg.csv", "shelfwise": "shelf.csv"},
            quality={"publication_gate": "passed"},
        )
        record = next(row for row in payload["skus"] if row["code"] == "SKU1")
        dark_store = next(row for row in record["locs"] if row["n"] == "Dark Store")
        self.assertEqual(dark_store["r"], 30)
        self.assertEqual(dark_store["d"], 10)
        self.assertEqual(record["fresh80"], 100)
        self.assertEqual(record["freshLE80"], 30)

    def test_unmapped_facility_blocks_publication(self) -> None:
        source = pd.read_csv(self._fg_source())
        source.loc[0, "Depot Name"] = "Unknown Hub"
        path = self.root / "FG INVENTORY REPORT_07082026102315.csv"
        source.to_csv(path, index=False)
        with self.assertRaisesRegex(ValueError, "unmapped facilities"):
            build_inventory_frame(path, self.master, min_rows=1, min_skus=1)

    def test_channel_demand_fc_is_a_separate_b2b_mumbai_location(self) -> None:
        source = pd.read_csv(self._fg_source())
        source.loc[len(source)] = [
            "C", "LittleJoys", "B2B", "Channel Demand FC", "SKU1", "One",
            100, 50, 5000, 100, 0, 20, 0, 0, 0, 0, 0,
        ]
        path = self.root / "FG INVENTORY REPORT_07082026102316.csv"
        source.to_csv(path, index=False)
        frame, _ = build_inventory_frame(path, self.master, min_rows=1, min_skus=1)
        payload = build_payload(
            frame,
            {},
            report_date=date(2026, 8, 7),
            source_files={"fg": path.name, "shelfwise": "shelf.csv"},
            quality={"publication_gate": "passed"},
        )
        record = next(row for row in payload["skus"] if row["code"] == "SKU1")
        locations = {row["n"]: row for row in record["locs"]}
        self.assertEqual(locations["B2B Mumbai"]["s"], 100)
        self.assertEqual(locations["B2B Mumbai"]["t"], 20)
        self.assertEqual(locations["Mumbai"]["s"], 600)

    def test_secondary_sales_drr_and_doi(self) -> None:
        secondary, quality = build_secondary_sales(
            self._channel_sales_source(), date(2026, 8, 7)
        )
        sku1 = secondary["sku_summary"].set_index("child_sku").loc["SKU1"]
        self.assertEqual(float(sku1["drr7"]), 16.0)
        self.assertEqual(float(sku1["drr30"]), 16.0)
        self.assertEqual(float(sku1["drr"]), 16.0)
        self.assertEqual(float(sku1["last_month_units"]), 62.0)
        self.assertFalse(quality["previous_month_values_complete"])

        frame, _ = build_inventory_frame(
            self._fg_source(), self.master, min_rows=1, min_skus=1
        )
        enriched = attach_secondary_metrics(frame, secondary)
        result = enriched.loc[enriched["SkuCode"].eq("SKU1")].iloc[0]
        self.assertEqual(float(result["Secondary DRR"]), 16.0)
        self.assertEqual(int(result["Secondary Overall DOI"]), 64)
        self.assertEqual(float(result["Secondary Mumbai DRR"]), 8.0)
        self.assertEqual(int(result["Secondary Mumbai DOI"]), 75)

    def test_previous_secondary_metrics_are_carried_with_current_inventory_doi(self) -> None:
        frame, _ = build_inventory_frame(
            self._fg_source(), self.master, min_rows=1, min_skus=1
        )
        previous_skus = [
            {
                "code": "SKU1",
                "sec7": 112,
                "secDrr7": 16,
                "sec30": 480,
                "secDrr30": 16,
                "secDRR": 16,
                "secMTDUnits": 50,
                "secMTDValue": 5000,
                "secLastMonthUnits": 450,
                "secLastMonthValue": 45000,
                "secMumbaiDRR": 8,
            }
        ]
        enriched = attach_previous_secondary_metrics(frame, previous_skus)
        result = enriched.loc[enriched["SkuCode"].eq("SKU1")].iloc[0]
        self.assertEqual(float(result["Secondary DRR"]), 16.0)
        self.assertEqual(int(result["Secondary Overall DOI"]), 64)
        self.assertEqual(float(result["Secondary Mumbai DRR"]), 8.0)
        self.assertEqual(int(result["Secondary Mumbai DOI"]), 75)

    def test_encryption_round_trip_and_wrong_passcode(self) -> None:
        payload = {"dateKey": "2026-08-07", "skus": [{"code": "SKU1"}]}
        envelope = encrypt_payload(payload, "correct horse battery staple", iterations=10_000)
        self.assertEqual(decrypt_payload(envelope, "correct horse battery staple"), payload)
        with self.assertRaises(Exception):
            decrypt_payload(envelope, "wrong passcode")

    def test_private_config_bundle_round_trip(self) -> None:
        config_dir = self.root / "private-config"
        config_dir.mkdir()
        master = config_dir / "Location master.xlsx"
        overrides = config_dir / "facility_overrides.csv"
        master.write_bytes(b"private master")
        overrides.write_bytes(b"private overrides")
        bundle = self.root / "config_bundle.enc.json"
        passcode = "correct horse battery staple"
        pack_config(master, overrides, bundle, passcode)
        master.unlink()
        overrides.unlink()
        self.assertTrue(restore_config(bundle, config_dir, passcode))
        self.assertEqual(master.read_bytes(), b"private master")
        self.assertEqual(overrides.read_bytes(), b"private overrides")

    def test_tableau_quantity_dimensions_are_restored_from_value_export(self) -> None:
        quantity_path = self.root / "EComm Overall.csv"
        value_path = self.root / "EComm Overall Sales.csv"
        quantity_dates = ["2026-08-01 00:00:00", "2026-08-13 00:00:00", "2026-08-14 00:00:00"]
        value_dates = ["31/07/2026", "01/08/2026", "13/08/2026"]

        with quantity_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
            writer.writerow(["", "", "", ""] + ["Date Level"] * len(quantity_dates))
            writer.writerow(
                ["child_sku", "product_name", "channel_name", "product_name"]
                + quantity_dates
            )
            writer.writerow(["Grand Total", "Total", "Total", "Total", 5, 7, 1])
            writer.writerow(["", "", "Amazon", "", 2, 3, 1])
            writer.writerow(["", "", "WebApp", "", 3, 4, 0])
            writer.writerow(["SKU1", "One", "Amazon", "One", 2, 3, 1])
            writer.writerow(["SKU2", "Two", "WebApp", "Two", 3, 4, 0])

        with value_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
            writer.writerow([""] * 7 + ["Date Level"] * len(value_dates))
            writer.writerow(
                [
                    "child_sku",
                    "product_name",
                    "SubCategory_PM",
                    "Category_PM",
                    "brand",
                    "channel_name",
                    "product_name",
                ]
                + value_dates
            )
            writer.writerow(["Grand Total"] + ["Total"] * 6 + [100, 500, 700])
            writer.writerow(["", "", "", "", "", "Amazon", "", 40, 200, 300])
            writer.writerow(["", "", "", "", "", "WebApp", "", 60, 300, 400])
            writer.writerow(
                ["SKU1", "One", "Sub1", "Cat1", "MM", "Amazon", "One", 40, 200, 300]
            )
            writer.writerow(
                ["SKU2", "Two", "Sub2", "Cat2", "LJ", "WebApp", "Two", 60, 300, 400]
            )

        quantity, value, date_columns, quality = normalize_exports(
            quantity_path, value_path
        )
        self.assertEqual(date_columns, ["2026-08-01", "2026-08-13"])
        self.assertTrue(quality["format_match"])
        self.assertEqual(quality["quantity_only_dates_ignored"], ["2026-08-14"])
        self.assertEqual(quality["value_only_dates_ignored"], [])
        self.assertEqual(quality["older_value_dates_ignored"], ["2026-07-31"])
        self.assertEqual(quality["target_month"], "2026-08")
        self.assertEqual(quantity.loc[0, "sub_category"], "Sub1")
        self.assertEqual(quantity.loc[0, "brand"], "MM")
        self.assertEqual(quality["quantity"]["subtotal_rows_ignored"], 2)
        self.assertEqual(quality["value"]["subtotal_rows_ignored"], 2)
        self.assertEqual(float(quantity[date_columns].sum().sum()), 12.0)
        self.assertEqual(float(value[date_columns].sum().sum()), 1200.0)

    def test_tableau_sparse_zero_measure_rows_are_reconciled(self) -> None:
        quantity_path = self.root / "EComm Overall sparse.csv"
        value_path = self.root / "EComm Overall Sales sparse.csv"
        dates = ["01/08/2026", "02/08/2026"]

        with quantity_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
            writer.writerow(["", "", ""] + ["Date Level"] * len(dates))
            writer.writerow(["child_sku", "product_name", "channel_name"] + dates)
            writer.writerow(["Grand Total", "Total", "Total", 59, 0])
            for index in range(50):
                writer.writerow([f"SKU{index}", f"Product {index}", "Amazon", 1, 0])
                if index == 0:
                    writer.writerow(["", "", "WebApp", 4, 0])
            writer.writerow(["QONLY", "Quantity only", "Amazon", 2, 0])
            writer.writerow(["", "Unmapped product", "Amazon", 3, 0])

        with value_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
            writer.writerow([""] * 6 + ["Date Level"] * len(dates))
            writer.writerow(
                [
                    "child_sku",
                    "product_name",
                    "SubCategory_PM",
                    "Category_PM",
                    "brand",
                    "channel_name",
                ]
                + dates
            )
            writer.writerow(["Grand Total"] + ["Total"] * 5 + [590, 0])
            for index in range(50):
                writer.writerow(
                    [f"SKU{index}", f"Product {index}", "Sub", "Cat", "MM", "Amazon", 10, 0]
                )
                if index == 0:
                    writer.writerow(["", "", "", "", "", "WebApp", 40, 0])
            writer.writerow(["VONLY", "Value only", "Sub", "Cat", "MM", "Amazon", 20, 0])
            writer.writerow(["", "Unmapped product", "Sub", "Cat", "MM", "Amazon", 30, 0])

        quantity, value, date_columns, quality = normalize_exports(
            quantity_path, value_path
        )
        self.assertEqual(len(quantity), 54)
        self.assertEqual(len(value), 54)
        self.assertEqual(int(quantity["child_sku"].eq("").sum()), 1)
        self.assertEqual(quality["quantity"]["hierarchy_continuation_rows_filled"], 1)
        self.assertEqual(
            quantity.loc[quantity["channel_name"].eq("WebApp"), "child_sku"].iloc[0],
            "SKU0",
        )
        self.assertEqual(quality["quantity_only_rows_zero_filled"], 1)
        self.assertEqual(quality["value_only_rows_zero_filled"], 1)
        self.assertEqual(
            float(quantity.loc[quantity["child_sku"].eq("VONLY"), date_columns].sum().sum()),
            0.0,
        )
        self.assertEqual(
            float(value.loc[value["child_sku"].eq("QONLY"), date_columns].sum().sum()),
            0.0,
        )


if __name__ == "__main__":
    unittest.main()
