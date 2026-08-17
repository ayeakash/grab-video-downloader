# grab

Bulk downloader for short videos from YouTube and Instagram.

Give it anything — one link, fifty links, a YouTube channel, an Instagram page —
and it figures out what you meant, expands it into a list of videos, and pulls
them down in parallel. Re-running never re-downloads what you already have.

## Setup

### macOS / Linux

```bash
./setup.sh
```

### Windows

```bat
setup.bat
```

Then use `grab.bat` wherever this README says `./grab` — e.g. `grab.bat --web`.
You need [Python](https://python.org) (tick **Add python.exe to PATH** during
install) and ffmpeg (`winget install Gyan.FFmpeg`). The browser-profile picker
covers Chrome, Edge, Brave, Vivaldi, Chromium and Firefox; Safari is macOS only.

> Windows support is written but **untested** — this was built and verified on
> macOS. The engine and the web UI are plain cross-platform Python, so the
> likely failures are environmental (Python or ffmpeg not on PATH) rather than
> logical. Please report anything that breaks.

That creates a `.venv`, installs `yt-dlp` + `instaloader`, and installs `ffmpeg`
via Homebrew if it is missing. Then confirm everything is wired up:

```bash
./grab --doctor
```

## The easy way: the browser interface

Double-click **`Open Downloader.command`** in Finder, or run:

```bash
./grab --web
```

A page opens at <http://127.0.0.1:8765> with a box to paste links into and
dropdowns for the options. It shows live progress and a summary when done.
Ctrl-C in the terminal stops the server. In the web UI, **Stop downloads**
cancels the current batch while leaving the app running. **Kill program**
immediately terminates downloads, ffmpeg/helper processes, and the local web
server; partial files remain available for resuming later.

There are two buttons:

- **Preview first** — resolves your links and shows the full list with
  thumbnails, titles, channel and length, each with a checkbox. Untick anything
  you don't want, then hit **Download selected**. Use this for channels and
  profiles, where you don't know what you're about to pull.
- **Download everything** — skips the preview and starts immediately. Fine for
  a handful of direct links.

In the preview, anything already in your archive is marked **have it** and
starts unchecked, so the default selection is exactly the new videos. The
**All / None / Only new** shortcuts re-select in one click.

It binds to `127.0.0.1` only, so nothing outside your Mac can reach it. There
is no password — don't expose the port to a network.

## The CLI

Every one of these works:

```bash
./grab https://www.youtube.com/shorts/VIDEO_ID          # one Short
./grab URL1 URL2 URL3                                   # several at once
./grab https://www.youtube.com/@somechannel             # the channel's Shorts tab
./grab https://www.instagram.com/someaccount            # every reel on the page
./grab https://www.instagram.com/reel/SHORTCODE/        # one reel
./grab https://www.youtube.com/playlist?list=PL...      # a playlist
./grab -f links.txt                                     # a file of links
./grab                                                  # reads links.txt, else prompts you to paste
cat links.txt | ./grab                                  # stdin
```

You can mix platforms freely in one run. Shorthand `yt:mkbhd` and `ig:natgeo`
work too, and a bare `@handle` is treated as YouTube.

### Common options

| Flag | What it does |
| --- | --- |
| `-n, --limit N` | Take at most N videos from each channel/profile |
| `-j, --jobs N` | Parallel downloads (default 4) |
| `-o, --out DIR` | Where to save (default `./downloads`) |
| `--all` | Any length, not just shorts |
| `--long` | Only videos longer than the shorts cutoff |
| `--short-seconds N` | Change the shorts cutoff (default 180) |
| `--tab shorts,videos` | Which channel tabs to pull (default `shorts`; `all` = shorts+videos+streams) |
| `-q, --quality` | `best`, `2160`, `1440`, `1080` (default), `720`, `480` |
| `--audio` | Extract to mp3 instead of video |
| `--since` / `--until` | Date range, `YYYYMMDD` |
| `-p, --pick` | Show the resolved list and choose what to skip before downloading |
| `--dry-run` | List what would be downloaded, download nothing |
| `--no-archive` | Re-download even things you already have |
| `--cookies-browser` | Pull cookies from a logged-in browser — **required for Instagram** |
| `--save-config` | Persist the current options as your defaults |
| `--web` | Open the browser interface (add `--port N` to change the port) |

Examples:

```bash
./grab https://www.youtube.com/@somechannel --pick
./grab https://www.youtube.com/@somechannel -n 50 -j 8
./grab https://www.instagram.com/natgeo --cookies-browser chrome
./grab -f links.txt --all -q 720 -o ~/Movies/Clips
./grab https://www.youtube.com/@somechannel --tab videos --long -n 10
```

## Instagram needs a login

Instagram serves almost nothing to logged-out clients, so listing a profile
will fail without a session. Log in to Instagram in a normal browser, then:

```bash
./grab https://www.instagram.com/someaccount --cookies-browser chrome
```

In the web UI, the **Instagram login** dropdown lists every browser profile
found on this Mac by name (e.g. "Chrome — Freelancer"), and **Check Instagram
login** verifies the session, reports which account it belongs to, and saves it
as your default. Do that once and you are done.

**If you use more than one Chrome profile, this matters.** Plain `chrome` means
Chrome's *Default* profile only. If you are logged into Instagram in a second
profile, a bare `chrome` finds no session and the listing fails — which looks
identical to not being logged in at all. Name the profile explicitly:

```bash
./grab https://www.instagram.com/someaccount --cookies-browser "chrome:Profile 2"
```

Supported: `chrome`, `chromium`, `brave`, `edge`, `safari`, `firefox`, `opera`,
`vivaldi`, `whale`. A `cookies.txt` file works too, via `--cookies cookies.txt`.

Make it the default so you stop typing it:

```bash
./grab --cookies-browser "chrome:Profile 2" --save-config
```

> The first read of a Chrome/Brave/Edge cookie store makes macOS ask for your
> **login keychain password** — those browsers encrypt cookies with a key kept
> there, so any tool reading them has to unlock it. Choosing "Always Allow"
> stops it asking again. Denying is fine; Instagram simply stays unavailable.

Listing **does** work without a session — Instagram just throttles it hard, so
expect waits. Logging in is dramatically faster and far less fragile.

Those waits used to be invisible: instaloader's reaction to a 429 is to sleep
until its sliding window clears, up to about eleven minutes, printing nothing.
That is what made the app look hung. The waiting is still allowed, but it is
now announced in the activity log, capped (90s per wait; 7 min total when
anonymous, 90s when logged in), and **Stop** interrupts it.

### How fast can this actually go?

Measured on this machine, and the honest answer is **your connection is the
limit**, not the tool:

| Thing | Measured |
| --- | --- |
| Internet ceiling | ~5 MB/s (~40 Mbps), single stream already saturates it |
| Parallel streams | 4 streams gave *less* total throughput than 1 |
| Instagram reel | ~10–15 MB each, one fixed quality (no smaller option exists) |
| 142 reels | ~2 GB → **~6.7 min is the theoretical floor** |
| Actual, after tuning | ~7–8 min (was ~28 min) |

So the pipeline now runs within roughly 15% of what the connection physically
allows. Things that do **not** help, all verified rather than assumed:

- **More workers.** Beyond ~4 it gets slower, not faster — the pipe is full.
- **Lower quality on Instagram.** Formats `1`, `2`, `3`, `b` and `worst` all
  return the *identical* file. Instagram publishes one progressive rendition.
- **A faster Mac.** Encoding is not involved; nothing is re-encoded.

Things that did help, and are now the defaults:

- **Progressive over DASH for Instagram** — ~20% faster, because DASH costs a
  second request plus an ffmpeg merge per reel, and it lands vp9 instead of h264.
- **4 workers with a 0.5s gap** instead of 1 worker with a 4s gap.
- **The archive.** Re-runs skip known videos with zero network calls, so the
  second run of a channel is nearly instant.

To genuinely go faster you need fewer bytes: use **Max per page**, or `-q 720`
on YouTube (roughly halves the size; on Instagram it changes nothing).

### When a profile refuses to list

Some Instagram accounts — business-category ones in particular — make
Instagram's own `web_profile_info` endpoint answer with:

```
400 Bad Request — Asset asset://laser.provider/ig_business_category_subvertical
has been deleted. You cannot use this schema
```

That is a fault on Instagram's side. It happens **identically whether or not
you are logged in**, so it is not a session problem and logging in will not fix
it. `grab` detects it and silently retries the listing through `gallery-dl`,
which reads the reels tab by a different route. The same fallback also covers
instaloader being rate-limited, so a 401 mid-listing is usually recovered too.

### Instagram speed

Instagram downloads run 3 at a time with a 1s gap. Tick **Gentle Instagram
pace** to fall back to one at a time with a 4s gap — roughly 4× slower, but
much less likely to get you temporarily rate-limited. If you start seeing
failures mid-run, switch to gentle and wait 10–15 minutes before retrying.

Large profiles are the main way to get blocked; **Max per page** keeps both
the listing and the download short.

> On macOS, reading Chrome cookies may prompt for your Keychain password, and
> Safari requires Full Disk Access for your terminal.

## How files are organized

```
downloads/
├── YouTube/
│   └── ChannelName/
│       └── 2026-07-10 Video Title [VIDEOID].mp4
├── Instagram/
│   └── accountname/
│       └── 2026-07-02 Caption text [SHORTCODE].mp4
├── .downloaded.txt     ← the archive; delete to allow re-downloads
└── failed.txt          ← written when something fails; retry with -f
```

The `[VIDEOID]` suffix is what makes re-runs safe, so keep it.

## Notes

- **Skipping is instant.** Re-running a channel checks the local archive first
  and makes no network calls for videos you already have.
- **Only successes are recorded.** A download that fails, or that you stop
  part-way, is never written to the archive, so the next run retries it.
  Interrupted files keep their `.part` and resume rather than restarting.
- **Deleted a file? It will not come back on its own.** The archive stores
  video ids, not paths, so anything you delete or move still counts as "have
  it". `grab` now notices — it compares the archive against the `[id]` in the
  filenames actually on disk and tells you how many are missing. Tick
  **Re-download missing** (or `--redownload-missing`) to fetch them again.
  It is off by default so that moving your library to another drive does not
  trigger a full re-download.
- **Failures don't stop the batch.** Anything that fails lands in
  `downloads/failed.txt`; retry with `./grab -f downloads/failed.txt`.
- **Interrupting is safe.** Ctrl-C leaves partial files that resume next run.
- **Quality defaults to 1080p H.264.** Shorts are vertical (1080×1920), and the
  cap is applied to the *narrow* side. H.264 is preferred over AV1 at equal
  resolution because AV1 chokes most editors and Apple hardware decoders. Use
  `-q best` to lift the cap.
- **When downloads suddenly break**, YouTube or Instagram changed something.
  Re-run `./setup.sh` to upgrade yt-dlp; that fixes it the large majority of the
  time.

Download things you have the rights to download, and respect each platform's
terms of service and the creators' rights.
