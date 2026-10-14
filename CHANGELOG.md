# Changelog

All notable changes to Idus GitHub Updater are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.2] – 2026-10-14

### Added
- Idus's own apps are default repositories at the top of the list: Idus GitHub Updater,
  Idus SC Toolbox, Idus Elgato Light and Idus HW-player (archive only — it runs on a Raspberry Pi).
- New format **Idus installer**: downloads the release's files, verifies them and runs the app's
  own `install-<app>.sh` as the user. The installed version is read from the app's `APP_VERSION`,
  so hand-installed copies are recognised. The updater can update itself (restart afterwards).
- Repositories that aren't on GitHub yet show "not on GitHub yet" instead of a warning on every check.

### Changed
- "Restore Craft Suite defaults" is now "Restore default repositories".

## [1.1] – 2026-10-14

### Changed
- Archive layout is now `<archive>/<App name>/<Linux|Windows|macOS>/<version>/` — no GitHub
  owner in the path, one folder per platform. v1.0 archives are moved automatically.
- Passwords: from the window and menu actions pacman runs through `sudo -A` with a graphical
  prompt (ksshaskpass), so several installs within sudo's memory window (~5 min) ask once.
  Falls back to pkexec (asks every time) without ksshaskpass, with sudo-rs or for non-sudoers.
- Archive sync no longer re-hashes archived files (size check); installs still verify SHA-256.

### Added
- Finds the same app installed another way: Flatpak (ArtCraft Launcher's way), pacman/AUR
  package or the owner of `/usr/bin/<app>`, loose AppImages, ArtCraft Launcher's sandbox —
  also next to an updater install ("⚠ two copies").
- ⋯ › Remove the other install… and `--remove-other REPO`.

### Fixed
- GTK warnings on KDE: "GtkGizmo (slider) reported min width -2" (Breeze scrollbars),
  "Use GtkSettings:gtk-interface-color-scheme instead" (GTK ≥ 4.20), and "Trying to measure
  GtkWindow …" when the window is shorter than its content (it now scrolls).

## [1.0] – 2026-10-14

First public release.

### Added
- Install and update apps published as GitHub releases, with the Craft App Suite
  (13 apps) as default repositories and any public repository addable by `owner/repo` or link.
- Two formats, chosen per app: a real pacman package built locally from the release tarball
  with makepkg (`igu-<name>`, dependencies worked out from the program's libraries), or an
  AppImage with menu entry and icon. Ready `.pkg.tar.zst` release files are installed directly.
- Channels per app: Stable, Latest (incl. alpha/beta/rc) and Pinned to one version.
- Offline archive: keeps the last N versions per app (default 3, 1–50 per app), always keeping
  the newest stable, a pinned and the installed version; prunes the rest. Optional extra
  formats: the other Linux format, Windows x64, macOS.
- Installs from the archive first, so archived versions install and roll back without a network.
- SHA-256 verification of every file against GitHub's asset digest, SHA256SUMS-style files
  and `<file>.sha256`; sources must agree. Resumable downloads.
- Update all: every pacman package in one `pacman -U` (one password prompt).
- Detects the same app installed outside the updater (AUR/pacman package, loose AppImage),
  and never replaces a running app.
- GTK4 window in the Idus style (same palette and layout as Idus SC Toolbox): filter,
  per-app rows, release notes, version picker, settings dialog, live log.
- CLI flags for every action (`--check`, `--list`, `--sync`, `--update-all`, `--install`,
  `--uninstall`, `--add`, `--remove`, `--offline`, `--notify`) and right-click menu actions.
- ETag-cached GitHub requests, clear rate-limit messages, optional GitHub token (mode 600).
- `install-idus-github-updater.sh`: self-contained folder in `~/.local/share`, menu entry
  with actions, dependency check.

### Notes
- The pacman format needs `base-devel`. Some AppImages need `fuse2`.
- Not affiliated with GitHub or the ArtCraft team.
