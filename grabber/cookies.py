"""One place to resolve cookies, shared by the yt-dlp engine and instaloader.

Instagram serves almost nothing to logged-out clients, and YouTube increasingly
asks for a session too, so most real use needs one of these paths.
"""

from __future__ import annotations

from http.cookiejar import CookieJar

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
