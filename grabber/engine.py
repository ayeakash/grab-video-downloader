"""Expansion and download, both built on yt-dlp."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .cookies import ydl_cookie_opts
from .sources import PLATFORM_DIRS, Source

QUALITY_RES = {"2160": 2160, "1440": 1440, "1080": 1080, "720": 720, "480": 480}


@dataclass
class VideoTask:
    url: str
    platform: str
    origin: str  # label of the Source it came from
    title: str = ""
    video_id: str = ""
    duration: float | None = None
    uploader: str = ""
    thumbnail: str = ""
    upload_date: str = ""  # YYYYMMDD when known


@dataclass
class Outcome:
    task: VideoTask
    status: str  # downloaded | skipped | filtered | failed
    detail: str = ""
    path: str | None = None


@dataclass
class Report:
    downloaded: list[Outcome] = field(default_factory=list)
    skipped: list[Outcome] = field(default_factory=list)
    filtered: list[Outcome] = field(default_factory=list)
    failed: list[Outcome] = field(default_factory=list)

    def add(self, outcome: Outcome) -> None:
        getattr(self, outcome.status).append(outcome)

    @property
    def total(self) -> int:
        return len(self.downloaded) + len(self.skipped) + len(self.filtered) + len(self.failed)


class _Silent:
    """yt-dlp logger that swallows output so it cannot fight the progress bar."""

    def __init__(self) -> None:
        self.errors: list[str] = []

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        self.errors.append(str(msg))


def format_selector(cfg: Config, platform: str = "youtube") -> tuple[str, list[str]]:
    """Return (format expression, format_sort preferences).

    Sorting prefers H.264 over AV1/VP9: the codec matters more than raw bitrate
    here, because AV1 files choke most editors and Apple hardware decoders, and
    a bulk shorts library is usually headed somewhere other than a browser.
    """
    if cfg.audio_only:
        return "bestaudio/best", ["acodec:m4a", "abr"]

    # The cap goes through format_sort's "res" (the *smallest* dimension) rather
    # than a [height<=N] filter. Shorts are vertical, so a 1080p short is
    # 1080x1920 -- filtering on height would reject it and quietly settle for
    # 480x854. Resolution outranks codec so a 1080p AV1 still beats a 720p H.264;
    # the h264 preference only breaks ties at equal resolution.
    target = QUALITY_RES.get(str(cfg.quality))
    sort = [f"res:{target}" if target else "res", "vcodec:h264", "ext:mp4:m4a", "br"]

    if platform == "instagram":
        # Instagram publishes one pre-muxed h264 file plus a DASH ladder at the
        # same resolution. Measured, the progressive file beats DASH by ~20%
        # despite being slightly larger, because picking DASH costs a second
        # request and an ffmpeg merge per reel. It also avoids landing vp9,
        # which most editors dislike. Selecting quality here is pointless --
        # every progressive format id returns the identical file.
        return "b/bv*+ba", sort

    return "bv*+ba/b", sort


def length_ok(duration: float | None, cfg: Config) -> bool:
    """Apply the shorts/long filter. Unknown duration always passes."""
    if duration is None or cfg.length_mode == "all":
        return True
    if cfg.length_mode == "shorts":
        return duration <= cfg.short_seconds
    if cfg.length_mode == "long":
        return duration > cfg.short_seconds
    return True


def _match_filter(cfg: Config, state: dict | None = None):
    """Length filter that also records its reason.

    yt-dlp returns None from extract_info both when a filter rejects the video
    and when the archive already has it. Recording the reason here is what lets
    the caller tell those two apart instead of guessing.
    """

    def check(info, *, incomplete=False):
        duration = info.get("duration")
        if not length_ok(duration, cfg):
            noun = "longer" if cfg.length_mode == "shorts" else "shorter"
            reason = f"{duration:.0f}s is {noun} than the {cfg.short_seconds}s cutoff"
            if state is not None:
                state["filtered"] = reason
            return f"{info.get('id', 'video')}: {reason}"
        return None

    return check


def load_archive_ids(path: Path) -> set[str]:
    """Read a yt-dlp archive file ("<extractor> <id>" per line) into an id set."""
    if not path.exists():
        return set()
    ids: set[str] = set()
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                parts = line.split()
                if len(parts) >= 2:
                    ids.add(parts[1])
                elif parts:
                    ids.add(parts[0])
    except OSError:
        return set()
    return ids


def base_opts(cfg: Config) -> dict:
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "consoletitle": False,
        "ignoreerrors": False,
        "retries": cfg.retries,
        "fragment_retries": cfg.retries,
        "extractor_retries": 3,
        "socket_timeout": 30,
        "nocheckcertificate": False,
        "restrictfilenames": False,
        "windowsfilenames": True,  # keeps names portable to external drives
        # No trim_file_name here: yt-dlp applies it to the whole path, so a deep
        # output directory would silently eat the [id] suffix. The %(title).70B
        # in the template is what bounds the length.
    }
    opts.update(ydl_cookie_opts(cfg.cookies_file, cfg.cookies_browser))
    return opts


def download_opts(cfg: Config, platform: str, state: dict | None = None) -> dict:
    out_root = Path(cfg.out_dir) / PLATFORM_DIRS.get(platform, "Other")
    name_tmpl = (
        "%(uploader,channel,uploader_id,id)s/"
        "%(upload_date>%Y-%m-%d,release_date>%Y-%m-%d,epoch>%Y-%m-%d|undated)s "
        "%(title).70B [%(id)s].%(ext)s"
    )

    fmt, fmt_sort = format_selector(cfg, platform)
    opts = base_opts(cfg)
    opts.update(
        {
            "outtmpl": {"default": str(out_root / name_tmpl)},
            "format": fmt,
            "format_sort": fmt_sort,
            "merge_output_format": "mp4",
            "concurrent_fragment_downloads": 4,
            "match_filter": _match_filter(cfg, state),
            "writethumbnail": cfg.write_thumbnail,
            "writeinfojson": cfg.write_info_json,
            "overwrites": False,
            "continuedl": True,
        }
    )

    if cfg.use_archive:
        cfg.archive_path.parent.mkdir(parents=True, exist_ok=True)
        opts["download_archive"] = str(cfg.archive_path)

    if cfg.since or cfg.until:
        from yt_dlp.utils import DateRange

        opts["daterange"] = DateRange(cfg.since, cfg.until)

    postprocessors = []
    if cfg.audio_only:
        postprocessors.append(
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        )
    if cfg.embed_metadata:
        postprocessors.append({"key": "FFmpegMetadata", "add_metadata": True})
    if cfg.write_thumbnail and not cfg.audio_only:
        postprocessors.append({"key": "EmbedThumbnail", "already_have_thumbnail": False})
    if postprocessors:
        opts["postprocessors"] = postprocessors

    if platform == "instagram":
        opts["sleep_interval"] = cfg.instagram_sleep
        opts["max_sleep_interval"] = cfg.instagram_sleep * 2

    return opts


# --------------------------------------------------------------------------
# Expansion
# --------------------------------------------------------------------------


def expand_source(
    src: Source, cfg: Config, jar=None, on_note=None, should_stop=None, on_error=None
) -> list[VideoTask]:
    """Turn one Source into concrete video tasks.

    on_error reports a source failing outright, as distinct from a note. The
    caller needs that separation: "resolved nothing because it failed" and
    "resolved nothing because you already have it all" must not look alike.
    """
    note = on_note or (lambda _msg: None)
    fail = on_error or (lambda _msg: None)

    if src.platform == "instagram" and src.kind == "profile":
        return _expand_instagram_profile(src, cfg, jar, note, should_stop, fail)

    if src.is_container:
        return _expand_with_ytdlp(src, cfg, note, fail)

    return [VideoTask(url=src.url, platform=src.platform, origin=src.label, video_id=src.label)]


def _expand_instagram_profile(
    src: Source, cfg: Config, jar, note, should_stop=None, fail=None
) -> list[VideoTask]:
    from .instagram import InstagramError, list_profile_videos

    note(f"Listing reels for @{src.label}…")
    try:
        items = list_profile_videos(
            src.label,
            jar=jar,
            limit=cfg.limit,
            should_stop=should_stop,
            on_progress=note,
            cookies_browser=cfg.cookies_browser,
        )
    except InstagramError as exc:
        note(f"[red]@{src.label}: {exc}[/red]")
        if fail:
            fail(f"@{src.label}: {exc}")
        return []

    tasks = []
    for item in items:
        if not length_ok(item.duration, cfg):
            continue
        tasks.append(
            VideoTask(
                url=item.url,
                platform="instagram",
                origin=f"@{src.label}",
                title=item.title,
                video_id=item.shortcode,
                duration=item.duration,
                uploader=item.owner or src.label,
                thumbnail=item.thumbnail,
                upload_date=item.date or "",
            )
        )
    note(f"@{src.label}: {len(tasks)} video(s) to consider")
    return tasks


def _channel_tab_urls(src: Source, cfg: Config) -> list[str]:
    if src.kind != "channel" or src.tab:
        return [src.url]
    tabs = cfg.channel_tabs or ["shorts"]
    if "all" in tabs:
        tabs = ["shorts", "videos", "streams"]
    return [f"{src.url}/{tab}" for tab in tabs]


def _expand_with_ytdlp(src: Source, cfg: Config, note, fail=None) -> list[VideoTask]:
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError

    tasks: list[VideoTask] = []
    seen: set[str] = set()

    for url in _channel_tab_urls(src, cfg):
        opts = base_opts(cfg)
        opts.update(
            {
                "extract_flat": "in_playlist",
                "skip_download": True,
                "lazy_playlist": True,
                "ignoreerrors": True,
                "logger": _Silent(),
            }
        )
        if cfg.limit:
            opts["playlistend"] = cfg.limit

        tab = url.rsplit("/", 1)[-1] if src.kind == "channel" else None
        label = f"{src.label}/{tab}" if tab else src.label
        note(f"Listing {label}")

        try:
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except DownloadError as exc:
            note(f"[yellow]{label}: {exc}[/yellow]")
            if fail:
                fail(f"{label}: {exc}")
            continue
        except Exception as exc:
            note(f"[yellow]{label}: {exc}[/yellow]")
            if fail:
                fail(f"{label}: {exc}")
            continue

        if not info:
            continue

        found = 0
        wrong_length = 0
        for entry in _walk_entries(info):
            vid = entry.get("id") or ""
            vurl = entry.get("url") or entry.get("webpage_url")
            if not vurl:
                continue
            if vid and vid in seen:
                continue
            seen.add(vid)

            duration = entry.get("duration")
            if not length_ok(duration, cfg):
                wrong_length += 1
                continue

            tasks.append(
                VideoTask(
                    url=vurl,
                    platform=src.platform,
                    origin=src.label,
                    title=entry.get("title") or vid,
                    video_id=vid,
                    duration=duration,
                    uploader=(
                        entry.get("uploader")
                        or entry.get("channel")
                        or info.get("uploader")
                        or info.get("channel")
                        or info.get("title")
                        or src.label
                    ),
                    thumbnail=_thumb(entry, src.platform, vid),
                    upload_date=entry.get("upload_date") or "",
                )
            )
            found += 1
            if cfg.limit and found >= cfg.limit:
                break

        summary = f"  {label}: {found} video(s)"
        if wrong_length:
            summary += (
                f" [dim]({wrong_length} skipped as not {cfg.length_mode}; "
                f"use --all to include them)[/dim]"
            )
        note(summary)

    return tasks


def _thumb(entry: dict, platform: str, vid: str) -> str:
    """Best-effort thumbnail URL for the preview list."""
    if platform == "youtube" and vid:
        # Deterministic and always present, unlike the flat-extract thumbnails.
        return f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
    thumbs = entry.get("thumbnails") or []
    if thumbs:
        return thumbs[len(thumbs) // 2].get("url") or ""
    return entry.get("thumbnail") or ""


def _walk_entries(info: dict):
    """Flatten nested playlist results (a channel tab can hold sub-playlists)."""
    entries = info.get("entries")
    if entries is None:
        if info.get("id"):
            yield info
        return
    for entry in entries:
        if not entry:
            continue
        if entry.get("_type") in ("playlist", "multi_video") or "entries" in entry:
            yield from _walk_entries(entry)
        else:
            yield entry


# --------------------------------------------------------------------------
# Downloading
# --------------------------------------------------------------------------


class Cancelled(Exception):
    """Raised out of a progress hook to abort an in-flight download."""


class Downloader:
    def __init__(self, cfg: Config, progress=None, cancel=None):
        self.cfg = cfg
        self.progress = progress
        self.cancel = cancel  # threading.Event, or None
        self._ig_gate = threading.Semaphore(max(1, cfg.instagram_jobs))
        self._lock = threading.Lock()
        # Worker thread id -> its row in the progress display.
        self._rows: dict[int, int] = {}
        # Checked up front so re-running a big channel costs no network calls.
        self._archived = load_archive_ids(cfg.archive_path) if cfg.use_archive else set()

    def run(self, tasks: list[VideoTask]) -> Report:
        report = Report()
        if not tasks:
            return report

        overall = None
        if self.progress is not None:
            overall = self.progress.add_task("[bold]Overall", total=len(tasks))

        workers = max(1, min(self.cfg.jobs, len(tasks)))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="grab") as pool:
            futures = {pool.submit(self._one, task): task for task in tasks}
            for future in as_completed(futures):
                task = futures[future]
                try:
                    outcome = future.result()
                except Exception as exc:  # never let one task kill the batch
                    outcome = Outcome(task, "failed", str(exc))
                with self._lock:
                    report.add(outcome)
                if overall is not None:
                    self.progress.advance(overall)

        if overall is not None:
            self.progress.remove_task(overall)
        return report

    def _stopping(self) -> bool:
        return self.cancel is not None and self.cancel.is_set()

    def _one(self, task: VideoTask) -> Outcome:
        if self._stopping():
            return Outcome(task, "skipped", "cancelled")
        if task.platform == "instagram":
            with self._ig_gate:
                if self._stopping():
                    return Outcome(task, "skipped", "cancelled")
                return self._download(task)
        return self._download(task)

    def _download(self, task: VideoTask) -> Outcome:
        from yt_dlp import YoutubeDL
        from yt_dlp.utils import DownloadError, ExistingVideoReached, MaxDownloadsReached

        if task.video_id and task.video_id in self._archived:
            return Outcome(task, "skipped", "already downloaded")

        logger = _Silent()
        state: dict = {"started": False, "path": None, "filtered": None}

        def hook(status):
            if self._stopping():
                raise Cancelled()
            if status.get("status") == "downloading":
                state["started"] = True
                self._tick(task, status)
            elif status.get("status") == "finished":
                state["started"] = True
                state["path"] = status.get("filename")

        opts = download_opts(self.cfg, task.platform, state)
        opts["logger"] = logger
        opts["progress_hooks"] = [hook]

        row = None
        if self.progress is not None:
            row = self.progress.add_task(self._label(task), total=None)
            self._rows[threading.get_ident()] = row

        try:
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(task.url, download=True)

            if info is None:
                # extract_info returns None for both a filter rejection and an
                # archive hit; the recorded reason is what separates them.
                if state["filtered"]:
                    return Outcome(task, "filtered", state["filtered"])
                return Outcome(task, "skipped", "already downloaded")

            if not state["started"]:
                # yt-dlp returned info but never fetched bytes: already archived.
                return Outcome(task, "skipped", "already downloaded")

            path = state["path"]
            if path and not Path(path).exists():
                # A merge or audio-extract postprocessor renamed the file.
                stem = Path(path).with_suffix("")
                matches = sorted(stem.parent.glob(stem.name + ".*")) if stem.parent.exists() else []
                path = str(matches[0]) if matches else path
            return Outcome(task, "downloaded", path=path)

        except (ExistingVideoReached, MaxDownloadsReached):
            return Outcome(task, "skipped", "already downloaded")
        except Cancelled:
            return Outcome(task, "skipped", "cancelled")
        except DownloadError as exc:
            # yt-dlp wraps hook exceptions, so a cancel arrives as a DownloadError.
            if self._stopping():
                return Outcome(task, "skipped", "cancelled")
            return Outcome(task, "failed", _tidy_error(str(exc), logger))
        except Exception as exc:
            if self._stopping():
                return Outcome(task, "skipped", "cancelled")
            return Outcome(task, "failed", _tidy_error(str(exc), logger))
        finally:
            if row is not None:
                self.progress.remove_task(row)
                self._rows.pop(threading.get_ident(), None)

    def _tick(self, task: VideoTask, status: dict) -> None:
        if self.progress is None:
            return
        row = self._rows.get(threading.get_ident())
        if row is None:
            return
        total = status.get("total_bytes") or status.get("total_bytes_estimate")
        done = status.get("downloaded_bytes") or 0
        try:
            self.progress.update(row, total=total, completed=done)
        except Exception:
            pass

    def _label(self, task: VideoTask) -> str:
        name = task.title or task.video_id or task.url
        if len(name) > 46:
            name = name[:45] + "…"
        tag = "IG" if task.platform == "instagram" else "YT"
        return f"[dim]{tag}[/dim] {name}"


def _tidy_error(message: str, logger: _Silent) -> str:
    text = message.strip()
    for prefix in ("ERROR: ", "[0;31mERROR:[0m "):
        if text.startswith(prefix):
            text = text[len(prefix):]
    if not text and logger.errors:
        text = logger.errors[-1]
    text = " ".join(text.split())
    return text[:300] if text else "unknown error"
