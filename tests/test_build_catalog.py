# SPDX-License-Identifier: MIT
"""Offline tests for tools/build_catalog.py: run with python3 -m unittest discover tests"""

import datetime as dt
import json
import sys
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import build_catalog as bc  # noqa: E402

GH_RELEASES = [
    {"tag_name": "v2.1-rc1", "prerelease": True, "draft": False,
     "assets": [{"name": "emu-2.1-rc1-x86_64.AppImage", "browser_download_url": "https://example.org/rc.AppImage",
                 "updated_at": "2026-11-01T00:00:00Z"}]},
    {"tag_name": "v2.0", "prerelease": False, "draft": False,
     "assets": [{"name": "emu-2.0-x86_64.AppImage", "browser_download_url": "https://example.org/2.0.AppImage",
                 "updated_at": "2026-10-01T00:00:00Z"},
                {"name": "emu-2.0-aarch64.zip", "browser_download_url": "https://example.org/2.0.zip",
                 "updated_at": "2026-10-01T00:00:00Z"}]},
]
INDEX = "2026-11-05 deadbeef snes9x_libretro.so.zip\n2026-11-04 cafebabe gambatte_libretro.so.zip\n"
SOURCES = yaml.safe_load("""
standalone:
  - id: emu
    name: Emu
    systems: [psx]
    builds:
      x86_64:
        - { type: appimage, repo: o/emu, asset: 'x86_64\\.AppImage$' }
        - { type: flatpak, ref: org.example.Emu }
      aarch64:
        - { type: archive, repo: o/emu, asset: 'aarch64\\.zip$', exec: '\\.AppImage$' }
cores:
  - { id: snes9x, name: Snes9x, systems: [snes] }
  - { id: gambatte, name: Gambatte, systems: [gb], arch: [x86_64] }
  - { id: missing, name: Missing, systems: [gb], arch: [x86_64] }
""")


class FakeNet:
    def __init__(self):
        self.hashed = []

    def get(self, url, accept=None):
        if url.startswith("https://api.github.com/repos/o/emu/releases"):
            return json.dumps(GH_RELEASES).encode()
        if url.endswith(".index-extended"):
            return INDEX.encode()
        raise bc.SourceError(f"unexpected {url}")

    def hasher(self, url):
        self.hashed.append(url)
        return "a" * 64, 1234


def build(previous=None):
    net = FakeNet()
    resolver = bc.Resolver(previous, get=net.get, hasher=net.hasher)
    catalog = bc.build_catalog(SOURCES, resolver, now=dt.datetime(2026, 11, 6, tzinfo=dt.timezone.utc))
    return catalog, resolver, net


class BuildCatalogTests(unittest.TestCase):
    def test_sources_of_the_repository_are_valid(self):
        sources = yaml.safe_load((bc.ROOT / "sources.yaml").read_text())
        self.assertEqual(bc.validate_sources(sources), [])

    def test_stable_release_is_preferred_and_hashed(self):
        catalog, resolver, net = build()
        emu = catalog["standalone"][0]
        appimage = emu["builds"]["x86_64"][0]
        self.assertEqual(appimage["url"], "https://example.org/2.0.AppImage")
        self.assertEqual(appimage["version"], "v2.0")
        self.assertEqual(appimage["sha256"], "a" * 64)
        self.assertEqual(emu["builds"]["x86_64"][1], {"type": "flatpak", "ref": "org.example.Emu", "remote": "flathub"})
        self.assertEqual(emu["builds"]["aarch64"][0]["exec"], "\\.AppImage$")
        self.assertEqual(bc.validate_catalog(catalog), [])

    def test_flathub_icon_is_the_default_logo(self):
        catalog, _, _ = build()
        self.assertEqual(catalog["standalone"][0]["logo"],
                         "https://dl.flathub.org/repo/appstream/x86_64/icons/128x128/org.example.Emu.png")

    def test_unchanged_build_is_not_downloaded_again(self):
        first, _, _ = build()
        _, _, net = build(previous=first)
        self.assertEqual(net.hashed, [])

    def test_cores_come_from_the_buildbot_index(self):
        catalog, resolver, _ = build()
        cores = {c["id"]: c for c in catalog["cores"]["items"]}
        self.assertEqual(cores["snes9x"]["builds"]["x86_64"]["version"], "2026-11-05")
        self.assertEqual(set(cores["snes9x"]["builds"]), {"x86_64", "aarch64"})
        self.assertEqual(set(cores["gambatte"]["builds"]), {"x86_64"})
        self.assertEqual(cores["missing"]["builds"], {})
        self.assertTrue(any("missing" in e for e in resolver.errors))

    def test_failed_source_keeps_the_previous_build(self):
        previous, _, _ = build()
        net = FakeNet()
        def broken(url, accept=None):
            if "api.github.com" in url:
                raise bc.SourceError("rate limited")
            return net.get(url, accept)
        resolver = bc.Resolver(previous, get=broken, hasher=net.hasher)
        catalog = bc.build_catalog(SOURCES, resolver)
        self.assertEqual(catalog["standalone"][0]["builds"], previous["standalone"][0]["builds"])
        self.assertTrue(resolver.errors)

    def test_stable_release_beyond_the_first_page_is_found(self):
        nightlies = [{"tag_name": f"v2.9.{i}", "prerelease": True, "draft": False, "assets": []} for i in range(20)]
        stable = GH_RELEASES[1]
        def get(url, accept=None):
            if url.endswith("/releases/latest"):
                return json.dumps(stable).encode()
            if "/releases?" in url:
                return json.dumps(nightlies).encode()
            return INDEX.encode()
        resolver = bc.Resolver(None, get=get, hasher=FakeNet().hasher)
        catalog = bc.build_catalog(SOURCES, resolver)
        self.assertEqual(catalog["standalone"][0]["builds"]["x86_64"][0]["version"], "v2.0")

    def test_bad_sources_are_reported(self):
        problems = bc.validate_sources({"standalone": [
            {"id": "Bad Id", "name": "x", "systems": [],
             "builds": {"riscv": [{"type": "archive", "repo": "o/r", "asset": "("}]}}]})
        self.assertTrue(any("bad id" in p for p in problems))
        self.assertTrue(any("unknown arch" in p for p in problems))
        self.assertTrue(any("regex" in p for p in problems))
        self.assertTrue(any("exec" in p for p in problems))


if __name__ == "__main__":
    unittest.main()
