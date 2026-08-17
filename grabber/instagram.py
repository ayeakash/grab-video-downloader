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

# instaloader's own 429 handler sleeps until its sliding window clears, which
# is up to ~11 minutes, silently. We would rather fail in seconds and tell the
# user to log in, so any single wait and the total waiting are both capped.
MAX_SINGLE_WAIT = 12.0
MAX_TOTAL_WAIT = 45.0


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


def _bounded_rate_controller(should_stop, budget: dict):
    """A RateController that refuses to sleep for minutes at a time.

    Instagram answers unauthenticated listing calls with 429, and instaloader's
    stock response is to sleep until its sliding window clears. Capping the wait
    turns a silent ten-minute stall into a fast, explainable failure, and gives
    the Stop button something to interrupt.
    """
    from instaloader import RateController

    class Bounded(RateController):
        def sleep(self, secs: float) -> None:
            if secs > MAX_SINGLE_WAIT or budget["slept"] + secs > MAX_TOTAL_WAIT:
                raise InstagramAborted("rate-limited")
            end = time.monotonic() + secs
            while time.monotonic() < end:
                if should_stop():
                    raise InstagramAborted("stopped")
                time.sleep(min(0.4, end - time.monotonic()))
            budget["slept"] += secs

    return Bounded


def _build_loader(jar: CookieJar | None, should_stop, budget: dict):
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
        rate_controller=_bounded_rate_controller(should_stop, budget),
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
        loader = _build_loader(jar, lambda: False, {"slept": 0.0})
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


def _aborted_message(exc: InstagramAborted, username: str) -> str:
    if str(exc) == "stopped":
        return f"Stopped while listing @{username}."
    return (
        f"Instagram is rate-limiting this account, so listing @{username} was "
        "abandoned rather than waiting out its ~10 minute cooldown. Wait a few "
        "minutes and retry, or set 'Max per page' to fetch fewer at a time."
    )


def list_profile_videos(
    username: str,
    jar: CookieJar | None = None,
    limit: int | None = None,
    prefer_reels: bool = True,
    should_stop=None,
    on_progress=None,
) -> list[ProfileItem]:
    """Return video posts for a profile, newest first."""
    try:
        import instaloader
        from instaloader.exceptions import InstaloaderException
    except ImportError as exc:  # pragma: no cover
        raise InstagramError("instaloader is not installed; run ./setup.sh") from exc

    stop = should_stop or (lambda: False)
    progress = on_progress or (lambda _msg: None)

    # Bail out before touching the network when there is no session. Instagram
    # answers anonymous profile queries with 429/400, and the retry-and-sleep
    # that follows is what used to stall the app for ten minutes.
    if "sessionid" not in instagram_cookies(jar):
        raise InstagramError(
            f"Instagram needs a logged-in session to list @{username}. {LOGIN_HINT}"
        )

    budget = {"slept": 0.0}
    loader = _build_loader(jar, stop, budget)
    logged_in = bool(getattr(loader.context, "username", None))
    if not logged_in:
        raise InstagramError(
            f"Your Instagram cookies were rejected, so @{username} cannot be listed. "
            "Re-open Instagram in your browser to refresh the session, then retry."
        )

    try:
        profile = instaloader.Profile.from_username(loader.context, username)
    except InstagramAborted as exc:
        raise InstagramError(_aborted_message(exc, username)) from exc
    except (InstaloaderException, OSError) as exc:
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
