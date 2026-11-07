# SPDX-License-Identifier: MIT
"""Offline tests for tools/build_themes.py: run with python3 -m unittest discover tests"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import build_themes as bt  # noqa: E402

BATOCERA = {
    "data": [
        {"theme": "Alekfull-NX", "theme_url": "https://github.com/fagnerpc/es-theme-alekfull-nx", "author": "fagnerpc",
         "last_update": "2024-01-01", "up_to_date": 1, "size": 30, "screenshot": "themes/alekfull-nx.jpg"},
        {"theme": "Carbon", "theme_url": "https://github.com/fabricecaruso/es-theme-carbon/", "author": "fabricecaruso",
         "screenshot": "https://cdn.example.org/carbon.png"},
        {"theme": "Carbon", "theme_url": "https://github.com/someone/duplicate"},
        {"theme": "Elsewhere", "theme_url": "https://gitlab.com/someone/es-theme-elsewhere"},
        {"theme": "NoShot", "theme_url": "https://github.com/someone/es-theme-noshot"},
    ]
}


class ImportTest(unittest.TestCase):
    def test_import(self):
        fetched = []

        def get(url):
            fetched.append(url)
            if "carbon" in url:
                raise bt.SourceError("gone")
            return b"jpeg"

        with tempfile.TemporaryDirectory() as tmp:
            shots = Path(tmp) / "screenshots"
            themes = bt.import_batocera(BATOCERA, "https://updates.batocera.org/themes.json", get, shots, log=lambda _: None)

            self.assertEqual([t["name"] for t in themes], ["Alekfull-NX", "Carbon", "NoShot"])
            self.assertEqual(themes[0], {"name": "Alekfull-NX", "url": "https://github.com/fagnerpc/es-theme-alekfull-nx",
                                         "author": "fagnerpc", "screenshot": "screenshots/alekfull-nx.jpg"})
            self.assertEqual((shots / "alekfull-nx.jpg").read_bytes(), b"jpeg")
            self.assertEqual(themes[1]["url"], "https://github.com/fabricecaruso/es-theme-carbon")
            self.assertNotIn("screenshot", themes[1])   # its screenshot could not be fetched
            self.assertIn("https://updates.batocera.org/themes/alekfull-nx.jpg", fetched)
            self.assertEqual(bt.validate_themes(themes, Path(tmp)), [])


class BuildTest(unittest.TestCase):
    THEMES = [
        {"name": "A", "url": "https://github.com/o/a", "author": "o", "screenshot": "screenshots/a.jpg"},
        {"name": "B", "url": "https://github.com/o/b"},
    ]

    def test_build(self):
        def info(url):
            if url.endswith("/b"):
                raise bt.SourceError("rate limited")
            return {"last_update": "2026-11-01", "size": 12}

        previous = {"B": {"theme": "B", "last_update": "2025-01-01", "size": 3}}
        listing = bt.build_themes(self.THEMES, previous, info, log=lambda _: None)

        self.assertEqual(listing["data"][0], {"theme": "A", "theme_url": "https://github.com/o/a", "author": "o",
                                              "last_update": "2026-11-01", "up_to_date": 1, "size": 12,
                                              "screenshot": "screenshots/a.jpg"})
        self.assertEqual(listing["data"][1]["last_update"], "2025-01-01")   # kept from the previous run
        self.assertEqual(listing["data"][1]["size"], 3)
        self.assertEqual(bt.validate_output(listing), [])

    def test_repo_info(self):
        def get(url):
            self.assertEqual(url, "https://api.github.com/repos/o/a")
            return json.dumps({"pushed_at": "2026-10-31T12:00:00Z", "size": 2049}).encode()

        self.assertEqual(bt.github_repo_info("https://github.com/o/a", get), {"last_update": "2026-10-31", "size": 3})
        with self.assertRaises(bt.SourceError):
            bt.github_repo_info("https://gitlab.com/o/a", get)

    def test_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            errors = bt.validate_themes([
                {"name": "A", "url": "https://github.com/o/a"},
                {"name": "A", "url": "https://github.com/o/a2"},
                {"name": "C", "url": "https://gitlab.com/o/c"},
                {"name": "D", "url": "https://github.com/o/d", "screenshot": "screenshots/missing.png", "colour": "red"},
            ], Path(tmp))
            self.assertEqual(len(errors), 4)


if __name__ == "__main__":
    unittest.main()
