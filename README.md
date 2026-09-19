# 🍬 CandyDownloader

A Telegram bot that downloads (almost) anything from a link — built the
same way as [media-downloader](https://github.com/mhogomchungu/media-downloader):
a thin dispatcher in front of the real workhorses, **yt-dlp**, **gallery-dl**,
and **aria2c**, picking whichever tool fits the link and falling back to the
next one if it fails.

Made with love for Candy. 🍭

---

## Features

- **Any link, one bot**: paste a URL, get the file back. Video/audio sites
  go through yt-dlp, image galleries through gallery-dl, plain direct
  links through aria2c — picked automatically, with fallback if the first
  choice fails.
- **Up to 2GB files** via a self-hosted Telegram Bot API server (not the
  50MB cloud default).
- **YouTube bot-check handled the standard way**: on a "sign in to
  confirm you're not a bot" error, the bot automatically retries with
  alternate player clients (tv/android/ios) — the actual first-line fix
  most yt-dlp setups use, since YouTube scrutinizes its own web client
  far more than these. A PO Token provider container backs this up for
  tougher cases. `/cookies` remains as the final fallback for the rare
  video that needs a real login. `/potcheck` (admin-only) shows what's
  actually being used for a given link.
- **Full per-user settings** via `/settings` — quality, audio-only +
  format/bitrate, subtitles (auto/manual, embed or not), playlist mode
  (single/full/range), thumbnail & metadata embedding, SponsorBlock,
  duplicate-skip archive, rate limiting, parallel fragments, proxy,
  cookies for logged-in sites.
- **Candy progress bar** 🍬🍬🍬🍬▫️▫️▫️▫️ with live speed/ETA, updated by
  editing a single message in place — no chat spam.
- **Non-blocking job queue**: downloads run in a worker pool
  (`MAX_CONCURRENT_DOWNLOADS`), so the bot stays responsive and progress
  updates never stall a download.
- **Auto-cleanup**: every job gets a throwaway workspace folder that's
  deleted the moment the file is sent — success, failure, or crash. Your
  VPS disk stays flat.
- **Daily self-update**: yt-dlp and gallery-dl are `pip install -U`'d once
  a day automatically (sites change constantly and break old versions),
  with a report sent to admins. Manual trigger via `/update`.
- **Real dynamic quality menu**: the bot probes the link first and only
  shows resolutions that actually exist for that video (no more offering
  1080p on a 720p-max clip). The default menu shows the top 3 plus best/
  audio; a "More options" button opens the full resolution list with a
  way back. Sites with no real quality concept (image galleries, plain
  direct files) skip the quality step entirely. If probing itself fails,
  a plain three-option fallback (best/smallest/audio) is used instead of
  guessing specific resolutions - and for sites like Instagram where
  yt-dlp's probe often gets blocked, a secondary gallery-dl-based probe
  is tried just for a title/thumbnail preview.
- **Spotify support**: Spotify's streams are DRM-protected and can never
  be downloaded directly (no tool can fix that). Instead, the bot reads
  the track's title via Spotify's public oEmbed endpoint and downloads a
  matching YouTube upload as audio — the standard approach every real
  "Spotify downloader" uses. Its own menu has no video/quality options.
- **Thumbnail + title preview** for links the bot can probe, so you see
  what you're about to download before picking an option. The original
  link message is deleted once received, so the whole exchange collapses
  into one evolving message rather than a growing chat thread.
- **Send as file**: attached right on the video message itself, this
  re-sends the exact same file (same quality you picked) as a plain
  document instead of Telegram's compressed/streamable video preview. A
  short-lived cache (5 minutes) means tapping it doesn't trigger a fresh
  download — it reuses the file already sitting on disk, then it's
  cleaned up like everything else.
- **Cancel button** on every stage of the process, including while still
  waiting in the queue (not just once downloading starts) and a "Try
  again" button if a download fails.
- **Queue heartbeat**: while waiting for a free download slot, the status
  message visibly updates ("Preparing your download...", live progress
  once it starts) instead of sitting on a static "Queued" forever.
- **Owner admin panel**: a `/{name}_admin` command (named after whoever
  `OWNER_NAME` is set to) opens an inline-button panel to flip the bot
  between public/private, manage an allow-list of who can use it, turn on
  a force-join-this-channel requirement, and see usage stats and recent
  activity — all stored in the database, changeable anytime without
  touching `.env` or redeploying.
- **Personalized throughout**: `OWNER_NAME`/`OWNER_EMOJI` show up in the
  welcome message, progress bar, and captions — not hardcoded "Candy".
- **One-command Docker deploy.**

---

## Setup

### 1. Get your credentials

- **Bot token**: message [@BotFather](https://t.me/BotFather) on Telegram, `/newbot`.
- **API ID/hash**: go to https://my.telegram.org → "API development tools",
  log in with your own phone number (free, one-time). This is required
  for the self-hosted Bot API server, not the bot token itself.
- **Owner's Telegram user ID**: message [@userinfobot](https://t.me/userinfobot)
  from the account this bot is for. This goes in `OWNER_USER_ID` — she
  always has admin-panel access and always bypasses access control,
  regardless of what she sets there.
- **Your own Telegram user ID** (as the developer/maintainer): same way,
  goes in `ADMIN_USER_IDS`.

### 2. Configure

```bash
cp .env.example .env
nano .env   # fill in BOT_TOKEN, TELEGRAM_API_ID, TELEGRAM_API_HASH,
            # ADMIN_USER_IDS, OWNER_USER_ID, OWNER_NAME, OWNER_EMOJI
```

### 3. Run

```bash
docker compose up -d --build
```

That's it. Two containers come up:
- `telegram-bot-api` — the local Bot API server (2GB file limit)
- `bot` — CandyDownloader itself

Check logs any time with:
```bash
docker compose logs -f bot
```

### 4. Talk to your bot

Open Telegram, find your bot, hit `/start`. Paste any link and watch the
candy bar fill up.

---

## Updating later

```bash
git pull            # if you change the code
docker compose up -d --build
```

yt-dlp/gallery-dl themselves update automatically every day at
`AUTO_UPDATE_HOUR_UTC` — no rebuild needed for those, they're plain pip
packages patched in place inside the running container. Admins can also
force it immediately with `/update` in the bot chat.

---

## How the dispatcher works

For each link, `downloader/site_map.py` decides which tool(s) to try and
in what order (e.g. Instagram tries yt-dlp then gallery-dl; Pixiv goes
straight to gallery-dl; an unrecognized domain tries yt-dlp → gallery-dl →
plain aria2c download as a last resort). If a tool fails partway through,
its partial output is wiped and the next tool in line is tried — the user
just sees the progress bar keep going, not an error followed by a retry.

## Where things live

```
bot/
  main.py                  entrypoint, Telegram handlers
  config.py                env var loading
  downloader/
    dispatcher.py           picks/falls back between tools
    site_map.py              domain -> tool priority list
    probe.py                 fetches real title/thumbnail/quality options
    ytdlp_handler.py         yt-dlp integration + progress hook + client fallback
    gallerydl_handler.py     gallery-dl integration
    generic_handler.py       aria2c fallback for direct links
    spotify_handler.py       Spotify -> YouTube-audio search+download
  jobqueue/
    job_manager.py           async queue + worker pool + progress edits
  settings/
    user_settings.py         per-user settings schema + SQLite storage
    access_control.py        owner-managed public/private mode, allow-list, force-join
  ui/
    messages.py               all bot copy (personalized to the owner)
    progress.py                progress bar rendering
    settings_menu.py           inline keyboard menus for /settings
    quick_menu.py               per-link quick-pick + dynamic quality menu
    admin_menu.py                owner admin panel menus
    start_menu.py                /start inline buttons
  updater/
    auto_update.py            daily yt-dlp/gallery-dl pip upgrade
  utils/
    cleanup.py                 per-job temp workspace, guaranteed cleanup
```

## Owner admin panel

`/{name}_admin` (built from `OWNER_NAME` - e.g. "Candy" → `/candy_admin`,
"Amy Rose" → `/amy_rose_admin`) opens an inline-button panel, usable by
the owner and by anyone in `ADMIN_USER_IDS`:

- **Access: Public/Private** — in Private mode, only the owner, admins,
  and users on the allow-list can use the bot at all.
- **Allowed users** — add/remove by numeric Telegram user ID (only
  relevant in Private mode).
- **Force-join channel** — require users to join a given `@channel`
  before doing *anything* with the bot (including `/start`); owner/admins
  always bypass this. Once someone taps "I've joined" and passes, they
  get the real welcome message they were held back from.
- **Stats** — total known users, current mode, download counts.
- **Activity** — the last 10 downloads (who, what link, success/failed).
- **All users** — everyone who has ever interacted with the bot, not just
  the allow-list, so she can see who to consider adding.

All of this lives in the database, not `.env` — she can change it
anytime from Telegram without a redeploy.

## Notes & caveats

- **Disk space**: even with auto-cleanup, make sure your VPS has enough
  free space to briefly hold the largest file you expect to download at
  once (times `MAX_CONCURRENT_DOWNLOADS`).
- **Cookies for private/login content**: per-user and private — each
  Telegram user's cookie file only ever affects their own downloads.
  Send a `cookies.txt` file directly to the bot, or find the same help
  text inside `/settings`.
- **Legal**: same as the original media-downloader project — this is a
  general-purpose tool. What you point it at is on you.
- **Access control scope**: Private mode and force-join now gate every
  command, not just downloads — nothing works, not even `/start`, until
  both checks pass.
- This is a genuinely large, multi-part project. I built it in one pass
  end-to-end, so give it a real test run on your VPS and tell me about
  anything that misbehaves — happy to iterate.
