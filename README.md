# grab

Bulk downloader for short videos from YouTube and Instagram.

Give it anything — one link, fifty links, a YouTube channel, an Instagram page —
and it figures out what you meant, expands it into a list of videos, and pulls
them down in parallel. Re-running never re-downloads what you already have.

## Setup

```bash
./setup.sh
```

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

Without a session, listing a profile now fails **immediately** with that
message rather than appearing to hang. This matters because instaloader's
built-in reaction to Instagram's rate limiting is to sleep until its sliding
window clears — up to about eleven minutes, silently. `grab` caps any single
wait at 12s and the total at 45s, then gives up with an explanation.

Instagram also rate-limits hard. Downloads from it are deliberately serialized
with a delay between each; if you start seeing failures, wait 10–15 minutes.
Pointing this at a very large profile in one go is the fastest way to get
temporarily blocked — set **Max per page** to keep listings short.

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
