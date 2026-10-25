# Good Launcher — a launcher for GOG

A [GOG](https://www.gog.com) game launcher for Linux and SteamOS, focused on making sure
**cloud saves always sync**, including in Steam Deck's Game Mode.

> **Disclaimer:** independent, unofficial project. It is not affiliated with, endorsed by or
> sponsored by GOG sp. z o.o. or CD PROJEKT. "GOG" and "GOG GALAXY" are trademarks of their
> respective owners. gogl uses undocumented GOG APIs that may change without notice.

## Features

- **GOG library** with cover art, search and filters.
- **Built-in installer** (no dependency on other launchers): parallel downloads, MD5
  verification, resume after interruptions, updates that only download what changed, plus
  verification and repair. Supports Windows games (Content System v1 and v2) and native
  Linux installers.
- **GOG dependencies**: DOSBox, ScummVM, etc. ship with the game; Visual C++, DirectX…
  are installed into the prefix before the first launch, along with each game's install
  scripts.
- **Managed Proton-GE**: downloads its own versions (verified with SHA-512) into its own
  data folder; it never uses or touches your Steam installation.
- **Reliable save sync**:
  - compares by content (MD5) using a three-way history, not by timestamps;
  - never empties the cloud because a local folder is empty (e.g. a freshly created prefix);
  - makes a local backup before overwriting or deleting anything;
  - a *watcher* (a transient systemd unit) uploads saves even if Steam closes the launcher
    when the game exits, and a timer retries pending uploads.
- **Account-wide cloud saves**: space used per game against your quota, and deletion of old
  saves.
- **Per-game settings**: Proton-GE version, language, save folders, arguments, environment
  variables and a launch prefix command (MangoHud, GameMode…).
- **Extras and offline installers**: manuals, soundtracks, wallpapers… and the full
  installers of each game (and its DLC) to keep for yourself, with resume and chunk-level
  verification. They are stored in your system's Downloads folder under `GOG/<game>`
  (`~/Downloads/GOG/<game>` by default; configurable in Settings).
- **Achievements**: on each game's page, the unlocked ones (with date) and the pending ones
  (with their rarity). For games that ship the Galaxy SDK, they unlock as you play, with an
  on-screen notification that also works in Game Mode. This uses
  [Comet](https://github.com/imLinguin/comet) (Apache-2.0), downloaded on first use with a
  pinned SHA-256 checksum. Offline, achievements are stored and uploaded next time.
- **GOG store** inside the launcher: purchases go through GOG's own checkout (the launcher
  never handles payment data); it warns you if you already own a game, and after buying, the
  game appears in your library without restarting.
- **Profile**: your account, unlocked achievements, 100 %-completed games and your wishlist
  with prices and discounts.
- **Steam integration**: adds games (and the launcher itself) as "non-Steam games" with their
  cover art.
- **Gamepad control** (D-pad/stick, A, B, LB/RB…), designed for SteamOS Game Mode. It also
  works inside the store pages (links and buttons), and there you can drag with a finger to scroll.
- **English and Spanish**: follows the system language (any other language falls back to
  English) and can be set in Settings → Language. The language games are installed in also
  comes from the system, unless you pick another one.

## Installation

### AppImage (recommended, also on SteamOS)

A single self-contained file (Python and Qt included): it installs nothing on your system,
so it works on SteamOS even though its system is read-only.

```sh
chmod +x gogl-*-x86_64.AppImage
./gogl-*-x86_64.AppImage            # user interface
./gogl-*-x86_64.AppImage run <id>   # any `gogl` command
```

It can live in any folder and be moved whenever you like. Steam, the application menu and
background uploads don't call it by its path; they call a small launcher
(`~/.local/share/gogl/gogl-launch`) that the AppImage updates every time it is opened. If you
move it and start a game from Steam before opening it again, the launcher looks for it in
your home folder and on SD cards and external drives (as long as its name contains "gogl").

To build it yourself: `packaging/build-appimage.sh` (downloads Python, PySide6 and
appimagetool with pinned versions and SHA-256 checksums; the result goes to `dist/`).

### From source

Requires Python 3.11 or later and, for the interface, PySide6 with QtWebEngine.

```sh
git clone https://github.com/jotayepes/good-launcher
cd good-launcher
python3 -m venv .venv
.venv/bin/pip install -e '.[ui]'
.venv/bin/gogl-ui
```

From **Settings** you can add Good Launcher to the application menu and to Steam (so you can
open it from Game Mode).

## Command line

Everything in the interface is also available through the `gogl` command:

```sh
gogl login                 # sign in to GOG
gogl install <id>          # install a game from your library
gogl run <id>              # sync, play and sync again
gogl sync <id>             # sync saves manually
gogl update <id>           # update to the latest version (only downloads changes)
gogl verify <id> --repair  # check file integrity and re-download damaged files
gogl uninstall <id>        # uninstall (cloud saves are left untouched)
gogl proton install        # download the latest Proton-GE version
gogl steam add <id>        # add a game to Steam
gogl achievements <id>     # unlocked and pending achievements
gogl offline list <id>     # extras and offline installers
gogl offline download <id> --extras   # download all extras
gogl service install       # timer that retries pending uploads every 15 minutes
gogl --help                # the remaining commands
```

## Where things are stored

| What | Where |
| --- | --- |
| Session and settings | `~/.config/gogl/` |
| Database, Proton-GE, Comet, prefixes, backups, store session | `~/.local/share/gogl/` |
| Extras and offline installers (default) | `~/Downloads/GOG/` (the system Downloads folder) |
| Per-game logs | `~/.local/state/gogl/logs/` |
| Store/web timing log (`web.log`) | `~/.local/state/gogl/` |
| Games (default) | `~/Games/gogl/` |

The `~/.config`, `~/.local/share` and `~/.local/state` locations follow the `XDG_CONFIG_HOME`,
`XDG_DATA_HOME` and `XDG_STATE_HOME` variables if they are set.

## Development

```sh
.venv/bin/pip install -e '.[ui,dev]'
.venv/bin/python -m pytest
```

## Translations

The source strings in the code are written in Spanish and marked with `_("…")`; translations
are gettext catalogs in [`gogl/locale/`](gogl/locale/) that are read as-is (no compilation).
English is provided in `en.po`.

- To **add a language**, copy `gogl/locale/gogl.pot` to `gogl/locale/<code>.po` (e.g.
  `fr.po`), put its name in the `X-Language-Name` header and translate the `msgstr` entries
  (with Poedit or any editor). It shows up in Settings automatically and is used when the
  system is set to that language.
- After changing strings in the code, `packaging/update_po.py` updates the template and the
  catalogs; the tests fail if any entry is left untranslated.

## License

[GNU General Public License v3.0 or later](LICENSE).
