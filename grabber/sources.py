"""Turn whatever the user pasted into a normalized, classified Source."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse, urlunparse

YOUTUBE_HOSTS = {
    "youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtube-nocookie.com",
    "youtu.be",
}
INSTAGRAM_HOSTS = {"instagram.com", "instagr.am", "ddinstagram.com"}

# Channel tabs yt-dlp understands as playlist pages.
CHANNEL_TABS = ("shorts", "videos", "streams", "live", "playlists", "featured")

# Instagram paths that are pages of the site rather than someone's profile.
IG_RESERVED = {
    "p", "reel", "reels", "tv", "stories", "s", "share", "explore", "accounts",
    "direct", "about", "developer", "legal", "privacy", "terms", "sitemap",
    "web", "graphql", "api", "challenge", "emails", "session", "oauth",
}

# Query params that are pure tracking noise and break archive de-duplication.
JUNK_PARAMS = {
    "si", "feature", "pp", "ab_channel", "igsh", "igshid", "img_index",
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "app", "_nc_cat",
}

PLATFORM_DIRS = {"youtube": "YouTube", "instagram": "Instagram", "other": "Other"}


@dataclass
class Source:
    """One thing the user asked for, before it is expanded into videos."""

    raw: str
    url: str
    platform: str  # youtube | instagram | other
    kind: str  # video | channel | playlist | profile | story | unknown
    label: str
    tab: str | None = None  # for channels: which tab this URL points at

    @property
    def is_container(self) -> bool:
        """True when this needs expanding into many videos."""
        return self.kind in ("channel", "playlist", "profile")


def _base_host(netloc: str) -> str:
    host = netloc.lower().split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host


def _clean_query(query: str, keep: set[str] | None = None) -> str:
    if not query:
        return ""
    pairs = parse_qs(query, keep_blank_values=False)
    out = []
    for key, values in pairs.items():
        if key in JUNK_PARAMS:
            continue
        if keep is not None and key not in keep:
            continue
        out.extend(f"{key}={v}" for v in values)
    return "&".join(out)


def _rebuild(parsed, path: str, query: str = "") -> str:
    return urlunparse((parsed.scheme or "https", parsed.netloc, path, "", query, ""))


def classify(raw: str) -> Source:
    """Map one line of user input to a Source. Never raises."""
    text = raw.strip().strip("<>\"'")
    if not text:
        return Source(raw, "", "other", "unknown", "empty")

    # Explicit prefixes let the user disambiguate a bare username.
    forced = None
    lowered = text.lower()
    for prefix, platform in (("yt:", "youtube"), ("ig:", "instagram")):
        if lowered.startswith(prefix):
            forced = platform
            text = text[len(prefix):].strip()
            break

    if forced == "instagram" and "/" not in text and "." not in text:
        text = f"https://www.instagram.com/{text.lstrip('@')}/"
    elif forced == "youtube" and "/" not in text and "." not in text:
        handle = text if text.startswith("@") else f"@{text}"
        text = f"https://www.youtube.com/{handle}"
    elif text.startswith("@") and "/" not in text:
        # A bare @handle with no other hint: YouTube is the likelier intent.
        text = f"https://www.youtube.com/{text}"

    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", text):
        text = "https://" + text.lstrip("/")

    parsed = urlparse(text)
    host = _base_host(parsed.netloc)
    path = parsed.path.rstrip("/")

    if host in YOUTUBE_HOSTS or host.endswith(".youtube.com"):
        return _classify_youtube(raw, parsed, host, path)
    if host in INSTAGRAM_HOSTS or host.endswith(".instagram.com"):
        return _classify_instagram(raw, parsed, path)

    # Unknown host: hand it to yt-dlp and let it try — but only if this really
    # looks like a hostname. Multiple links can share a line, so input is split
    # on whitespace, and without this check a stray word ("not a url at all")
    # turns into five bogus https://word targets.
    if not _plausible_host(host):
        return Source(raw, "", "other", "unknown", raw.strip())
    return Source(raw, text, "other", "video", host)


def _plausible_host(host: str) -> bool:
    """A dotted name with a letter-ish TLD, or a bare IP address."""
    if not host or host.startswith(".") or host.endswith("."):
        return False
    if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", host):
        return True
    labels = host.split(".")
    if len(labels) < 2 or not all(labels):
        return False
    return bool(re.fullmatch(r"[a-z]{2,}", labels[-1]))


def _classify_youtube(raw: str, parsed, host: str, path: str) -> Source:
    query = parse_qs(parsed.query)
    segments = [s for s in path.split("/") if s]

    # youtu.be/<id>
    if host == "youtu.be" and segments:
        vid = segments[0]
        return Source(raw, f"https://www.youtube.com/watch?v={vid}", "youtube", "video", vid)

    # /watch?v=<id>  — a watch URL that also carries a list= is still one video.
    if segments and segments[0] == "watch" and "v" in query:
        vid = query["v"][0]
        return Source(raw, f"https://www.youtube.com/watch?v={vid}", "youtube", "video", vid)

    # /shorts/<id>, /live/<id>, /embed/<id>, /v/<id>
    if len(segments) >= 2 and segments[0] in ("shorts", "live", "embed", "v"):
        vid = segments[1]
        return Source(raw, f"https://www.youtube.com/watch?v={vid}", "youtube", "video", vid)

    # /playlist?list=<id>  or any bare list=
    if "list" in query:
        pid = query["list"][0]
        return Source(
            raw, f"https://www.youtube.com/playlist?list={pid}", "youtube", "playlist", pid
        )

    # Channel forms: /@handle, /channel/UC..., /c/<name>, /user/<name>
    channel_root = None
    tail: list[str] = []
    if segments:
        if segments[0].startswith("@"):
            channel_root = "/" + segments[0]
            tail = segments[1:]
        elif segments[0] in ("channel", "c", "user") and len(segments) >= 2:
            channel_root = "/" + "/".join(segments[:2])
            tail = segments[2:]

    if channel_root:
        tab = tail[0] if tail and tail[0] in CHANNEL_TABS else None
        url = f"https://www.youtube.com{channel_root}" + (f"/{tab}" if tab else "")
        return Source(raw, url, "youtube", "channel", channel_root.lstrip("/"), tab=tab)

    clean = _clean_query(parsed.query)
    return Source(raw, _rebuild(parsed, parsed.path, clean), "youtube", "unknown", path or "youtube")


def _classify_instagram(raw: str, parsed, path: str) -> Source:
    segments = [s for s in path.split("/") if s]
    if not segments:
        return Source(raw, "https://www.instagram.com/", "instagram", "unknown", "instagram")

    head = segments[0].lower()

    # Single post / reel / IGTV item.
    if head in ("p", "reel", "reels", "tv") and len(segments) >= 2:
        # /reels/<code> (singular content) vs /<user>/reels (a profile tab) —
        # the former always has the code directly after the keyword.
        code = segments[1]
        kind_path = "reel" if head in ("reel", "reels") else head
        return Source(
            raw, f"https://www.instagram.com/{kind_path}/{code}/", "instagram", "video", code
        )

    # New-style share links redirect to the real post; let yt-dlp follow them.
    if head in ("share", "s") and len(segments) >= 2:
        return Source(raw, f"https://www.instagram.com/{'/'.join(segments)}/",
                      "instagram", "video", segments[-1])

    # /stories/<user>/... — a story or highlight reel.
    if head == "stories" and len(segments) >= 2:
        return Source(raw, f"https://www.instagram.com/{'/'.join(segments)}/",
                      "instagram", "story", segments[1])

    if head in IG_RESERVED:
        return Source(raw, f"https://www.instagram.com/{'/'.join(segments)}/",
                      "instagram", "unknown", head)

    # Otherwise it is a profile, possibly with a tab suffix we can ignore.
    user = segments[0].lstrip("@")
    return Source(raw, f"https://www.instagram.com/{user}/", "instagram", "profile", user)


def parse_input(lines: list[str]) -> tuple[list[Source], list[str]]:
    """Classify many lines, dropping blanks/comments and de-duplicating.

    Returns (sources, skipped_lines).
    """
    sources: list[Source] = []
    skipped: list[str] = []
    seen: set[str] = set()

    for line in lines:
        # Allow several links pasted on one line, and inline comments.
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        for token in re.split(r"[\s,]+", stripped):
            if not token or token.startswith("#"):
                continue
            src = classify(token)
            if not src.url or src.kind == "unknown":
                skipped.append(token)
                continue
            if src.url in seen:
                continue
            seen.add(src.url)
            sources.append(src)

    return sources, skipped
