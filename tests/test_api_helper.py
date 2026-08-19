"""Unit tests for api_helper.py — api_get() retry logic.

Uses unittest.mock.patch to mock requests.get and time.sleep so tests
run instantly without hitting the real network.
"""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock

import requests

# Add parent directory to path so we can import api_helper
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api_helper


class TestApiGet(unittest.TestCase):
    """Test api_get() retry and success behaviour."""

    @patch("api_helper.time.sleep")
    @patch("api_helper.requests.get")
    def test_api_get_success(self, mock_get, mock_sleep):
        """A successful GET returns the response without retrying."""
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        result = api_helper.api_get("http://example.com/test")

        self.assertIs(result, mock_response)
        mock_get.assert_called_once()
        mock_sleep.assert_not_called()

    @patch("api_helper.time.sleep")
    @patch("api_helper.requests.get")
    def test_api_get_retries_on_timeout(self, mock_get, mock_sleep):
        """ReadTimeout twice then success — retries and returns response."""
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        mock_get.side_effect = [
            requests.exceptions.ReadTimeout("timeout 1"),
            requests.exceptions.ReadTimeout("timeout 2"),
            mock_response,
        ]

        result = api_helper.api_get("http://example.com/test", max_retries=3)

        self.assertIs(result, mock_response)
        self.assertEqual(mock_get.call_count, 3)
        # Should have slept twice (between the 3 attempts)
        self.assertEqual(mock_sleep.call_count, 2)

    @patch("api_helper.time.sleep")
    @patch("api_helper.requests.get")
    def test_api_get_fails_after_max_retries(self, mock_get, mock_sleep):
        """Always ReadTimeout — raises after max_retries attempts."""
        mock_get.side_effect = requests.exceptions.ReadTimeout("always timeout")

        with self.assertRaises(requests.exceptions.ReadTimeout):
            api_helper.api_get("http://example.com/test", max_retries=3)

        self.assertEqual(mock_get.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 3)

    @patch("api_helper.time.sleep")
    @patch("api_helper.requests.get")
    def test_api_get_retries_on_connection_error(self, mock_get, mock_sleep):
        """ConnectionError once then success — retries and returns."""
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        mock_get.side_effect = [
            requests.exceptions.ConnectionError("conn error"),
            mock_response,
        ]

        result = api_helper.api_get("http://example.com/test", max_retries=3)

        self.assertIs(result, mock_response)
        self.assertEqual(mock_get.call_count, 2)
        self.assertEqual(mock_sleep.call_count, 1)

    def test_constants_exported(self):
        """api_helper exports the expected constants."""
        self.assertEqual(api_helper.API_DELAY, 0.2)
        self.assertEqual(api_helper.API_TIMEOUT, 60)
        self.assertEqual(api_helper.API_MAX_RETRIES, 3)
        self.assertEqual(api_helper.BATCH_SIZE, 1000)
        self.assertIsNotNone(api_helper.logger)


if __name__ == "__main__":
    unittest.main()
