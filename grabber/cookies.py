"""One place to resolve cookies, shared by the yt-dlp engine and instaloader.

Instagram serves almost nothing to logged-out clients, and YouTube increasingly
asks for a session too, so most real use needs one of these paths.
"""

from __future__ import annotations

import configparser
import json
from http.cookiejar import CookieJar
from pathlib import Path

SUPPORTED_BROWSERS = (
    "chrome", "chromium", "brave", "edge", "safari", "firefox", "opera", "vivaldi", "whale",
)


class CookieError(RuntimeError):
    pass


def browser_spec(name: str | None) -> tuple | None:
    """Build the (browser, profile, keyring, container) tuple yt-dlp expects."""
    if not name:
        return None
    parts = name.split(":", 1)
    browser = parts[0].strip().lower()
    if browser not in SUPPORTED_BROWSERS:
        raise CookieError(
            f"Unknown browser {browser!r}. Pick one of: {', '.join(SUPPORTED_BROWSERS)}"
        )
    profile = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None
    return (browser, profile, None, None)


def ydl_cookie_opts(cookies_file: str | None, cookies_browser: str | None) -> dict:
    """yt-dlp params for the configured cookie source. File wins over browser."""
    if cookies_file:
        return {"cookiefile": cookies_file}
    spec = browser_spec(cookies_browser)
    if spec:
        return {"cookiesfrombrowser": spec}
    return {}


def load_jar(cookies_file: str | None, cookies_browser: str | None) -> CookieJar | None:
    """Load cookies into a plain CookieJar, reusing yt-dlp's extractors.

    Returns None when no cookie source is configured.
    """
    if not cookies_file and not cookies_browser:
        return None

    from yt_dlp import YoutubeDL
    from yt_dlp.cookies import load_cookies

    opts = ydl_cookie_opts(cookies_file, cookies_browser)
    with YoutubeDL({"quiet": True, "no_warnings": True, **opts}) as ydl:
        try:
            return load_cookies(
                opts.get("cookiefile"), opts.get("cookiesfrombrowser"), ydl
            )
        except Exception as exc:  # keyring denied, locked DB, missing profile...
            raise CookieError(f"Could not read cookies: {exc}") from exc


# macOS data directories, relative to ~/Library/Application Support.
CHROMIUM_DIRS = {
    "chrome": "Google/Chrome",
    "chromium": "Chromium",
    "brave": "BraveSoftware/Brave-Browser",
    "edge": "Microsoft Edge",
    "vivaldi": "Vivaldi",
    "opera": "com.operasoftware.Opera",
}


def _chromium_profiles(browser: str, root: Path) -> list[dict]:
    """Read profile directory names out of Local State (plaintext JSON).

    Only metadata is touched here -- no cookie database is opened, so this
    never triggers the macOS keychain prompt.
    """
    found: list[dict] = []
    try:
        state = json.loads((root / "Local State").read_text(encoding="utf-8"))
        cache = state.get("profile", {}).get("info_cache", {})
    except (OSError, json.JSONDecodeError):
        cache = {}

    for directory, meta in sorted(cache.items()):
        if not (root / directory).is_dir():
            continue
        nice = (meta or {}).get("name") or directory
        found.append({"value": f"{browser}:{directory}", "label": f"{browser.title()} — {nice}"})

    if not found and (root / "Default").is_dir():
        found.append({"value": browser, "label": browser.title()})
    return found


def _firefox_profiles(root: Path) -> list[dict]:
    found: list[dict] = []
    parser = configparser.ConfigParser()
    try:
        parser.read(root / "profiles.ini")
    except (OSError, configparser.Error):
        return found
    for section in parser.sections():
        if not section.lower().startswith("profile"):
            continue
        name = parser.get(section, "Name", fallback=None)
        if name:
            found.append({"value": f"firefox:{name}", "label": f"Firefox — {name}"})
    if not found and root.is_dir():
        found.append({"value": "firefox", "label": "Firefox"})
    return found


def list_browser_profiles() -> list[dict]:
    """Enumerate installed browsers and their profiles, for the UI dropdown.

    A browser with several profiles is the usual reason an Instagram session
    "isn't found": the bare browser name only ever means its default profile.
    """
    support = Path.home() / "Library" / "Application Support"
    options: list[dict] = []

    for browser, relative in CHROMIUM_DIRS.items():
        root = support / relative
        if root.is_dir():
            options.extend(_chromium_profiles(browser, root))

    firefox = support / "Firefox"
    if firefox.is_dir():
        options.extend(_firefox_profiles(firefox))

    if (Path.home() / "Library" / "Cookies").is_dir() or Path("/Applications/Safari.app").exists():
        options.append({"value": "safari", "label": "Safari"})

    return options


def instagram_cookies(jar: CookieJar | None) -> dict[str, str]:
    """Pull just the instagram.com cookies out of a jar, as name -> value."""
    if jar is None:
        return {}
    found: dict[str, str] = {}
    for cookie in jar:
        domain = (cookie.domain or "").lstrip(".")
        if domain.endswith("instagram.com") and cookie.value:
            found[cookie.name] = cookie.value
    return found
