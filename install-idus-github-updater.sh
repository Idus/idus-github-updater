#!/usr/bin/env bash
# Idus GitHub Updater installer  ·  v1.3  ·  build 2026-10-14
# Installs Idus GitHub Updater (GTK4) as a self-contained folder in ~/.local/share/idus-github-updater
# + a start-menu entry with icon and right-click actions.
# Your settings and install records live in that folder and are never touched by a re-install.
# Run:  bash install-idus-github-updater.sh          (next to the downloaded files)
#  or:  bash <(curl -fsSL https://github.com/Idus/idus-github-updater/releases/latest/download/install-idus-github-updater.sh)
set -e
SRC="$(cd "$(dirname "$0")" 2>/dev/null && pwd || pwd)"
APP="$HOME/.local/share/idus-github-updater"

# One-line install: without the app next to this script, fetch it from the latest GitHub release
if [ ! -f "$SRC/idus-github-updater.py" ]; then
    BASE="${IGU_DOWNLOAD_BASE:-https://github.com/Idus/idus-github-updater/releases/latest/download}"
    command -v curl >/dev/null 2>&1 || { echo "❌ curl is needed:  sudo pacman -S --needed curl"; exit 1; }
    TMP="$(mktemp -d)"
    trap 'rm -rf "$TMP"' EXIT
    echo "⬇️  downloading Idus GitHub Updater (latest release) …"
    curl -fsSL "$BASE/idus-github-updater.py" -o "$TMP/idus-github-updater.py" || \
        { echo "❌ download failed — check your connection, or download the files from GitHub"; exit 1; }
    curl -fsL "$BASE/idus-github-updater.png" -o "$TMP/idus-github-updater.png" || true
    head -n 5 "$TMP/idus-github-updater.py" | grep -q "Idus GitHub Updater" || \
        { echo "❌ that download isn't Idus GitHub Updater — stopping"; exit 1; }
    SRC="$TMP"
fi
BIN="$HOME/.local/bin"
ICONS="$HOME/.local/share/icons/hicolor/512x512/apps"

mkdir -p "$APP" "$BIN" "$ICONS" ~/.local/share/applications

# App + icon in the same folder (one folder backup captures everything)
install -m 755 "$SRC/idus-github-updater.py" "$APP/idus-github-updater.py"
if [ -f "$SRC/idus-github-updater.png" ]; then
    install -m 644 "$SRC/idus-github-updater.png" "$APP/idus-github-updater.png"
    install -m 644 "$SRC/idus-github-updater.png" "$ICONS/idus-github-updater.png"
    ICON_LINE="Icon=idus-github-updater"
else
    echo "⚠️  no idus-github-updater.png next to the script — using a generic update icon."
    ICON_LINE="Icon=system-software-update"
fi

# PATH wrapper
{
    echo '#!/usr/bin/env bash'
    echo "exec python3 \"$APP/idus-github-updater.py\" \"\$@\""
} > "$BIN/idus-github-updater"
chmod 755 "$BIN/idus-github-updater"

# Start-menu entry + right-click actions (results arrive as a desktop notification)
cat > ~/.local/share/applications/idus-github-updater.desktop <<DESK
[Desktop Entry]
Type=Application
Name=Idus GitHub Updater
Comment=Install, update and archive apps from GitHub releases (Craft App Suite and more)
Exec=$BIN/idus-github-updater
$ICON_LINE
Categories=System;PackageManager;Utility;
Keywords=github;update;appimage;pacman;craft;photocraft;archive;offline;
StartupWMClass=se.wolfpack.idusgithubupdater
Actions=check;updateall;sync;

[Desktop Action check]
Name=Check for updates
Exec=$BIN/idus-github-updater --check --notify

[Desktop Action updateall]
Name=Update all
Exec=$BIN/idus-github-updater --update-all --notify

[Desktop Action sync]
Name=Sync offline archive
Exec=$BIN/idus-github-updater --sync --notify
DESK

# Bust icon & menu caches (NO plasma restart — do that yourself if needed)
command -v gtk-update-icon-cache >/dev/null 2>&1 && \
    gtk-update-icon-cache -f -q "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
command -v update-desktop-database >/dev/null 2>&1 && \
    update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
rm -f "$HOME/.cache/icon-cache.kcache" 2>/dev/null || true
command -v kbuildsycoca6 >/dev/null 2>&1 && kbuildsycoca6 >/dev/null 2>&1 || \
    { command -v kbuildsycoca5 >/dev/null 2>&1 && kbuildsycoca5 >/dev/null 2>&1; } || true

# Dependency check
MISSING=0
if ! python3 -c "import gi; gi.require_version('Gtk','4.0')" 2>/dev/null; then
    echo "⚠️  Missing GTK4 bindings. Run:  sudo pacman -S --needed python-gobject gtk4"
    MISSING=1
fi
if ! command -v makepkg >/dev/null 2>&1 || ! command -v fakeroot >/dev/null 2>&1; then
    echo "⚠️  makepkg/fakeroot not found — needed for the pacman format (AppImage works without):"
    echo "      sudo pacman -S --needed base-devel"
    MISSING=1
fi
if ! command -v ksshaskpass >/dev/null 2>&1 && [ -z "$SUDO_ASKPASS" ]; then
    echo "ℹ️  ksshaskpass not found — every pacman install asks for the password separately."
    echo "      For one prompt per few minutes:  sudo pacman -S --needed ksshaskpass"
fi
command -v pkexec >/dev/null 2>&1 || command -v ksshaskpass >/dev/null 2>&1 || \
    echo "⚠️  neither ksshaskpass nor pkexec found — the window can't ask for your password to run pacman."
command -v notify-send >/dev/null 2>&1 || \
    echo "ℹ️  notify-send not found — no desktop notifications (sudo pacman -S --needed libnotify)."
if command -v pacman >/dev/null 2>&1 && ! pacman -Qq fuse2 >/dev/null 2>&1; then
    echo "ℹ️  Some AppImages need FUSE 2 to start:  sudo pacman -S --needed fuse2"
fi
case ":$PATH:" in *":$BIN:"*) ;; *) echo "ℹ️  $BIN is not in your PATH — the menu entry works anyway." ;; esac

echo "✅ Done."
echo "   • Launch: search 'Idus GitHub Updater' in the menu, or run 'idus-github-updater' in a terminal."
echo "   • Right-click the menu entry for: Check for updates · Update all · Sync offline archive."
echo "   • New icon not showing? Log out/in (the icon cache is stubborn)."
[ "$MISSING" = 0 ] || echo "   • Install the missing packages above first."
