# EmulationStation-RE catalog

The list of emulators and libretro cores that [EmulationStation-RE](https://github.com/JyuHo/emulationstation-re)
can install from its **Emulator catalog** menu.

ES downloads a single file:

    https://raw.githubusercontent.com/JyuHo/es-re-catalog/main/catalog.json

## How it works

* [`sources.yaml`](sources.yaml) is written by hand: for each emulator, where its builds come
  from (a GitHub or Forgejo release asset, a fixed URL, or a Flathub app), per architecture, in
  order of preference; for each core, its libretro name.
* [`tools/build_catalog.py`](tools/build_catalog.py) resolves those sources into `catalog.json`:
  exact URL, version, size and sha256 of each build, and the date and CRC of each core on the
  libretro buildbot. A GitHub Action runs it every day and commits the result, so ES never queries
  the GitHub API (60 requests an hour without an account) and always checks what it downloads.
* A source that fails to resolve keeps the build it had in the previous `catalog.json`.

## catalog.json, format 1

```jsonc
{
  "format": 1,
  "generated": "2026-11-06T04:17:00Z",
  "standalone": [
    {
      "id": "duckstation",                 // emulator name used by es-launcher and es_systems.cfg
      "name": "DuckStation",
      "description": "PlayStation emulator.",
      "systems": ["psx"],
      "homepage": "https://www.duckstation.org",
      "binaries": ["duckstation-qt"],      // installed by the system if one of these is in PATH
      "logo": "https://…/icon.png",        // https URL, or a path relative to catalog.json
      "builds": {                          // per architecture (uname -m), most preferred first
        "x86_64": [
          { "type": "appimage", "url": "https://…", "version": "latest-2026-10-13",
            "sha256": "…", "size": 52428800 },
          { "type": "archive", "url": "https://….zip", "version": "1.1",
            "sha256": "…", "size": 1234, "exec": "\\.AppImage$" },   // regex of the program inside
          { "type": "flatpak", "ref": "org.example.App", "remote": "flathub" }
        ]
      }
    }
  ],
  "cores": {
    "info": "https://buildbot.libretro.com/assets/frontend/info.zip",
    "items": [
      { "id": "snes9x", "name": "Snes9x", "systems": ["snes"],
        "builds": { "x86_64": { "url": "https://buildbot.libretro.com/nightly/linux/x86_64/latest/snes9x_libretro.so.zip",
                                "version": "2026-11-05", "crc32": "deadbeef" } } }
    ]
  }
}
```

An architecture that has no build is simply absent: ES does not show the entry on that machine.
A future incompatible change bumps `format`; ES ignores catalogs whose format it does not know.

## Themes

ES's theme downloader reads a second file:

    https://raw.githubusercontent.com/JyuHo/es-re-catalog/main/themes.json

* [`themes.yaml`](themes.yaml) is the list, by hand: `name`, `url` (a GitHub repository, which
  ES downloads as `<url>/archive/<default branch>.zip`), `author`, and `screenshot` (a file of
  [`screenshots/`](screenshots) or an https URL).
* [`tools/build_themes.py`](tools/build_themes.py) adds the date of the last push and the size of
  each repository and writes `themes.json`, in the format of Batocera's theme list
  (`{"data": [{"theme", "theme_url", "author", "last_update", "up_to_date", "size", "screenshot"}]}`).
  The daily `catalog` workflow runs it too.
* The list was seeded from Batocera's: the `import Batocera themes` workflow (run by hand)
  rewrites `themes.yaml` and `screenshots/` from <https://updates.batocera.org/themes.json>,
  keeping the themes hosted on GitHub.

To add a theme, add it to `themes.yaml` (and its screenshot to `screenshots/`), then run
`python3 tools/build_themes.py --check`.

## Where ES installs things

Everything goes in the user's data directory, without root:

| What | Where |
| --- | --- |
| AppImage and archive emulators | `$XDG_DATA_HOME/emulationstation/emulators/<id>/` |
| Flatpak emulators | `flatpak install --user` |
| Cores and their `.info` files | `$XDG_DATA_HOME/emulationstation/cores/` |
| What is installed, which version | `$XDG_DATA_HOME/emulationstation/emulators/installed.json` |

## Adding an emulator or a core

1. Add it to `sources.yaml` (the comments at the top list every field).
2. Run `python3 tools/build_catalog.py --check` and `python3 -m unittest discover tests`.
3. Open a pull request. Once merged, the next daily run (or a manual run of the `catalog`
   workflow) publishes it.

To use your own logo instead of the Flathub icon, put a PNG in `logos/` and set
`logo: logos/<id>.png`.

## License

The tools and the catalog are under the MIT license. Logos belong to their projects.
