"""Unit tests for check_recess.py — recess gate script.

Tests cover: not-in-recess, in-recess, API errors (fail open),
empty response, malformed HTML, and boundary dates.
"""

import io
import os
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

# Add parent directory to path so we can import check_recess
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import check_recess


def make_recess_html(start_date, end_date, description="Summer recess"):
    """Build a minimal HTML page with one recess date row.

    Dates should be in ISO yyyy-MM-dd format; they are converted to the
    "Friday 18 December 2026" format that parse_recess_dates expects.
    """
    def to_long_format(iso_date):
        dt = datetime.strptime(iso_date, "%Y-%m-%d")
        return dt.strftime("%A %d %B %Y")
    return f"""
    <table>
    <tr><td>{description}</td><td>{to_long_format(start_date)}</td><td>{to_long_format(end_date)}</td></tr>
    </table>
    """


class TestCheckRecess(unittest.TestCase):
    """Test the recess check logic."""

    @patch("check_recess.fetch_recess_html")
    def test_not_in_recess(self, mock_fetch):
        """Today is not in any recess range -> skip=false."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        # Recess is in the past
        mock_fetch.return_value = make_recess_html("2020-01-01", "2020-02-01")
        result = check_recess.is_in_recess_today()
        self.assertFalse(result)

    @patch("check_recess.fetch_recess_html")
    def test_in_recess(self, mock_fetch):
        """Today is within a recess range -> skip=true."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        # Recess spans a wide range that includes today
        start = "2020-01-01"
        end = "2099-12-31"
        mock_fetch.return_value = make_recess_html(start, end)
        result = check_recess.is_in_recess_today()
        self.assertTrue(result)

    @patch("check_recess.fetch_recess_html")
    def test_api_timeout(self, mock_fetch):
        """API raises ReadTimeout -> fail open (skip=false)."""
        import requests
        mock_fetch.side_effect = requests.exceptions.ReadTimeout("timeout")
        # The main() function should catch this and output skip=false
        with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
            check_recess.main()
            output = mock_stdout.getvalue()
            self.assertIn("skip=false", output)

    @patch("check_recess.fetch_recess_html")
    def test_api_connection_error(self, mock_fetch):
        """API raises ConnectionError -> fail open (skip=false)."""
        import requests
        mock_fetch.side_effect = requests.exceptions.ConnectionError("no connection")
        with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
            check_recess.main()
            output = mock_stdout.getvalue()
            self.assertIn("skip=false", output)

    @patch("check_recess.fetch_recess_html")
    def test_empty_response(self, mock_fetch):
        """Empty HTML -> no recess dates -> skip=false."""
        mock_fetch.return_value = "<html></html>"
        result = check_recess.is_in_recess_today()
        self.assertFalse(result)

    @patch("check_recess.fetch_recess_html")
    def test_malformed_html(self, mock_fetch):
        """Unparseable HTML -> no recess dates extracted -> skip=false."""
        mock_fetch.return_value = "this is not html at all"
        result = check_recess.is_in_recess_today()
        self.assertFalse(result)

    @patch("check_recess.fetch_recess_html")
    def test_boundary_start_date(self, mock_fetch):
        """Today == startDate of a recess range -> skip=true (inclusive)."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        mock_fetch.return_value = make_recess_html(today, "2099-12-31")
        result = check_recess.is_in_recess_today()
        self.assertTrue(result)

    @patch("check_recess.fetch_recess_html")
    def test_boundary_end_date(self, mock_fetch):
        """Today == endDate of a recess range -> skip=true (inclusive)."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        mock_fetch.return_value = make_recess_html("2020-01-01", today)
        result = check_recess.is_in_recess_today()
        self.assertTrue(result)


if __name__ == "__main__":
    unittest.main()
