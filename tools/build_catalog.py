#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build catalog.json from sources.yaml.

sources.yaml says where each emulator and core comes from. This script turns it into
catalog.json, the only file EmulationStation-RE downloads: every build gets its exact URL,
version, size and sha256, so ES never has to query the GitHub API itself.

  python3 tools/build_catalog.py               resolve everything, write catalog.json
  python3 tools/build_catalog.py --check       validate sources.yaml and catalog.json, no network

A source that fails to resolve keeps the build it had in the previous catalog.json, so one
broken upstream never removes an emulator from the catalog.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

import yaml

ROOT = Path(__file__).resolve().parent.parent
FORMAT = 1
ARCHES = ("x86_64", "aarch64")
BUILD_TYPES = ("appimage", "archive", "flatpak")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")

BUILDBOT = "https://buildbot.libretro.com/nightly/linux/{arch}/latest"
CORE_INFO_URL = "https://buildbot.libretro.com/assets/frontend/info.zip"
USER_AGENT = "es-re-catalog-builder"
# Flathub publishes a 128x128 icon for every app: the logo of last resort.
FLATHUB_ICON = "https://dl.flathub.org/repo/appstream/x86_64/icons/128x128/{ref}.png"


class SourceError(Exception):
    pass


# --- network -------------------------------------------------------------------------------

def http_get(url: str, accept: str | None = None) -> bytes:
    headers = {"User-Agent": USER_AGENT}
    if accept:
        headers["Accept"] = accept
    token = os.environ.get("GITHUB_TOKEN")
    if token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read()
    except urllib.error.URLError as e:
        raise SourceError(f"{url}: {e}") from e


def hash_url(url: str) -> tuple[str, int]:
    """sha256 and size of a remote file, streamed."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    digest = hashlib.sha256()
    size = 0
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            while chunk := resp.read(1 << 20):
                digest.update(chunk)
                size += len(chunk)
    except urllib.error.URLError as e:
        raise SourceError(f"{url}: {e}") from e
    return digest.hexdigest(), size


# --- resolving -----------------------------------------------------------------------------

class Resolver:
    """Turns source entries into catalog builds. Network calls go through the injectable
    functions so that the tests can run offline."""

    def __init__(self, previous: dict[str, Any] | None = None,
                 get: Callable[[str, str | None], bytes] = http_get,
                 hasher: Callable[[str], tuple[str, int]] = hash_url):
        self.get = get
        self.hasher = hasher
        self.errors: list[str] = []
        self._releases: dict[str, list[dict[str, Any]]] = {}
        self._indexes: dict[str, dict[str, tuple[str, str]]] = {}
        self._previous_builds: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._previous_cores: dict[tuple[str, str], dict[str, Any]] = {}
        if previous:
            for emu in previous.get("standalone", []):
                for arch, builds in emu.get("builds", {}).items():
                    self._previous_builds[(emu["id"], arch)] = builds
            for core in previous.get("cores", {}).get("items", []):
                for arch, build in core.get("builds", {}).items():
                    self._previous_cores[(core["id"], arch)] = build

    # GitHub releases

    def releases(self, repo: str, forge: str | None = None) -> list[dict[str, Any]]:
        """Recent releases of a GitHub repository, or of a Forgejo/Gitea one when `forge`
        is that server's base URL. Both APIs return the fields used here."""
        key = f"{forge or 'github'}:{repo}"
        if key not in self._releases:
            if forge:
                url = f"{forge.rstrip('/')}/api/v1/repos/{repo}/releases?limit=20"
            else:
                url = f"https://api.github.com/repos/{repo}/releases?per_page=20"
            self._releases[key] = json.loads(self.get(url, "application/json"))
        return self._releases[key]

    def latest_release(self, repo: str) -> dict[str, Any] | None:
        key = f"latest:{repo}"
        if key not in self._releases:
            try:
                data = json.loads(self.get(f"https://api.github.com/repos/{repo}/releases/latest", "application/json"))
                self._releases[key] = [data] if isinstance(data, dict) and "tag_name" in data else []
            except (SourceError, ValueError):
                self._releases[key] = []
        return self._releases[key][0] if self._releases[key] else None

    def resolve_github(self, emu_id: str, arch: str, src: dict[str, Any]) -> dict[str, Any]:
        pattern = re.compile(src["asset"])
        tag_filter = re.compile(src["tag"]) if "tag" in src else None
        candidates = self.releases(src["repo"], src.get("forge"))
        # Projects that publish a nightly prerelease per commit (PCSX2) push their stable
        # release out of the first page: ask GitHub for it directly.
        if not src.get("prerelease") and not src.get("forge"):
            latest = self.latest_release(src["repo"])
            if latest:
                candidates = [latest] + candidates
        for release in candidates:
            if release.get("draft"):
                continue
            if release.get("prerelease") and not src.get("prerelease", False):
                continue
            if tag_filter and not tag_filter.search(release["tag_name"]):
                continue
            for asset in release.get("assets", []):
                if not pattern.search(asset["name"]):
                    continue
                url = asset["browser_download_url"]
                # A rolling tag ("latest", "continuous") keeps its name, so the asset's
                # update time is what identifies the build.
                version = release["tag_name"]
                if src.get("rolling"):
                    stamp = asset.get("updated_at") or asset.get("created_at") or ""
                    version = f"{version}-{stamp[:10]}"
                return self.finish(emu_id, arch, src, url, version)
        raise SourceError(f"{src['repo']}: no release asset matches /{src['asset']}/")

    def finish(self, emu_id: str, arch: str, src: dict[str, Any], url: str, version: str) -> dict[str, Any]:
        build: dict[str, Any] = {"type": src["type"], "url": url, "version": version}
        if src["type"] == "archive":
            build["exec"] = src["exec"]
        # Hashing means downloading the whole file: reuse the previous hash when nothing changed.
        for old in self._previous_builds.get((emu_id, arch), []):
            if old.get("url") == url and old.get("version") == version and old.get("sha256"):
                build["sha256"], build["size"] = old["sha256"], old["size"]
                return build
        build["sha256"], build["size"] = self.hasher(url)
        return build

    def resolve_build(self, emu_id: str, arch: str, src: dict[str, Any]) -> dict[str, Any]:
        kind = src["type"]
        if kind == "flatpak":
            return {"type": "flatpak", "ref": src["ref"], "remote": src.get("remote", "flathub")}
        if "repo" in src:
            return self.resolve_github(emu_id, arch, src)
        return self.finish(emu_id, arch, src, src["url"], str(src["version"]))

    def resolve_emulator(self, emu: dict[str, Any]) -> dict[str, Any]:
        out = {k: emu[k] for k in ("id", "name", "systems") if k in emu}
        for k in ("description", "homepage", "binaries", "license"):
            if k in emu:
                out[k] = emu[k]
        builds: dict[str, list[dict[str, Any]]] = {}
        for arch in ARCHES:
            resolved = []
            for src in emu.get("builds", {}).get(arch, []):
                try:
                    resolved.append(self.resolve_build(emu["id"], arch, src))
                except (SourceError, KeyError, ValueError) as e:
                    self.errors.append(f"{emu['id']} ({arch}): {e}")
                    # Keep what the previous catalog had for this kind of build.
                    resolved.extend(b for b in self._previous_builds.get((emu["id"], arch), [])
                                    if b.get("type") == src["type"] and b not in resolved)
            if resolved:
                builds[arch] = resolved
        out["builds"] = builds
        logo = emulator_logo(emu)
        if logo:
            out["logo"] = logo
        return out

    # libretro buildbot

    def index(self, arch: str) -> dict[str, tuple[str, str]]:
        """file name -> (date, crc32) from the buildbot's .index-extended."""
        if arch not in self._indexes:
            text = self.get(BUILDBOT.format(arch=arch) + "/.index-extended", None).decode()
            entries = {}
            for line in text.splitlines():
                parts = line.split()
                if len(parts) == 3:
                    date, crc, name = parts
                    entries[name] = (date, crc)
            self._indexes[arch] = entries
        return self._indexes[arch]

    def resolve_core(self, core: dict[str, Any]) -> dict[str, Any]:
        out = {k: core[k] for k in ("id", "name", "systems") if k in core}
        if "description" in core:
            out["description"] = core["description"]
        file = f"{core.get('file', core['id'])}_libretro.so.zip"
        builds = {}
        for arch in core.get("arch", ARCHES):
            try:
                entry = self.index(arch).get(file)
                if entry is None:
                    raise SourceError(f"{file} is not on the buildbot")
                builds[arch] = {"url": f"{BUILDBOT.format(arch=arch)}/{file}",
                                "version": entry[0], "crc32": entry[1]}
            except SourceError as e:
                self.errors.append(f"core {core['id']} ({arch}): {e}")
                if (core["id"], arch) in self._previous_cores:
                    builds[arch] = self._previous_cores[(core["id"], arch)]
        out["builds"] = builds
        return out


def emulator_logo(emu: dict[str, Any]) -> str | None:
    """The logo ES shows next to the emulator: `logo` from sources.yaml (a file under
    logos/, relative to catalog.json, or an https URL), else the Flathub icon of its
    flatpak, if it has one."""
    if emu.get("logo"):
        return emu["logo"]
    for srcs in emu.get("builds", {}).values():
        for src in srcs:
            if src.get("type") == "flatpak" and src.get("remote", "flathub") == "flathub":
                return FLATHUB_ICON.format(ref=src["ref"])
    return None


def build_catalog(sources: dict[str, Any], resolver: Resolver,
                  now: dt.datetime | None = None) -> dict[str, Any]:
    now = now or dt.datetime.now(dt.timezone.utc)
    return {
        "format": FORMAT,
        "generated": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "standalone": [resolver.resolve_emulator(e) for e in sources.get("standalone", [])],
        "cores": {
            "info": CORE_INFO_URL,
            "items": [resolver.resolve_core(c) for c in sources.get("cores", [])],
        },
    }


# --- validation ----------------------------------------------------------------------------

def validate_sources(sources: dict[str, Any]) -> list[str]:
    problems = []
    seen: set[str] = set()
    for section in ("standalone", "cores"):
        for item in sources.get(section, []):
            ident = item.get("id", "?")
            if not ID_RE.match(str(ident)):
                problems.append(f"{section}: bad id {ident!r}")
            if (section, ident) in seen:
                problems.append(f"{section}: duplicate id {ident}")
            seen.add((section, ident))
            for key in ("name", "systems"):
                if key not in item:
                    problems.append(f"{ident}: missing {key}")
            logo = item.get("logo")
            if logo and not (logo.startswith("https://") or (ROOT / logo).is_file()):
                problems.append(f"{ident}: logo {logo} is neither an https URL nor a file of this repository")
            for arch in item.get("arch", []):
                if arch not in ARCHES:
                    problems.append(f"{ident}: unknown arch {arch}")
            for arch, srcs in item.get("builds", {}).items():
                if arch not in ARCHES:
                    problems.append(f"{ident}: unknown arch {arch}")
                for src in srcs:
                    kind = src.get("type")
                    if kind not in BUILD_TYPES:
                        problems.append(f"{ident} ({arch}): unknown type {kind!r}")
                    elif kind == "flatpak":
                        if "ref" not in src:
                            problems.append(f"{ident} ({arch}): flatpak needs ref")
                    elif "repo" in src:
                        if "asset" not in src:
                            problems.append(f"{ident} ({arch}): github source needs asset")
                        else:
                            try:
                                re.compile(src["asset"])
                            except re.error as e:
                                problems.append(f"{ident} ({arch}): bad asset regex: {e}")
                    elif "url" not in src or "version" not in src:
                        problems.append(f"{ident} ({arch}): needs repo+asset or url+version")
                    if kind == "archive" and "exec" not in src:
                        problems.append(f"{ident} ({arch}): archive needs exec")
    return problems


def validate_catalog(catalog: dict[str, Any]) -> list[str]:
    problems = []
    if catalog.get("format") != FORMAT:
        problems.append(f"format must be {FORMAT}")
    for emu in catalog.get("standalone", []):
        for arch, builds in emu.get("builds", {}).items():
            for b in builds:
                if b.get("type") == "flatpak":
                    if not b.get("ref"):
                        problems.append(f"{emu['id']} ({arch}): flatpak without ref")
                elif not (b.get("url", "").startswith("https://") and len(b.get("sha256", "")) == 64):
                    problems.append(f"{emu['id']} ({arch}): build without https url or sha256")
    for core in catalog.get("cores", {}).get("items", []):
        for arch, b in core.get("builds", {}).items():
            if not b.get("url", "").startswith("https://"):
                problems.append(f"core {core['id']} ({arch}): build without https url")
    return problems


# --- main ----------------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="validate only, no network")
    parser.add_argument("--sources", type=Path, default=ROOT / "sources.yaml")
    parser.add_argument("--output", type=Path, default=ROOT / "catalog.json")
    args = parser.parse_args()

    sources = yaml.safe_load(args.sources.read_text())
    problems = validate_sources(sources)
    previous = json.loads(args.output.read_text()) if args.output.exists() else None

    if args.check:
        if previous:
            problems += validate_catalog(previous)
        for p in problems:
            print(f"error: {p}", file=sys.stderr)
        return 1 if problems else 0

    if problems:
        for p in problems:
            print(f"error: {p}", file=sys.stderr)
        return 1

    resolver = Resolver(previous)
    catalog = build_catalog(sources, resolver)
    for e in resolver.errors:
        print(f"warning: {e}", file=sys.stderr)

    problems = validate_catalog(catalog)
    if problems:
        for p in problems:
            print(f"error: {p}", file=sys.stderr)
        return 1

    # Only rewrite the file when something other than the timestamp changed.
    if previous and {**previous, "generated": None} == {**catalog, "generated": None}:
        print("catalog.json is up to date")
        return 0
    args.output.write_text(json.dumps(catalog, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
