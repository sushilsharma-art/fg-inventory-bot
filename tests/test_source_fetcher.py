from __future__ import annotations

import unittest
from datetime import date, time
from unittest.mock import patch

from source_fetcher import REPORTS, ReportSpec, _stamps, scan_cloudfront


class SourceFetcherTests(unittest.TestCase):
    def test_fg_checks_rotated_folder_and_late_window(self) -> None:
        fg = REPORTS[0]
        self.assertIn("6a9e5934547c863b8a4e8d33", fg.base_urls[0])
        self.assertEqual(fg.scan_windows[-1][1], time(12, 5))

    def test_stamps_are_newest_first_inside_each_window(self) -> None:
        stamps = _stamps(
            date(2026, 9, 7),
            ((time(10, 0, 0), time(10, 0, 2)),),
        )
        self.assertEqual(
            stamps,
            ["07092026100002", "07092026100001", "07092026100000"],
        )

    @patch("source_fetcher._url_exists")
    def test_scan_returns_latest_hit_from_rotated_folder(self, exists) -> None:
        exists.side_effect = lambda url: (
            "new-folder" in url and url.endswith("07092026100002.csv")
        )
        spec = ReportSpec(
            key="fg",
            label="FG INVENTORY REPORT",
            base_urls=("https://example.test/new-folder/", "https://example.test/old/"),
            encoded_prefix="FG%20INVENTORY%20REPORT_",
            filename_prefix="FG INVENTORY REPORT_",
            expected_header="Category",
            min_size=1,
            min_rows=1,
            scan_windows=((time(10, 0, 0), time(10, 0, 2)),),
        )

        url = scan_cloudfront(spec, date(2026, 9, 7))

        self.assertEqual(
            url,
            "https://example.test/new-folder/"
            "FG%20INVENTORY%20REPORT_07092026100002.csv",
        )


if __name__ == "__main__":
    unittest.main()
