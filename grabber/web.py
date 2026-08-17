"""A small local web UI, so links can be pasted into a box instead of a shell.

Deliberately built on the standard library: no Flask, no CDN assets, nothing new
in requirements.txt. It binds to 127.0.0.1 only and is meant for one person on
one machine -- there is no auth, so do not expose it to a network.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .config import Config
from .cookies import CookieError, list_browser_profiles, load_jar
from .engine import Downloader, VideoTask, expand_source, load_archive_ids
from .sources import parse_input

MARKUP = re.compile(r"\[/?[a-z0-9 #_]+\]")


def plain(text: str) -> str:
    """Strip rich console markup so the browser shows clean text."""
    return MARKUP.sub("", str(text)).strip()


def _descendant_pids(root_pid: int) -> list[int]:
    """Return every current descendant of root_pid, parents before children."""
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,ppid="],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return []

    children: dict[int, list[int]] = {}
    for line in result.stdout.splitlines():
        try:
            pid_text, parent_text = line.split()
            pid, parent = int(pid_text), int(parent_text)
        except (TypeError, ValueError):
            continue
        children.setdefault(parent, []).append(pid)

    descendants: list[int] = []
    pending = list(children.get(root_pid, ()))
    while pending:
        pid = pending.pop()
        descendants.append(pid)
        pending.extend(children.get(pid, ()))
    return descendants


def _kill_program(state: "JobState") -> None:
    """Terminate downloads, helper processes, worker threads, and this server."""
    state.cancel.set()
    if sys.platform == "win32":
        # There is no SIGKILL on Windows, and no ps to walk. taskkill /T does
        # the whole tree in one call.
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(os.getpid())],
                check=False,
                capture_output=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        # yt-dlp runs in this process, while ffmpeg and similar helpers are child
        # processes. Kill deepest descendants first so none are orphaned when the
        # Python server exits.
        descendants = _descendant_pids(os.getpid())
        for pid in reversed(descendants):
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        time.sleep(0.35)
        for pid in reversed(descendants):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    # Python cannot safely kill arbitrary worker threads. Exiting the process
    # is the reliable way to stop blocked resolver/download threads as well as
    # the HTTP server. os._exit deliberately skips waiting for those threads.
    os._exit(0)


class JobState:
    """Everything the browser polls for. Guarded by a lock; mutated by workers."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.cancel = threading.Event()
        self.reset()

    def reset(self) -> None:
        # idle | resolving | ready | downloading | done | error
        self.phase = "idle"
        self.notes: list[str] = []
        self.rows: dict[int, dict] = {}
        self.overall_id: int | None = None
        self.total = 0
        self.done = 0
        self.summary: dict | None = None
        self.failures: list[dict] = []
        self.items: list[dict] = []
        self.tasks: list[VideoTask] = []
        self.errors: list[str] = []
        self.out_dir = ""
        self.started = 0.0
        self.cancel.clear()

    def begin_download(self) -> None:
        """Clear the previous run's counters but keep the resolved list."""
        with self.lock:
            self.rows.clear()
            self.overall_id = None
            self.total = 0
            self.done = 0
            self.summary = None
            self.failures = []
            self.started = time.monotonic()
        self.cancel.clear()

    # -- called from the worker thread ------------------------------------
    def note(self, message) -> None:
        text = plain(message)
        if not text:
            return
        with self.lock:
            self.notes.append(text)
            del self.notes[:-200]

    def fail(self, message) -> None:
        text = plain(message)
        with self.lock:
            if text and text not in self.errors:
                self.errors.append(text)

    def set_phase(self, phase: str) -> None:
        with self.lock:
            self.phase = phase

    def snapshot(self, with_items: bool = True) -> dict:
        with self.lock:
            active = [
                {"label": r["label"], "completed": r["completed"], "total": r["total"]}
                for tid, r in self.rows.items()
                if tid != self.overall_id
            ]
            data = {
                "phase": self.phase,
                "notes": list(self.notes),
                "active": active,
                "total": self.total,
                "done": self.done,
                "summary": self.summary,
                "failures": self.failures,
                "outDir": self.out_dir,
                "count": len(self.items),
                "errors": list(self.errors),
                "elapsed": round(time.monotonic() - self.started, 1) if self.started else 0,
                "cancelling": self.cancel.is_set() and self.phase == "downloading",
            }
            # The list can be hundreds of rows; only ship it when it changed.
            data["items"] = list(self.items) if with_items else None
            return data


class WebProgress:
    """Implements the slice of rich's Progress API that Downloader uses."""

    def __init__(self, state: JobState) -> None:
        self.state = state
        self._next = 0

    def add_task(self, description, total=None):
        with self.state.lock:
            self._next += 1
            tid = self._next
            label = plain(description)
            self.state.rows[tid] = {"label": label, "total": total, "completed": 0}
            if label.lower().startswith("overall"):
                self.state.overall_id = tid
                self.state.total = total or 0
                self.state.done = 0
        return tid

    def update(self, tid, total=None, completed=None, **_):
        with self.state.lock:
            row = self.state.rows.get(tid)
            if row is None:
                return
            if total is not None:
                row["total"] = total
            if completed is not None:
                row["completed"] = completed

    def advance(self, tid, amount=1):
        with self.state.lock:
            row = self.state.rows.get(tid)
            if row is not None:
                row["completed"] += amount
            if tid == self.state.overall_id:
                self.state.done += amount

    def remove_task(self, tid):
        with self.state.lock:
            self.state.rows.pop(tid, None)


def build_config(options: dict) -> Config:
    """Apply the form values on top of the saved defaults."""
    cfg = Config.load()

    mode = options.get("mode")
    if mode in ("shorts", "all", "long"):
        cfg.length_mode = mode

    for key, attr, cast in (
        ("limit", "limit", int),
        ("jobs", "jobs", int),
        ("shortSeconds", "short_seconds", int),
    ):
        value = options.get(key)
        if value not in (None, "", 0):
            try:
                setattr(cfg, attr, max(1, cast(value)))
            except (TypeError, ValueError):
                pass
    if not options.get("limit"):
        cfg.limit = None

    if options.get("quality"):
        cfg.quality = str(options["quality"])
    cfg.audio_only = bool(options.get("audio"))
    cfg.use_archive = not options.get("noArchive")
    cfg.ig_gentle = bool(options.get("igGentle"))
    cfg.redownload_missing = bool(options.get("redownloadMissing"))

    browser = (options.get("cookiesBrowser") or "").strip()
    cfg.cookies_browser = browser or None

    out = (options.get("outDir") or "").strip()
    if out:
        cfg.out_dir = str(Path(out).expanduser())

    tabs = (options.get("tabs") or "").strip()
    if tabs:
        cfg.channel_tabs = [t.strip() for t in tabs.split(",") if t.strip()]

    return cfg


def _resolve(state: JobState, links_text: str, options: dict) -> list[VideoTask] | None:
    """Expand the pasted links into a concrete video list. None means stop."""
    cfg = build_config(options)
    with state.lock:
        state.out_dir = cfg.out_dir
        state.started = time.monotonic()

    sources, skipped = parse_input(links_text.splitlines())
    if skipped:
        state.note(f"Ignored {len(skipped)} non-link token(s): {', '.join(skipped[:6])}")
    if not sources:
        state.note("No usable links found.")
        state.set_phase("error")
        return None

    try:
        jar = load_jar(cfg.cookies_file, cfg.cookies_browser)
    except CookieError as exc:
        state.note(str(exc))
        state.set_phase("error")
        return None

    state.set_phase("resolving")
    state.note(f"Resolving {len(sources)} source(s) in {cfg.length_mode} mode")

    tasks: list[VideoTask] = []
    seen: set[str] = set()
    for src in sources:
        if state.cancel.is_set():
            break
        for task in expand_source(
            src,
            cfg,
            jar=jar,
            on_note=state.note,
            should_stop=state.cancel.is_set,
            on_error=state.fail,
        ):
            key = task.video_id or task.url
            if key in seen:
                continue
            seen.add(key)
            tasks.append(task)

    if state.cancel.is_set():
        state.note("Cancelled.")
        state.set_phase("done")
        return None

    archived = load_archive_ids(cfg.archive_path) if cfg.use_archive else set()
    items = [
        {
            "i": index,
            "title": task.title or task.video_id or task.url,
            "url": task.url,
            "platform": task.platform,
            "duration": task.duration,
            "uploader": task.uploader or task.origin,
            "thumb": task.thumbnail,
            "date": task.upload_date,
            "have": bool(task.video_id) and task.video_id in archived,
        }
        for index, task in enumerate(tasks)
    ]
    with state.lock:
        state.tasks = tasks
        state.items = items
    return tasks


def _download(state: JobState, tasks: list[VideoTask], options: dict) -> None:
    cfg = build_config(options)
    state.begin_download()
    with state.lock:
        state.out_dir = cfg.out_dir

    state.note(f"Downloading {len(tasks)} video(s) with {cfg.jobs} worker(s)")
    state.set_phase("downloading")

    downloader = Downloader(cfg, WebProgress(state), cancel=state.cancel)
    gone = sum(1 for t in tasks if t.video_id in downloader.missing_ids)
    if gone and not cfg.redownload_missing:
        state.note(
            f"{gone} of these are in the archive but missing from disk — tick "
            "'Re-download missing' to fetch them again."
        )
    report = downloader.run(tasks)

    with state.lock:
        state.summary = {
            "downloaded": len(report.downloaded),
            "skipped": len(report.skipped),
            "filtered": len(report.filtered),
            "failed": len(report.failed),
        }
        state.failures = [
            {"title": o.task.title or o.task.url, "url": o.task.url, "detail": o.detail}
            for o in report.failed[:50]
        ]

    if report.failed:
        path = Path(cfg.out_dir) / "failed.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(o.task.url for o in report.failed) + "\n", encoding="utf-8")

    state.set_phase("done")


def resolve_only_job(state: JobState, links_text: str, options: dict) -> None:
    try:
        tasks = _resolve(state, links_text, options)
        if tasks is None:
            return
        if not tasks:
            state.note("Nothing to download — see the reasons above.")
            state.set_phase("empty")
            return
        state.note(f"Found {len(tasks)} video(s). Pick which ones to download.")
        state.set_phase("ready")
    except Exception as exc:
        state.note(f"Unexpected error: {type(exc).__name__}: {exc}")
        state.set_phase("error")


def run_job(state: JobState, links_text: str, options: dict) -> None:
    """Resolve and immediately download everything (the no-preview path)."""
    try:
        tasks = _resolve(state, links_text, options)
        if tasks is None:
            return
        if not tasks:
            # No summary tiles here: a green "0 downloaded" next to "Finished"
            # reads as success when the sources actually failed to resolve.
            state.note(
                "Nothing to download — see the reasons above."
                if state.errors
                else "No videos matched your filters."
            )
            state.set_phase("empty")
            return
        _download(state, tasks, options)
    except Exception as exc:
        state.note(f"Unexpected error: {type(exc).__name__}: {exc}")
        state.set_phase("error")


def download_selected_job(state: JobState, indexes: list[int], options: dict) -> None:
    try:
        with state.lock:
            everything = list(state.tasks)
        chosen = [everything[i] for i in indexes if 0 <= i < len(everything)]
        if not chosen:
            state.note("Nothing selected.")
            state.set_phase("ready")
            return
        dropped = len(everything) - len(chosen)
        if dropped:
            state.note(f"Skipping {dropped} video(s) you unchecked.")
        _download(state, chosen, options)
    except Exception as exc:
        state.note(f"Unexpected error: {type(exc).__name__}: {exc}")
        state.set_phase("error")


def _open_folder(target: Path) -> None:
    """Reveal a directory in the platform's file manager."""
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(target)])
    elif sys.platform == "win32":
        os.startfile(str(target))  # noqa: S606 - the only reliable way on Windows
    else:
        subprocess.Popen(["xdg-open", str(target)])


class Handler(BaseHTTPRequestHandler):
    server_version = "grab"
    state: JobState
    worker: threading.Thread | None = None

    def log_message(self, *args):  # keep the terminal quiet
        pass

    # -- helpers -----------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict, code: int = 200) -> None:
        self._send(code, json.dumps(payload).encode("utf-8"), "application/json")

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def _busy(self) -> bool:
        worker = type(self).worker
        return bool(worker and worker.is_alive())

    def _spawn(self, target, *args) -> None:
        cls = type(self)
        cls.worker = threading.Thread(target=target, args=args, daemon=True)
        cls.worker.start()

    # -- routes ------------------------------------------------------------
    def do_GET(self):
        route = self.path.split("?")[0]
        if route in ("/", "/index.html"):
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif route == "/api/browsers":
            saved = Config.load().cookies_browser or ""
            self._json({"options": list_browser_profiles(), "selected": saved})
        elif route == "/api/state":
            # ?items=0 keeps the 2x/second poll small once the list is loaded.
            want = "items=0" not in self.path
            self._json(self.state.snapshot(with_items=want))
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        route = self.path.split("?")[0]

        if route in ("/api/start", "/api/preview"):
            if self._busy():
                self._json({"ok": False, "error": "A run is already in progress."}, 409)
                return
            data = self._body()
            links = (data.get("links") or "").strip()
            if not links:
                self._json({"ok": False, "error": "Paste at least one link."}, 400)
                return
            self.state.reset()
            target = resolve_only_job if route == "/api/preview" else run_job
            self._spawn(target, self.state, links, data.get("options") or {})
            self._json({"ok": True})

        elif route == "/api/download":
            if self._busy():
                self._json({"ok": False, "error": "A run is already in progress."}, 409)
                return
            data = self._body()
            raw = data.get("selected") or []
            try:
                indexes = [int(x) for x in raw]
            except (TypeError, ValueError):
                self._json({"ok": False, "error": "Bad selection."}, 400)
                return
            if not indexes:
                self._json({"ok": False, "error": "Select at least one video."}, 400)
                return
            self._spawn(download_selected_job, self.state, indexes, data.get("options") or {})
            self._json({"ok": True})

        elif route == "/api/checklogin":
            from .instagram import check_session

            browser = (self._body().get("browser") or "").strip()
            if not browser:
                self._json({"ok": False, "detail": "Pick a browser profile first."})
                return
            try:
                jar = load_jar(None, browser)
            except CookieError as exc:
                self._json({"ok": False, "detail": str(exc)})
                return
            result = check_session(jar)
            if result.get("ok"):
                # Remember it, so this only has to be done once.
                cfg = Config.load()
                cfg.cookies_browser = browser
                try:
                    cfg.save()
                    result["detail"] += " · saved as your default"
                except OSError:
                    pass
            self._json(result)

        elif route == "/api/cancel":
            self.state.cancel.set()
            self.state.note("Stopping — partial files are kept and resume next run.")
            self._json({"ok": True})

        elif route == "/api/kill":
            self.state.cancel.set()
            self.state.note("Killing the downloader and all helper processes now.")
            self._json({"ok": True})
            threading.Thread(target=_kill_program, args=(self.state,), daemon=True).start()

        elif route == "/api/reveal":
            target = Path(self.state.out_dir or ".")
            if target.exists():
                _open_folder(target)
                self._json({"ok": True})
            else:
                self._json({"ok": False, "error": "Nothing downloaded yet."}, 404)

        else:
            self._send(404, b"not found", "text/plain")


def serve(port: int = 8765, open_browser: bool = True) -> None:
    Handler.state = JobState()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}"
    print(f"\n  grab is running at  {url}")
    print("  Press Ctrl-C to stop.\n")
    if open_browser:
        import webbrowser

        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
    finally:
        httpd.server_close()


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>grab · bulk video downloader</title>
<style>
  :root {
    --bg: #f6f7f9; --panel: #fff; --ink: #16181d; --muted: #6b7280;
    --line: #e3e6ea; --accent: #2563eb; --ok: #15803d; --bad: #b91c1c;
    --bar: #e8ebef; --chip: #eef1f5;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #0f1115; --panel: #171a20; --ink: #e8eaed; --muted: #9aa1ac;
      --line: #272b33; --accent: #60a5fa; --ok: #4ade80; --bad: #f87171;
      --bar: #23272f; --chip: #21262e;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--ink);
    font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  }
  .wrap { max-width: 880px; margin: 0 auto; padding: 28px 20px 70px; }
  h1 { font-size: 21px; margin: 0 0 2px; letter-spacing: -.01em; }
  .sub { color: var(--muted); font-size: 13.5px; margin-bottom: 22px; }
  .card {
    background: var(--panel); border: 1px solid var(--line);
    border-radius: 12px; padding: 18px; margin-bottom: 16px;
  }
  label { display: block; font-size: 12.5px; font-weight: 600; margin-bottom: 6px;
          color: var(--muted); text-transform: uppercase; letter-spacing: .04em; }
  textarea, input, select {
    width: 100%; padding: 9px 11px; border: 1px solid var(--line); border-radius: 8px;
    background: var(--bg); color: var(--ink); font-size: 14px; font-family: inherit;
  }
  textarea { min-height: 120px; resize: vertical; font-size: 13px;
             font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  textarea:focus, input:focus, select:focus {
    outline: 2px solid var(--accent); outline-offset: -1px; border-color: transparent; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(132px, 1fr));
          gap: 12px; margin-top: 14px; }
  .checks { display: flex; flex-wrap: wrap; gap: 18px; margin-top: 14px; }
  .checks label { display: flex; align-items: center; gap: 7px; text-transform: none;
                  letter-spacing: 0; font-size: 13.5px; font-weight: 500;
                  color: var(--ink); margin: 0; }
  .checks input { width: auto; }
  .row { display: flex; gap: 10px; align-items: center; margin-top: 18px; flex-wrap: wrap; }
  button {
    padding: 10px 20px; border-radius: 8px; border: 1px solid transparent;
    font-size: 14px; font-weight: 600; cursor: pointer; font-family: inherit;
  }
  .primary { background: var(--accent); color: #fff; }
  .primary:disabled { opacity: .5; cursor: not-allowed; }
  .ghost { background: transparent; color: var(--ink); border-color: var(--line); }
  .ghost:disabled { opacity: .45; cursor: not-allowed; }
  .danger { background: var(--bad); color: #fff; }
  .danger:hover { filter: brightness(.92); }
  .link { background: none; border: none; color: var(--accent); padding: 0;
          font-size: 13px; font-weight: 500; cursor: pointer; }
  .hint { color: var(--muted); font-size: 12.5px; margin-top: 9px; }
  .hint code, .chip { background: var(--chip); padding: 1px 6px; border-radius: 4px;
                      font-size: 12px; }
  /* Must be #preview-card, not #preview -- that id belongs to the button. */
  #preview-card, #live { display: none; }

  /* preview list */
  .phead { display: flex; align-items: center; justify-content: space-between;
           gap: 12px; flex-wrap: wrap; margin-bottom: 12px; }
  .phead b { font-size: 16px; }
  .tools { display: flex; gap: 14px; align-items: center; }
  .list { max-height: 460px; overflow: auto; border: 1px solid var(--line);
          border-radius: 9px; }
  /* .item is a <label>, so it must undo the uppercase form-label styling above. */
  .item { display: flex; gap: 11px; padding: 9px 11px; align-items: center;
          border-bottom: 1px solid var(--line); text-transform: none;
          letter-spacing: 0; font-weight: 400; color: var(--ink);
          margin: 0; font-size: 14px; cursor: pointer; }
  .item:hover { background: var(--chip); }
  .item:last-child { border-bottom: none; }
  .item.off { opacity: .42; }
  .item input { width: auto; flex: none; }
  .item img { width: 60px; height: 42px; object-fit: cover; border-radius: 5px;
              background: var(--bar); flex: none; }
  .item .meta { min-width: 0; flex: 1; }
  .item .t { font-size: 13.5px; white-space: nowrap; overflow: hidden;
             text-overflow: ellipsis; }
  .item .s { font-size: 12px; color: var(--muted); margin-top: 1px; }
  .badge { font-size: 10.5px; font-weight: 700; letter-spacing: .04em;
           padding: 1px 5px; border-radius: 4px; background: var(--chip);
           color: var(--muted); text-transform: uppercase; }

  /* progress */
  .bar { height: 7px; background: var(--bar); border-radius: 99px;
         overflow: hidden; margin-top: 7px; }
  .bar > i { display: block; height: 100%; background: var(--accent);
             width: 0; transition: width .25s; }
  .file { margin-bottom: 11px; }
  .file .nm { font-size: 13px; display: flex; justify-content: space-between; gap: 12px; }
  .file .nm span:last-child { color: var(--muted); font-variant-numeric: tabular-nums;
                              white-space: nowrap; }
  .status { display: flex; justify-content: space-between; align-items: baseline; }
  .status b { font-size: 16px; }
  .status .el { color: var(--muted); font-size: 13px; font-variant-numeric: tabular-nums; }
  pre#log {
    background: var(--bg); border: 1px solid var(--line); border-radius: 8px;
    padding: 11px 13px; max-height: 170px; overflow: auto; font-size: 12.5px;
    white-space: pre-wrap; word-break: break-word; margin: 0;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace; color: var(--muted);
  }
  .tiles { display: flex; gap: 22px; flex-wrap: wrap; margin-bottom: 12px; }
  .tile b { display: block; font-size: 24px; line-height: 1.25;
            font-variant-numeric: tabular-nums; }
  .tile span { font-size: 12px; color: var(--muted); text-transform: uppercase;
               letter-spacing: .04em; }
  .tile.ok b { color: var(--ok); } .tile.bad b { color: var(--bad); }
  .problem { border-left: 3px solid var(--bad); background: var(--bar);
             border-radius: 0 7px 7px 0; padding: 9px 12px; margin-top: 12px;
             font-size: 13px; }
  .problem b { color: var(--bad); display: block; margin-bottom: 3px; }
  .fail { border-left: 3px solid var(--bad); padding: 7px 0 7px 11px;
          margin-top: 9px; font-size: 13px; }
  .fail div:last-child { color: var(--muted); font-size: 12px; margin-top: 2px; }
  h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .04em;
       color: var(--muted); margin: 0 0 11px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>grab</h1>
  <div class="sub">Bulk-download short videos from YouTube and Instagram.</div>

  <div class="card">
    <label for="links">Links or page names</label>
    <textarea id="links" spellcheck="false" placeholder="https://www.youtube.com/@somechannel
https://www.instagram.com/someaccount
https://www.youtube.com/shorts/VIDEO_ID
ig:natgeo"></textarea>
    <div class="hint">One per line. Channel and profile pages expand to all their videos.
      Shorthand <code>ig:name</code> and <code>yt:@name</code> work too.</div>

    <div class="grid">
      <div>
        <label for="mode">Length</label>
        <select id="mode">
          <option value="shorts">Shorts only</option>
          <option value="all">Any length</option>
          <option value="long">Long only</option>
        </select>
      </div>
      <div>
        <label for="limit">Max per page</label>
        <input id="limit" type="number" min="1" placeholder="all">
      </div>
      <div>
        <label for="quality">Quality</label>
        <select id="quality">
          <option value="1080">1080p</option>
          <option value="720">720p</option>
          <option value="480">480p</option>
          <option value="1440">1440p</option>
          <option value="2160">4K</option>
          <option value="best">Best</option>
        </select>
      </div>
      <div>
        <label for="jobs">Parallel</label>
        <input id="jobs" type="number" min="1" max="16" value="4">
      </div>
      <div>
        <label for="cookiesBrowser">Instagram login</label>
        <select id="cookiesBrowser">
          <option value="">None — YouTube only</option>
        </select>
      </div>
      <div>
        <label for="tabs">Channel tab</label>
        <select id="tabs">
          <option value="shorts">Shorts</option>
          <option value="videos">Videos</option>
          <option value="shorts,videos">Both</option>
        </select>
      </div>
    </div>

    <div class="hint" id="loginrow">
      <button class="link" id="checkLogin">Check Instagram login</button>
      <span id="loginstatus"></span>
    </div>

    <div class="checks">
      <label><input type="checkbox" id="audio"> Audio only (mp3)</label>
      <label><input type="checkbox" id="noArchive"> Re-download existing</label>
      <label><input type="checkbox" id="igGentle"> Gentle Instagram pace</label>
      <label><input type="checkbox" id="redownloadMissing"> Re-download missing</label>
    </div>
    <div class="hint">Gentle pace downloads Instagram one at a time with a 4s gap —
      slower, but less likely to get you rate-limited.</div>

    <div class="row">
      <button class="primary" id="preview">Preview first</button>
      <button class="ghost" id="go">Download everything</button>
      <button class="ghost" id="stop" style="display:none">Stop downloads</button>
      <button class="danger" id="kill">Kill program</button>
      <button class="ghost" id="reveal">Open folder</button>
    </div>
    <div class="hint" id="err" style="color:var(--bad)"></div>
  </div>

  <div class="card" id="preview-card">
    <div class="phead">
      <b id="pcount">Found 0 videos</b>
      <div class="tools">
        <button class="link" id="selAll">All</button>
        <button class="link" id="selNone">None</button>
        <button class="link" id="selNew">Only new</button>
      </div>
    </div>
    <div class="list" id="list"></div>
    <div class="row">
      <button class="primary" id="dl">Download selected</button>
      <span class="hint" id="selinfo" style="margin:0"></span>
    </div>
  </div>

  <div class="card" id="live">
    <div class="status">
      <b id="phase">Working…</b>
      <span class="el"><span id="count"></span> <span id="elapsed"></span></span>
    </div>
    <div class="bar"><i id="overall"></i></div>
    <div id="problems"></div>
    <div id="files" style="margin-top:16px"></div>
    <div id="summary"></div>
    <h2 style="margin-top:18px">Activity</h2>
    <pre id="log"></pre>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
let timer = null, items = [], picked = new Set(), gotItems = false;

function options() {
  return {
    mode: $('mode').value,
    limit: $('limit').value ? Number($('limit').value) : null,
    quality: $('quality').value,
    jobs: Number($('jobs').value) || 4,
    cookiesBrowser: $('cookiesBrowser').value,
    tabs: $('tabs').value,
    audio: $('audio').checked,
    noArchive: $('noArchive').checked,
    igGentle: $('igGentle').checked,
    redownloadMissing: $('redownloadMissing').checked,
  };
}

const esc = s => String(s).replace(/[&<>"]/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

function bytes(n) {
  if (!n && n !== 0) return '';
  const u = ['B','KB','MB','GB'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(i ? 1 : 0) + ' ' + u[i];
}

function clock(sec) {
  if (!sec && sec !== 0) return '';
  sec = Math.round(sec);
  const m = Math.floor(sec / 60), s = sec % 60;
  return m + ':' + String(s).padStart(2, '0');
}

function ymd(d) {
  return (d && d.length === 8) ? `${d.slice(0,4)}-${d.slice(4,6)}-${d.slice(6)}` : '';
}

function busy(on) {
  $('preview').disabled = on;
  $('go').disabled = on;
  $('dl').disabled = on;
  $('stop').style.display = on ? 'inline-block' : 'none';
}

// ---- preview list ----------------------------------------------------
function renderList() {
  $('list').innerHTML = items.map(it => {
    const on = picked.has(it.i);
    const bits = [
      it.duration ? clock(it.duration) : null,
      it.uploader ? esc(it.uploader) : null,
      ymd(it.date),
    ].filter(Boolean).join(' · ');
    const tag = it.platform === 'instagram' ? 'IG' : 'YT';
    const have = it.have ? ' <span class="badge">have it</span>' : '';
    const img = it.thumb
      ? `<img src="${esc(it.thumb)}" loading="lazy" referrerpolicy="no-referrer"
              onerror="this.style.visibility='hidden'">`
      : `<img>`;
    return `<label class="item ${on ? '' : 'off'}">
      <input type="checkbox" data-i="${it.i}" ${on ? 'checked' : ''}>
      ${img}
      <div class="meta">
        <div class="t"><span class="badge">${tag}</span> ${esc(it.title)}${have}</div>
        <div class="s">${bits}</div>
      </div></label>`;
  }).join('');

  $('list').querySelectorAll('input[type=checkbox]').forEach(cb => {
    cb.onchange = () => {
      const i = Number(cb.dataset.i);
      cb.checked ? picked.add(i) : picked.delete(i);
      cb.closest('.item').classList.toggle('off', !cb.checked);
      updateCounts();
    };
  });
  updateCounts();
}

function updateCounts() {
  $('pcount').textContent = `Found ${items.length} video${items.length === 1 ? '' : 's'}`;
  const skip = items.length - picked.size;
  $('selinfo').textContent = picked.size
    ? `${picked.size} selected` + (skip ? `, ${skip} skipped` : '')
    : 'Nothing selected';
  $('dl').disabled = picked.size === 0;
}

$('selAll').onclick = () => { picked = new Set(items.map(i => i.i)); renderList(); };
$('selNone').onclick = () => { picked = new Set(); renderList(); };
$('selNew').onclick = () => {
  picked = new Set(items.filter(i => !i.have).map(i => i.i));
  renderList();
};

// ---- actions ---------------------------------------------------------
async function post(url, body) {
  const res = await fetch(url, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body || {}),
  });
  return res.json();
}

function startPolling() {
  gotItems = false;
  poll();
  clearInterval(timer);
  timer = setInterval(poll, 500);
}

$('preview').onclick = async () => {
  $('err').textContent = '';
  const d = await post('/api/preview', {links: $('links').value, options: options()});
  if (!d.ok) { $('err').textContent = d.error || 'Could not start.'; return; }
  items = []; picked = new Set();
  $('preview-card').style.display = 'none';
  $('live').style.display = 'block';
  $('summary').innerHTML = '';
  busy(true);
  startPolling();
};

$('go').onclick = async () => {
  $('err').textContent = '';
  const d = await post('/api/start', {links: $('links').value, options: options()});
  if (!d.ok) { $('err').textContent = d.error || 'Could not start.'; return; }
  items = []; picked = new Set();
  $('preview-card').style.display = 'none';
  $('live').style.display = 'block';
  $('summary').innerHTML = '';
  busy(true);
  startPolling();
};

$('dl').onclick = async () => {
  $('err').textContent = '';
  const d = await post('/api/download', {selected: [...picked], options: options()});
  if (!d.ok) { $('err').textContent = d.error || 'Could not start.'; return; }
  $('summary').innerHTML = '';
  busy(true);
  startPolling();
};

$('stop').onclick = () => post('/api/cancel');

// Populate the Instagram login list with the browser profiles that actually
// exist. "Chrome" alone means Chrome's Default profile, which is the usual
// reason a session appears to be missing when you are logged in elsewhere.
(async () => {
  try {
    const d = await (await fetch('/api/browsers')).json();
    const sel = $('cookiesBrowser');
    for (const o of d.options) {
      const el = document.createElement('option');
      el.value = o.value;
      el.textContent = o.label;
      sel.appendChild(el);
    }
    if (d.selected) sel.value = d.selected;
  } catch (e) { /* leave the None-only list in place */ }
})();

$('checkLogin').onclick = async () => {
  const browser = $('cookiesBrowser').value;
  const status = $('loginstatus');
  if (!browser) {
    status.textContent = ' — pick a browser profile first.';
    status.style.color = 'var(--bad)';
    return;
  }
  status.style.color = 'var(--muted)';
  status.textContent = ' — checking… macOS may ask for your keychain password.';
  $('checkLogin').disabled = true;
  try {
    const d = await post('/api/checklogin', {browser});
    status.textContent = ' — ' + d.detail;
    status.style.color = d.ok ? 'var(--ok)' : 'var(--bad)';
  } catch (e) {
    status.textContent = ' — check failed.';
    status.style.color = 'var(--bad)';
  }
  $('checkLogin').disabled = false;
};

$('kill').onclick = async () => {
  const warning = 'Immediately terminate all downloads, ffmpeg processes, and the local server? Partial files will be kept.';
  if (!confirm(warning)) return;
  clearInterval(timer);
  $('kill').disabled = true;
  $('stop').style.display = 'none';
  $('phase').textContent = 'Program terminated';
  $('err').textContent = 'The downloader has been killed. You can close this tab.';
  try { await post('/api/kill'); } catch (_) {}
  setTimeout(() => window.close(), 500);
};

$('reveal').onclick = async () => {
  const d = await post('/api/reveal');
  if (!d.ok) $('err').textContent = d.error || '';
};

const PHASES = {
  resolving: 'Finding videos…',
  ready: 'Ready — pick what to download',
  empty: 'Nothing downloaded',
  downloading: 'Downloading…',
  done: 'Finished',
  error: 'Stopped',
  idle: 'Ready',
};

async function poll() {
  let s;
  const url = gotItems ? '/api/state?items=0' : '/api/state';
  try { s = await (await fetch(url)).json(); } catch (e) { return; }

  $('phase').textContent = s.cancelling ? 'Stopping…' : (PHASES[s.phase] || s.phase);
  $('elapsed').textContent = s.elapsed ? s.elapsed + 's' : '';
  $('count').textContent = s.total ? `${s.done} / ${s.total}` : '';
  $('overall').style.width = s.total ? (100 * s.done / s.total) + '%' : '0';

  if (s.items && !gotItems && s.items.length) {
    items = s.items;
    picked = new Set(items.filter(i => !i.have).map(i => i.i));
    if (!picked.size) picked = new Set(items.map(i => i.i));
    gotItems = true;
    renderList();
  }
  if (s.phase === 'ready' && items.length) $('preview-card').style.display = 'block';

  $('files').innerHTML = s.active.map(f => {
    const pct = f.total ? Math.min(100, 100 * f.completed / f.total) : 0;
    const size = f.total ? `${bytes(f.completed)} / ${bytes(f.total)}` : bytes(f.completed);
    return `<div class="file"><div class="nm"><span>${esc(f.label)}</span>
            <span>${size}</span></div>
            <div class="bar"><i style="width:${pct}%"></i></div></div>`;
  }).join('');

  $('problems').innerHTML = (s.errors || []).map(e =>
    `<div class="problem"><b>Could not read this source</b>${esc(e)}</div>`).join('');

  const log = $('log');
  const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 24;
  log.textContent = s.notes.join('\n');
  if (atBottom) log.scrollTop = log.scrollHeight;

  if (s.summary) {
    const t = s.summary;
    let html = '<div class="tiles">'
      + tile('ok', t.downloaded, 'Downloaded')
      + (t.skipped ? tile('', t.skipped, 'Already had') : '')
      + (t.filtered ? tile('', t.filtered, 'Filtered') : '')
      + (t.failed ? tile('bad', t.failed, 'Failed') : '')
      + '</div>';
    if (s.outDir) html += `<div class="hint">Saved to <code>${esc(s.outDir)}</code></div>`;
    for (const f of s.failures) {
      html += `<div class="fail"><div>${esc(f.title)}</div><div>${esc(f.detail)}</div></div>`;
    }
    $('summary').innerHTML = html;
  }

  if (['done', 'error', 'ready', 'empty'].includes(s.phase)) {
    clearInterval(timer);
    busy(false);
    if (s.phase !== 'ready') $('dl').disabled = picked.size === 0;
  }
}

const tile = (cls, n, label) =>
  `<div class="tile ${cls}"><b>${n}</b><span>${label}</span></div>`;
</script>
</body>
</html>
"""
