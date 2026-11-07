#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build themes.json from themes.yaml.

themes.yaml lists the themes ES offers in its theme downloader: name, GitHub repository,
author, screenshot. This script adds what changes on its own (date of the last push, size of
the repository) and writes themes.json, the file ES downloads, in the format Batocera's
theme list uses, so ES reads it as it always did.

  python3 tools/build_themes.py                     refresh themes.json from GitHub
  python3 tools/build_themes.py --check             validate themes.yaml and themes.json, no network
  python3 tools/build_themes.py --import-batocera   seed themes.yaml and screenshots/ from Batocera's list

A theme whose repository cannot be reached keeps its date and size from the previous themes.json.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

import yaml

ROOT = Path(__file__).resolve().parent.parent
BATOCERA_THEMES = "https://updates.batocera.org/themes.json"
GITHUB_REPO_RE = re.compile(r"^https://github\.com/([^/\s]+)/([^/\s]+?)/?$")
USER_AGENT = "es-re-catalog-builder"
SCREENSHOTS = "screenshots"


class SourceError(Exception):
    pass


# --- network -------------------------------------------------------------------------------

def http_get(url: str) -> bytes:
    headers = {"User-Agent": USER_AGENT}
    token = os.environ.get("GITHUB_TOKEN")
    if token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read()
    except urllib.error.URLError as e:
        raise SourceError(f"{url}: {e}") from e


def github_repo_info(url: str, get: Callable[[str], bytes] = http_get) -> dict[str, Any]:
    """last_update (YYYY-MM-DD of the last push) and size (MB, rounded up) of a GitHub repository."""
    match = GITHUB_REPO_RE.match(url)
    if not match:
        raise SourceError(f"{url}: not a GitHub repository URL")
    repo = json.loads(get(f"https://api.github.com/repos/{match.group(1)}/{match.group(2)}"))
    return {
        "last_update": str(repo.get("pushed_at", ""))[:10],
        "size": max(1, math.ceil(int(repo.get("size", 0)) / 1024)),   # GitHub gives KB
    }


# --- themes.json ---------------------------------------------------------------------------

def build_themes(themes: list[dict[str, Any]], previous: dict[str, dict[str, Any]],
                 info: Callable[[str], dict[str, Any]], log: Callable[[str], None] = print) -> dict[str, Any]:
    data = []
    for theme in themes:
        entry = {
            "theme": theme["name"],
            "theme_url": theme["url"].rstrip("/"),
            "author": theme.get("author", ""),
            "last_update": "",
            "up_to_date": 1,
            "size": 0,
        }
        if theme.get("screenshot"):
            entry["screenshot"] = theme["screenshot"]

        try:
            entry.update(info(entry["theme_url"]))
            # The download is the default branch's archive: a size given by hand (MB) beats
            # GitHub's, which counts the whole history
            if theme.get("size"):
                entry["size"] = int(theme["size"])
        except (SourceError, ValueError, KeyError) as e:
            old = previous.get(theme["name"])
            log(f"warning: {theme['name']}: {e}" + ("; keeping the previous date and size" if old else ""))
            if old:
                entry["last_update"] = old.get("last_update", "")
                entry["size"] = old.get("size", 0)
            if theme.get("size"):
                entry["size"] = int(theme["size"])
        data.append(entry)

    return {"data": data}


def previous_entries(path: Path) -> dict[str, dict[str, Any]]:
    try:
        return {item["theme"]: item for item in json.loads(path.read_text())["data"]}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


# --- import from Batocera ------------------------------------------------------------------

def import_batocera(listing: dict[str, Any], base_url: str, get: Callable[[str], bytes],
                    screenshots_dir: Path, log: Callable[[str], None] = print) -> list[dict[str, Any]]:
    """themes.yaml entries from Batocera's themes.json; screenshots are copied into screenshots_dir."""
    themes = []
    seen = set()
    for item in listing.get("data", []):
        name = str(item.get("theme", "")).strip()
        url = str(item.get("theme_url", "")).strip().rstrip("/")
        if not name or not url or name in seen:
            continue
        if not GITHUB_REPO_RE.match(url):
            log(f"warning: {name}: {url} is not a GitHub repository, skipped (ES downloads <url>/archive/<branch>.zip)")
            continue
        seen.add(name)

        theme: dict[str, Any] = {"name": name, "url": url}
        if item.get("author"):
            theme["author"] = str(item["author"])
        size = str(item.get("size", "")).strip()
        if size.isdigit() and int(size) > 0:
            theme["size"] = int(size)

        screenshot = str(item.get("screenshot", "")).strip()
        if screenshot:
            source = urllib.parse.urljoin(base_url, screenshot)
            target = screenshots_dir / Path(urllib.parse.urlparse(source).path).name
            try:
                content = get(source)
                screenshots_dir.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
                theme["screenshot"] = f"{SCREENSHOTS}/{target.name}"
            except SourceError as e:
                log(f"warning: {name}: screenshot: {e}")

        themes.append(theme)
    return themes


# --- validation ----------------------------------------------------------------------------

def validate_themes(themes: Any, root: Path = ROOT) -> list[str]:
    if not isinstance(themes, list):
        return ["themes.yaml: expected a list of themes"]

    errors = []
    names = set()
    for i, theme in enumerate(themes):
        where = f"themes.yaml[{i}]"
        if not isinstance(theme, dict):
            errors.append(f"{where}: expected a mapping")
            continue
        name = theme.get("name")
        if not isinstance(name, str) or not name.strip():
            errors.append(f"{where}: name is missing")
        elif name in names:
            errors.append(f"{where}: {name} is listed twice")
        else:
            names.add(name)
        if not isinstance(theme.get("url"), str) or not GITHUB_REPO_RE.match(theme["url"]):
            errors.append(f"{where}: url must be https://github.com/<owner>/<repo>")
        screenshot = theme.get("screenshot")
        if screenshot is not None:
            if not isinstance(screenshot, str):
                errors.append(f"{where}: screenshot must be a path or an https URL")
            elif not screenshot.startswith("https://") and not (root / screenshot).is_file():
                errors.append(f"{where}: {screenshot} does not exist")
        if "size" in theme and (not isinstance(theme["size"], int) or theme["size"] < 0):
            errors.append(f"{where}: size must be a number of MB")
        unknown = set(theme) - {"name", "url", "author", "screenshot", "size"}
        if unknown:
            errors.append(f"{where}: unknown field(s) {', '.join(sorted(unknown))}")
    return errors


def validate_output(listing: Any) -> list[str]:
    if not isinstance(listing, dict) or not isinstance(listing.get("data"), list):
        return ["themes.json: expected {\"data\": [...]}"]
    errors = []
    for i, item in enumerate(listing["data"]):
        for field in ("theme", "theme_url"):
            if not isinstance(item, dict) or not item.get(field):
                errors.append(f"themes.json data[{i}]: {field} is missing")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="validate only, no network")
    group.add_argument("--import-batocera", action="store_true", help="rewrite themes.yaml from Batocera's list")
    parser.add_argument("--themes", type=Path, default=ROOT / "themes.yaml")
    parser.add_argument("--output", type=Path, default=ROOT / "themes.json")
    args = parser.parse_args()

    if args.import_batocera:
        listing = json.loads(http_get(BATOCERA_THEMES))
        themes = import_batocera(listing, BATOCERA_THEMES, http_get, args.themes.parent / SCREENSHOTS)
        header = ("# Themes offered by EmulationStation-RE's theme downloader. tools/build_themes.py turns\n"
                  "# this list into themes.json. Fields: name, url (GitHub repository), author, screenshot\n"
                  "# (a path in this repository or an https URL), size (MB of the download, optional:\n"
                  "# GitHub's repository size otherwise). Seeded from Batocera's theme list.\n")
        args.themes.write_text(header + yaml.safe_dump(themes, allow_unicode=True, sort_keys=False, width=200))
        print(f"themes.yaml: {len(themes)} themes")
        return 0

    themes = yaml.safe_load(args.themes.read_text()) if args.themes.exists() else []
    themes = themes or []
    errors = validate_themes(themes, args.themes.parent)

    if args.check:
        if args.output.exists():
            errors += validate_output(json.loads(args.output.read_text()))
        for error in errors:
            print(error, file=sys.stderr)
        return 1 if errors else 0

    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1

    listing = build_themes(themes, previous_entries(args.output), github_repo_info)
    args.output.write_text(json.dumps(listing, indent=2, ensure_ascii=False) + "\n")
    print(f"themes.json: {len(listing['data'])} themes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
