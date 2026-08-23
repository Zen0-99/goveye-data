"""Unit tests for build_mp_tags.py — MP tag aggregation with recency weighting.

Tests:
- build_tags.py can be imported and TAG_DICTIONARY has 26 entries
- (Task 2 expands this file with full recency weighting tests)
"""

import os
import sys
import unittest

# Add parent directory to path so we can import build_tags
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import build_tags


class TestBuildTagsDictionary(unittest.TestCase):
    """Verify build_tags.py is importable and TAG_DICTIONARY is intact."""

    def test_tag_dictionary_has_26_entries(self):
        """TAG_DICTIONARY must have exactly 26 tags (unchanged — no new tags added)."""
        self.assertEqual(len(build_tags.TAG_DICTIONARY), 26)

    def test_build_publication_tags_exists(self):
        """build_publication_tags function must exist."""
        self.assertTrue(callable(getattr(build_tags, "build_publication_tags", None)))

    def test_build_statement_tags_exists(self):
        """build_statement_tags function must exist."""
        self.assertTrue(callable(getattr(build_tags, "build_statement_tags", None)))

    def test_build_legislation_tags_exists(self):
        """build_legislation_tags function must exist."""
        self.assertTrue(callable(getattr(build_tags, "build_legislation_tags", None)))

    def test_publication_tag_threshold_exists(self):
        """PUBLICATION_TAG_THRESHOLD constant must exist."""
        self.assertTrue(hasattr(build_tags, "PUBLICATION_TAG_THRESHOLD"))

    def test_statement_tag_threshold_exists(self):
        """STATEMENT_TAG_THRESHOLD constant must exist."""
        self.assertTrue(hasattr(build_tags, "STATEMENT_TAG_THRESHOLD"))

    def test_legislation_tag_threshold_exists(self):
        """LEGISLATION_TAG_THRESHOLD constant must exist."""
        self.assertTrue(hasattr(build_tags, "LEGISLATION_TAG_THRESHOLD"))


if __name__ == "__main__":
    unittest.main()
