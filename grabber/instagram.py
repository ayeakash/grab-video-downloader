"""Expand an Instagram profile into individual reel/video URLs.

yt-dlp ships an ``instagram:user`` extractor but currently reports it as broken,
so profile listing goes through instaloader instead. The actual media download
still happens in yt-dlp, which handles the per-post pages fine.
"""

from __future__ import annotations

from dataclasses import dataclass
from http.cookiejar import CookieJar

from .cookies import instagram_cookies


class InstagramError(RuntimeError):
    pass


@dataclass
class ProfileItem:
    url: str
    shortcode: str
    duration: float | None
    title: str
    date: str | None
    thumbnail: str = ""
    owner: str = ""


def _build_loader(jar: CookieJar | None):
    import instaloader

    loader = instaloader.Instaloader(
        quiet=True,
        download_pictures=False,
        download_videos=False,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        max_connection_attempts=3,
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


def list_profile_videos(
    username: str,
    jar: CookieJar | None = None,
    limit: int | None = None,
    prefer_reels: bool = True,
) -> list[ProfileItem]:
    """Return video posts for a profile, newest first."""
    try:
        import instaloader
        from instaloader.exceptions import InstaloaderException
    except ImportError as exc:  # pragma: no cover
        raise InstagramError("instaloader is not installed; run ./setup.sh") from exc

    loader = _build_loader(jar)
    logged_in = bool(getattr(loader.context, "username", None))

    try:
        profile = instaloader.Profile.from_username(loader.context, username)
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
    try:
        for post in _iter_posts(loader, profile, prefer_reels):
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
    except (InstaloaderException, OSError) as exc:
        # A partial listing is still worth downloading; only surface a hard
        # failure when nothing at all came back.
        if not items:
            raise InstagramError(_friendly(exc, username, logged_in)) from exc

    return items
