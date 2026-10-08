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
| Per-site routing via WARP when a site blocks the server's IP (YouTube, X, Instagram, Pinterest, Reddit, TikTok; `PROXY_DOMAINS`). yt-dlp + gallery-dl only (aria2c can't do SOCKS) | `downloader/proxy.py`, `dispatcher._routed`, `warp` service in compose |
| Stray-process reaping (ffmpeg/aria2c) per job workspace | `utils/procs.py`, `utils/cleanup.job_workspace` |
| **Toolbox**: send a video/audio file (or Tools on /start, `/tools`) -> trim (fast/exact), extract audio (MP3/M4A), compress to 10/25/50/100 MB, GIF, remove metadata, burn an .srt. Runs as a normal job (`url=tool://<name>`, `settings["tool"]`), input kept in `tools.store` (TMP_DIR/tools, 1 h TTL, max 3 per person) | `downloader/tools.py`, `ui/tools_menu.py`, `main.media_handler` / `tools_callback` / `srt_handler`, `jobqueue/job_manager._run_job` |
| Subtitles mode **Burned in** (first language drawn into the picture, others as .srt; videos over `BURN_MAX_SECONDS` get an .srt instead) | `ytdlp_handler._burn_subtitles`, `tools.burn_into`, `ui/subtitle_menu` |
| History with linked titles; History on /start | `ui/history_menu.py` |
| Settings: ADHD toggle, **Appearance** (sizes + bar style), Advanced, Cookies, Reset | `ui/settings_menu.py` |
| Post-processing fallbacks (retry without cover art; Opus -> MP3); ffmpeg's real error captured to logs | `ytdlp_handler.download` |
| X/Pinterest: yt-dlp first (full menu for video); plain Download on an image post sets `prefer_gallerydl` so gallery-dl goes first | `site_map` (`IMAGE_SITES`), `main.link_handler`, `dispatcher.download` |

## Conventions worth keeping
- Callback data `ns|action|...|<id>` (id last, < 64 bytes): `dl|sec`, `dl|sub`, `bt`, `hist`, `misc`, `s`, `nav`.
- `delivery_notes` (a list in the job's settings dict) = things to tell the person after delivery; `job_manager` sends them.
- Errors shown inside menus as a `⚠` line (a second `query.answer()` is ignored by Telegram).
- Anything user/site-controlled is HTML-escaped where it enters a template.
- Symbols, not emoji, for log/UI glyphs where possible (`ui/steplog.py` maps step text -> symbol: ✦ video, 𝄞 audio, ✄ clip ...).
- Callback namespace `tl|action|...|<rid>` = toolbox. Tool jobs are not logged to /history and have no link / send-as-file buttons (`quick_menu._markup` drops empty rows).
- Sections and subtitles are mutually exclusive (subtitles are for the whole video: wrong timing on a clip).
- ADHD Mode is unchanged: no menus, first link only, no picker.

## Decisions (developer's)
- No shared/global cookie. No inline mode. Subtitles: soft (embedded / .srt) AND burned-in. Burned-in re-encodes (x264 veryfast, audio copied), so it is capped by `BURN_MAX_SECONDS`; the image now has `fonts-noto-core` (Persian/Arabic, no CJK).
- The upload/share-link center is **not built here**: it lives in the developer's other project (candyflix, github.com/AmirHBuilds/candyflix); this bot will only be an **API client** (needs: endpoints, key header, upload method, limits, link shape).
- VPS blocking: developer will test provider/IP/WARP themselves; the bot already routes via WARP automatically when blocked.

## Changelog
- rev1 (2026-10-07): WARP routing generalised from YouTube-only to a per-site list with per-site block tracking; gallery-dl now routed too; PySocks added to requirements (gallery-dl needs it for SOCKS); X/Pinterest order is yt-dlp first. Tests: 400. Patch zips contain only changed files, in project structure; copy over the project.

- rev2 (2026-10-07): Toolbox, burned-in subtitles, Tools on /start. Needs a rebuild (new fonts in the image).
- rev3 (2026-10-07): Subtitles screen names the language that gets burned in; housekeeping loop (`utils/housekeeping.py`, every 5 min): kills orphaned ffmpeg/ffprobe/aria2c/gallery-dl whose job folder is no longer active, collects zombies, sweeps the local Bot API server's old copies of sent files; the server's copy of an upload / .srt / cookies.txt is deleted right after the bot has its own; `init: true` on the bot service (tini reaps orphans); the bot's `bot_api_data` mount is now read-write.
- rev4 (2026-10-08): Subtitles + Add section share a row on More options; subtitle delivery is three independent switches (Embedded / .srt file / Burned in; Embedded and Burned in exclude each other; modes `embed|file|both|burn|burnfile`); a subtitle failure (YouTube HTTP 429 on the timedtext request) no longer just drops them: the video is downloaded without, then the subtitles are fetched in their own yt-dlp pass (`SUBTITLE_RETRY_DELAYS` 0/8/25 s, only missing languages re-asked) and embedded/attached/burned by us (`_recover_subtitles`, `tools.embed_subtitles`). The note now says when it was a 429.
- rev5 (2026-10-08): toolbox keeps the upload for an hour after the LAST use (clock restarts after each result) and the progress message turns back into the toolbox after a result (`job_manager._restore_toolbox`) so more tools can be run; toolbox/burn/embed progress is one updating line (ffmpeg reports its work as a LABEL, not a stage: a stage starts a new line at every tick - `tools.run_ffmpeg`); a failed preview now shows the site's own reason under the note (`messages.preview_failed_reason`).
- rev6 (2026-10-08): cookies are per site inside the one per-person file (`downloader/cookies.py`: `merge_upload`, `list_sites`, `remove_site`): an upload replaces only the sites it contains (YouTube = youtube.com + every google.* domain, X = x.com + twitter.com), a file with no cookies changes nothing (it used to overwrite the working file), the Cookies screen lists sites with a 🗑 Remove button each (`ck|rm|<site>`, `ui/cookies_menu.py`); when yt-dlp fails and gallery-dl still finds a title/thumbnail the menu now says the preview failed and why.

## Unverified in real use (check first after a rebuild)
1. `warp` container actually starts on the developer's setup (`docker compose logs warp`); the bot works direct if not.
2. ffmpeg `-progress` via yt-dlp's `external_downloader_args["ffmpeg_i"]` (clip progress bar). If not honoured the bar falls back to elapsed time and logs "No ffmpeg progress data".
3. Menu size vs real size mismatch (one report: 2.2 GB shown, ~750 MB fetched). Logs now print `Size estimate (best): ...` and `Size check: formats ..., announced ..., actually fetched ...` — compare them.
4. Opus "Conversion failed": cause unknown; the log now carries ffmpeg's real complaint, and the bot falls back (no cover art, then MP3).
5. Instagram: previews usually fail without cookies, which drops to the plain menu (no sizes/sections).
6. Telegram's built-in player probably doesn't show embedded subtitle tracks (the .srt option exists for that).
7. rev1: gallery-dl through `socks5h://warp:1080` (needs the rebuild for PySocks); Instagram/X blocks being recognised by the 403/429 wording; ADHD Mode on an X *image* post now shows one failed yt-dlp step before gallery-dl succeeds.
8. Many-file sends may hit Telegram flood limits (batches are capped at 50).

## Next
- candyflix API client + "Get a link" as one more button in the toolbox / start menu.
- Toolbox ideas not built: images (strip EXIF, convert), merge files, change speed, resize, cover art for audio. Per-link prompts for any removed global settings. VPS deployment.
9. rev2: a real Persian video with burned-in subtitles (font/shaping in the Docker image: `docker compose exec bot fc-list | grep -i arab`); an upload of ~1 GB through the local Bot API (`download_to_drive` in local mode); the toolbox on a very large file (disk: tools.store keeps up to 3 originals per person for 1 h).
10. rev3: the Bot API storage layout (`/var/lib/telegram-bot-api/<token>/{documents,videos,...}`) - `utils/cleanup.SERVER_FILE_DIRS` is a whitelist so the server's database is never touched, but check with `docker compose exec bot ls /var/lib/telegram-bot-api/*` and watch the `Housekeeping:` log lines; after a large upload `du -sh` that volume should drop right away. Zombies: `docker compose exec bot ps -eo pid,stat,comm | grep Z` should print nothing.
11. rev4: whether the second-chance subtitle fetch actually gets through YouTube's 429 on your setup (a WARP address is shared, so it may stay limited for a while - a direct retry or cookies are the next levers); embedded tracks in mp4 (mov_text) showing up in VLC / players (Telegram's player still won't list them).
12. rev5: Instagram. Its order is yt-dlp first (gallery-dl is only the fallback download tool), so no sections/sizes/qualities means yt-dlp's preview failed - the menu now prints the reason. Without a logged-in cookies file Instagram usually answers "rate-limit reached or login required". Check `docker compose logs bot | grep "Probe failed.*instagram"`.
