"""Unit tests for check_seed.py — seed delta flag checker.

Tests the manifest comparison logic: no changes, some changes, first run,
missing manifests, and hash mismatch detection.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import check_seed


def make_manifest(db_hash, version=1):
    """Build a minimal per-API manifest.json dict."""
    return {
        "version": version,
        "previousVersion": version - 1 if version > 1 else None,
        "schemaVersion": 1,
        "generatedAt": "2026-08-20T12:00:00.000000+00:00",
        "dbHash": db_hash,
        "dbSize": 1000,
        "patchHash": "abc123",
        "patchSize": 282,
    }


def make_seed_manifest(hashes):
    """Build a seed-manifest.json dict with perApiHashes."""
    return {
        "generatedAt": "2026-08-20T06:00:00.000000+00:00",
        "perApiHashes": hashes,
        "perApiVersions": {k: 1 for k in hashes},
    }


class TestCheckSeed(unittest.TestCase):
    """Test the seed check logic."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def _write_manifest(self, filename, manifest_dict):
        """Write a manifest dict to a temp file and return the path."""
        path = os.path.join(self.tmpdir, filename)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(manifest_dict, f)
        return path

    def _write_seed_manifest(self, hashes):
        """Write a seed-manifest.json with the given perApiHashes."""
        return self._write_manifest("seed-manifest.json", make_seed_manifest(hashes))

    def _write_api_manifests(self, hashes):
        """Write all 7 per-API manifests and return the paths dict."""
        paths = {}
        filenames = {
            "mps": "mps_manifest.json",
            "commons_votes": "commons_manifest.json",
            "lords_votes": "lords_manifest.json",
            "bills": "bills_manifest.json",
            "committees": "committees_manifest.json",
            "recess": "recess_manifest.json",
            "interests": "interests_manifest.json",
        }
        for api_name, filename in filenames.items():
            paths[api_name] = self._write_manifest(filename, make_manifest(hashes[api_name]))
        return paths

    def test_no_changes(self):
        """All hashes match seed-manifest — needs_update=false."""
        hashes = {
            "mps": "hash_mps_1",
            "commons_votes": "hash_cv_1",
            "lords_votes": "hash_lv_1",
            "bills": "hash_b_1",
            "committees": "hash_c_1",
            "recess": "hash_r_1",
            "interests": "hash_i_1",
        }
        seed_path = self._write_seed_manifest(hashes)
        api_paths = self._write_api_manifests(hashes)

        needs_update, changed = check_seed.check_seed(seed_path, api_paths)
        self.assertFalse(needs_update)
        self.assertEqual(changed, [])

    def test_one_changed(self):
        """One per-API hash differs — needs_update=true, only that API in changed."""
        seed_hashes = {
            "mps": "hash_mps_1",
            "commons_votes": "hash_cv_1",
            "lords_votes": "hash_lv_1",
            "bills": "hash_b_1",
            "committees": "hash_c_1",
            "recess": "hash_r_1",
            "interests": "hash_i_1",
        }
        current_hashes = dict(seed_hashes)
        current_hashes["bills"] = "hash_b_2"  # Bills changed

        seed_path = self._write_seed_manifest(seed_hashes)
        api_paths = self._write_api_manifests(current_hashes)

        needs_update, changed = check_seed.check_seed(seed_path, api_paths)
        self.assertTrue(needs_update)
        self.assertEqual(changed, ["bills"])

    def test_multiple_changed(self):
        """Multiple per-API hashes differ — all changed APIs listed."""
        seed_hashes = {
            "mps": "hash_mps_1",
            "commons_votes": "hash_cv_1",
            "lords_votes": "hash_lv_1",
            "bills": "hash_b_1",
            "committees": "hash_c_1",
            "recess": "hash_r_1",
            "interests": "hash_i_1",
        }
        current_hashes = dict(seed_hashes)
        current_hashes["mps"] = "hash_mps_2"
        current_hashes["interests"] = "hash_i_2"

        seed_path = self._write_seed_manifest(seed_hashes)
        api_paths = self._write_api_manifests(current_hashes)

        needs_update, changed = check_seed.check_seed(seed_path, api_paths)
        self.assertTrue(needs_update)
        self.assertIn("mps", changed)
        self.assertIn("interests", changed)
        self.assertEqual(len(changed), 2)

    def test_first_run_no_seed_manifest(self):
        """No seed-manifest.json — first run, needs_update=true, all APIs changed."""
        api_paths = self._write_api_manifests({
            "mps": "hash_mps_1",
            "commons_votes": "hash_cv_1",
            "lords_votes": "hash_lv_1",
            "bills": "hash_b_1",
            "committees": "hash_c_1",
            "recess": "hash_r_1",
            "interests": "hash_i_1",
        })

        needs_update, changed = check_seed.check_seed(None, api_paths)
        self.assertTrue(needs_update)
        self.assertEqual(len(changed), 7)

    def test_seed_manifest_no_per_api_hashes(self):
        """seed-manifest.json exists but has no perApiHashes — needs_update=true."""
        seed_path = self._write_manifest("seed-manifest.json", {
            "generatedAt": "2026-08-20T06:00:00.000000+00:00",
        })
        api_paths = self._write_api_manifests({
            "mps": "hash_mps_1",
            "commons_votes": "hash_cv_1",
            "lords_votes": "hash_lv_1",
            "bills": "hash_b_1",
            "committees": "hash_c_1",
            "recess": "hash_r_1",
            "interests": "hash_i_1",
        })

        needs_update, changed = check_seed.check_seed(seed_path, api_paths)
        self.assertTrue(needs_update)
        self.assertEqual(len(changed), 7)

    def test_missing_api_manifest(self):
        """One per-API manifest is missing — that API is flagged as changed."""
        hashes = {
            "mps": "hash_mps_1",
            "commons_votes": "hash_cv_1",
            "lords_votes": "hash_lv_1",
            "bills": "hash_b_1",
            "committees": "hash_c_1",
            "recess": "hash_r_1",
            "interests": "hash_i_1",
        }
        seed_path = self._write_seed_manifest(hashes)
        api_paths = self._write_api_manifests(hashes)
        # Remove one manifest
        os.remove(api_paths["committees"])
        api_paths["committees"] = os.path.join(self.tmpdir, "nonexistent.json")

        needs_update, changed = check_seed.check_seed(seed_path, api_paths)
        self.assertTrue(needs_update)
        self.assertIn("committees", changed)

    def test_all_changed(self):
        """All 7 hashes differ — all 7 APIs in changed list."""
        seed_hashes = {f"api_{i}": f"old_{i}" for i in range(7)}
        # Use real API names
        api_names = ["mps", "commons_votes", "lords_votes", "bills",
                     "committees", "recess", "interests"]
        seed_hashes = {name: f"old_{name}" for name in api_names}
        current_hashes = {name: f"new_{name}" for name in api_names}

        seed_path = self._write_seed_manifest(seed_hashes)
        api_paths = self._write_api_manifests(current_hashes)

        needs_update, changed = check_seed.check_seed(seed_path, api_paths)
        self.assertTrue(needs_update)
        self.assertEqual(len(changed), 7)


class TestGenerateSeedManifest(unittest.TestCase):
    """Test the seed-manifest generator."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def _write_manifest(self, filename, db_hash, version=1):
        path = os.path.join(self.tmpdir, filename)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(make_manifest(db_hash, version), f)
        return path

    def test_generate_with_all_manifests(self):
        """Generate seed-manifest from 7 per-API manifests."""
        import generate_seed_manifest

        api_paths = {
            "mps": self._write_manifest("mps.json", "hash_mps", 3),
            "commons_votes": self._write_manifest("cv.json", "hash_cv", 5),
            "lords_votes": self._write_manifest("lv.json", "hash_lv", 2),
            "bills": self._write_manifest("b.json", "hash_b", 4),
            "committees": self._write_manifest("c.json", "hash_c", 2),
            "recess": self._write_manifest("r.json", "hash_r", 1),
            "interests": self._write_manifest("i.json", "hash_i", 1),
        }

        result = generate_seed_manifest.generate_seed_manifest(api_paths)

        self.assertEqual(result["perApiHashes"]["mps"], "hash_mps")
        self.assertEqual(result["perApiHashes"]["commons_votes"], "hash_cv")
        self.assertEqual(result["perApiVersions"]["mps"], 3)
        self.assertEqual(result["perApiVersions"]["commons_votes"], 5)
        self.assertIn("generatedAt", result)

    def test_generate_with_missing_manifest(self):
        """Missing manifest results in null hash."""
        import generate_seed_manifest

        api_paths = {
            "mps": self._write_manifest("mps.json", "hash_mps", 1),
            "commons_votes": os.path.join(self.tmpdir, "nonexistent.json"),
            "lords_votes": self._write_manifest("lv.json", "hash_lv", 1),
            "bills": self._write_manifest("b.json", "hash_b", 1),
            "committees": self._write_manifest("c.json", "hash_c", 1),
            "recess": self._write_manifest("r.json", "hash_r", 1),
            "interests": self._write_manifest("i.json", "hash_i", 1),
        }

        result = generate_seed_manifest.generate_seed_manifest(api_paths)

        self.assertEqual(result["perApiHashes"]["mps"], "hash_mps")
        self.assertIsNone(result["perApiHashes"]["commons_votes"])


if __name__ == "__main__":
    unittest.main()
