"""Settings, with optional persistence to config.json next to the project root."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.json"

# Anything at or under this many seconds counts as a "short".
DEFAULT_SHORT_SECONDS = 180


@dataclass
class Config:
    out_dir: str = str(PROJECT_ROOT / "downloads")

    # Parallelism. Instagram gets its own much lower cap because it rate-limits
    # aggressively and will temporarily block an account that hammers it.
    jobs: int = 4
    # Instagram still gets a tighter cap than YouTube because it rate-limits on
    # the per-post page fetch, but fully serialising it with a 4s pause made a
    # 142-reel profile take ~40 minutes. Media itself comes from a CDN that
    # tolerates concurrency fine. ig_gentle restores the cautious old pace.
    ig_jobs: int = 4
    ig_sleep: float = 0.5
    ig_gentle: bool = False
    ig_gentle_jobs: int = 1
    ig_gentle_sleep: float = 4.0
    # Anonymous downloading has a far lower ceiling than a logged-in session:
    # Instagram starts redirecting post pages to the login screen after a
    # modest number of hits. The fast pace is only safe with cookies.
    ig_anon_jobs: int = 2
    ig_anon_sleep: float = 3.0

    @property
    def has_cookies(self) -> bool:
        return bool(self.cookies_browser or self.cookies_file)

    @property
    def instagram_jobs(self) -> int:
        if self.ig_gentle:
            return self.ig_gentle_jobs
        if not self.has_cookies:
            return self.ig_anon_jobs
        return min(self.ig_jobs, self.jobs)

    @property
    def instagram_sleep(self) -> float:
        if self.ig_gentle:
            return self.ig_gentle_sleep
        if not self.has_cookies:
            return self.ig_anon_sleep
        return self.ig_sleep

    # Length filtering. mode is one of: shorts, long, all
    length_mode: str = "shorts"
    short_seconds: int = DEFAULT_SHORT_SECONDS

    limit: int | None = None
    # 1080 rather than "best": shorts are vertical 1080x1920 at source, so
    # anything higher is upscaled bulk (a 50s clip can land at 80 MB in 4K AV1)
    # with no real gain. Pass -q best to lift the cap.
    quality: str = "1080"  # best | 2160 | 1440 | 1080 | 720 | 480
    audio_only: bool = False

    use_archive: bool = True
    archive_name: str = ".downloaded.txt"
    # The archive records ids, not paths, so a file you delete or move stays
    # marked as "have it" and is skipped forever. Off by default: people who
    # move finished videos to another drive would otherwise re-fetch the lot.
    redownload_missing: bool = False

    cookies_browser: str | None = None
    cookies_file: str | None = None

    # Which channel tabs to pull when handed a bare YouTube channel link.
    channel_tabs: list[str] = field(default_factory=lambda: ["shorts"])

    write_thumbnail: bool = False
    write_info_json: bool = False
    embed_metadata: bool = True

    since: str | None = None  # YYYYMMDD
    until: str | None = None  # YYYYMMDD

    retries: int = 10

    @property
    def archive_path(self) -> Path:
        # Audio keeps a separate archive: it is recorded under the same video id,
        # so sharing one file would make "already have the mp3" wrongly skip a
        # later request for the actual video.
        name = self.archive_name
        if self.audio_only:
            stem, dot, ext = name.rpartition(".")
            name = f"{stem}-audio{dot}{ext}" if dot else f"{name}-audio"
        return Path(self.out_dir) / name

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> "Config":
        cfg = cls()
        if not path.exists():
            return cfg
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return cfg
        known = {f.name for f in fields(cls)}
        for key, value in raw.items():
            if key in known:
                setattr(cfg, key, value)
        return cfg

    def save(self, path: Path = CONFIG_PATH) -> None:
        path.write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")
