# CandyDownloader — handoff (Oct 2026)

Private Telegram bot that downloads video/audio/images from links (yt-dlp in-process, gallery-dl and aria2c as
subprocesses), branded per deployment (`OWNER_NAME`/`OWNER_EMOJI`). "Developer" = the person chatting; "owner" =
the bot's persona/admin. Python 3.12, python-telegram-bot 21.10, Docker Compose (bot + local Bot API server +
bgutil PO-token provider + optional warp proxy). The older architecture notes (rid as correlation key, local Bot API
volume, yt-dlp as a library so auto-update restarts the process) still hold.

## Run and test
- `docker compose up --build` after ANY code change (Dockerfile/requirements changes need it; so do code changes).
- Tests (383, offline): `cd bot && python -m unittest discover -s . -p "test_*.py" -t .` — stub packages for
  telegram / yt_dlp / httpx live in `bot/tests/stubs`; several tests drive real `ffmpeg`/`ffprobe`.
- Routine used for every change: write tests, then **mutation-check** (break the feature, confirm a test fails,
  restore). Always back files up to a stable dir and verify with `sha256sum -c` after restoring — a restore step that
  silently failed once left deliberate breakage in the tree.

## What exists now (beyond the original)
| Feature | Where |
|---|---|
| Time-range clips ("sections"): per-row Start/End, merge or separate, exact cuts, ffmpeg-progress bar, cancel kills ffmpeg | `downloader/sections.py`, `ui/section_menu.py`, clip path in `ytdlp_handler.py`, `main.sections_callback` |
| Chapters as one-tap sections; `?t=` links pre-fill a section | `probe._chapters`, `section_menu.chapters_*`, `main._prefill_start_time` |
| Sizes on buttons (estimated, setting `show_sizes`); HEAD lookup for sites that announce none | `downloader/sizes.py`, `probe._fill_missing_sizes` |
| Playlist / several-links picker with one shared batch message | `downloader/playlist.py`, `jobqueue/batch.py`, `ui/batch_menu.py`, `bt\|` callbacks |
| Music: tags, square cover, one MP3/Opus per chapter | `downloader/music.py`, `_finish_audio` |
| Soft subtitles (embed / .srt / both), Persian+English first, paged | `downloader/subtitles.py`, `ui/subtitle_menu.py`, `dl\|sub\|` callbacks |
| Cookie-expiry warning (per person, failure-based) | `downloader/cookie_health.py` |
| Safe logs (token redaction, httpx quiet) | `utils/safe_logging.py` |
| YouTube routing via WARP when the server's IP is blocked | `downloader/proxy.py`, `warp` service in compose |
| Stray-process reaping (ffmpeg/aria2c) per job workspace | `utils/procs.py`, `utils/cleanup.job_workspace` |
| History with linked titles; History on /start | `ui/history_menu.py` |
| Settings: ADHD toggle, **Appearance** (sizes + bar style), Advanced, Cookies, Reset | `ui/settings_menu.py` |
| Post-processing fallbacks (retry without cover art; Opus -> MP3); ffmpeg's real error captured to logs | `ytdlp_handler.download` |
| X/Pinterest videos previewed with yt-dlp (full menu), job told `prefer_ytdlp` | `main.link_handler`, `dispatcher.download` |

## Conventions worth keeping
- Callback data `ns|action|...|<id>` (id last, < 64 bytes): `dl|sec`, `dl|sub`, `bt`, `hist`, `misc`, `s`, `nav`.
- `delivery_notes` (a list in the job's settings dict) = things to tell the person after delivery; `job_manager` sends them.
- Errors shown inside menus as a `⚠` line (a second `query.answer()` is ignored by Telegram).
- Anything user/site-controlled is HTML-escaped where it enters a template.
- Symbols, not emoji, for log/UI glyphs where possible (`ui/steplog.py` maps step text -> symbol: ✦ video, 𝄞 audio, ✄ clip ...).
- Sections and subtitles are mutually exclusive (subtitles are for the whole video: wrong timing on a clip).
- ADHD Mode is unchanged: no menus, first link only, no picker.

## Decisions (developer's)
- No shared/global cookie. No inline mode. Soft subtitles only: **burned-in is deferred to the VPS** (re-encode is too heavy locally; needs a Persian-capable font in the image).
- The upload/share-link center is **not built here**: it lives in the developer's other project (candyflix, github.com/AmirHBuilds/candyflix); this bot will only be an **API client** (needs: endpoints, key header, upload method, limits, link shape).
- VPS blocking: developer will test provider/IP/WARP themselves; the bot already routes via WARP automatically when blocked.

## Unverified in real use (check first after a rebuild)
1. `warp` container actually starts on the developer's setup (`docker compose logs warp`); the bot works direct if not.
2. ffmpeg `-progress` via yt-dlp's `external_downloader_args["ffmpeg_i"]` (clip progress bar). If not honoured the bar falls back to elapsed time and logs "No ffmpeg progress data".
3. Menu size vs real size mismatch (one report: 2.2 GB shown, ~750 MB fetched). Logs now print `Size estimate (best): ...` and `Size check: formats ..., announced ..., actually fetched ...` — compare them.
4. Opus "Conversion failed": cause unknown; the log now carries ffmpeg's real complaint, and the bot falls back (no cover art, then MP3).
5. Instagram: previews usually fail without cookies, which drops to the plain menu (no sizes/sections).
6. Telegram's built-in player probably doesn't show embedded subtitle tracks (the .srt option exists for that).
7. Many-file sends may hit Telegram flood limits (batches are capped at 50).

## Next
- candyflix API client + "Get a link" in a future Tools menu (and the Tools/media-toolbox idea: trim, compress-to-fit, extract audio, GIF, metadata strip...).
- Burned-in subtitles on the VPS. Per-link prompts for any removed global settings. VPS deployment.
