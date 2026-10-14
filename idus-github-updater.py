#!/usr/bin/env python3
# ─────────────────────────────────────────────────────────────────────────────
#  Idus GitHub Updater  ·  v1.2  ·  build 2026-10-14
#  (bump APP_VERSION + BUILD below and this line together on every update)
# ─────────────────────────────────────────────────────────────────────────────
"""
Idus GitHub Updater — install, update and archive apps that are published as GitHub
releases. Built for Arch Linux (CachyOS, EndeavourOS, Manjaro …). GTK4.

Ships with the Craft App Suite (PhotoCraft, LightCraft, FilmCraft …) as default
repositories; add any other GitHub repository that publishes Linux builds.

 • Install as a native pacman package (built locally from the release tarball with
   makepkg) or as an AppImage — chosen per app.
 • Channel per app: Stable, Latest (incl. alpha/beta/rc) or Pinned to one version.
 • Offline archive: keeps the last N versions per app, verified against the published
   SHA-256 checksums, so you can install or roll back without a network.

GUI launches with no arguments. Same actions via CLI (KDE shortcuts / Stream Deck):
  idus-github-updater --check               Check every repository, print a table
  idus-github-updater --list                Same table from cache (no network)
  idus-github-updater --sync                Download missing archive versions, prune old ones
  idus-github-updater --update-all          Install every available update
  idus-github-updater --install REPO        Install / update one app
        [--tag TAG] [--format pacman|appimage] [--pin]
  idus-github-updater --uninstall REPO
  idus-github-updater --add OWNER/REPO [--name NAME]
  idus-github-updater --remove REPO
  idus-github-updater --remove-other REPO   Remove copies installed another way (Flatpak, AUR, AppImage)
  --offline                                 Use cache + archive only, no network
  --notify                                  Send a desktop notification with the result

Dependencies (Arch):  sudo pacman -S --needed python-gobject gtk4 base-devel
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------- configuration
APP_NAME = "Idus GitHub Updater"
APP_SLUG = "idus-github-updater"
APP_ID = "se.wolfpack.idusgithubupdater"
APP_VERSION = "1.2"
BUILD = "2026-10-14"  # keep in sync with the header comment at the top of this file
REPO_URL = "https://github.com/Idus/idus-github-updater"
KOFI_URL = "https://ko-fi.com/N4N2QZHKK"

# Self-contained folder: icon + settings + state next to the app (like SC Toolbox)
APP_DIR = Path(__file__).resolve().parent
ICON_FILE = APP_DIR / f"{APP_SLUG}.png"
SETTINGS_FILE = APP_DIR / f"{APP_SLUG}-settings.json"
STATE_FILE = APP_DIR / f"{APP_SLUG}-state.json"

_XDG_DATA = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / APP_SLUG
LOG_FILE = CACHE_DIR / f"{APP_SLUG}.log"
DESKTOP_DIR = _XDG_DATA / "applications"
ICON_THEME_DIR = _XDG_DATA / "icons/hicolor"

API_BASE = os.environ.get("IGU_API_BASE", "https://api.github.com").rstrip("/")
USER_AGENT = f"{APP_SLUG}/{APP_VERSION} (+{REPO_URL})"
PKG_PREFIX = "igu-"          # pacman packages we build: igu-<repo>, never clashes with AUR names
ARCH = platform.machine() or "x86_64"
NET_TIMEOUT = 20

CHANNELS = ["stable", "latest", "pinned"]
CHANNEL_LABELS = ["Stable", "Latest", "Pinned"]
FORMATS = ["pacman", "appimage", "idus"]
FORMAT_LABELS = ["pacman", "AppImage", "Idus installer"]

# Default repositories, in this order: Idus's own apps first, then the Craft App Suite.
# Idus apps ship <app>.py + <app>.png + install-<app>.sh as release files; the "Idus installer"
# format downloads and verifies them and runs install-<app>.sh as you (no root).
IDUS_APPS = [
    ("idus-github-updater", "Idus GitHub Updater", {}),
    ("idus-sc-toolbox", "Idus SC Toolbox", {}),
    ("idus-elgato-light", "Idus Elgato Light", {}),
    ("idus-hw-player", "Idus HW-player", {"archive_only": True,
                                          "about": "Raspberry Pi app — archived here, installed on the Pi"}),
]
IDUS_PRESETS = [dict({"repo": f"Idus/{slug}", "name": name, "group": "Idus", "format": "idus",
                      "icon": f"https://raw.githubusercontent.com/Idus/{slug}/main/{slug}.png"}, **extra)
                for slug, name, extra in IDUS_APPS]
PRESET_KEYS = ("group", "icon", "archive_only", "about")  # owned by the preset, refreshed on every start

# The Craft App Suite (https://getartcraft.com/apps)
CRAFT_APPS = [
    ("photocraft", "PhotoCraft"), ("lightcraft", "LightCraft"), ("filmcraft", "FilmCraft"),
    ("designcraft", "DesignCraft"), ("vectorcraft", "VectorCraft"), ("pdfcraft", "PdfCraft"),
    ("effectcraft", "EffectCraft"), ("wordcraft", "WordCraft"), ("deckcraft", "DeckCraft"),
    ("gridcraft", "GridCraft"), ("cadcraft", "CADCraft"), ("soundcraft", "SoundCraft"),
    ("figcraft", "FigCraft"),
]
PRESETS = IDUS_PRESETS + [{
    "repo": f"storytold/{app}", "name": name, "group": "Craft Suite",
    "icon": (f"https://raw.githubusercontent.com/storytold/{app}/main/assets/app-icon/"
             f"hicolor/64x64/apps/ai.storyteller.{app}.png"),
} for app, name in CRAFT_APPS]

REPO_DEFAULTS = {"channel": "stable", "pinned": "", "format": "pacman", "archive": False,
                 "keep": 0, "hidden": False, "group": "", "icon": "", "archive_only": False, "about": ""}


def is_idus(entry) -> bool:
    return entry["repo"].split("/")[0].lower() == "idus"

DEFAULT_SETTINGS = {
    "archive_dir": "~/GitHub-archive",
    "appimage_dir": "~/AppImages",
    "keep_default": 3,
    "archive_other_linux": False,
    "archive_windows": False,
    "archive_macos": False,
    "check_on_start": True,
    "check_hourly": False,
    "sync_after_check": True,
    "notify": True,
    "require_checksum": True,
    "github_token": "",
    "show_hidden": False,
    "removed_presets": [],
    "repos": [],
}

_lock = threading.RLock()


# ---------------------------------------------------------------- small helpers
def human(n: float) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _atomic_write(path: Path, text: str, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def file_log(text: str):
    """Everything shown in the log also lands in ~/.cache/idus-github-updater/…log (1 MB, one rotation)."""
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 1_000_000:
            os.replace(LOG_FILE, LOG_FILE.with_suffix(".log.1"))
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {text}\n")
    except Exception:
        pass


def short_date(iso: str) -> str:
    try:
        return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d")
    except Exception:
        return iso[:10] if iso else ""


def notify(title: str, body: str):
    if not shutil.which("notify-send"):
        return
    cmd = ["notify-send", "-a", APP_NAME, title, body]
    if ICON_FILE.exists():
        cmd[1:1] = ["-i", str(ICON_FILE)]
    try:
        subprocess.Popen(cmd, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def open_uri(uri: str):
    try:
        subprocess.Popen(["xdg-open", uri], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


# ---------------------------------------------------------------- settings + state
def load_settings() -> dict:
    s = json.loads(json.dumps(DEFAULT_SETTINGS))
    try:
        s.update(json.loads(SETTINGS_FILE.read_text()))
    except Exception:
        pass
    have = {r.get("repo", "").lower() for r in s["repos"]}
    removed = {x.lower() for x in s.get("removed_presets", [])}
    for p in PRESETS:  # new presets in later versions show up automatically
        if p["repo"].lower() not in have and p["repo"].lower() not in removed:
            s["repos"].append(dict(p))
    by_repo = {p["repo"].lower(): p for p in PRESETS}
    for r in s["repos"]:
        for k, v in REPO_DEFAULTS.items():
            r.setdefault(k, v)
        r.setdefault("name", r["repo"].split("/")[-1])
        preset = by_repo.get(r["repo"].lower())
        if preset:
            for k in PRESET_KEYS:
                r[k] = preset.get(k, REPO_DEFAULTS[k])
        if r["channel"] not in CHANNELS:
            r["channel"] = "stable"
        if r["format"] not in FORMATS or (r["format"] == "idus") != is_idus(r):
            r["format"] = "idus" if is_idus(r) else "pacman"
    s["repos"].sort(key=lambda r: 0 if r.get("group") == "Idus" else 1)  # Idus apps on top
    return s


def save_settings():
    with _lock:  # 0600: the file may hold a GitHub token
        _atomic_write(SETTINGS_FILE, json.dumps(CFG, indent=2, ensure_ascii=False), 0o600)


def load_state() -> dict:
    st = {"installed": {}, "releases": {}, "last_check": 0, "notified": ""}
    try:
        st.update(json.loads(STATE_FILE.read_text()))
    except Exception:
        pass
    return st


def save_state():
    with _lock:
        _atomic_write(STATE_FILE, json.dumps(ST, indent=1, ensure_ascii=False))


CFG = load_settings()
ST = load_state()


def archive_root() -> Path:
    return Path(os.path.expanduser(CFG["archive_dir"]))


def appimage_root() -> Path:
    return Path(os.path.expanduser(CFG["appimage_dir"]))


def keep_count(entry) -> int:
    return int(entry.get("keep") or CFG.get("keep_default") or 3)


def parse_repo(text: str):
    """'owner/repo', 'github.com/owner/repo' or any github.com URL → 'owner/repo' (or None)."""
    t = (text or "").strip()
    if not t:
        return None
    if "://" in t or t.lower().startswith(("github.com/", "www.github.com/")):
        u = urllib.parse.urlparse(t if "://" in t else "https://" + t)
        if u.netloc.lower() not in ("github.com", "www.github.com"):
            return None
        parts = [p for p in u.path.split("/") if p]
    else:
        parts = t.split("/")
        if len(parts) != 2:
            return None
    if len(parts) < 2:
        return None
    owner, name = parts[0], re.sub(r"\.git$", "", parts[1])
    ok = re.compile(r"[A-Za-z0-9_.-]+")
    if not ok.fullmatch(owner) or not ok.fullmatch(name):
        return None
    return f"{owner}/{name}"


def find_repo(query: str):
    """Match 'owner/repo', 'repo' or a display name (case-insensitive)."""
    q = (query or "").strip().lower()
    full = (parse_repo(query) or "").lower()
    hits = [r for r in CFG["repos"] if r["repo"].lower() == full] or \
           [r for r in CFG["repos"] if r["repo"].split("/")[1].lower() == q or r["name"].lower() == q]
    return hits[0] if len(hits) == 1 else None


# ---------------------------------------------------------------- versions + channels
PRE_RE = re.compile(r"(?<![a-z])(alpha|beta|rc|pre|preview|dev|nightly|canary|snapshot)(?![a-z])", re.I)
NUM_RE = re.compile(r"(\d+(?:[._-]\d+)*)(.*)$")


def version_key(tag: str):
    """Sort key for a tag. 1.2.0 > 1.2.0-rc.10 > 1.2.0-rc.9 > 1.2.0-beta > 1.1.9. None if no number."""
    m = NUM_RE.search(tag or "")
    if not m:
        return None
    nums = [int(x) for x in re.split(r"[._-]", m.group(1))]
    nums += [0] * max(0, 4 - len(nums))
    rest = m.group(2).split("+")[0].strip("-._ ")
    if not rest:
        return (tuple(nums), 1, ())
    segs = []
    for part in re.findall(r"\d+|[A-Za-z]+", rest):
        segs.append((0, int(part), "") if part.isdigit() else (1, 0, part.lower()))
    return (tuple(nums), 0, tuple(segs))


def is_pre(tag: str, flag) -> bool:
    """Pre-release if GitHub says so OR the tag says so (alpha, beta, rc, nightly …)."""
    return bool(flag) or bool(PRE_RE.search(tag or ""))


def sort_releases(rels: list) -> list:
    keys = [version_key(r.get("tag", "")) for r in rels]
    if rels and all(k is not None for k in keys):
        pairs = sorted(zip(keys, rels), key=lambda kr: (kr[0], kr[1].get("published") or ""), reverse=True)
        return [r for _, r in pairs]
    return sorted(rels, key=lambda r: r.get("published") or "", reverse=True)


def channel_list(entry, rels: list) -> list:
    ch = entry.get("channel", "stable")
    if ch == "latest":
        return list(rels)
    if ch == "pinned":
        return [r for r in rels if r["tag"] == entry.get("pinned")]
    return [r for r in rels if not r["pre"]]


# ---------------------------------------------------------------- release assets
ARCH_PATTERNS = {"x86_64": r"x86[_-]?64|amd64|x64", "aarch64": r"aarch64|arm64"}
OTHER_ARCH = r"i[3-6]86|armv[5-8]\w*|armhf|armel|arm32|riscv64|ppc64le|ppc64|s390x|loong(?:arch)?64|x86(?![_-]?64)|386"
NOISE_EXT = (".sig", ".asc", ".sha256", ".sha256sum", ".sha512", ".md5", ".zsync", ".wixpdb", ".pdb",
             ".blockmap", ".yml", ".yaml", ".json", ".txt", ".sbom", ".spdx", ".pem", ".cert", ".minisig")
NOISE_WORDS = r"web|wasm|src|source|sources|debug|dbg|dbgsym|symbols|docs?"
OS_WIN = r"windows|win64|win32|win|msvc|mingw"
OS_MAC = r"macos|darwin|osx|mac|apple"
OS_OTHER = r"freebsd|openbsd|netbsd|android|ios"
SUMS_RE = re.compile(r"(sha256sums?|checksums?)(\.txt)?$", re.I)


def _has(pattern: str, name: str) -> bool:
    return re.search(rf"(?<![a-z0-9])(?:{pattern})(?![a-z0-9])", name, re.I) is not None


def arch_matches(name: str, arch: str = ARCH) -> bool:
    """True if the file name says our CPU — or says no CPU at all."""
    mine = ARCH_PATTERNS.get(arch, re.escape(arch))
    if _has(mine, name):
        return True
    others = [p for a, p in ARCH_PATTERNS.items() if a != arch] + [OTHER_ARCH]
    return not any(_has(p, name) for p in others)


def classify(name: str):
    """→ 'appimage' | 'pkg' | 'tarball' | 'windows' | 'macos' | None"""
    n = name.lower()
    if re.fullmatch(r"install-[a-z0-9._-]+\.sh", n):
        return "installer"
    if n.endswith(NOISE_EXT) or _has(NOISE_WORDS, n):
        return None
    if n.endswith(".appimage"):
        return "appimage" if arch_matches(n) else None
    if re.search(r"\.pkg\.tar\.(zst|xz|gz)$", n):
        return "pkg" if (arch_matches(n) or _has("any", n)) else None
    if re.search(r"\.(tar\.(gz|xz|zst|bz2)|tgz|txz)$", n):
        if _has(OS_WIN, n) or _has(OS_MAC, n) or _has(OS_OTHER, n):
            return None
        return "tarball" if arch_matches(n) else None  # no OS in the name → probably Linux (ranked lower)
    if n.endswith((".msi", ".exe")) or (n.endswith(".zip") and _has(OS_WIN, n)):
        if _has(r"aarch64|arm64|i[3-6]86|x86(?![_-]?64)|386", n) and not _has(ARCH_PATTERNS["x86_64"], n):
            return None
        return "windows"
    if n.endswith(".dmg") or (n.endswith((".zip", ".pkg")) and _has(OS_MAC, n)):
        return "macos"
    return None


def _score(name: str, cls: str):
    n = name.lower()
    s = 0
    if _has("cli", n):
        s += 5
    if _has("portable", n):
        s += 2
    if _has("musl", n):
        s += 1
    if cls == "windows":
        s += {".msi": 0, ".exe": 1}.get(Path(n).suffix, 3)
    if cls == "macos":
        s += 0 if n.endswith(".dmg") else 2
        s += 0 if _has("universal", n) else (1 if _has(ARCH_PATTERNS["aarch64"], n) else 2)
    if cls in ("tarball", "appimage") and not _has(ARCH_PATTERNS.get(ARCH, ARCH), n):
        s += 1
    if cls == "tarball" and not _has(r"linux|gnu|musl", n):
        s += 3
    return (s, len(n))


def pick_asset(release, cls: str):
    hits = [a for a in release.get("assets", []) if classify(a["name"]) == cls]
    return min(hits, key=lambda a: _score(a["name"], cls)) if hits else None


def install_asset(release, fmt: str):
    """The file we install for a format. pacman prefers a ready .pkg.tar.zst over a tarball."""
    for cls in {"pacman": ("pkg", "tarball"), "appimage": ("appimage",), "idus": ("installer",)}[fmt]:
        a = pick_asset(release, cls)
        if a:
            return a, cls
    return None, None


def payload_assets(release) -> list:
    """Everything an Idus release ships (the .py, .png, install-*.sh …), minus checksums/signatures."""
    return [a for a in release.get("assets", []) if not SUMS_RE.search(a["name"])
            and not a["name"].lower().endswith((".sig", ".asc", ".sha256", ".sha256sum", ".minisig"))]


def archive_assets(release, entry) -> list:
    if entry["format"] == "idus":
        return [(a, "installer") for a in payload_assets(release)] if install_asset(release, "idus")[0] else []
    out = []
    a, cls = install_asset(release, entry["format"])
    if a:
        out.append((a, cls))
    if CFG.get("archive_other_linux"):
        other = "appimage" if entry["format"] == "pacman" else "pacman"
        a, cls = install_asset(release, other)
        if a:
            out.append((a, cls))
    for flag, cls in (("archive_windows", "windows"), ("archive_macos", "macos")):
        if CFG.get(flag):
            a = pick_asset(release, cls)
            if a:
                out.append((a, cls))
    seen, uniq = set(), []
    for a, cls in out:
        if a["name"] not in seen:
            seen.add(a["name"])
            uniq.append((a, cls))
    return uniq


# ---------------------------------------------------------------- network
class NetError(Exception):
    pass


class Offline(NetError):
    pass


def _request(url, headers=None, timeout=NET_TIMEOUT):
    h = {"User-Agent": USER_AGENT}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return resp.status, resp.headers, resp
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise Offline(str(getattr(e, "reason", e)))


def _small_get(url: str, limit=2_000_000) -> str:
    code, _h, resp = _request(url)
    with resp:
        if code != 200:
            raise NetError(f"HTTP {code} for {url}")
        return resp.read(limit).decode("utf-8", "replace")


def _trim(r: dict) -> dict:
    tag = r.get("tag_name") or ""
    return {
        "tag": tag,
        "name": (r.get("name") or tag).strip(),
        "published": r.get("published_at") or r.get("created_at") or "",
        "pre": is_pre(tag, r.get("prerelease")),
        "url": r.get("html_url", ""),
        "notes": (r.get("body") or "")[:20000],
        "assets": [{"name": a.get("name", ""), "size": int(a.get("size") or 0),
                    "url": a.get("browser_download_url", ""), "digest": a.get("digest") or ""}
                   for a in r.get("assets", [])],
    }


def _fetch_releases_live(repo: str) -> list:
    key = repo.lower()
    cache = ST["releases"].get(key)
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if cache and cache.get("etag"):
        headers["If-None-Match"] = cache["etag"]
    token = CFG.get("github_token") or os.environ.get("GITHUB_TOKEN", "")
    if token:  # only ever sent to the API host, never to download hosts
        headers["Authorization"] = f"Bearer {token}"
    code, hdrs, resp = _request(f"{API_BASE}/repos/{repo}/releases?per_page=40", headers)
    with resp:
        body = resp.read() if code == 200 else b""
    if code == 304 and cache:
        with _lock:
            cache["checked"] = time.time()
        return cache["list"]
    if code == 200:
        rels = sort_releases([_trim(r) for r in json.loads(body) if not r.get("draft")])
        with _lock:
            ST["releases"][key] = {"etag": hdrs.get("ETag", ""), "checked": time.time(), "list": rels}
        return rels
    if code in (403, 429) and (hdrs.get("X-RateLimit-Remaining") == "0" or code == 429):
        reset = int(hdrs.get("X-RateLimit-Reset") or 0)
        when = f" (resets {dt.datetime.fromtimestamp(reset):%H:%M})" if reset else ""
        raise NetError(f"GitHub rate limit reached{when} — add a token in Settings")
    if code == 404:
        with _lock:
            ST["releases"][key] = {"etag": "", "checked": time.time(), "list": [], "missing": True}
        raise NetError("repository not found (or it is private)")
    raise NetError(f"GitHub answered HTTP {code}")


def get_releases(entry, offline=False):
    """→ (releases, source, problem). source: 'live' | 'cache' | 'archive' | 'none'.
    Raises Offline on a network failure when nothing else is known, so callers can stop
    trying the network for the remaining repositories."""
    repo = entry["repo"]
    problem = ""
    if not offline:
        try:
            return _fetch_releases_live(repo), "live", ""
        except Offline as e:
            problem = f"offline ({e})"
            offline_err = e
        except NetError as e:
            problem = str(e)
            offline_err = None
    else:
        offline_err = None
    cache = ST["releases"].get(repo.lower())
    if cache and cache.get("list"):
        return cache["list"], "cache", problem
    arch = archived_releases(repo)
    if arch:
        return arch, "archive", problem
    if offline_err is not None:
        raise offline_err
    return [], "none", problem


def known_releases(entry) -> list:
    """Cached release list, else what the archive knows (works fully offline)."""
    cache = ST["releases"].get(entry["repo"].lower())
    rels = list(cache["list"]) if cache and cache.get("list") else []
    tags = {r["tag"] for r in rels}
    extra = [r for r in archived_releases(entry["repo"]) if r["tag"] not in tags]
    return sort_releases(rels + extra) if extra else rels


# ---------------------------------------------------------------- checksums + downloads
_sums_cache = {}


def _parse_sums(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        line = line.strip()
        m = re.match(r"^([0-9a-fA-F]{64})\s+\*?(.+)$", line)
        if m:
            out[Path(m.group(2).strip()).name] = m.group(1).lower()
            continue
        m = re.match(r"^SHA256\s*\((.+)\)\s*=\s*([0-9a-fA-F]{64})$", line, re.I)
        if m:
            out[Path(m.group(1)).name] = m.group(2).lower()
    return out


def expected_sha256(release, asset):
    """SHA-256 from GitHub's asset digest, a SHA256SUMS-style file and/or <file>.sha256.
    All sources that exist must agree. None if nothing is published."""
    found = set()
    d = (asset.get("digest") or "").lower()
    if d.startswith("sha256:"):
        found.add(d[7:])
    names = {a["name"]: a for a in release.get("assets", [])}
    for per in (asset["name"] + ".sha256", asset["name"] + ".sha256sum"):
        if per in names:
            m = re.search(r"\b([0-9a-fA-F]{64})\b", _small_get(names[per]["url"]))
            if m:
                found.add(m.group(1).lower())
    for a in release.get("assets", []):
        if SUMS_RE.search(a["name"]) and not a["name"].lower().endswith((".sha256", ".sha256sum")):
            if a["url"] not in _sums_cache:
                _sums_cache[a["url"]] = _parse_sums(_small_get(a["url"]))
            h = _sums_cache[a["url"]].get(asset["name"])
            if h:
                found.add(h)
    if len(found) > 1:
        raise NetError(f"published checksums for {asset['name']} disagree — refusing")
    return found.pop() if found else None


def download(asset, dest: Path, sha, log) -> Path:
    """Resumable download to dest (via dest.part), size + SHA-256 checked before it is kept."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    size = int(asset.get("size") or 0)
    if dest.exists() and (not size or dest.stat().st_size == size):
        if not sha or sha256_file(dest) == sha:
            return dest
    part = dest.with_name(dest.name + ".part")
    have = part.stat().st_size if part.exists() else 0
    if size and have >= size:
        part.unlink()
        have = 0
    code, hdrs, resp = _request(asset["url"], {"Range": f"bytes={have}-"} if have else None, timeout=60)
    if code not in (200, 206):
        resp.close()
        raise NetError(f"download of {asset['name']} failed: HTTP {code}")
    if code == 200:
        have = 0
    total = size or (int(hdrs.get("Content-Length") or 0) + have)
    mark = 0.25
    with resp, open(part, "ab" if have else "wb") as f:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            have += len(chunk)
            if total > 8_000_000 and have / total >= mark and mark < 1:
                log(f"   … {int(mark * 100)}%  ({human(have)} of {human(total)})")
                mark += 0.25
    if size and part.stat().st_size != size:
        raise NetError(f"{asset['name']}: got {human(part.stat().st_size)} of {human(size)} — try again to resume")
    if sha:
        got = sha256_file(part)
        if got != sha:
            part.unlink(missing_ok=True)
            raise NetError(f"checksum mismatch for {asset['name']} (expected {sha[:12]}…, got {got[:12]}…)")
    os.replace(part, dest)
    return dest


# ---------------------------------------------------------------- offline archive
# Layout:  <archive>/<App name>/<Linux|Windows|macOS>/<version>/  files + SHA256SUMS + release.json
PLATFORMS = ("Linux", "Windows", "macOS")
PLATFORM_OF = {"appimage": "Linux", "pkg": "Linux", "tarball": "Linux", "installer": "Linux",
               "windows": "Windows", "macos": "macOS"}


def safe_tag(tag: str) -> str:
    return re.sub(r"[^A-Za-z0-9._+-]", "_", tag) or "_"


def platform_of(filename: str) -> str:
    return PLATFORM_OF.get(classify(filename), "Linux")


def _folder_name(text: str) -> str:
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", text or "").strip(" .") or "_"


def app_archive_dir(repo: str) -> Path:
    """<archive>/<App name> — '<App name> (owner)' only if two apps in the list share a name."""
    owner, rname = repo.split("/", 1)
    entry = next((r for r in CFG["repos"] if r["repo"].lower() == repo.lower()), None)
    name = _folder_name(entry["name"] if entry else rname)
    clash = any(r["repo"].lower() != repo.lower() and _folder_name(r["name"]).lower() == name.lower()
                for r in CFG["repos"])
    return archive_root() / (f"{name} ({owner})" if clash else name)


def archive_dir_for(repo: str, tag: str, platform: str = "Linux") -> Path:
    return app_archive_dir(repo) / platform / safe_tag(tag)


_scan = {"t": 0.0, "root": "", "data": {}}


def _archive_index() -> dict:
    """{repo (lower): [(version folder, release.json)]}, cached a few seconds (the archive may be on a NAS).
    Found by the repo recorded in release.json, so renaming an app never loses its archive."""
    root = archive_root()
    if _scan["root"] == str(root) and time.time() - _scan["t"] < 3:
        return _scan["data"]
    data = {}
    if root.is_dir():
        for meta_f in root.glob("*/*/*/release.json"):
            if meta_f.parent.parent.name not in PLATFORMS:
                continue
            try:
                meta = json.loads(meta_f.read_text())
            except Exception:
                continue
            data.setdefault(str(meta.get("repo", "")).lower(), []).append((meta_f.parent, meta))
    _scan.update(t=time.time(), root=str(root), data=data)
    return data


def _archive_changed():
    _scan["t"] = 0.0


def _archive_dirs(repo: str) -> list:
    return list(_archive_index().get(repo.lower(), []))


def _rmdir_up(d: Path, levels=2):
    """Remove d and then its parents while they are empty (never the archive root itself)."""
    root = archive_root().resolve()
    for _ in range(levels):
        try:
            if d.resolve() == root:
                return
            d.rmdir()
        except OSError:
            return
        d = d.parent


def read_sums(d: Path) -> dict:
    try:
        return _parse_sums((d / "SHA256SUMS").read_text())
    except Exception:
        return {}


def write_sums(d: Path, add: dict):
    sums = read_sums(d)
    sums.update({k: v for k, v in add.items() if v})
    _atomic_write(d / "SHA256SUMS", "".join(f"{h}  {n}\n" for n, h in sorted(sums.items())))


def write_release_meta(d: Path, release: dict, repo: str):
    meta = dict(release)
    meta["repo"] = repo
    meta["archived_at"] = dt.datetime.now().isoformat(timespec="seconds")
    _atomic_write(d / "release.json", json.dumps(meta, indent=1, ensure_ascii=False))
    _archive_changed()


def archived_releases(repo: str) -> list:
    seen, out = set(), []
    for d, meta in _archive_dirs(repo):
        tag = meta.get("tag", d.name)
        if tag in seen:
            continue
        seen.add(tag)
        rel = dict(meta)
        rel["pre"] = is_pre(tag, meta.get("pre"))
        out.append(rel)
    return sort_releases(out)


def archive_usage(repo: str):
    """→ ({tag: [files]}, total bytes)"""
    tags, total = {}, 0
    for d, meta in _archive_dirs(repo):
        files = [n for n in read_sums(d) if (d / n).is_file()]
        total += sum((d / n).stat().st_size for n in files)
        tags.setdefault(meta.get("tag", d.name), []).extend(files)
    return tags, total


def archived_file(repo: str, tag: str, name: str, verify=True):
    """Path of an archived file listed in its SHA256SUMS (and, with verify, still matching it)."""
    for d, meta in _archive_dirs(repo):
        if meta.get("tag") != tag:
            continue
        p = d / name
        want = read_sums(d).get(name)
        if p.is_file() and want and (not verify or sha256_file(p) == want):
            return p
    return None


def wanted_tags(entry, rels: list, installed_tag=None) -> list:
    """Archive keep-set: newest N in the app's channel + newest stable + pinned + installed."""
    n = keep_count(entry)
    ch_rels = [r for r in rels if not r["pre"]] if entry["channel"] == "pinned" else channel_list(entry, rels)
    tags = [r["tag"] for r in ch_rels[:n]]
    stable = next((r for r in rels if not r["pre"]), None)
    if stable and stable["tag"] not in tags:
        tags.append(stable["tag"])
    if entry["channel"] == "pinned" and entry.get("pinned") and entry["pinned"] not in tags:
        tags.append(entry["pinned"])
    if installed_tag and installed_tag not in tags:
        tags.append(installed_tag)
    return tags


def sync_archive(entry, rels: list, log, offline=False):
    """Download what the keep-set lacks, then prune what falls outside it. → (new files, pruned versions)"""
    if not entry.get("archive"):
        return 0, 0
    repo, name = entry["repo"], entry["name"]
    rec = ST["installed"].get(repo.lower())
    keep = wanted_tags(entry, rels, rec["version"] if rec else None)
    new = 0
    by_tag = {r["tag"]: r for r in rels}
    if not offline:
        for tag in keep:
            rel = by_tag.get(tag)
            if not rel:
                continue
            for asset, cls in archive_assets(rel, entry):
                have = archived_file(repo, tag, asset["name"], verify=False)  # size check only: no re-hashing a NAS
                if have and (not asset.get("size") or have.stat().st_size == asset["size"]):
                    continue
                d = archive_dir_for(repo, tag, PLATFORM_OF[cls])
                try:
                    sha = expected_sha256(rel, asset)
                    if not sha and CFG.get("require_checksum"):
                        log(f"⚠️ {name} {tag}: no published checksum for {asset['name']} — skipped")
                        continue
                    log(f"📦 {name} {tag} ({PLATFORM_OF[cls]}): {asset['name']} ({human(asset['size'])})")
                    download(asset, d / asset["name"], sha, log)
                    write_sums(d, {asset["name"]: sha or sha256_file(d / asset["name"])})
                    write_release_meta(d, rel, repo)
                    new += 1
                except Offline as e:
                    log(f"⚪ offline — archive sync stopped ({e})")
                    return new, 0
                except NetError as e:
                    log(f"❌ {name} {tag}: {e}")
    # prune: only folders we wrote (they carry release.json), and counted from what is actually
    # archived — a download that failed above can never cost us the last good version
    _archive_changed()
    keep = set(wanted_tags(entry, archived_releases(repo), rec["version"] if rec else None))
    gone = set()
    for d, meta in _archive_dirs(repo):
        tag = meta.get("tag", d.name)
        if tag not in keep:
            shutil.rmtree(d, ignore_errors=True)
            _rmdir_up(d.parent)
            gone.add(tag)
    for tag in sorted(gone):
        log(f"🗑 {name}: pruned {tag} from the archive")
    _archive_changed()
    return new, len(gone)


def purge_archive(repo: str, log) -> int:
    tags = set()
    for d, meta in _archive_dirs(repo):
        shutil.rmtree(d, ignore_errors=True)
        _rmdir_up(d.parent)
        tags.add(meta.get("tag", d.name))
    _archive_changed()
    return len(tags)


def migrate_archive(log) -> int:
    """v1.0 kept <archive>/<owner>/<repo>/<version>/ — move it to <archive>/<App>/<Platform>/<version>/."""
    root = archive_root()
    if not root.is_dir():
        return 0
    moved = 0
    for meta_f in list(root.glob("*/*/*/release.json")):
        old = meta_f.parent
        if old.parent.name in PLATFORMS:
            continue  # already the new layout
        try:
            meta = json.loads(meta_f.read_text())
        except Exception:
            continue
        repo = str(meta.get("repo", ""))
        if "/" not in repo or f"{old.parent.parent.name}/{old.parent.name}".lower() != repo.lower():
            continue  # not a folder we wrote
        tag = meta.get("tag", old.name)
        for fname, sha in read_sums(old).items():
            src = old / fname
            if not src.is_file():
                continue
            dest = archive_dir_for(repo, tag, platform_of(fname))
            dest.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest / fname))
            write_sums(dest, {fname: sha})
            if not (dest / "release.json").exists():
                shutil.copyfile(meta_f, dest / "release.json")
            moved += 1
        for f in ("SHA256SUMS", "release.json"):
            (old / f).unlink(missing_ok=True)
        _rmdir_up(old, levels=3)
    if moved:
        _archive_changed()
        log(f"📦 archive: moved {moved} file(s) to the new layout <App>/<Linux|Windows|macOS>/<version>")
    return moved


# ---------------------------------------------------------------- system helpers
GUI_MODE = False


class InstallError(Exception):
    pass


def pacman_query():
    """{package: version} of everything installed, or None when pacman isn't there."""
    if not shutil.which("pacman"):
        return None
    try:
        out = subprocess.run(["pacman", "-Q"], capture_output=True, text=True, timeout=30).stdout
    except Exception:
        return None
    return {p[0]: p[1] for p in (line.split() for line in out.splitlines()) if len(p) >= 2}


ASKPASS_PROGRAMS = ("ksshaskpass", "lxqt-openssh-askpass", "/usr/lib/ssh/ssh-askpass",
                    "/usr/lib/seahorse/ssh-askpass", "ssh-askpass", "x11-ssh-askpass")
_sudo_kind = {}


def _askpass():
    """A graphical password prompt sudo can use (KDE: ksshaskpass)."""
    env = os.environ.get("SUDO_ASKPASS")
    if env and os.access(env, os.X_OK):
        return env
    for c in ASKPASS_PROGRAMS:
        p = c if c.startswith("/") else shutil.which(c)
        if p and os.access(p, os.X_OK):
            return p
    return None


def _sudo_supports_askpass() -> bool:
    if "ok" not in _sudo_kind:
        ok = False
        if shutil.which("sudo"):
            try:
                v = subprocess.run(["sudo", "--version"], capture_output=True, text=True, timeout=5).stdout.lower()
                ok = "sudo-rs" not in v  # sudo-rs has no askpass support
            except Exception:
                pass
        _sudo_kind["ok"] = ok
    return _sudo_kind["ok"]


def run_root(cmd: list, log):
    """Run a command as root.
    Terminal → sudo. Window / menu action → sudo with a graphical askpass (ksshaskpass), so sudo's
    normal ~5-minute memory applies and several installs in a row ask once; pkexec as a fallback
    (pkexec asks every time)."""
    env = None
    if os.geteuid() == 0:
        full = list(cmd)
    elif not GUI_MODE and sys.stdin and sys.stdin.isatty() and shutil.which("sudo"):
        full = ["sudo"] + cmd
    elif _askpass() and _sudo_supports_askpass() and not _sudo_kind.get("refused"):
        full = ["sudo", "-A", "-p", f"{APP_NAME} needs your password for pacman: "] + cmd
        env = dict(os.environ, SUDO_ASKPASS=_askpass())
    elif shutil.which("pkexec"):
        full = ["pkexec"] + cmd
    else:
        raise InstallError("needs root: install ksshaskpass or polkit (pkexec), or run from a terminal")
    log(f"🔐 {' '.join(shlex.quote(c) for c in cmd)}")
    r = subprocess.run(full, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env,
                       stdin=subprocess.DEVNULL if full[0] != "sudo" or "-A" in full else None)
    out = r.stdout.lower()
    if r.returncode and "-A" in full and ("not in the sudoers" in out or "not allowed" in out
                                         or "may not run sudo" in out) and shutil.which("pkexec"):
        _sudo_kind["refused"] = True  # not a sudoer → polkit from now on
        return run_root(cmd, log)
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    for ln in lines[-12:]:
        log(f"   {ln}")
    if r.returncode == 0:
        return
    if full[0] == "pkexec" and r.returncode in (126, 127):
        raise InstallError("password dialog was cancelled")
    if "-A" in full and ("no password was provided" in out or "a password is required" in out):
        raise InstallError("password dialog was cancelled")
    if "incorrect password" in out:
        raise InstallError("wrong password")
    if "unable to lock database" in out:
        raise InstallError("pacman is busy (another update running?) — try again when it's done")
    if "in conflict" in out or "conflicting" in out:
        raise InstallError("conflicts with an installed package — remove that one first")
    if "exists in filesystem" in out:
        raise InstallError("files already exist on the system — try the AppImage format for this app")
    raise InstallError(f"{cmd[0]} failed (exit {r.returncode})")


def refresh_desktop_caches():
    for cmd in (["update-desktop-database", str(DESKTOP_DIR)],
                ["gtk-update-icon-cache", "-f", "-q", str(ICON_THEME_DIR)],
                ["kbuildsycoca6"]):
        if shutil.which(cmd[0]):
            try:
                subprocess.run(cmd, capture_output=True, timeout=90)
            except Exception:
                pass


def running_pids(exe_paths=(), appimage=None, script=None) -> list:
    """PIDs of this user's processes running one of exe_paths, the given AppImage or Python script."""
    paths = {os.path.realpath(p) for p in exe_paths} | set(exe_paths)
    needle = (b"APPIMAGE=" + str(appimage).encode() + b"\0") if appimage else None
    in_cmd = str(script).encode() if script else None
    pids, me = [], os.getuid()
    for p in Path("/proc").iterdir():
        if not p.name.isdigit():
            continue
        try:
            if p.stat().st_uid != me:
                continue
            if paths and os.readlink(p / "exe") in paths:
                pids.append(int(p.name))
                continue
            if needle and needle in (p / "environ").read_bytes():
                pids.append(int(p.name))
                continue
            if in_cmd and int(p.name) != os.getpid() and in_cmd in (p / "cmdline").read_bytes():
                pids.append(int(p.name))
        except Exception:
            continue
    return pids


def is_running(rec) -> bool:
    if not rec:
        return False
    if rec.get("format") == "appimage":
        return bool(running_pids(appimage=rec.get("path")))
    if rec.get("format") == "idus":
        return bool(running_pids(script=rec.get("script")))
    return bool(running_pids(exe_paths=rec.get("binaries") or []))


def idus_installed(entry):
    """An Idus app installed by its own installer lives in ~/.local/share/<app>/<app>.py — its
    APP_VERSION is the truth, however it got installed (by hand or by the updater)."""
    slug = entry["repo"].split("/")[1]
    d = _XDG_DATA / slug
    f = d / f"{slug}.py"
    try:
        text = f.read_text(errors="replace")[:30000]
    except Exception:
        return None
    m = re.search(r"^APP_VERSION\s*=\s*[\"']([^\"']+)[\"']", text, re.M)
    if not m:
        return None
    ver = m.group(1)
    cache = ST["releases"].get(entry["repo"].lower()) or {}
    tag = next((r["tag"] for r in cache.get("list") or [] if r["tag"].lstrip("vV") == ver), f"v{ver}")
    return {"format": "idus", "path": str(d), "script": str(f), "version": tag}


def installed_record(entry, pacq):
    """Our install record — dropped if the app was removed outside this tool."""
    if is_idus(entry):
        return idus_installed(entry)
    key = entry["repo"].lower()
    rec = ST["installed"].get(key)
    if not rec:
        return None
    gone = (rec.get("format") == "pacman" and pacq is not None and rec.get("pkgname") not in pacq) or \
           (rec.get("format") == "appimage" and not Path(rec.get("path", "")).exists())
    if gone:
        with _lock:
            ST["installed"].pop(key, None)
        save_state()
        return None
    return rec


_fp = {"t": 0.0, "data": []}


def flatpak_apps() -> list:
    """[(app id, installation)] of installed Flatpak apps, cached a few seconds."""
    if time.time() - _fp["t"] < 5:
        return _fp["data"]
    data = []
    if shutil.which("flatpak"):
        try:
            out = subprocess.run(["flatpak", "list", "--app", "--columns=application,installation"],
                                 capture_output=True, text=True, timeout=30).stdout
            for line in out.splitlines():
                parts = line.split()
                if parts and "." in parts[0]:
                    data.append((parts[0], parts[1] if len(parts) > 1 else "system"))
        except Exception:
            pass
    _fp.update(t=time.time(), data=data)
    return data


ARTCRAFT_SANDBOX = Path.home() / ".var/app/ai.storyteller.artcraft-launcher/data/artcraft-launcher/apps"


def external_installs(entry, pacq, rec=None) -> list:
    """The same app installed some other way: pacman/AUR package, Flatpak, a loose AppImage,
    ArtCraft Launcher's sandbox. → [{kind, id, label, scope?}]"""
    name = entry["repo"].split("/")[1].lower()
    ours_pkg = rec.get("pkgname") if rec and rec.get("format") == "pacman" else pkgname_of(entry)
    ours_path = rec.get("path") if rec and rec.get("format") == "appimage" else None
    out, seen = [], {ours_pkg}
    if pacq:
        for cand in (name, f"{name}-bin", f"{name}-appimage", f"{name}-git", f"{name}-nightly"):
            if cand in pacq and cand not in seen:
                out.append({"kind": "pacman", "id": cand, "label": f"pacman package {cand} {pacq[cand]}"})
                seen.add(cand)
        exe = Path("/usr/bin") / name
        if exe.exists():
            try:
                owner = subprocess.run(["pacman", "-Qqo", str(exe)], capture_output=True, text=True,
                                       timeout=20).stdout.strip()
            except Exception:
                owner = ""
            if owner and owner not in seen:
                out.append({"kind": "pacman", "id": owner, "label": f"pacman package {owner} (owns {exe})"})
                seen.add(owner)
    for app_id, inst in flatpak_apps():
        if app_id.lower().rsplit(".", 1)[-1] == name:
            out.append({"kind": "flatpak", "id": app_id, "scope": inst, "label": f"Flatpak {app_id} ({inst})"})
    dirs = {Path.home() / "Applications", appimage_root(), Path.home() / ".local/bin", Path.home() / "bin"}
    for d in dirs:
        if d.is_dir():
            for f in d.iterdir():
                if f.is_file() and f.suffix.lower() == ".appimage" and name in f.name.lower() \
                        and str(f) != ours_path:
                    out.append({"kind": "appimage", "id": str(f), "label": f"AppImage {f}"})
    if (ARTCRAFT_SANDBOX / name).is_dir():
        out.append({"kind": "dir", "id": str(ARTCRAFT_SANDBOX / name),
                    "label": "inside ArtCraft Launcher's Flatpak sandbox (remove it there)"})
    return out


def remove_external(item: dict, log):
    """Remove one install found by external_installs()."""
    kind = item["kind"]
    if kind == "pacman":
        run_root(["pacman", "-R", "--noconfirm", item["id"]], log)
    elif kind == "flatpak":
        scope = item.get("scope", "user")
        where = f"--{scope}" if scope in ("user", "system") else f"--installation={scope}"
        log(f"· flatpak uninstall {where} {item['id']}")
        r = subprocess.run(["flatpak", "uninstall", "-y", "--noninteractive", where, item["id"]],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=600)
        if r.returncode:
            raise InstallError((r.stdout.strip().splitlines() or ["flatpak uninstall failed"])[-1])
        _fp["t"] = 0.0
    elif kind == "appimage":
        p = Path(item["id"])
        p.unlink(missing_ok=True)
        if DESKTOP_DIR.is_dir():  # its menu entries (AppImageLauncher makes appimagekit_*.desktop)
            for dfile in DESKTOP_DIR.glob("*.desktop"):
                try:
                    if str(p) in dfile.read_text(errors="replace"):
                        dfile.unlink()
                except Exception:
                    pass
        refresh_desktop_caches()
    else:
        raise InstallError("remove this one by hand")


def _is_elf(f: Path) -> bool:
    try:
        with open(f, "rb") as fh:
            return fh.read(4) == b"\x7fELF"
    except Exception:
        return False


def rewrite_desktop(text: str, exec_cmd: str, icon=None, repo=None) -> str:
    out = []
    for line in text.splitlines():
        if line.startswith("Exec="):
            rest = line[5:].strip()
            if rest.startswith('"'):
                end = rest.find('"', 1)
                args = rest[end + 1:].strip() if end > 0 else ""
            else:
                parts = rest.split(None, 1)
                args = parts[1] if len(parts) > 1 else ""
            line = f"Exec={exec_cmd} {args}".rstrip()
        elif line.startswith("TryExec="):
            continue
        elif icon and line.startswith("Icon="):
            line = f"Icon={icon}"
        out.append(line)
        if repo and line.strip() == "[Desktop Entry]":
            out.append(f"X-Idus-GitHub-Updater={repo}")
    return "\n".join(out) + "\n"


def _png_size(p: Path) -> int:
    try:
        with open(p, "rb") as f:
            head = f.read(24)
        return int.from_bytes(head[16:20], "big") if head[:8] == b"\x89PNG\r\n\x1a\n" else 0
    except Exception:
        return 0


def _icon_slot(p: Path) -> str:
    try:
        with open(p, "rb") as f:
            head = f.read(256).lstrip()
    except Exception:
        return ""
    if head.startswith(b"<?xml") or head.startswith(b"<svg"):
        return "scalable"
    w = _png_size(p)
    return f"{w}x{w}" if w in (16, 22, 24, 32, 48, 64, 96, 128, 256, 512, 1024) else ("256x256" if w else "")


def slug_of(entry) -> str:
    return PKG_PREFIX + re.sub(r"[^a-z0-9]+", "-", entry["repo"].lower()).strip("-")


def pkgname_of(entry) -> str:
    name = re.sub(r"[^a-z0-9@._+-]", "-", entry["repo"].split("/")[1].lower()).lstrip("-.")
    return PKG_PREFIX + (name or "app")


def pkgver_of(tag: str) -> str:
    v = re.sub(r"^[^0-9]*", "", tag) or tag
    v = re.sub(r"(?<=\d)-(?=\d)", ".", v).replace("-", "")
    v = re.sub(r"[^A-Za-z0-9._+]", "_", v).strip("._") or "0"
    return v


# ---------------------------------------------------------------- getting the file
def ensure_asset_file(entry, release, asset, log, offline=False) -> Path:
    """Verified local copy: from the archive when it's there, else downloaded (into the archive
    when the app is archived, else into the cache)."""
    repo, tag = entry["repo"], release["tag"]
    hit = archived_file(repo, tag, asset["name"])
    if hit:
        log(f"· using archived {asset['name']} (SHA-256 ok)")
        return hit
    if offline:
        raise InstallError(f"{asset['name']} is not in the archive and you are offline")
    sha = expected_sha256(release, asset)
    if not sha:
        if CFG.get("require_checksum"):
            raise InstallError(f"no published SHA-256 for {asset['name']} — refusing "
                               "(Settings › Require a published checksum)")
        log(f"⚠️ no published checksum for {asset['name']} — continuing unverified")
    to_archive = bool(entry.get("archive"))
    d = archive_dir_for(repo, tag, platform_of(asset["name"])) if to_archive else CACHE_DIR / "downloads"
    log(f"⬇️ {asset['name']} ({human(asset['size'])})")
    path = download(asset, d / asset["name"], sha, log)
    log("✅ SHA-256 verified" if sha else "· downloaded")
    if to_archive:
        write_sums(d, {asset["name"]: sha or sha256_file(path)})
        write_release_meta(d, release, repo)
    return path


# ---------------------------------------------------------------- pacman: build from tarball
def _lib_owners(sonames: set) -> set:
    paths = [str(p) for p in (Path("/usr/lib") / s for s in sorted(sonames)) if p.exists()]
    if not paths or not shutil.which("pacman"):
        return set()
    r = subprocess.run(["pacman", "-Qqo", *paths], capture_output=True, text=True, timeout=60)
    return set(r.stdout.split())


SONAME_RE = re.compile(rb"(?<![A-Za-z0-9_.+-])(lib[A-Za-z0-9_+-]+(?:\.[A-Za-z0-9_+-]+)*?\.so(?:\.[0-9]+)*)(?![A-Za-z0-9_+-])")


def build_pacman_pkg(entry, release, tarball: Path, log) -> dict:
    """Wrap a release tarball in a real pacman package (igu-<name>) with makepkg.
    Layouts: <root>/bin+share → /usr · <root>/usr → / · anything else → /opt/<pkg> + /usr/bin link."""
    if os.geteuid() == 0:
        raise InstallError("makepkg can't run as root — start the updater as your normal user")
    for tool in ("makepkg", "fakeroot", "bsdtar"):
        if not shutil.which(tool):
            raise InstallError(f"{tool} is missing — sudo pacman -S --needed base-devel "
                               "(or switch this app to AppImage)")
    repo, tag = entry["repo"], release["tag"]
    pkgname, pkgver = pkgname_of(entry), pkgver_of(tag)
    bdir = CACHE_DIR / "build" / pkgname
    sha = sha256_file(tarball)
    stamp = f"{sha}|{pkgver}|{APP_VERSION}"
    meta_f = bdir / "build.json"
    try:
        meta = json.loads(meta_f.read_text())
        if meta.get("stamp") == stamp and Path(meta["pkgfile"]).is_file():
            log(f"· reusing built package {Path(meta['pkgfile']).name}")
            return meta
    except Exception:
        pass
    shutil.rmtree(bdir, ignore_errors=True)
    stage = bdir / "stage"
    stage.mkdir(parents=True)
    log(f"🔧 building {pkgname} {pkgver} with makepkg …")

    with tarfile.open(tarball) as tf:
        members = [m for m in tf.getmembers() if m.name not in (".", "./")]
        tops = {m.name.lstrip("./").split("/")[0] for m in members if m.name.lstrip("./")}
        if hasattr(tarfile, "data_filter"):
            tf.extractall(stage, filter="data")
        else:  # Python < 3.12: refuse anything that escapes the folder
            for m in members:
                if m.name.startswith("/") or ".." in Path(m.name).parts or m.issym() or m.islnk():
                    if m.issym() and not (os.path.isabs(m.linkname) or ".." in Path(m.linkname).parts):
                        continue
                    raise InstallError(f"unsafe path in tarball: {m.name}")
            tf.extractall(stage)
    root = next(iter(tops)) if len(tops) == 1 and (stage / next(iter(tops))).is_dir() else ""
    base = stage / root if root else stage
    subdirs = {p.name for p in base.iterdir() if p.is_dir()}
    if "usr" in subdirs:
        layout = "usr"
    elif (base / "bin").is_dir() or (base / "share").is_dir():
        layout = "prefix"
    else:
        layout = "opt"

    files = [f for f in base.rglob("*") if f.is_file() and not f.is_symlink()]
    elfs = [f for f in files if _is_elf(f)]
    bundled = {f.name for f in base.rglob("*") if ".so" in f.name}
    sonames = set()
    for f in elfs:
        try:
            sonames |= {m.decode() for m in SONAME_RE.findall(f.read_bytes())}
        except Exception:
            pass
    depends = sorted(d for d in _lib_owners(sonames - bundled) if not d.startswith(PKG_PREFIX))

    q = shlex.quote
    tarname = tarball.name
    body = ['  mkdir -p "$srcdir/igu-src"',
            f'  bsdtar -xf "$srcdir/{tarname}" -C "$srcdir/igu-src" --no-same-owner',
            '  chmod -R u+rwX,go+rX,go-w "$srcdir/igu-src"',
            '  cd "$srcdir/igu-src"' + (f"/{q(root)}" if root else "")]
    binaries, desktops = [], []
    if layout == "prefix":
        for d in sorted(subdirs):
            if d in ("bin", "sbin"):
                body += ['  install -d "$pkgdir/usr/bin"', f'  cp -a {d}/. "$pkgdir/usr/bin/"']
                binaries += [f"/usr/bin/{f.name}" for f in (base / d).iterdir() if f.is_file()]
            elif d in ("lib", "lib64"):
                body += ['  install -d "$pkgdir/usr/lib"', f'  cp -a {d}/. "$pkgdir/usr/lib/"']
            elif d in ("share", "libexec", "include"):
                body += ['  install -d "$pkgdir/usr"', f'  cp -a {d} "$pkgdir/usr/"']
            elif d in ("etc", "opt"):
                body += [f'  cp -a {d} "$pkgdir/"']
    elif layout == "usr":
        for d in ("usr", "etc", "opt"):
            if d in subdirs:
                body.append(f'  cp -a {d} "$pkgdir/"')
        body += ['  for d in local/bin local/sbin sbin; do [ -d "$pkgdir/usr/$d" ] && install -d "$pkgdir/usr/bin" '
                 '&& cp -a "$pkgdir/usr/$d/." "$pkgdir/usr/bin/" && rm -rf "$pkgdir/usr/$d"; done; true',
                 '  for d in local/lib lib64; do [ -d "$pkgdir/usr/$d" ] && install -d "$pkgdir/usr/lib" '
                 '&& cp -a "$pkgdir/usr/$d/." "$pkgdir/usr/lib/" && rm -rf "$pkgdir/usr/$d"; done; true',
                 '  [ -d "$pkgdir/usr/local/share" ] && cp -a "$pkgdir/usr/local/share" "$pkgdir/usr/"; '
                 'rm -rf "$pkgdir/usr/local"; true']
        for d in ("bin", "sbin"):
            if (base / "usr" / d).is_dir():
                binaries += [f"/usr/bin/{f.name}" for f in (base / "usr" / d).iterdir() if f.is_file()]
    else:
        rname = entry["repo"].split("/")[1].lower()
        execs = [f for f in files if os.access(f, os.X_OK) and ".so" not in f.name
                 and (f in elfs or f.read_bytes()[:2] == b"#!")]
        main = [f for f in execs if f.name.lower() == rname] or \
               [f for f in execs if f.name.lower().startswith(rname)] or (execs if len(execs) == 1 else [])
        if not main:
            raise InstallError("can't tell which program to start in this tarball — use the AppImage format")
        exe = min(main, key=lambda f: len(f.relative_to(base).parts))
        rel = exe.relative_to(base).as_posix()
        body += [f'  install -d "$pkgdir/opt/{pkgname}"', f'  cp -a . "$pkgdir/opt/{pkgname}/"',
                 '  install -d "$pkgdir/usr/bin"',
                 f'  ln -s {q(f"/opt/{pkgname}/{rel}")} "$pkgdir/usr/bin/{q(exe.name)}"']
        binaries = [f"/usr/bin/{exe.name}", f"/opt/{pkgname}/{rel}"]
        (bdir / "desktop").mkdir()
        for dfile in base.rglob("*.desktop"):
            text = dfile.read_text(errors="replace")
            m = re.search(r"^Icon=(.+)$", text, re.M)
            icon = None
            if m:
                cand = next((f for f in files if f.stem == m.group(1).strip() and f.suffix in (".png", ".svg")), None)
                if cand:
                    icon = f"/opt/{pkgname}/{cand.relative_to(base).as_posix()}"
            (bdir / "desktop" / dfile.name).write_text(rewrite_desktop(text, exe.name, icon))
            desktops.append(dfile.name)
            body.append(f'  install -Dm644 "$startdir/desktop/{q(dfile.name)}" '
                        f'"$pkgdir/usr/share/applications/{q(dfile.name)}"')
    for lic in sorted(p for p in base.iterdir() if p.is_file() and re.match(r"(LICEN[CS]E|COPYING)", p.name, re.I)):
        body.append(f'  install -Dm644 {q(lic.name)} "$pkgdir/usr/share/licenses/{pkgname}/{q(lic.name)}"')

    real = repo.split("/")[1].lower()
    desc = f"{entry['name']} {tag} from github.com/{repo} (installed by {APP_NAME})"
    pkgbuild = "\n".join([
        f"# Generated by {APP_NAME} v{APP_VERSION} — {REPO_URL}",
        f"# Source: https://github.com/{repo}/releases/tag/{tag}",
        f"pkgname={pkgname}",
        f"pkgver={pkgver}",
        "pkgrel=1",
        f"pkgdesc={q(desc)}",
        f"arch=({q(ARCH)})",
        f"url={q(f'https://github.com/{repo}')}",
        "license=('custom')",
        "depends=(" + " ".join(q(d) for d in depends) + ")",
        f"provides=({q(real)})",
        "conflicts=(" + " ".join(q(c) for c in (real, f"{real}-bin", f"{real}-appimage", f"{real}-git")) + ")",
        "options=('!strip' '!debug' '!lto' '!emptydirs')",
        f"source=({q(tarname)})",
        f"noextract=({q(tarname)})",
        f"sha256sums=({q(sha)})",
        "",
        "package() {",
        *body,
        "}",
        "",
    ])
    (bdir / "PKGBUILD").write_text(pkgbuild)
    try:
        os.link(tarball, bdir / tarname)
    except OSError:
        shutil.copy2(tarball, bdir / tarname)
    shutil.rmtree(stage, ignore_errors=True)
    env = dict(os.environ, PKGDEST=str(bdir / "out"), BUILDDIR=str(bdir / "work"),
               PKGEXT=".pkg.tar.zst", SRCDEST=str(bdir))
    (bdir / "out").mkdir(exist_ok=True)
    r = subprocess.run(["makepkg", "-f", "--nodeps", "--noconfirm", "--noprogressbar", "--nocolor"],
                       cwd=bdir, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                       timeout=1800)
    if r.returncode != 0:
        for ln in r.stdout.splitlines()[-15:]:
            log(f"   {ln}")
        raise InstallError(f"makepkg failed (exit {r.returncode}) — PKGBUILD is in {bdir}")
    pkgs = [p for p in (bdir / "out").glob(f"{pkgname}-*.pkg.tar.zst") if "-debug-" not in p.name]
    if not pkgs:
        raise InstallError("makepkg finished but produced no package")
    shutil.rmtree(bdir / "work", ignore_errors=True)
    (bdir / tarname).unlink(missing_ok=True)
    meta = {"stamp": stamp, "pkgfile": str(max(pkgs, key=lambda p: p.stat().st_mtime)), "pkgname": pkgname,
            "binaries": binaries, "depends": depends, "layout": layout}
    _atomic_write(meta_f, json.dumps(meta, indent=1))
    log(f"✅ built {Path(meta['pkgfile']).name}  (layout: {layout}; depends: {', '.join(depends) or '—'})")
    return meta


def native_pkg_info(path: Path) -> dict:
    """For releases that ship a ready .pkg.tar.zst."""
    r = subprocess.run(["pacman", "-Qip", str(path)], capture_output=True, text=True, timeout=60)
    m = re.search(r"^Name\s*:\s*(\S+)", r.stdout, re.M)
    if not m:
        raise InstallError(f"pacman can't read {path.name}")
    lst = subprocess.run(["pacman", "-Qlpq", str(path)], capture_output=True, text=True, timeout=60).stdout
    bins = [ln for ln in lst.splitlines() if ln.startswith("/usr/bin/") and not ln.endswith("/")]
    return {"pkgfile": str(path), "pkgname": m.group(1), "binaries": bins}


# ---------------------------------------------------------------- AppImage
def install_appimage(entry, release, src: Path, log) -> dict:
    root = appimage_root()
    root.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._+-]+", "-", entry["name"]).strip("-") or entry["repo"].split("/")[1]
    dest = root / f"{safe}.AppImage"
    tmp = dest.with_name(dest.name + ".new")
    shutil.copy2(src, tmp)
    os.chmod(tmp, 0o755)
    os.replace(tmp, dest)  # a running copy keeps its old inode, nothing breaks mid-session
    slug = slug_of(entry)
    files = []
    env = {k: v for k, v in os.environ.items() if k not in ("APPIMAGE", "APPDIR", "ARGV0", "OWD")}
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=CACHE_DIR) as td:
        sq = Path(td) / "squashfs-root"

        def extract(pattern):
            try:
                subprocess.run([str(dest), "--appimage-extract", pattern], cwd=td, env=env,
                               capture_output=True, timeout=180)
            except Exception:
                pass

        extract("*.desktop")
        desk = next(iter(sorted(sq.glob("*.desktop"))), None) if sq.is_dir() else None
        text = desk.read_text(errors="replace") if desk else ""
        m = re.search(r"^Icon=(.+)$", text, re.M)
        icon_val = m.group(1).strip() if m else ""
        icon_file = None
        if icon_val:
            for pat in (f"usr/share/icons/hicolor/scalable/apps/{icon_val}.svg",
                        f"usr/share/icons/hicolor/512x512/apps/{icon_val}.png",
                        f"usr/share/icons/hicolor/256x256/apps/{icon_val}.png",
                        f"{icon_val}.svg", f"{icon_val}.png"):
                extract(pat)
                if (sq / pat).is_file():
                    icon_file = sq / pat
                    break
        if not icon_file:
            extract(".DirIcon")
            p = sq / ".DirIcon"
            if p.is_symlink():
                target = os.readlink(p)
                extract(target)
                p = sq / target
            if p.is_file():
                icon_file = p
        icon_name = None
        if icon_file:
            slot = _icon_slot(icon_file)
            if slot:
                ext = ".svg" if slot == "scalable" else ".png"
                tgt = ICON_THEME_DIR / slot / "apps" / f"{slug}{ext}"
                tgt.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(icon_file, tgt)
                files.append(str(tgt))
                icon_name = slug
        quoted = '"' + str(dest).replace("\\", "\\\\").replace('"', '\\"') + '"'
        if text:
            new = rewrite_desktop(text, quoted, icon_name, entry["repo"])
        else:
            new = ("[Desktop Entry]\nType=Application\n"
                   f"Name={entry['name']}\nExec={quoted} %F\nIcon={icon_name or 'application-x-executable'}\n"
                   f"Terminal=false\nCategories=Utility;\nX-Idus-GitHub-Updater={entry['repo']}\n")
        DESKTOP_DIR.mkdir(parents=True, exist_ok=True)
        dpath = DESKTOP_DIR / f"{slug}.desktop"
        dpath.write_text(new)
        files.append(str(dpath))
    refresh_desktop_caches()
    log(f"✅ {entry['name']} → {dest}" + ("" if text else "  (no .desktop inside — made a basic menu entry)"))
    pq = pacman_query()
    if pq is not None and "fuse2" not in pq:
        log("ℹ️ if it won't start: some AppImages need FUSE 2 → sudo pacman -S --needed fuse2")
    return {"format": "appimage", "path": str(dest), "files": files}


def _remove_record_files(rec, log):
    if rec.get("format") == "idus":
        slug = Path(rec["path"]).name
        if slug == APP_SLUG:
            raise InstallError(f"{APP_NAME} can't uninstall itself — see Uninstall in its README")
        for f in (Path.home() / ".local/bin" / slug, DESKTOP_DIR / f"{slug}.desktop"):
            f.unlink(missing_ok=True)
        for f in ICON_THEME_DIR.glob(f"*/apps/{slug}.png"):
            f.unlink(missing_ok=True)
        shutil.rmtree(rec["path"], ignore_errors=True)
        refresh_desktop_caches()
    elif rec.get("format") == "appimage":
        for f in [rec.get("path")] + (rec.get("files") or []):
            if f:
                Path(f).unlink(missing_ok=True)
        refresh_desktop_caches()
    else:
        run_root(["pacman", "-R", "--noconfirm", rec["pkgname"]], log)


# ---------------------------------------------------------------- install / update / uninstall
def compute_target(entry, rels: list):
    """→ (release to install or None, note)"""
    if entry["format"] == "idus" and not is_idus(entry):
        return None, "“Idus installer” is only for Idus's own apps — pick pacman or AppImage"
    cands = channel_list(entry, rels)
    note = ""
    for r in cands:
        a, _cls = install_asset(r, entry["format"])
        if a:
            return r, note
        if not note:
            note = (f"{r['tag']} has no install-*.sh attached to the release" if entry["format"] == "idus" else
                    f"{r['tag']} has no {FORMAT_LABELS[FORMATS.index(entry['format'])]} build for {ARCH}")
    if entry["channel"] == "pinned" and not entry.get("pinned"):
        note = "Pinned, but no version chosen yet — ⋯ › Install another version"
    elif not rels:
        checked = ST["releases"].get(entry["repo"].lower())
        note = note or ("not on GitHub yet (or private)" if checked and checked.get("missing") else
                        "no releases published on GitHub yet" if checked else
                        "no releases known yet — press Check for updates")
    elif not cands and entry["channel"] == "stable":
        note = "no stable release yet — switch the channel to Latest to get pre-releases"
    return None, note


def repo_status(entry, pacq) -> dict:
    rels = known_releases(entry)
    rec = installed_record(entry, pacq)
    target, note = compute_target(entry, rels)
    inst = rec["version"] if rec else None
    fmt_switch = bool(rec and rec.get("format") != entry["format"])
    update = bool(rec and target and (target["tag"] != inst or fmt_switch)) and not entry.get("archive_only")
    hint = ""
    if entry["channel"] == "stable" and rels and rels[0]["pre"] and (not target or rels[0]["tag"] != target["tag"]):
        hint = f"pre-release {rels[0]['tag']} is out (channel Latest)"
    tags, size = archive_usage(entry["repo"])
    return {"installed": inst, "rec": rec, "target": target, "update": update, "note": note, "hint": hint,
            "external": external_installs(entry, pacq, rec), "archived": tags,
            "archive_size": size, "running": is_running(rec), "fmt_switch": fmt_switch,
            "releases": rels, "archive_only": bool(entry.get("archive_only")), "about": entry.get("about", "")}


def prepare(entry, release, log, offline=False) -> dict:
    """Download/verify (and for pacman: build) everything; nothing is installed yet."""
    asset, cls = install_asset(release, entry["format"])
    if not asset:
        raise InstallError(f"{release['tag']} has no {FORMAT_LABELS[FORMATS.index(entry['format'])]} build for {ARCH}")
    if entry.get("archive_only"):
        raise InstallError(f"archive only here — {entry.get('about') or 'install it on its own machine'}")
    rec = installed_record(entry, None)
    self_update = is_idus(entry) and entry["repo"].split("/")[1] == APP_SLUG
    if is_running(rec) and not self_update:
        raise InstallError(f"{entry['name']} is running — close it first")
    if cls == "installer":
        if not is_idus(entry):
            raise InstallError("the Idus installer format is only for Idus's own apps")
        files = [ensure_asset_file(entry, release, a, log, offline) for a in payload_assets(release)]
        return {"entry": entry, "release": release, "cls": cls, "src": files[0], "temp": False,
                "files": [str(f) for f in files], "script": asset["name"]}
    src = ensure_asset_file(entry, release, asset, log, offline)
    prep = {"entry": entry, "release": release, "cls": cls, "src": src,
            "temp": src.parent == CACHE_DIR / "downloads"}
    if cls == "tarball":
        prep.update(build_pacman_pkg(entry, release, src, log))
    elif cls == "pkg":
        prep.update(native_pkg_info(src))
    return prep


def _record(prep, rec, log):
    entry = prep["entry"]
    key = entry["repo"].lower()
    old = ST["installed"].get(key)
    rec.update(version=prep["release"]["tag"], date=dt.datetime.now().isoformat(timespec="seconds"))
    with _lock:
        ST["installed"][key] = rec
    save_state()
    if old and old.get("format") != rec["format"]:
        log(f"· removing the old {old.get('format')} install of {entry['name']}")
        try:
            _remove_record_files(old, log)
        except Exception as e:
            log(f"⚠️ could not remove the old install: {e}")
    if prep.get("temp"):
        Path(prep["src"]).unlink(missing_ok=True)


def commit(preps: list, log) -> int:
    """Install prepared items: every pacman package in ONE pacman -U (one password prompt)."""
    done = 0
    pac = [p for p in preps if p["cls"] in ("tarball", "pkg")]
    if pac:
        try:
            run_root(["pacman", "-U", "--noconfirm", *[p["pkgfile"] for p in pac]], log)
            for p in pac:
                _record(p, {"format": "pacman", "pkgname": p["pkgname"], "binaries": p.get("binaries", [])}, log)
                log(f"✅ {p['entry']['name']} {p['release']['tag']} installed (pacman: {p['pkgname']})")
                done += 1
        except InstallError as e:
            log(f"❌ pacman: {e}")
    for p in (p for p in preps if p["cls"] == "appimage"):
        try:
            _record(p, install_appimage(p["entry"], p["release"], Path(p["src"]), log), log)
            done += 1
        except Exception as e:
            log(f"❌ {p['entry']['name']}: {e}")
    for p in (p for p in preps if p["cls"] == "installer"):
        try:
            _record(p, run_idus_installer(p, log), log)
            done += 1
        except Exception as e:
            log(f"❌ {p['entry']['name']}: {e}")
    return done


def run_idus_installer(prep, log) -> dict:
    """Run the release's own install-<app>.sh — as you, from a clean folder holding the verified files."""
    entry, tag = prep["entry"], prep["release"]["tag"]
    slug = entry["repo"].split("/")[1]
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=CACHE_DIR) as td:
        for f in prep["files"]:
            shutil.copy2(f, Path(td) / Path(f).name)
        log(f"▶ running {prep['script']} from {tag} (as you, no root)")
        r = subprocess.run(["bash", prep["script"]], cwd=td, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=600)
        for ln in [ln for ln in r.stdout.splitlines() if ln.strip()][-15:]:
            log(f"   {ln}")
        if r.returncode:
            raise InstallError(f"{prep['script']} failed (exit {r.returncode})")
    for f in prep["files"]:
        if Path(f).parent == CACHE_DIR / "downloads":
            Path(f).unlink(missing_ok=True)
    log(f"✅ {entry['name']} {tag} installed" +
        (f" — restart {APP_NAME} to use it" if slug == APP_SLUG else ""))
    return {"format": "idus", "path": str(_XDG_DATA / slug), "script": str(_XDG_DATA / slug / f"{slug}.py")}


def act_install(entry, log, tag=None, offline=False, pin=False) -> bool:
    if entry.get("archive_only"):
        log(f"· {entry['name']}: archive only here — {entry.get('about') or 'install it on its own machine'}")
        return False
    try:
        rels, _src, problem = get_releases(entry, offline)
    except Offline:
        rels, offline = known_releases(entry), True
        problem = "offline"
    if problem:
        log(f"⚪ {entry['name']}: {problem} — using what is cached/archived")
        offline = offline or problem.startswith("offline")
    rels = sort_releases(rels + [r for r in archived_releases(entry["repo"]) if r["tag"] not in {x["tag"] for x in rels}])
    if tag:
        release = next((r for r in rels if r["tag"] == tag), None)
        if not release:
            log(f"❌ {entry['name']}: version {tag} not found")
            return False
    else:
        release, note = compute_target(entry, rels)
        if not release:
            log(f"❌ {entry['name']}: {note}")
            return False
    log(f"▶ {entry['name']} {release['tag']} ({FORMAT_LABELS[FORMATS.index(entry['format'])]})")
    try:
        prep = prepare(entry, release, log, offline)
    except (InstallError, NetError) as e:
        log(f"❌ {entry['name']}: {e}")
        return False
    if not commit([prep], log):
        return False
    if pin:
        entry["channel"], entry["pinned"] = "pinned", release["tag"]
        save_settings()
        log(f"📌 {entry['name']} pinned to {release['tag']}")
    if entry.get("archive") and not offline:
        sync_archive(entry, rels, log)
    return True


def act_update_all(log, offline=False, check_first=False) -> int:
    if check_first:
        offline = act_check(log, offline)["offline"]
    pacq = pacman_query()
    preps = []
    for e in (e for e in CFG["repos"] if not e.get("hidden")):
        s = repo_status(e, pacq)
        if not (s["update"] and s["target"]):
            continue
        if s["running"]:
            log(f"⏸ {e['name']} is running — skipped (close it and try again)")
            continue
        log(f"▶ {e['name']}: {s['installed']} → {s['target']['tag']}")
        try:
            preps.append(prepare(e, s["target"], log, offline))
        except (InstallError, NetError) as ex:
            log(f"❌ {e['name']}: {ex}")
    if not preps:
        log("✅ nothing to update")
        return 0
    n = commit(preps, log)
    log(f"✅ {n} of {len(preps)} updated")
    return n


def act_uninstall(entry, log) -> bool:
    key = entry["repo"].lower()
    rec = installed_record(entry, pacman_query())
    if not rec:
        log(f"· {entry['name']} isn't installed by {APP_NAME}")
        return False
    if is_running(rec):
        log(f"❌ {entry['name']} is running — close it first")
        return False
    try:
        _remove_record_files(rec, log)
    except InstallError as e:
        log(f"❌ {entry['name']}: {e}")
        return False
    with _lock:
        ST["installed"].pop(key, None)
    save_state()
    log(f"✅ {entry['name']} uninstalled (the archive is untouched)")
    return True


def act_check(log, offline=False, only=None) -> dict:
    entries = only if only is not None else [e for e in CFG["repos"] if not e.get("hidden")]
    live = 0
    rel_map = {}
    for e in entries:
        try:
            rels, src, problem = get_releases(e, offline)
        except Offline as ex:
            log(f"⚪ offline ({ex}) — showing cached data")
            offline = True
            rels, src, problem = known_releases(e), "cache", ""
        if problem.startswith("offline"):
            if not offline:
                log(f"⚪ {problem} — showing cached data")
            offline = True
        elif problem and not (problem.startswith("repository not found") and e.get("group") == "Idus"):
            log(f"⚠️ {e['name']}: {problem}")
        live += src == "live"
        rel_map[e["repo"]] = rels
    with _lock:
        if live:
            ST["last_check"] = time.time()
        ST["online"] = bool(live)
    save_state()
    pacq = pacman_query()
    updates = []
    for e in entries:
        s = repo_status(e, pacq)
        if s["update"]:
            updates.append((e, s))
            log(f"⬆️ {e['name']}: {s['installed']} → {s['target']['tag']}")
    if live:
        log(f"🔍 checked {live} repositories · {len(updates)} update(s) available")
    if CFG.get("sync_after_check") and not offline and any(e.get("archive") for e in entries):
        act_sync(log, rel_map)
    return {"live": live, "offline": offline, "updates": updates, "rel_map": rel_map}


def act_sync(log, rel_map=None, offline=False):
    new = pruned = 0
    for e in (e for e in CFG["repos"] if e.get("archive") and not e.get("hidden")):
        rels = (rel_map or {}).get(e["repo"])
        if rels is None:
            try:
                rels, _src, problem = get_releases(e, offline)
                offline = offline or problem.startswith("offline")
            except Offline:
                offline, rels = True, known_releases(e)
        n, p = sync_archive(e, rels, log, offline)
        new, pruned = new + n, pruned + p
    total = sum(archive_usage(e["repo"])[1] for e in CFG["repos"])
    log(f"📦 archive: {new} new file(s), {pruned} old version(s) pruned · {human(total)} in {archive_root()}")
    return new, pruned


def act_add(text: str, log, name=None, fmt=None, channel=None, archive=False):
    repo = parse_repo(text)
    if not repo:
        log(f"❌ '{text}' is not a GitHub repository (use owner/repo or a github.com link)")
        return None
    if any(r["repo"].lower() == repo.lower() for r in CFG["repos"]):
        log(f"· {repo} is already in the list")
        return None
    preset = next((p for p in PRESETS if p["repo"].lower() == repo.lower()), {})
    entry = dict(REPO_DEFAULTS)
    entry.update(preset)
    entry["repo"] = preset.get("repo", repo)
    entry["name"] = name or preset.get("name") or repo.split("/")[1]
    entry["format"] = "idus" if is_idus(entry) else (fmt or entry["format"])
    entry["channel"] = channel or entry["channel"]
    entry["archive"] = bool(archive)
    CFG["repos"].append(entry)
    CFG["repos"].sort(key=lambda r: 0 if r.get("group") == "Idus" else 1)
    CFG["removed_presets"] = [x for x in CFG.get("removed_presets", []) if x.lower() != repo.lower()]
    save_settings()
    log(f"➕ added {repo}")
    try:
        rels, _src, problem = get_releases(entry)
        if problem:
            log(f"⚠️ {problem}")
        if rels:
            newest = rels[0]
            kinds = sorted({classify(a["name"]) for a in newest["assets"]} - {None})
            log(f"· {len(rels)} release(s); newest {newest['tag']} has: {', '.join(kinds) or 'no Linux builds'}")
            if entry["format"] == "pacman" and not install_asset(newest, "pacman")[0] \
                    and install_asset(newest, "appimage")[0] and not fmt:
                entry["format"] = "appimage"
                save_settings()
                log("· no tarball/pacman package for your CPU — format set to AppImage")
    except NetError as e:
        log(f"⚠️ couldn't reach GitHub ({e}) — the repository is added, check later")
    save_state()
    return entry


def act_remove_other(entry, log) -> int:
    """Remove the copies of this app that were installed some other way (keeps ours)."""
    pacq = pacman_query()
    rec = installed_record(entry, pacq)
    items = external_installs(entry, pacq, rec)
    if not items:
        log(f"· {entry['name']}: no other install found")
        return 0
    n = 0
    for it in items:
        try:
            remove_external(it, log)
            log(f"✅ {entry['name']}: removed {it['label']}")
            n += 1
        except Exception as e:
            log(f"❌ {entry['name']}: {it['label']} — {e}")
    return n


def act_remove(entry, log):
    CFG["repos"] = [r for r in CFG["repos"] if r["repo"].lower() != entry["repo"].lower()]
    if any(p["repo"].lower() == entry["repo"].lower() for p in PRESETS):
        CFG.setdefault("removed_presets", []).append(entry["repo"])
    save_settings()
    log(f"➖ removed {entry['repo']} from the list (installed app and archive are untouched)")


def restore_presets():
    CFG["removed_presets"] = []
    have = {r["repo"].lower() for r in CFG["repos"]}
    for p in PRESETS:
        if p["repo"].lower() not in have:
            CFG["repos"].append(dict(REPO_DEFAULTS, **p))
    CFG["repos"].sort(key=lambda r: 0 if r.get("group") == "Idus" else 1)
    save_settings()


# ---------------------------------------------------------------- CLI
def print_table(log=print):
    pacq = pacman_query()
    rows = [("NAME", "INSTALLED", "AVAILABLE", "CHANNEL", "FORMAT", "ARCHIVE")]
    for e in (e for e in CFG["repos"] if not e.get("hidden")):
        s = repo_status(e, pacq)
        inst = (s["installed"] or ("other" if s["external"] else "—")) + \
            (" (+other)" if s["installed"] and s["external"] else "")
        avail = (s["target"]["tag"] if s["target"] else "—") + ("  ⬆" if s["update"] else "")
        ch = e["channel"] + (f" {e['pinned']}" if e["channel"] == "pinned" and e.get("pinned") else "")
        arch = (f"{len(s['archived'])} · {human(s['archive_size'])}" if s["archived"]
                else ("on" if e.get("archive") else "off"))
        rows.append((e["name"], inst, avail, ch, FORMAT_LABELS[FORMATS.index(e["format"])], arch))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for r in rows:
        log("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())


def cli():
    ap = argparse.ArgumentParser(prog=APP_SLUG, description=f"{APP_NAME} v{APP_VERSION} — CLI mode "
                                 "(no arguments opens the window)")
    ap.add_argument("--check", action="store_true", help="check every repository and print a table")
    ap.add_argument("--list", action="store_true", help="print the table from cache, no network")
    ap.add_argument("--sync", action="store_true", help="sync the offline archive")
    ap.add_argument("--update-all", action="store_true", help="install every available update")
    ap.add_argument("--install", metavar="REPO", help="install or update one app (owner/repo or name)")
    ap.add_argument("--tag", metavar="TAG", help="with --install: this exact version")
    ap.add_argument("--format", choices=FORMATS, help="with --install/--add: pacman or appimage (saved)")
    ap.add_argument("--pin", action="store_true", help="with --install: pin the app to that version")
    ap.add_argument("--uninstall", metavar="REPO")
    ap.add_argument("--add", metavar="OWNER/REPO", help="add a repository (a github.com link works too)")
    ap.add_argument("--name", help="with --add: display name")
    ap.add_argument("--remove", metavar="REPO", help="remove a repository from the list")
    ap.add_argument("--remove-other", metavar="REPO",
                    help="remove copies of an app installed some other way (Flatpak, AUR, loose AppImage)")
    ap.add_argument("--offline", action="store_true", help="use cache + archive only")
    ap.add_argument("--notify", action="store_true", help="desktop notification with the result")
    ap.add_argument("-V", "--version", action="version", version=f"{APP_NAME} v{APP_VERSION} · {BUILD}")
    a = ap.parse_args()
    if not any([a.check, a.list, a.sync, a.update_all, a.install, a.uninstall, a.add, a.remove,
                a.remove_other]):
        return False
    lines = []
    migrate_archive(print)

    def log(t):
        print(t, flush=True)
        lines.append(t)
        file_log(t)

    def need(query):
        e = find_repo(query)
        if not e:
            log(f"❌ no repository matches '{query}' — see --list")
        return e

    summary = ""
    if a.add:
        e = act_add(a.add, log, name=a.name, fmt=a.format)
        summary = f"Added {e['repo']}" if e else ""
    if a.remove and (e := need(a.remove)):
        act_remove(e, log)
    if a.remove_other and (e := need(a.remove_other)):
        n = act_remove_other(e, log)
        summary = f"{e['name']}: removed {n} other install(s)"
    if a.check:
        res = act_check(log, a.offline)
        print_table(log)
        ups = res["updates"]
        summary = (f"{len(ups)} update(s): " + ", ".join(f"{e['name']} {s['target']['tag']}" for e, s in ups)
                   if ups else "Everything is up to date")
    elif a.list:
        print_table(log)
    if a.sync:
        n, p = act_sync(log, offline=a.offline)
        summary = f"Archive: {n} new file(s), {p} old version(s) pruned"
    if a.install and (e := need(a.install)):
        if a.format and a.format != e["format"]:
            e["format"] = a.format
            save_settings()
        ok = act_install(e, log, tag=a.tag, offline=a.offline, pin=a.pin)
        summary = f"{e['name']}: " + ("installed" if ok else "failed — see the log")
    if a.update_all:
        n = act_update_all(log, a.offline, check_first=not a.offline)
        summary = f"{n} app(s) updated" if n else "Nothing to update"
    if a.uninstall and (e := need(a.uninstall)):
        ok = act_uninstall(e, log)
        summary = f"{e['name']}: " + ("uninstalled" if ok else "not uninstalled — see the log")
    if a.notify:
        errors = [ln for ln in lines if ln.startswith("❌")]
        notify(APP_NAME, (summary or (lines[-1] if lines else "Done")) +
               (f"\n{len(errors)} error(s) — see {LOG_FILE}" if errors else ""))
    return True


# ---------------------------------------------------------------- GUI (GTK4)
def gui():
    global GUI_MODE
    GUI_MODE = True
    import traceback
    import gi
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk, Gio, GLib, Pango  # noqa: E402

    S = 1.15
    V, H = Gtk.Orientation.VERTICAL, Gtk.Orientation.HORIZONTAL
    FILTERS = ["All", "Installed", "Updates", "Archived", "Not installed"]

    def sc(n):
        return int(round(n * S))

    def margins(w, top, side=None):
        side = top if side is None else side
        w.set_margin_top(top)
        w.set_margin_bottom(top)
        w.set_margin_start(side)
        w.set_margin_end(side)

    def label(text, *classes, xalign=0.0, **kw):
        lb = Gtk.Label(label=text, xalign=xalign, **kw)
        for c in classes:
            lb.add_css_class(c)
        return lb

    class App(Gtk.Application):
        def __init__(self):
            super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

        def do_activate(self):
            win = self.get_active_window()
            (win or Win(self)).present()

    # ------------------------------------------------------------ one repository row
    class RepoRow:
        MENU = [("notes", "📝  Release notes"), ("versions", "🕘  Install another version…"),
                ("github", "🌐  Open on GitHub"), ("folder", "📂  Open archive folder"),
                ("purge", "🧹  Delete archived files…"), ("other", "♻️  Remove the other install…"),
                ("uninstall", "🗑  Uninstall…"),
                ("hide", "🙈  Hide"), ("remove", "✖  Remove from list…")]

        def __init__(self, win, entry):
            self.win, self.entry, self.status, self._quiet = win, entry, None, True
            self.key = entry["repo"].lower()
            self.row = Gtk.ListBoxRow(activatable=False, selectable=False)
            self.row.set_name(self.key)
            self.row.add_css_class("reporow")
            box = Gtk.Box(orientation=V, spacing=sc(6))
            margins(box, sc(9), sc(12))
            self.row.set_child(box)

            l1 = Gtk.Box(orientation=H, spacing=sc(10))
            self.icon = Gtk.Image.new_from_icon_name("package-x-generic")
            self.icon.set_pixel_size(sc(36))
            l1.append(self.icon)
            names = Gtk.Box(orientation=V, hexpand=True, valign=Gtk.Align.CENTER)
            self.name_lbl = label("", "repo-name")
            self.sub_lbl = label("", "repo-sub", ellipsize=Pango.EllipsizeMode.END)
            names.append(self.name_lbl)
            names.append(self.sub_lbl)
            l1.append(names)
            self.state_lbl = label("", "state", xalign=1.0, valign=Gtk.Align.CENTER)
            l1.append(self.state_lbl)
            self.act_btn = Gtk.Button(label="Install", valign=Gtk.Align.CENTER)
            self.act_btn.add_css_class("rowbtn")
            self.act_btn.connect("clicked", lambda _b: self.on_action())
            l1.append(self.act_btn)
            box.append(l1)

            l2 = Gtk.Box(orientation=H, spacing=sc(8))
            l2.append(label("Channel", "field", valign=Gtk.Align.CENTER))
            self.chan_dd = Gtk.DropDown.new_from_strings(CHANNEL_LABELS)
            self.chan_dd.set_tooltip_text("Stable — releases not marked as pre-release\n"
                                          "Latest — the newest release, incl. alpha / beta / rc\n"
                                          "Pinned — stay on one version (⋯ › Install another version)")
            l2.append(self.chan_dd)
            l2.append(label("Format", "field", valign=Gtk.Align.CENTER))
            self.formats = ["idus"] if is_idus(entry) else ["pacman", "appimage"]
            self.fmt_dd = Gtk.DropDown.new_from_strings([FORMAT_LABELS[FORMATS.index(f)] for f in self.formats])
            self.fmt_dd.set_sensitive(len(self.formats) > 1)
            self.fmt_dd.set_tooltip_text(
                "Idus installer — downloads the release files, checks them and runs install-<app>.sh as you"
                if is_idus(entry) else
                "pacman — built locally into a real package (igu-…), removable with pacman\n"
                "AppImage — one file in your AppImage folder, no root")
            l2.append(self.fmt_dd)
            self.arch_chk = Gtk.CheckButton(label="Archive", valign=Gtk.Align.CENTER)
            self.arch_chk.set_tooltip_text("Keep the last versions of this app in the offline archive")
            l2.append(self.arch_chk)
            l2.append(label("Keep", "field", valign=Gtk.Align.CENTER))
            self.keep_spin = Gtk.SpinButton.new_with_range(1, 50, 1)
            self.keep_spin.set_valign(Gtk.Align.CENTER)
            self.keep_spin.set_tooltip_text("How many versions to keep in the archive "
                                            "(the newest stable is always kept as well)")
            l2.append(self.keep_spin)
            self.arch_lbl = label("", "repo-sub", hexpand=True, valign=Gtk.Align.CENTER)
            l2.append(self.arch_lbl)
            self.menu_btn = Gtk.MenuButton(label="⋯", valign=Gtk.Align.CENTER)
            self.menu_btn.add_css_class("rowmenu")
            pop = Gtk.Popover()
            pbox = Gtk.Box(orientation=V, spacing=sc(2))
            margins(pbox, sc(4))
            self.mi = {}
            for key, text in self.MENU:
                b = Gtk.Button(label=text)
                b.add_css_class("menuitem")
                b.get_child().set_xalign(0)
                b.connect("clicked", lambda _b, k=key: (pop.popdown(), self.on_menu(k)))
                pbox.append(b)
                self.mi[key] = b
            pop.set_child(pbox)
            self.menu_btn.set_popover(pop)
            l2.append(self.menu_btn)
            box.append(l2)

            self.note_lbl = label("", "note", wrap=True, visible=False)
            box.append(self.note_lbl)

            self.load_entry()
            self.chan_dd.connect("notify::selected", self.on_channel)
            self.fmt_dd.connect("notify::selected", self.on_format)
            self.arch_chk.connect("toggled", self.on_archive)
            self.keep_spin.connect("value-changed", self.on_keep)
            self._quiet = False

        def load_entry(self):
            e = self.entry
            self._quiet = True
            self.name_lbl.set_label(e["name"])
            self.chan_dd.set_selected(CHANNELS.index(e["channel"]))
            if e["format"] not in self.formats:
                e["format"] = self.formats[0]
            self.fmt_dd.set_selected(self.formats.index(e["format"]))
            self.arch_chk.set_active(bool(e.get("archive")))
            self.keep_spin.set_value(keep_count(e))
            self.keep_spin.set_sensitive(bool(e.get("archive")))
            self._quiet = False

        def set_icon(self, path):
            try:
                self.icon.set_from_file(path)
                self.icon.set_pixel_size(sc(36))
            except Exception:
                pass
            return False

        # ---- user changes
        def _changed(self):
            save_settings()
            self.win.refresh_rows([self])

        def on_channel(self, dd, _p):
            if self._quiet:
                return
            ch = CHANNELS[dd.get_selected()]
            if ch == "pinned" and not self.entry.get("pinned"):
                s = self.status or {}
                self.entry["pinned"] = s.get("installed") or (s["target"]["tag"] if s.get("target") else "")
                if self.entry["pinned"]:
                    self.win.log(f"📌 {self.entry['name']} pinned to {self.entry['pinned']} "
                                 "(⋯ › Install another version to pick another)")
            self.entry["channel"] = ch
            self._changed()

        def on_format(self, dd, _p):
            if self._quiet:
                return
            self.entry["format"] = self.formats[dd.get_selected()]
            self._changed()

        def on_archive(self, chk):
            if self._quiet:
                return
            self.entry["archive"] = chk.get_active()
            self.keep_spin.set_sensitive(chk.get_active())
            if chk.get_active():
                self.win.log(f"📦 {self.entry['name']}: archive on — it fills on the next "
                             "Sync archive (or after the next check)")
            self._changed()

        def on_keep(self, spin):
            if self._quiet:
                return
            v = int(spin.get_value())
            self.entry["keep"] = 0 if v == int(CFG.get("keep_default") or 3) else v
            self._changed()

        def on_action(self):
            self.win.run_job(lambda log: act_install(self.entry, log))

        def on_menu(self, key):
            e, w = self.entry, self.win
            if key == "notes":
                w.show_notes(self)
            elif key == "versions":
                w.show_versions(self)
            elif key == "github":
                open_uri(f"https://github.com/{e['repo']}/releases")
            elif key == "folder":
                p = app_archive_dir(e["repo"])
                p.mkdir(parents=True, exist_ok=True)
                open_uri(str(p))
            elif key == "purge":
                tags, size = archive_usage(e["repo"])
                w.confirm(f"Delete the archived files of {e['name']}?\n\n{len(tags)} version(s), {human(size)}.",
                          lambda: w.run_job(lambda log: log(f"🧹 {e['name']}: deleted "
                                                            f"{purge_archive(e['repo'], log)} archived version(s)")),
                          "Delete")
            elif key == "other":
                items = (self.status or {}).get("external") or []
                listing = "\n".join(f"• {it['label']}" for it in items)
                keep = (f"\n\nYour {APP_NAME} install ({self.status['installed']}) is kept."
                        if (self.status or {}).get("installed") else "")
                w.confirm(f"Remove the other install(s) of {e['name']}?\n\n{listing}{keep}",
                          lambda: w.run_job(lambda log: act_remove_other(e, log)), "Remove")
            elif key == "uninstall":
                extra = (f"\nIts folder ~/.local/share/{e['repo'].split('/')[1]} (with its settings) goes too."
                         if is_idus(e) else "")
                w.confirm(f"Uninstall {e['name']}?\n\nThe offline archive is kept.{extra}",
                          lambda: w.run_job(lambda log: act_uninstall(e, log)), "Uninstall")
            elif key == "hide":
                e["hidden"] = not e.get("hidden")
                save_settings()
                w.log(f"· {e['name']} {'hidden (Settings › Show hidden)' if e['hidden'] else 'visible again'}")
                self.update(self.status or {})
                w.apply_filter()
            elif key == "remove":
                w.confirm(f"Remove {e['repo']} from the list?\n\nAn installed app and its archive are left alone.",
                          lambda: (act_remove(e, w.log), w.rebuild_rows()), "Remove")

        # ---- show a status
        def update(self, s):
            self.status = s
            if not s:
                return
            e = self.entry
            sub = e["repo"] + (f" · {e['group']}" if e.get("group") else "") + (" · hidden" if e.get("hidden") else "")
            self.sub_lbl.set_label(sub)
            for c in ("ok", "warn", "crit", "muted"):
                self.state_lbl.remove_css_class(c)
            self.act_btn.remove_css_class("primary")
            tgt = s["target"]["tag"] if s["target"] else None
            btn = None
            if s["installed"] and s["update"]:
                extra = f"  ({FORMAT_LABELS[FORMATS.index(e['format'])]})" if s["fmt_switch"] else ""
                txt, cls = f"{s['installed']}  →  {tgt}{extra}", "warn"
                btn = "Switch" if (e["channel"] == "pinned" or s["fmt_switch"]) else "Update"
                self.act_btn.add_css_class("primary")
            elif s["installed"]:
                txt, cls = f"{s['installed']}  ✓", "ok"
            elif tgt and not s.get("archive_only"):
                txt, cls = tgt + ("  · pre" if s["target"]["pre"] else ""), "muted"
                btn = "Install"
            elif tgt:
                txt, cls = tgt + ("  · pre" if s["target"]["pre"] else ""), "muted"
            else:
                txt, cls = "—", "muted"
            self.state_lbl.set_label(txt)
            self.state_lbl.add_css_class(cls)
            self.act_btn.set_label(btn or ("Installed" if s["installed"] else
                                           "Archive only" if s.get("archive_only") else "—"))
            self.act_btn.set_sensitive(bool(btn) and not self.win.busy)
            n = len(s["archived"])
            self.arch_lbl.set_label(f"📦 {n} · {human(s['archive_size'])}" if n else
                                    ("📦 empty — Sync archive" if e.get("archive") else ""))
            notes = []
            if s.get("about"):
                notes.append(s["about"])
            if s["running"]:
                notes.append("▶ running")
            if s["external"]:
                what = ", ".join(it["label"] for it in s["external"])
                notes.append(("⚠ two copies — also installed as " if s["installed"] else
                              "already installed outside this app: ") + what + "  (⋯ › Remove the other install)")
            if s["note"]:
                notes.append(s["note"])
            if s["hint"]:
                notes.append(s["hint"])
            self.note_lbl.set_label("  ·  ".join(notes))
            self.note_lbl.set_visible(bool(notes))
            has_rel = bool(s["releases"])
            self.mi["notes"].set_sensitive(has_rel)
            self.mi["versions"].set_sensitive(has_rel and not self.win.busy)
            self.mi["folder"].set_sensitive(bool(n) or bool(e.get("archive")))
            self.mi["purge"].set_sensitive(bool(n) and not self.win.busy)
            self.mi["other"].set_sensitive(any(it["kind"] != "dir" for it in s["external"]) and not self.win.busy)
            self.mi["uninstall"].set_sensitive(bool(s["installed"]) and not self.win.busy)
            self.mi["hide"].set_label("👁  Show again" if e.get("hidden") else "🙈  Hide")
            self.mi["hide"].get_child().set_xalign(0)

        def visible(self, mode) -> bool:
            e, s = self.entry, self.status or {}
            if e.get("hidden") and not CFG.get("show_hidden"):
                return False
            if mode == "Installed":
                return bool(s.get("installed"))
            if mode == "Updates":
                return bool(s.get("update"))
            if mode == "Archived":
                return bool(e.get("archive") or s.get("archived"))
            if mode == "Not installed":
                return not s.get("installed")
            return True

    # ------------------------------------------------------------ main window
    class Win(Gtk.ApplicationWindow):
        def __init__(self, app):
            super().__init__(application=app, title=APP_NAME)
            self.set_default_size(sc(830), sc(940))
            try:
                st = Gtk.Settings.get_default()
                if any(pr.name == "gtk-interface-color-scheme" for pr in st.list_properties()):
                    st.set_property("gtk-interface-color-scheme", Gtk.InterfaceColorScheme.DARK)
                else:
                    st.set_property("gtk-application-prefer-dark-theme", True)
                if ICON_FILE.exists():
                    self.set_icon_name(APP_SLUG)
            except Exception:
                pass
            self.busy = False
            self.rows = {}
            self._buttons = []
            self._hourly_id = None

            root = Gtk.Box(orientation=V, spacing=sc(14))
            margins(root, sc(16))
            outer = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
            outer.set_child(root)
            self.set_child(outer)

            hb = Gtk.Box(orientation=V, spacing=sc(4), halign=Gtk.Align.CENTER)
            if ICON_FILE.exists():
                logo = Gtk.Image.new_from_file(str(ICON_FILE))
                logo.set_pixel_size(sc(85))
                hb.append(logo)
            hb.append(label(APP_NAME, "app-title", xalign=0.5))
            hb.append(label(f"v{APP_VERSION} · {BUILD}", "app-ver", xalign=0.5))
            root.append(hb)

            top = Gtk.Box(orientation=H, spacing=sc(8))
            top.append(label("Show:", valign=Gtk.Align.CENTER))
            self.filter_dd = Gtk.DropDown.new_from_strings(FILTERS)
            self.filter_dd.connect("notify::selected", lambda *_: self.apply_filter())
            top.append(self.filter_dd)
            self.status = label("", "status", xalign=1.0, hexpand=True, valign=Gtk.Align.CENTER)
            top.append(self.status)
            root.append(top)

            self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
            self.listbox.add_css_class("repolist")
            self.listbox.set_filter_func(self._filter)
            self.listbox.set_placeholder(label("Nothing to show — change the filter, or add a repository.",
                                               "repo-sub", xalign=0.5))
            ls = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
            ls.set_min_content_height(sc(190))  # at least one full row; the window scrolls below that
            ls.set_child(self.listbox)
            margins(ls, sc(6))
            self._frame(root, "1 · Repositories", ls, vexpand=True)

            g = Gtk.Grid(column_spacing=sc(8), row_spacing=sc(8), column_homogeneous=True)
            margins(g, sc(10))
            self._btn(g, 0, 0, "🔍  Check for updates", lambda: self.run_job(self._check_job),
                      "Ask GitHub for new releases (and sync the archive, if that's on in Settings)")
            self._btn(g, 0, 1, "⬆️  Update all", lambda: self.run_job(lambda lg: act_update_all(lg)),
                      "Install every update shown — all pacman packages in one password prompt")
            self._btn(g, 0, 2, "📦  Sync archive", lambda: self.run_job(lambda lg: act_sync(lg)),
                      "Download missing versions into the offline archive and prune old ones")
            self._btn(g, 1, 0, "➕  Add repository…", self.show_add)
            self._btn(g, 1, 1, "📂  Open archive folder",
                      lambda: (archive_root().mkdir(parents=True, exist_ok=True), open_uri(str(archive_root()))),
                      str(archive_root()))
            self._btn(g, 1, 2, "⚙️  Settings…", self.show_settings)
            self._frame(root, "2 · Actions", g)

            self.log_view = Gtk.TextView(editable=False, monospace=True, cursor_visible=False,
                                         wrap_mode=Gtk.WrapMode.WORD_CHAR)
            self.log_view.add_css_class("logbox")
            lsw = Gtk.ScrolledWindow()
            lsw.set_min_content_height(sc(110))
            lsw.set_child(self.log_view)
            root.append(lsw)

            foot = Gtk.Label()
            foot.set_markup(f'Made by Idus · <a href="{REPO_URL}">GitHub</a>'
                            f' · <a href="{KOFI_URL}">☕ Tip me on Ko-fi</a>')
            foot.set_halign(Gtk.Align.CENTER)
            foot.add_css_class("foot")
            foot.connect("activate-link", lambda _l, uri: (open_uri(uri), True)[1])
            root.append(foot)

            self._install_css()
            migrate_archive(self.log)
            self.rebuild_rows()
            self._set_hourly()
            if CFG.get("check_on_start"):
                GLib.timeout_add(500, lambda: (self.run_job(self._check_job), False)[1])

        # ---- plumbing
        def _frame(self, root, title, child, vexpand=False):
            frame = Gtk.Frame(label=title, vexpand=vexpand)
            frame.add_css_class("group")
            frame.set_child(child)
            root.append(frame)
            return frame

        def _btn(self, grid, r, c, text, cb, tip=""):
            b = Gtk.Button(label=text, hexpand=True)
            if tip:
                b.set_tooltip_text(tip)
            b.connect("clicked", lambda _w: cb())
            grid.attach(b, c, r, 1, 1)
            self._buttons.append(b)
            return b

        def log(self, text):
            file_log(text)

            def append():
                b = self.log_view.get_buffer()
                b.insert(b.get_end_iter(), text + "\n")
                mark = b.get_mark("igu-end") or b.create_mark("igu-end", b.get_end_iter(), False)
                b.move_mark(mark, b.get_end_iter())
                self.log_view.scroll_mark_onscreen(mark)  # a mark scrolls reliably, an iter doesn't
                return False
            GLib.idle_add(append)

        def set_busy(self, busy):
            self.busy = busy
            for b in self._buttons:
                b.set_sensitive(not busy)
            for r in self.rows.values():
                r.update(r.status)
            if busy:
                self.status.set_label("⏳ working …")

        def run_job(self, fn, after=None):
            if self.busy:
                self.log("· busy — wait for the current job to finish")
                return
            self.set_busy(True)
            self.log(f"— {dt.datetime.now():%H:%M:%S} —")

            def job():
                try:
                    fn(self.log)
                except Exception as e:  # noqa
                    self.log(f"❌ error: {e}")
                    file_log(traceback.format_exc())

                def done():
                    self.set_busy(False)
                    self.refresh_rows()
                    if after:
                        after()
                    return False
                GLib.idle_add(done)
            threading.Thread(target=job, daemon=True).start()

        def _check_job(self, log):
            res = act_check(log)
            ups = res["updates"]
            if CFG.get("notify") and ups:
                sig = ",".join(sorted(f"{e['repo']}@{s['target']['tag']}" for e, s in ups))
                if sig != ST.get("notified"):
                    notify(f"{len(ups)} update(s) available",
                           ", ".join(f"{e['name']} {s['target']['tag']}" for e, s in ups))
                    with _lock:
                        ST["notified"] = sig
                    save_state()

        def _set_hourly(self):
            if CFG.get("check_hourly") and not self._hourly_id:
                self._hourly_id = GLib.timeout_add_seconds(3600, self._hourly)
            elif not CFG.get("check_hourly") and self._hourly_id:
                GLib.source_remove(self._hourly_id)
                self._hourly_id = None

        def _hourly(self):
            if not self.busy:
                self.run_job(self._check_job)
            return True

        # ---- rows
        def rebuild_rows(self):
            while (child := self.listbox.get_first_child()) is not None:
                self.listbox.remove(child)
            self.rows = {}
            for e in CFG["repos"]:
                r = RepoRow(self, e)
                self.rows[r.key] = r
                self.listbox.append(r.row)
            self.refresh_rows()
            self.load_icons()

        def refresh_rows(self, rows=None):
            rows = list(self.rows.values()) if rows is None else rows

            def work():
                pacq = pacman_query()
                res = []
                for r in rows:
                    try:
                        res.append((r, repo_status(r.entry, pacq)))
                    except Exception:
                        file_log(f"status {r.key}: {traceback.format_exc()}")

                def apply():
                    for r, s in res:
                        r.update(s)
                    self.apply_filter()
                    self._update_status()
                    return False
                GLib.idle_add(apply)
            threading.Thread(target=work, daemon=True).start()

        def _update_status(self):
            if self.busy:
                return
            ups = sum(1 for r in self.rows.values() if r.status and r.status.get("update")
                      and not r.entry.get("hidden"))
            last = ST.get("last_check") or 0
            if not last:
                self.status.set_label("⚪ not checked yet")
                return
            t = dt.datetime.fromtimestamp(last)
            when = t.strftime("%H:%M") if t.date() == dt.date.today() else t.strftime("%d %b %H:%M")
            if ST.get("online", True):
                self.status.set_label(f"🟢 checked {when} · {ups} update{'s' if ups != 1 else ''}")
            else:
                self.status.set_label(f"⚪ offline · cached data from {when} · {ups} update{'s' if ups != 1 else ''}")

        def _filter(self, row):
            r = self.rows.get(row.get_name())
            return r.visible(FILTERS[self.filter_dd.get_selected()]) if r else True

        def apply_filter(self):
            self.listbox.invalidate_filter()

        def load_icons(self):
            rows = list(self.rows.values())

            def work():
                for r in rows:
                    e = r.entry
                    url = e.get("icon") or f"https://github.com/{e['repo'].split('/')[0]}.png?size=64"
                    p = CACHE_DIR / "icons" / (re.sub(r"[^a-z0-9]+", "-", r.key) + ".img")
                    if not p.exists() or time.time() - p.stat().st_mtime > 30 * 86400:
                        try:
                            code, _h, resp = _request(url, timeout=10)
                            with resp:
                                data = resp.read(2_000_000) if code == 200 else b""
                            if data[:8] == b"\x89PNG\r\n\x1a\n" or data[:3] == b"\xff\xd8\xff":
                                p.parent.mkdir(parents=True, exist_ok=True)
                                p.write_bytes(data)
                        except Exception:
                            pass
                    if p.exists():
                        GLib.idle_add(r.set_icon, str(p))
            threading.Thread(target=work, daemon=True).start()

        # ---- dialogs
        def confirm(self, text, on_yes, yes="Run"):
            dlg = Gtk.AlertDialog()
            dlg.set_message(text)
            dlg.set_buttons(["Cancel", yes])
            dlg.set_default_button(0)
            dlg.set_cancel_button(0)

            def cb(d, res):
                try:
                    if d.choose_finish(res) == 1:
                        on_yes()
                except Exception:
                    pass
            dlg.choose(self, None, cb)

        def _dialog(self, title, w=560, h=0):
            d = Gtk.Window(transient_for=self, modal=True, title=title)
            d.set_default_size(sc(w), sc(h) if h else -1)
            box = Gtk.Box(orientation=V, spacing=sc(10))
            margins(box, sc(16))
            sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True)
            sw.set_child(box)
            d.set_child(sw)
            box.append(label(title, "dlg-title"))
            return d, box

        def _button_row(self, box, buttons):
            hb = Gtk.Box(orientation=H, spacing=sc(8), halign=Gtk.Align.END, margin_top=sc(6))
            for text, cb, primary in buttons:
                b = Gtk.Button(label=text)
                if primary:
                    b.add_css_class("primary")
                b.connect("clicked", lambda _b, f=cb: f())
                hb.append(b)
            box.append(hb)
            return hb

        def show_notes(self, row):
            rels = (row.status or {}).get("releases") or known_releases(row.entry)
            d, box = self._dialog(f"{row.entry['name']} — release notes", 680, 620)
            tv = Gtk.TextView(editable=False, wrap_mode=Gtk.WrapMode.WORD_CHAR, cursor_visible=False)
            tv.add_css_class("logbox")
            parts = []
            for r in rels[:15]:
                head = f"{r['tag']}  ·  {short_date(r.get('published', ''))}" + ("  ·  pre-release" if r["pre"] else "")
                parts.append(f"{head}\n{'─' * 52}\n{(r.get('notes') or '(no release notes)').strip()}\n")
            tv.get_buffer().set_text("\n".join(parts) or "No releases known yet.")
            sw = Gtk.ScrolledWindow(vexpand=True)
            sw.set_child(tv)
            box.append(sw)
            self._button_row(box, [("Open on GitHub", lambda: open_uri(f"https://github.com/{row.entry['repo']}/releases"), False),
                                   ("Close", d.close, True)])
            d.present()

        def show_versions(self, row):
            e, s = row.entry, row.status or {}
            rels = sort_releases(list(s.get("releases") or []) or known_releases(e))
            d, box = self._dialog(f"{e['name']} — versions", 660, 560)
            box.append(label("Install any version. Archived versions (📦) install without a network. "
                             "“Install & pin” keeps the app on that version.", "dlg-help", wrap=True))
            lb = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
            lb.add_css_class("repolist")
            archived = s.get("archived") or {}
            for r in rels:
                rr = Gtk.ListBoxRow(activatable=False, selectable=False)
                rr.add_css_class("reporow")
                hb = Gtk.Box(orientation=H, spacing=sc(8))
                margins(hb, sc(6), sc(10))
                hb.append(label(r["tag"], "repo-name", valign=Gtk.Align.CENTER))
                hb.append(label(short_date(r.get("published", "")), "repo-sub", valign=Gtk.Align.CENTER))
                badges = []
                if r["pre"]:
                    badges.append(("pre-release", "warn"))
                if r["tag"] in archived:
                    badges.append(("📦 archived", "ok"))
                if r["tag"] == s.get("installed"):
                    badges.append(("installed", "ok"))
                if e["channel"] == "pinned" and r["tag"] == e.get("pinned"):
                    badges.append(("📌 pinned", "warn"))
                for text, cls in badges:
                    hb.append(label(text, "state", cls, valign=Gtk.Align.CENTER))
                hb.append(Gtk.Box(hexpand=True))
                ok = install_asset(r, e["format"])[0] is not None
                for text, pin in (("Install", False), ("Install & pin", True)):
                    b = Gtk.Button(label=text, valign=Gtk.Align.CENTER, sensitive=ok and not self.busy)
                    b.add_css_class("rowbtn")
                    if not ok:
                        b.set_tooltip_text(f"no {FORMAT_LABELS[FORMATS.index(e['format'])]} build in this release")
                    b.connect("clicked", lambda _b, t=r["tag"], p=pin: (
                        d.close(), self.run_job(lambda lg: act_install(e, lg, tag=t, pin=p))))
                    hb.append(b)
                rr.set_child(hb)
                lb.append(rr)
            sw = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
            sw.set_child(lb)
            box.append(sw)
            btns = [("Close", d.close, True)]
            if e["channel"] == "pinned":
                btns.insert(0, ("Unpin (back to Stable)", lambda: (
                    e.update(channel="stable", pinned=""), save_settings(), d.close(),
                    row.load_entry(), self.refresh_rows([row])), False))
            self._button_row(box, btns)
            d.present()

        def show_add(self):
            d, box = self._dialog("Add a repository", 560)
            box.append(label("Any public GitHub repository that publishes Linux builds as releases "
                             "(an AppImage, a tarball or a .pkg.tar.zst).", "dlg-help", wrap=True))
            g = Gtk.Grid(column_spacing=sc(10), row_spacing=sc(8))
            repo_e = Gtk.Entry(hexpand=True, placeholder_text="owner/repo  or  https://github.com/owner/repo")
            name_e = Gtk.Entry(placeholder_text="optional — defaults to the repository name")
            fmt_d = Gtk.DropDown.new_from_strings(FORMAT_LABELS[:2])
            ch_d = Gtk.DropDown.new_from_strings(CHANNEL_LABELS[:2])
            arch_c = Gtk.CheckButton(label="Keep versions in the offline archive")
            for i, (t, w) in enumerate((("Repository", repo_e), ("Name", name_e), ("Format", fmt_d),
                                        ("Channel", ch_d))):
                g.attach(label(t, "field", valign=Gtk.Align.CENTER), 0, i, 1, 1)
                g.attach(w, 1, i, 1, 1)
            g.attach(arch_c, 1, 4, 1, 1)
            box.append(g)
            msg = label("", "note", wrap=True)
            box.append(msg)

            def add():
                text = repo_e.get_text()
                if not parse_repo(text):
                    msg.set_label("That isn't a GitHub repository — use owner/repo or a github.com link.")
                    return
                d.close()
                fmt = FORMATS[fmt_d.get_selected()]
                self.run_job(lambda lg: act_add(text, lg, name=name_e.get_text().strip() or None,
                                                fmt=fmt if fmt_d.get_selected() else None,
                                                channel=CHANNELS[ch_d.get_selected()],
                                                archive=arch_c.get_active()),
                             after=self.rebuild_rows)
            repo_e.connect("activate", lambda _e: add())
            self._button_row(box, [("Cancel", d.close, False), ("Add", add, True)])
            d.present()

        def show_settings(self):
            d, box = self._dialog("Settings", 640)
            g = Gtk.Grid(column_spacing=sc(10), row_spacing=sc(8))
            box.append(g)
            row = 0

            def folder_row(title, key, help_text):
                nonlocal row
                e = Gtk.Entry(hexpand=True, text=CFG[key])
                b = Gtk.Button(label="Choose…")

                def choose():
                    fd = Gtk.FileDialog(title=f"Choose the {title.lower()}")
                    try:
                        p = Path(os.path.expanduser(e.get_text()))
                        if p.is_dir():
                            fd.set_initial_folder(Gio.File.new_for_path(str(p)))
                    except Exception:
                        pass

                    def cb(dlg, res):
                        try:
                            e.set_text(dlg.select_folder_finish(res).get_path())
                        except Exception:
                            pass
                    fd.select_folder(d, None, cb)
                b.connect("clicked", lambda _b: choose())
                g.attach(label(title, "field", valign=Gtk.Align.CENTER), 0, row, 1, 1)
                g.attach(e, 1, row, 1, 1)
                g.attach(b, 2, row, 1, 1)
                g.attach(label(help_text, "dlg-help", wrap=True), 1, row + 1, 2, 1)
                row += 2
                return e

            arch_e = folder_row("Archive folder", "archive_dir",
                                "Point it at a NAS mount if you like. Changing it doesn't move existing files.")
            appi_e = folder_row("AppImage folder", "appimage_dir",
                                "Avoid ~/Applications if AppImageLauncher watches it (duplicate menu entries).")
            keep = Gtk.SpinButton.new_with_range(1, 50, 1)
            keep.set_value(int(CFG.get("keep_default") or 3))
            keep.set_halign(Gtk.Align.START)
            g.attach(label("Versions to keep", "field", valign=Gtk.Align.CENTER), 0, row, 1, 1)
            g.attach(keep, 1, row, 1, 1)
            g.attach(label("Default per app — each app can override it in its row.", "dlg-help", wrap=True),
                     1, row + 1, 2, 1)
            row += 2
            also = Gtk.Box(orientation=H, spacing=sc(14))
            chk = {}
            for key, text in (("archive_other_linux", "the other Linux format"),
                              ("archive_windows", "Windows x64"), ("archive_macos", "macOS")):
                chk[key] = Gtk.CheckButton(label=text, active=bool(CFG.get(key)))
                also.append(chk[key])
            g.attach(label("Also archive", "field", valign=Gtk.Align.CENTER), 0, row, 1, 1)
            g.attach(also, 1, row, 2, 1)
            row += 1
            for key, text in (("check_on_start", "Check for updates when the app starts"),
                              ("check_hourly", "… and every hour while it is open"),
                              ("sync_after_check", "Sync the archive after each check"),
                              ("notify", "Desktop notification when updates are found"),
                              ("require_checksum", "Require a published SHA-256 checksum (recommended)"),
                              ("show_hidden", "Show hidden repositories")):
                chk[key] = Gtk.CheckButton(label=text, active=bool(CFG.get(key)))
                g.attach(chk[key], 1, row, 2, 1)
                row += 1
            tok = Gtk.PasswordEntry(show_peek_icon=True, hexpand=True)
            tok.set_text(CFG.get("github_token") or "")
            g.attach(label("GitHub token", "field", valign=Gtk.Align.CENTER), 0, row, 1, 1)
            g.attach(tok, 1, row, 2, 1)
            g.attach(label("Optional. Raises GitHub's limit from 60 to 5,000 requests an hour. A fine-grained "
                           "token with public read-only access is enough. Stored in the settings file (mode 600).",
                           "dlg-help", wrap=True), 1, row + 1, 2, 1)
            row += 2

            def save():
                old_dir = CFG["archive_dir"]
                CFG["archive_dir"] = arch_e.get_text().strip() or DEFAULT_SETTINGS["archive_dir"]
                CFG["appimage_dir"] = appi_e.get_text().strip() or DEFAULT_SETTINGS["appimage_dir"]
                CFG["keep_default"] = int(keep.get_value())
                for k, c in chk.items():
                    CFG[k] = c.get_active()
                CFG["github_token"] = tok.get_text().strip()
                save_settings()
                if CFG["archive_dir"] != old_dir:
                    self.log(f"· archive folder is now {archive_root()} (old files stay in {old_dir})")
                self.log("✅ settings saved")
                self._set_hourly()
                d.close()
                self.rebuild_rows()

            def restore():
                restore_presets()
                self.log("· default repositories restored")
                d.close()
                self.rebuild_rows()
            hb = self._button_row(box, [("Cancel", d.close, False), ("Save", save, True)])
            rb = Gtk.Button(label="Restore default repositories", hexpand=True, halign=Gtk.Align.START)
            rb.connect("clicked", lambda _b: restore())
            hb.prepend(rb)
            hb.set_halign(Gtk.Align.FILL)
            d.present()

        def _install_css(self):
            f = int(round(13 * S))
            css = f"""
            window {{ background-color: #16181d; color: #e8eaed; font-size: {f}px; }}
            .app-title {{ font-size: {int(28*S)}px; font-weight: 800; letter-spacing: 0.5px; color: #ffd9a0; }}
            .app-ver {{ font-size: {int(11*S)}px; color: #8a93a3; margin-top: -{sc(2)}px; }}
            .foot {{ font-size: {int(11*S)}px; color: #8a93a3; margin-top: {sc(4)}px; }}
            .foot a, .foot link {{ color: #ff9a3c; text-decoration: none; }}
            .status {{ color: #9fb4d4; }}
            frame.group > label {{ font-size: {int(13*S)}px; font-weight: 700; color: #8a93a3;
                                   margin-left: {sc(4)}px; }}
            frame.group {{ background-color: #1e222b; border-radius: {sc(12)}px;
                           border: 1px solid #2a2f3a; }}
            button {{ background-color: #262b36; background-image: none; color: #e8eaed; border-radius: {sc(8)}px;
                      border: 1px solid #333a47; padding: {sc(9)}px {sc(10)}px; box-shadow: none; }}
            button:hover {{ background-color: #2f3644; }}
            button:disabled {{ color: #5a6270; background-color: #20242d; }}
            button.primary {{ background-color: #ff9a3c; color: #16181d; font-weight: 700; border-color: #ff9a3c; }}
            button.primary:hover {{ background-color: #ffb066; }}
            button.primary:disabled {{ background-color: #6b5338; color: #16181d; border-color: #6b5338; }}
            button.rowbtn {{ padding: {sc(6)}px {sc(16)}px; min-width: {sc(86)}px; }}
            button.menuitem {{ background: transparent; border: none; padding: {sc(6)}px {sc(12)}px; }}
            button.menuitem:hover {{ background-color: #2f3644; }}
            menubutton.rowmenu > button {{ padding: {sc(3)}px {sc(12)}px; font-weight: 800; }}
            dropdown > button {{ padding: {sc(4)}px {sc(8)}px; }}
            spinbutton, entry, passwordentry {{ background-color: #0f1115; color: #e8eaed;
                      border: 1px solid #333a47; border-radius: {sc(8)}px; }}
            entry, passwordentry {{ padding: {sc(4)}px {sc(8)}px; }}
            spinbutton > button, spinbutton > button:disabled {{ padding: {sc(2)}px {sc(7)}px; border: none;
                      background-color: transparent; }}
            spinbutton > text {{ min-width: {sc(22)}px; }}
            popover > contents {{ background-color: #1e222b; color: #e8eaed; border: 1px solid #2a2f3a;
                                  border-radius: {sc(10)}px; }}
            popover > arrow {{ background-color: #1e222b; border-color: #2a2f3a; }}
            popover listview, popover listview > row {{ background-color: transparent; color: #e8eaed; }}
            popover listview > row:hover {{ background-color: #2f3644; }}
            scrollbar {{ background-color: transparent; border: none; }}
            scrollbar slider {{ margin: 0; min-width: {sc(6)}px; min-height: {sc(6)}px;
                                border: none; border-radius: {sc(4)}px; background-color: #3a4150; }}
            scrollbar slider:hover {{ background-color: #4a5366; }}
            list.repolist {{ background-color: transparent; }}
            list.repolist > row.reporow {{ background-color: #232833; border-radius: {sc(10)}px;
                                           border: 1px solid #2a2f3a; margin: {sc(3)}px {sc(2)}px; }}
            list.repolist > row.reporow:hover {{ background-color: #262c38; }}
            .repo-name {{ font-size: {int(15*S)}px; font-weight: 700; color: #e8eaed; }}
            .repo-sub {{ font-size: {int(11*S)}px; color: #8a93a3; }}
            .field {{ font-size: {int(11*S)}px; color: #8a93a3; }}
            .state {{ font-family: monospace; font-size: {int(12*S)}px; }}
            .ok {{ color: #46d160; }}
            .warn {{ color: #ffb13c; }}
            .crit {{ color: #e0525b; }}
            .muted {{ color: #8a93a3; }}
            .note {{ font-size: {int(11*S)}px; color: #9fb4d4; }}
            .logbox, .logbox text {{ background-color: #0f1115; color: #c7d1df; }}
            .logbox {{ padding: {sc(8)}px; border-radius: {sc(8)}px; }}
            checkbutton {{ color: #9fb4d4; font-size: {int(12*S)}px; }}
            .dlg-title {{ font-size: {int(18*S)}px; font-weight: 800; color: #ffd9a0; }}
            .dlg-help {{ font-size: {int(11*S)}px; color: #8a93a3; }}
            """
            provider = Gtk.CssProvider()
            provider.load_from_data(css.encode())
            Gtk.StyleContext.add_provider_for_display(
                self.get_display(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    App().run([sys.argv[0]])


if __name__ == "__main__":
    try:
        if not cli():
            gui()
    except BrokenPipeError:  # e.g. `idus-github-updater --list | head`
        os._exit(0)
    except KeyboardInterrupt:
        os._exit(130)
