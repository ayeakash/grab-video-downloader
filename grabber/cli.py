"""Command-line entry point."""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import time
from pathlib import Path

from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.table import Table

from . import __version__
from .config import PROJECT_ROOT, Config
from .cookies import SUPPORTED_BROWSERS, CookieError, load_jar
from .engine import Downloader, Report, VideoTask, expand_source, load_archive_ids
from .sources import parse_input

DEFAULT_LINKS_FILE = PROJECT_ROOT / "links.txt"
FAILED_FILE = "failed.txt"

console = Console()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="grab",
        description="Bulk-download short videos from YouTube and Instagram.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  grab https://youtube.com/shorts/abc123
  grab <url1> <url2> <url3>
  grab https://youtube.com/@mrbeast              # the channel's Shorts tab
  grab https://instagram.com/natgeo              # every reel on the page
  grab -f links.txt                              # one link per line
  grab                                           # reads links.txt, or prompts

  grab https://youtube.com/@somechannel --all -n 50
  grab https://instagram.com/natgeo --cookies-browser chrome
  grab -f links.txt -j 8 -o ~/Movies/Clips
""",
    )

    parser.add_argument("urls", nargs="*", help="One or more links (any mix of platforms).")
    parser.add_argument("-f", "--file", help="Read links from a text file, one per line.")
    parser.add_argument("-o", "--out", help="Download directory.")
    parser.add_argument("-j", "--jobs", type=int, help="Parallel downloads (default 4).")
    parser.add_argument(
        "-n", "--limit", type=int, help="Max videos to take from each channel/profile."
    )

    length = parser.add_mutually_exclusive_group()
    length.add_argument(
        "--all", action="store_true", help="Any length, not just shorts."
    )
    length.add_argument(
        "--long", action="store_true", help="Only videos longer than the shorts cutoff."
    )
    parser.add_argument(
        "--short-seconds", type=int, help="Shorts cutoff in seconds (default 180)."
    )

    parser.add_argument(
        "--tab",
        help="Channel tabs to pull: shorts, videos, streams, or all. Comma-separated.",
    )
    parser.add_argument(
        "-q", "--quality", choices=["best", "2160", "1440", "1080", "720", "480"],
        help="Cap the video height.",
    )
    parser.add_argument("--audio", action="store_true", help="Extract audio to mp3.")

    parser.add_argument(
        "--cookies-browser",
        metavar="BROWSER[:PROFILE]",
        help=f"Take cookies from a logged-in browser: {', '.join(SUPPORTED_BROWSERS)}.",
    )
    parser.add_argument("--cookies", help="Path to a cookies.txt file.")

    parser.add_argument("--since", help="Only videos uploaded on/after YYYYMMDD.")
    parser.add_argument("--until", help="Only videos uploaded on/before YYYYMMDD.")

    parser.add_argument(
        "--no-archive",
        action="store_true",
        help="Ignore the download history and re-fetch everything.",
    )
    parser.add_argument(
        "--redownload-missing",
        action="store_true",
        help="Re-fetch videos that are in the archive but no longer on disk.",
    )
    parser.add_argument("--thumbnail", action="store_true", help="Also save/embed thumbnails.")
    parser.add_argument("--info-json", action="store_true", help="Save metadata JSON per video.")

    parser.add_argument(
        "--dry-run", action="store_true", help="Resolve and list videos without downloading."
    )
    parser.add_argument(
        "-p",
        "--pick",
        action="store_true",
        help="Show the resolved list and choose what to skip before downloading.",
    )
    parser.add_argument(
        "--save-config", action="store_true", help="Persist these options as the new defaults."
    )
    parser.add_argument("--doctor", action="store_true", help="Check the local setup and exit.")
    parser.add_argument(
        "--web", action="store_true", help="Open the browser interface instead of the CLI."
    )
    parser.add_argument("--port", type=int, default=8765, help="Port for --web (default 8765).")
    parser.add_argument("--version", action="version", version=f"grab {__version__}")

    return parser


def apply_args(cfg: Config, args) -> Config:
    if args.out:
        cfg.out_dir = str(Path(args.out).expanduser().resolve())
    if args.jobs:
        cfg.jobs = max(1, args.jobs)
    if args.limit:
        cfg.limit = args.limit
    if args.all:
        cfg.length_mode = "all"
    elif args.long:
        cfg.length_mode = "long"
    if args.short_seconds:
        cfg.short_seconds = args.short_seconds
    if args.tab:
        cfg.channel_tabs = [t.strip() for t in args.tab.split(",") if t.strip()]
    if args.quality:
        cfg.quality = args.quality
    if args.audio:
        cfg.audio_only = True
    if args.cookies_browser:
        cfg.cookies_browser = args.cookies_browser
    if args.cookies:
        cfg.cookies_file = str(Path(args.cookies).expanduser())
    if args.since:
        cfg.since = args.since
    if args.until:
        cfg.until = args.until
    if args.no_archive:
        cfg.use_archive = False
    if args.redownload_missing:
        cfg.redownload_missing = True
    if args.thumbnail:
        cfg.write_thumbnail = True
    if args.info_json:
        cfg.write_info_json = True
    return cfg


def gather_input(args) -> list[str]:
    """Collect raw link lines from argv, a file, links.txt, stdin, or a prompt."""
    lines: list[str] = list(args.urls)

    if args.file:
        path = Path(args.file).expanduser()
        if not path.exists():
            console.print(f"[red]No such file:[/red] {path}")
            raise SystemExit(2)
        lines += path.read_text(encoding="utf-8").splitlines()

    if not lines and not sys.stdin.isatty():
        lines += sys.stdin.read().splitlines()

    if not lines and DEFAULT_LINKS_FILE.exists():
        content = DEFAULT_LINKS_FILE.read_text(encoding="utf-8").splitlines()
        if any(l.strip() and not l.strip().startswith("#") for l in content):
            console.print(f"[dim]Reading links from {DEFAULT_LINKS_FILE.name}[/dim]")
            lines += content

    if not lines and sys.stdin.isatty():
        console.print(
            "[bold]Paste links[/bold] (one per line). Press Enter on a blank line to start."
        )
        try:
            while True:
                entry = input("  > ").strip()
                if not entry:
                    break
                lines.append(entry)
        except (EOFError, KeyboardInterrupt):
            console.print()

    return lines


def doctor(cfg: Config) -> int:
    table = Table(title="grab · setup check", show_header=False, box=None, padding=(0, 2))
    ok = True

    import yt_dlp

    table.add_row("yt-dlp", f"[green]{yt_dlp.version.__version__}[/green]")

    try:
        import instaloader

        table.add_row("instaloader", f"[green]{instaloader.__version__}[/green]")
    except ImportError:
        table.add_row("instaloader", "[red]missing — Instagram pages will not expand[/red]")
        ok = False

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        table.add_row("ffmpeg", f"[green]{ffmpeg}[/green]")
    else:
        table.add_row("ffmpeg", "[red]missing — run: brew install ffmpeg[/red]")
        ok = False

    out = Path(cfg.out_dir)
    table.add_row("output dir", f"{out} {'[green](exists)[/green]' if out.exists() else '[dim](will be created)[/dim]'}")

    archive = cfg.archive_path
    count = 0
    if archive.exists():
        count = sum(1 for _ in archive.open(encoding="utf-8"))
    table.add_row("archive", f"{count} video(s) already recorded")

    source = cfg.cookies_file or cfg.cookies_browser
    if source:
        try:
            jar = load_jar(cfg.cookies_file, cfg.cookies_browser)
            from .cookies import instagram_cookies

            ig = instagram_cookies(jar)
            total = sum(1 for _ in jar) if jar else 0
            detail = f"[green]{total} cookie(s) from {source}[/green]"
            detail += (
                f" · [green]Instagram session found[/green]"
                if "sessionid" in ig
                else " · [yellow]no Instagram sessionid — log in to Instagram in that browser[/yellow]"
            )
            table.add_row("cookies", detail)
        except CookieError as exc:
            table.add_row("cookies", f"[red]{exc}[/red]")
            ok = False
    else:
        table.add_row(
            "cookies",
            "[yellow]none configured — Instagram will mostly fail. "
            "Use --cookies-browser chrome[/yellow]",
        )

    console.print(table)
    return 0 if ok else 1


def clock(seconds: float | None) -> str:
    if not seconds:
        return "   ?  "
    total = int(seconds)
    return f"{total // 60:>3}:{total % 60:02d}"


def parse_ranges(text: str, ceiling: int) -> set[int]:
    """Parse '2,5-7 9' into a set of 1-based numbers, ignoring anything odd."""
    picked: set[int] = set()
    for chunk in re.split(r"[\s,]+", text.strip()):
        if not chunk:
            continue
        match = re.fullmatch(r"(\d+)(?:\s*-\s*(\d+))?", chunk)
        if not match:
            continue
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if start > end:
            start, end = end, start
        picked.update(n for n in range(start, end + 1) if 1 <= n <= ceiling)
    return picked


def pick_tasks(tasks: list[VideoTask], cfg: Config) -> list[VideoTask]:
    """Show the resolved list and let the user drop some before downloading."""
    archived = load_archive_ids(cfg.archive_path) if cfg.use_archive else set()

    console.print(f"\n[bold]{len(tasks)} video(s) resolved:[/bold]")
    for number, task in enumerate(tasks, 1):
        tag = "IG" if task.platform == "instagram" else "YT"
        have = " [dim](have it)[/dim]" if task.video_id in archived else ""
        who = f" [dim]· {task.uploader}[/dim]" if task.uploader else ""
        console.print(
            f"  [dim]{number:>3}.[/dim] [dim]{tag}[/dim] {clock(task.duration)}  "
            f"{task.title}{who}{have}"
        )

    console.print(
        "\n[dim]Enter numbers to SKIP (e.g. 2,5-7), 'new' to skip ones you already have,\n"
        "'q' to cancel, or just press Enter to download all.[/dim]"
    )
    try:
        answer = input("  skip > ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        console.print()
        return []

    if answer in ("q", "quit", "cancel"):
        return []
    if answer in ("new", "n"):
        keep = [t for t in tasks if t.video_id not in archived]
        console.print(f"[dim]Skipping {len(tasks) - len(keep)} already downloaded.[/dim]")
        return keep
    if not answer:
        return tasks

    drop = parse_ranges(answer, len(tasks))
    keep = [t for n, t in enumerate(tasks, 1) if n not in drop]
    console.print(f"[dim]Skipping {len(tasks) - len(keep)}, downloading {len(keep)}.[/dim]")
    return keep


def make_progress() -> Progress:
    return Progress(
        SpinnerColumn(style="cyan"),
        TextColumn("{task.description}"),
        BarColumn(bar_width=24),
        DownloadColumn(binary_units=True),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=console,
        transient=True,
        refresh_per_second=8,
    )


def summarize(report: Report, cfg: Config, elapsed: float) -> None:
    console.print()
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_row("[green]Downloaded[/green]", str(len(report.downloaded)))
    if report.skipped:
        table.add_row("[dim]Already had[/dim]", str(len(report.skipped)))
    if report.filtered:
        why = report.filtered[0].detail
        table.add_row(
            "[dim]Filtered out[/dim]",
            f"{len(report.filtered)} [dim](e.g. {why})[/dim]" if why else str(len(report.filtered)),
        )
    if report.failed:
        table.add_row("[red]Failed[/red]", str(len(report.failed)))
    table.add_row("[dim]Time[/dim]", f"{elapsed:.0f}s")
    table.add_row("[dim]Saved to[/dim]", cfg.out_dir)
    console.print(table)

    if report.failed:
        console.print("\n[red]Failures:[/red]")
        for outcome in report.failed[:15]:
            name = outcome.task.title or outcome.task.url
            console.print(f"  [dim]•[/dim] {name}\n    [dim]{outcome.detail}[/dim]")
        if len(report.failed) > 15:
            console.print(f"  [dim]… and {len(report.failed) - 15} more[/dim]")

        path = Path(cfg.out_dir) / FAILED_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "\n".join(o.task.url for o in report.failed) + "\n", encoding="utf-8"
        )
        console.print(f"\n[dim]Retry them with:[/dim] grab -f {path}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    cfg = apply_args(Config.load(), args)

    if args.save_config:
        cfg.save()
        console.print("[green]Saved defaults to config.json[/green]")

    if args.doctor:
        return doctor(cfg)

    if args.web:
        from .web import serve

        serve(port=args.port)
        return 0

    raw_lines = gather_input(args)
    if not raw_lines:
        console.print("[yellow]No links given.[/yellow] Try: grab --help")
        return 1

    sources, skipped = parse_input(raw_lines)
    if skipped:
        shown = ", ".join(repr(s) for s in skipped[:6])
        more = f" (+{len(skipped) - 6} more)" if len(skipped) > 6 else ""
        console.print(f"[yellow]Ignored {len(skipped)} non-link token(s):[/yellow] {shown}{more}")
    if not sources:
        console.print("[red]Nothing to download.[/red]")
        return 1

    try:
        jar = load_jar(cfg.cookies_file, cfg.cookies_browser)
    except CookieError as exc:
        console.print(f"[red]{exc}[/red]")
        return 2

    needs_login = any(s.platform == "instagram" and s.is_container for s in sources)
    if needs_login and jar is None:
        console.print(
            "[yellow]Instagram pages need a logged-in session.[/yellow] "
            "Add [bold]--cookies-browser chrome[/bold] (or safari/firefox) if this fails."
        )

    # ---- Resolve every source into a flat list of videos --------------------
    console.print(
        f"[bold]Resolving {len(sources)} source(s)[/bold] "
        f"[dim]({cfg.length_mode} mode)[/dim]"
    )
    tasks: list[VideoTask] = []
    seen: set[str] = set()
    with console.status("[cyan]Working…", spinner="dots"):
        for src in sources:
            for task in expand_source(src, cfg, jar=jar, on_note=console.print, should_stop=None):
                key = task.video_id or task.url
                if key in seen:
                    continue
                seen.add(key)
                tasks.append(task)

    if not tasks:
        console.print("[yellow]No videos matched.[/yellow]")
        return 1

    if args.dry_run:
        console.print(f"\n[bold]{len(tasks)} video(s) would be downloaded:[/bold]")
        for task in tasks:
            length = f"{task.duration:.0f}s" if task.duration else "?"
            console.print(f"  [dim]{length:>6}[/dim]  {task.title or task.video_id}")
            console.print(f"          [dim]{task.url}[/dim]")
        return 0

    if args.pick:
        if not sys.stdin.isatty():
            console.print("[yellow]--pick needs an interactive terminal; downloading all.[/yellow]")
        else:
            tasks = pick_tasks(tasks, cfg)
            if not tasks:
                console.print("[yellow]Nothing selected.[/yellow]")
                return 0

    console.print(f"[bold]Downloading {len(tasks)} video(s)[/bold] with {cfg.jobs} worker(s)\n")

    start = time.monotonic()
    with make_progress() as progress:
        report = Downloader(cfg, progress).run(tasks)
    summarize(report, cfg, time.monotonic() - start)

    return 1 if report.failed and not report.downloaded else 0


def entry() -> None:
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped. Partial files resume on the next run.[/yellow]")
        raise SystemExit(130)


if __name__ == "__main__":
    entry()
