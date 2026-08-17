"""Expand an Instagram profile into individual reel/video URLs.

yt-dlp ships an ``instagram:user`` extractor but currently reports it as broken,
so profile listing goes through instaloader instead. The actual media download
still happens in yt-dlp, which handles the per-post pages fine.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from http.cookiejar import CookieJar

from .cookies import instagram_cookies

# How long a single HTTP request may hang. instaloader defaults this to 300s,
# which combined with its retries means a stalled call can block for a quarter
# of an hour with no output.
REQUEST_TIMEOUT = 20.0
CONNECTION_ATTEMPTS = 2

# instaloader's own 429 handler sleeps until its sliding window clears (~11
# minutes) with no output, which is what made the app look hung. Anonymous
# listing does eventually succeed after such a wait, so the waiting is kept --
# but it is capped, announced, and interruptible. Logged-in sessions should
# rarely be throttled at all, hence the much smaller budget.
MAX_SINGLE_WAIT = 90.0
ANON_TOTAL_WAIT = 420.0
AUTH_TOTAL_WAIT = 90.0

# Ceiling for the gallery-dl fallback, which runs as a subprocess.
GALLERY_DL_TIMEOUT = 300.0


class InstagramError(RuntimeError):
    pass


class InstagramAborted(Exception):
    """Raised to break out of instaloader when stopped or rate-limited.

    Deliberately not an InstaloaderException: those get caught and retried by
    instaloader's own error handling, which is exactly what we're escaping.
    """


@dataclass
class ProfileItem:
    url: str
    shortcode: str
    duration: float | None
    title: str
    date: str | None
    thumbnail: str = ""
    owner: str = ""


def _bounded_rate_controller(should_stop, budget: dict, progress):
    """A RateController whose waits are bounded, announced and interruptible.

    Instagram throttles unauthenticated listing hard, and instaloader's stock
    response is to sleep until its sliding window clears without printing a
    thing. The wait is legitimate -- the listing does succeed afterwards -- so
    it is preserved, but the user gets told it is happening and Stop works.
    """
    from instaloader import RateController

    class Bounded(RateController):
        def sleep(self, secs: float) -> None:
            allowance = budget["total"] - budget["slept"]
            if secs > MAX_SINGLE_WAIT or secs > allowance:
                raise InstagramAborted("rate-limited")
            progress(
                f"  Instagram rate limit — waiting {secs:.0f}s"
                + ("" if budget["logged_in"] else " (logging in avoids most of this)")
            )
            end = time.monotonic() + secs
            while time.monotonic() < end:
                if should_stop():
                    raise InstagramAborted("stopped")
                time.sleep(min(0.4, max(0.05, end - time.monotonic())))
            budget["slept"] += secs

    return Bounded


def _build_loader(jar: CookieJar | None, should_stop, budget: dict, progress=None):
    import instaloader

    loader = instaloader.Instaloader(
        quiet=True,
        download_pictures=False,
        download_videos=False,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        max_connection_attempts=CONNECTION_ATTEMPTS,
        request_timeout=REQUEST_TIMEOUT,
        rate_controller=_bounded_rate_controller(
            should_stop, budget, progress or (lambda _m: None)
        ),
    )

    cookies = instagram_cookies(jar)
    if cookies:
        session = loader.context._session
        for name, value in cookies.items():
            session.cookies.set(name, value, domain=".instagram.com", path="/")
        try:
            username = loader.test_login()
        except Exception:
            username = None
        if username:
            loader.context.username = username

    return loader


def _gallery_dl_profile(
    username: str, limit: int | None, cookies_browser: str | None, progress
) -> list[ProfileItem]:
    """List a profile's reels with gallery-dl instead of instaloader.

    instaloader resolves profiles through Instagram's web_profile_info
    endpoint, which currently returns HTTP 400 for accounts carrying a
    business category ("Asset ig_business_category_subvertical has been
    deleted"). That is an Instagram-side fault, identical logged in or out,
    so no amount of session fixing helps. gallery-dl reads the reels tab by a
    different route and is unaffected.
    """
    import json
    import subprocess
    import sys

    command = [sys.executable, "-m", "gallery_dl", "--dump-json", "--quiet"]
    if limit:
        command += ["--range", f"1-{limit}"]
    if cookies_browser:
        # gallery-dl's BROWSER[:PROFILE] spelling matches ours already.
        command += ["--cookies-from-browser", cookies_browser]
    command.append(f"https://www.instagram.com/{username}/reels/")

    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=GALLERY_DL_TIMEOUT
        )
    except FileNotFoundError as exc:
        raise InstagramError("gallery-dl is not installed; run ./setup.sh again") from exc
    except subprocess.TimeoutExpired as exc:
        raise InstagramError(
            f"Listing @{username} with gallery-dl timed out after "
            f"{GALLERY_DL_TIMEOUT:.0f}s. Set 'Max per page' to fetch fewer."
        ) from exc

    if not result.stdout.strip():
        detail = " ".join((result.stderr or "").split())[:200]
        raise InstagramError(
            f"Neither instaloader nor gallery-dl could list @{username}."
            + (f" gallery-dl said: {detail}" if detail else "")
        )

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise InstagramError(f"Could not read gallery-dl's output for @{username}") from exc

    items: list[ProfileItem] = []
    seen: set[str] = set()
    for entry in payload:
        meta = entry[-1] if isinstance(entry, list) and entry else None
        if not isinstance(meta, dict):
            continue
        code = meta.get("shortcode") or meta.get("post_shortcode")
        if not code or code in seen:
            continue
        seen.add(code)
        caption = (meta.get("description") or "").strip()
        date = str(meta.get("date") or "")[:10].replace("-", "")
        items.append(
            ProfileItem(
                url=meta.get("post_url") or f"https://www.instagram.com/reel/{code}/",
                shortcode=code,
                duration=None,
                title=caption.splitlines()[0][:80] if caption else code,
                date=date or None,
                thumbnail=meta.get("display_url") or "",
                owner=meta.get("username") or username,
            )
        )
        if limit and len(items) >= limit:
            break

    progress(f"  @{username}: gallery-dl found {len(items)} reel(s)")
    return items


def check_session(jar: CookieJar | None) -> dict:
    """Verify a browser's Instagram cookies and report who they belong to."""
    cookies = instagram_cookies(jar)
    if not cookies:
        return {
            "ok": False,
            "detail": "No Instagram cookies in that browser profile. "
            "Open instagram.com there, log in, then check again.",
        }
    if "sessionid" not in cookies:
        return {
            "ok": False,
            "detail": f"Found {len(cookies)} Instagram cookie(s) but no login session. "
            "You are probably signed out in that profile.",
        }
    try:
        loader = _build_loader(
            jar, lambda: False, {"slept": 0.0, "total": AUTH_TOTAL_WAIT, "logged_in": True}
        )
    except Exception as exc:
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}

    name = getattr(loader.context, "username", None)
    if name:
        return {"ok": True, "detail": f"Logged in as @{name}", "username": name}
    return {
        "ok": False,
        "detail": "Instagram rejected these cookies. Re-open instagram.com in that "
        "browser profile to refresh the session, then check again.",
    }


def _iter_posts(loader, profile, want_reels_only: bool):
    """Prefer the dedicated reels feed, fall back to the full post grid."""
    if want_reels_only and hasattr(profile, "get_reels"):
        try:
            yield from profile.get_reels()
            return
        except Exception:
            # Reels endpoint needs a session and changes often; fall through.
            pass
    yield from profile.get_posts()


LOGIN_HINT = (
    "Log in to Instagram in your browser, then re-run with "
    "--cookies-browser chrome (or safari/firefox/edge)."
)


def _friendly(exc: Exception, username: str, logged_in: bool) -> str:
    """Translate instaloader's exceptions into something actionable.

    Instagram serves almost nothing anonymously, so the overwhelmingly common
    cause of any failure here is a missing session rather than a real fault.
    """
    from instaloader.exceptions import (
        LoginRequiredException,
        PrivateProfileNotFollowedException,
        ProfileNotExistsException,
        QueryReturnedBadRequestException,
        QueryReturnedForbiddenException,
        TooManyRequestsException,
    )

    if isinstance(exc, ProfileNotExistsException):
        return (
            f"No profile named @{username} — or Instagram is hiding it from a "
            f"logged-out client. {LOGIN_HINT}"
            if not logged_in
            else f"No Instagram profile named @{username}."
        )
    if isinstance(exc, TooManyRequestsException):
        return (
            f"Instagram is rate-limiting this account. Wait 10-15 minutes, then "
            f"retry — lower --jobs also helps."
        )
    if isinstance(exc, PrivateProfileNotFollowedException):
        return f"@{username} is private and your account does not follow them."
    if isinstance(
        exc,
        (
            LoginRequiredException,
            QueryReturnedForbiddenException,
            QueryReturnedBadRequestException,
        ),
    ):
        if logged_in:
            return (
                f"Instagram rejected the request for @{username} ({type(exc).__name__}). "
                "The session may be stale — re-open Instagram in your browser and retry."
            )
        return f"Instagram requires a logged-in session to list @{username}. {LOGIN_HINT}"
    return f"{type(exc).__name__}: {exc}"


def _is_instagram_schema_bug(exc: Exception) -> bool:
    """Detect Instagram's broken web_profile_info response.

    Business-category accounts currently make that endpoint return HTTP 400
    with an "Asset ... has been deleted. You cannot use this schema" body.
    It is unrelated to authentication -- it fails identically with and
    without a session -- so it must not be reported as a login problem.
    """
    text = str(exc)
    return "has been deleted" in text and "schema" in text


def _aborted_message(exc: InstagramAborted, username: str) -> str:
    if str(exc) == "stopped":
        return f"Stopped while listing @{username}."
    return (
        f"Instagram kept rate-limiting the listing of @{username}, so it was "
        f"abandoned rather than waiting indefinitely. {LOGIN_HINT} Logging in "
        "makes this far faster; otherwise wait a few minutes and retry."
    )


def list_profile_videos(
    username: str,
    jar: CookieJar | None = None,
    limit: int | None = None,
    prefer_reels: bool = True,
    should_stop=None,
    on_progress=None,
    cookies_browser: str | None = None,
) -> list[ProfileItem]:
    """Return video posts for a profile, newest first."""
    try:
        import instaloader
        from instaloader.exceptions import InstaloaderException
    except ImportError as exc:  # pragma: no cover
        raise InstagramError("instaloader is not installed; run ./setup.sh") from exc

    stop = should_stop or (lambda: False)
    progress = on_progress or (lambda _msg: None)

    # Anonymous listing is not blocked outright: Instagram throttles it, but it
    # does succeed after waiting, and for some profiles that is the only route a
    # user has. It is merely slow and fragile, so say so up front.
    has_session = "sessionid" in instagram_cookies(jar)
    budget = {
        "slept": 0.0,
        "total": AUTH_TOTAL_WAIT if has_session else ANON_TOTAL_WAIT,
        "logged_in": has_session,
    }
    if not has_session:
        progress(
            f"  No Instagram session — listing @{username} anonymously, which "
            f"Instagram throttles heavily. {LOGIN_HINT}"
        )

    loader = _build_loader(jar, stop, budget, progress)
    logged_in = bool(getattr(loader.context, "username", None))
    budget["logged_in"] = logged_in
    if has_session and not logged_in:
        progress("  Those cookies were rejected; continuing without a session.")

    try:
        profile = instaloader.Profile.from_username(loader.context, username)
    except InstagramAborted as exc:
        raise InstagramError(_aborted_message(exc, username)) from exc
    except (InstaloaderException, OSError) as exc:
        if _is_instagram_schema_bug(exc):
            progress(
                f"  Instagram's profile endpoint is broken for @{username} "
                "(a fault on their side, not your login) — trying gallery-dl instead."
            )
        else:
            progress(f"  instaloader could not list @{username}: {exc}")
        try:
            return _gallery_dl_profile(username, limit, cookies_browser, progress)
        except InstagramError as fallback_exc:
            if _is_instagram_schema_bug(exc):
                raise fallback_exc from exc
            raise InstagramError(_friendly(exc, username, logged_in)) from exc

    try:
        if profile.is_private and not profile.followed_by_viewer:
            raise InstagramError(
                f"@{username} is private. Use an account that follows them. {LOGIN_HINT}"
                if not logged_in
                else f"@{username} is private and your account does not follow them."
            )
    except InstaloaderException:
        pass  # privacy fields need a session; let the listing itself decide

    items: list[ProfileItem] = []
    scanned = 0
    try:
        for post in _iter_posts(loader, profile, prefer_reels):
            if stop():
                break
            scanned += 1
            # Listing a large profile takes minutes of paging; without this the
            # UI looks frozen and people assume it has hung.
            if scanned % 12 == 0:
                progress(f"  @{username}: scanned {scanned}, found {len(items)} video(s)…")
            if not post.is_video:
                continue
            caption = (post.caption or "").strip()
            try:
                thumb = post.url or ""
            except Exception:
                thumb = ""
            items.append(
                ProfileItem(
                    url=f"https://www.instagram.com/reel/{post.shortcode}/",
                    shortcode=post.shortcode,
                    duration=post.video_duration,
                    title=caption.splitlines()[0][:80] if caption else post.shortcode,
                    date=post.date_utc.strftime("%Y%m%d") if post.date_utc else None,
                    thumbnail=thumb,
                    owner=username,
                )
            )
            if limit and len(items) >= limit:
                break
    except InstagramAborted as exc:
        if not items:
            raise InstagramError(_aborted_message(exc, username)) from exc
        progress(f"  @{username}: stopped early, keeping {len(items)} found so far")
    except (InstaloaderException, OSError) as exc:
        # A partial listing is still worth downloading; only surface a hard
        # failure when nothing at all came back.
        if not items:
            raise InstagramError(_friendly(exc, username, logged_in)) from exc

    return items
