import asyncio
import logging
import os
import re
import time
import uuid

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)

import config
from jobqueue.batch import Batch, BatchItem
from jobqueue.job_manager import JobManager
from settings import access_control as ac
from settings.user_settings import get_settings, update_setting, reset_settings
from ui import messages, admin_menu, batch_menu
from ui.subtitle_menu import subtitles_menu, subtitles_text
from ui.quick_menu import (
    extended_video_menu, simple_menu, fallback_menu, spotify_menu,
    queued_menu, redo_menu, quality_menu,
)
from ui.section_menu import (
    editor_text, editor_menu, prompt_text, prompt_menu, sections_summary,
    chapters_text, chapters_menu, chapter_pages,
)
from ui.settings_menu import (
    main_menu, advanced_menu, confirm_reset_menu, back_to_main, ADVANCED_TEXT_FIELDS,
    bars_menu, bars_title, look_menu, look_title,
)
from ui.history_menu import (
    history_text, history_menu, confirm_clear_menu, PAGE_SIZE, ORIGIN_HOME, ORIGIN_SETTINGS,
)
from ui.start_menu import start_menu
from ui import tools_menu
from downloader import tools, warp_control
from downloader.proxy import tcp_alive
from downloader import cookie_health
from downloader.probe import probe, ProbeResult
from downloader.sections import SectionDraft, SectionError, parse_timestamp, start_time_from_url
from downloader.sizes import size_labels
from downloader.subtitles import SubChoice, SubtitleError, clamp_page as sub_clamp_page, summary as subtitle_summary
from downloader import gallerydl_probe
from downloader.cookies import (
    inspect_cookie_file, list_sites, merge_upload, remove_site, site_label,
)
from ui import cookies_menu
from downloader.playlist import (
    Entry, ListingError, MAX_LISTED, list_playlist, looks_like_playlist, quick_titles, short_label,
)
from downloader.site_map import is_image_site, tool_order_for
from downloader.dispatcher import FRIENDLY_TOOL
from downloader.spotify_handler import get_track_info
from updater.auto_update import daily_update_loop, run_update_once
from utils.cleanup import drop_server_copy, sweep_orphaned_workspaces
from utils import housekeeping
from utils.safe_logging import install_safe_logging
from utils.text import esc
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
install_safe_logging(config.BOT_TOKEN)   # the token is in every Bot API URL - never let it reach the logs
log = logging.getLogger("candy.main")

URL_RE = re.compile(r"https?://\S+")

job_manager: JobManager | None = None
# Every one of these is keyed by a per-request token (rid), not just the
# user's id. Two links from the same user in flight at once each get
# their own slot - otherwise the second link would silently overwrite
# the first's state and the first message's buttons would end up
# operating on the wrong link.
pending_links: dict[str, tuple[int, str]] = {}          # rid -> (user_id, url)
pending_probes: dict[str, ProbeResult] = {}             # rid -> probe result, for "more options"
pending_admin_input: dict[int, str] = {}                # user_id -> which admin panel field they're typing
pending_setting_input: dict[int, str] = {}              # user_id -> which advanced-settings field they're typing
last_download: dict[str, tuple[int, str, dict]] = {}    # rid -> (user_id, url, settings)
pending_sections: dict[str, SectionDraft] = {}          # rid -> the clip-sections editor's state
# user_id -> where a typed timestamp should go: {rid, field, chat_id, message_id, is_photo, at}.
# Keyed by user (a person can only type one thing at a time) and time-limited,
# unlike pending_admin_input, so a forgotten prompt can't swallow a message
# minutes later.
pending_section_input: dict[int, dict] = {}
# bid -> a playlist / several-links picker, then the running batch (the id is also a "rid" for _touch_rid / sweeping)
pending_batches: dict[str, Batch] = {}
pending_subs: dict[str, SubChoice] = {}                  # rid -> the subtitle languages picked for that link
# user_id -> what the toolbox is waiting for: {rid, kind: "trim" | "gif" | "srt", chat_id, message_id, at}
pending_tool_input: dict[int, dict] = {}
tool_drafts: dict[str, dict] = {}                        # rid -> {"start", "end"} typed for a trim, awaiting Fast / Exact
SECTION_INPUT_TTL_SECONDS = 10 * 60

# None of the rid-keyed dicts above ever had anything removing an entry
# once its message was long gone (deleted, or from a chat the person left)
# - a heavily-used bot would accumulate one entry per link forever. This
# tracks when each rid was last touched so a background sweep can drop
# anything old enough that it can no longer be reached from any live
# button (Telegram callback buttons on a message don't expire, but a
# multi-hour-old "Queued"/quality-pick button is realistically dead).
_rid_last_touch: dict[str, float] = {}
RID_STATE_TTL_SECONDS = 2 * 60 * 60

# Populated only by the "cancel" action while a link is still being probed
# (no job exists yet to cancel) - probe()/get_track_info() keep running in
# the background regardless, so link_handler/handle_spotify_link check
# this once they come back to avoid clobbering the Cancelled message with
# a freshly-finished quality picker.
cancelled_pre_job_rids: set[str] = set()


def _touch_rid(rid: str) -> None:
    _rid_last_touch[rid] = time.time()


def _sweep_stale_state_once() -> int:
    now = time.time()
    stale = [rid for rid, ts in _rid_last_touch.items() if now - ts > RID_STATE_TTL_SECONDS]
    pruned = 0
    for rid in stale:
        batch = pending_batches.get(rid)
        if batch is not None and batch.run_indices and not batch.finished:
            _touch_rid(rid)       # still downloading: its Cancel button must keep working however long it takes
            continue
        pending_links.pop(rid, None)
        pending_probes.pop(rid, None)
        last_download.pop(rid, None)
        pending_sections.pop(rid, None)
        pending_subs.pop(rid, None)
        pending_batches.pop(rid, None)
        tool_drafts.pop(rid, None)
        cancelled_pre_job_rids.discard(rid)
        _rid_last_touch.pop(rid, None)
        pruned += 1
    for uid in [u for u, st in pending_section_input.items() if now - st["at"] > SECTION_INPUT_TTL_SECONDS]:
        pending_section_input.pop(uid, None)
    for uid in [u for u, st in pending_tool_input.items() if now - st["at"] > SECTION_INPUT_TTL_SECONDS]:
        pending_tool_input.pop(uid, None)
    tools.store.expire()                                  # uploaded files nobody picked a tool for
    return pruned


async def _sweep_stale_link_state_loop() -> None:
    while True:
        await asyncio.sleep(15 * 60)
        pruned = _sweep_stale_state_once()
        if pruned:
            log.info("Pruned %d stale link state entries", pruned)


def new_rid() -> str:
    return uuid.uuid4().hex[:10]


async def _delete_quietly(message) -> None:
    try:
        await message.delete()
    except Exception:  # noqa: BLE001
        pass


def is_owner_or_admin(user_id: int) -> bool:
    return user_id == config.OWNER_USER_ID or user_id in config.ADMIN_USER_IDS


# ---------- access control (gates EVERYTHING, not just downloads) ----------

async def gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Checked at the top of every user-facing command/handler. Nothing -
    not even /start - works until this passes."""
    user_id = update.effective_user.id
    ac.record_known_user(user_id)  # track everyone who's ever touched the bot, not just /start users

    if not ac.is_allowed(user_id):
        await update.message.reply_text(messages.PRIVATE_BOT)
        return False

    channel = ac.get_force_join_channel()
    if channel and not ac.is_privileged(user_id):
        try:
            member = await context.bot.get_chat_member(channel, user_id)
            joined = member.status not in ("left", "kicked")
        except Exception:  # noqa: BLE001
            joined = False
        if not joined:
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("→ Open channel", url=f"https://t.me/{channel.lstrip('@')}")],
                [InlineKeyboardButton("✓ I've joined", callback_data="fj|check")],
            ])
            await update.message.reply_text(messages.join_required(channel), reply_markup=kb)
            return False

    return True


def _with_current_look(user_id: int, saved: dict) -> dict:
    """Retry / Send-as-file re-run a download with the settings it originally
    used. Only the *look* of the progress display should follow the user's
    current preferences though - otherwise picking a new bar style (or
    toggling ADHD Mode) and then tapping Try again would still show the old
    one. Quality/format/etc. deliberately stay as they were."""
    current = get_settings(user_id)
    merged = dict(saved)
    merged["bar_style"] = current.get("bar_style", "auto")
    merged["adhd_mode"] = current.get("adhd_mode", False)
    return merged


async def gate_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """gate(), but for a CallbackQuery - old dl|/nav|/s| buttons from
    before someone was removed, or from before Private mode was turned
    on, kept working forever because nothing checked access on the
    callback path at all (only the /command handlers did). Skips the
    force-join UI here (there's no good way to show it inline on someone
    else's message) and just checks the allow-list."""
    user_id = update.effective_user.id
    if not ac.is_allowed(user_id):
        await update.callback_query.answer(messages.PRIVATE_BOT, show_alert=True)
        return False
    return True


async def force_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = update.effective_user.id
    channel = ac.get_force_join_channel()

    if not channel:
        await query.answer("That's not required anymore")
        await query.edit_message_text(messages.WELCOME, parse_mode=ParseMode.HTML, reply_markup=start_menu())
        return

    try:
        member = await context.bot.get_chat_member(channel, user_id)
        joined = member.status not in ("left", "kicked")
    except Exception:  # noqa: BLE001
        joined = False

    if joined:
        await query.answer("Thanks, you're in")
        ac.record_known_user(user_id)
        # send the real welcome now - they never got it while gated
        await query.edit_message_text(messages.WELCOME, parse_mode=ParseMode.HTML, reply_markup=start_menu())
    else:
        await query.answer("Still don't see you in there — join, then tap again.", show_alert=True)


# ---------- basic commands ----------

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate(update, context):
        return
    await _delete_quietly(update.message)
    ac.record_known_user(update.effective_user.id)
    await update.message.reply_text(messages.WELCOME, parse_mode=ParseMode.HTML, reply_markup=start_menu())


async def tools_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate(update, context):
        return
    await _delete_quietly(update.message)
    await update.message.reply_text(tools_menu.TOOLS_INTRO, parse_mode=ParseMode.HTML,
                                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("← Back", callback_data="nav|home")]]))


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate(update, context):
        return
    await _delete_quietly(update.message)
    lines = [
        f"{config.OWNER_EMOJI} <b>{config.OWNER_NAME}'s Downloader</b>",
        "Paste a link, pick from the buttons.",
        "",
        "/adhd_on — skip the picker, always grab best quality, no questions",
        "/adhd_off — turn that back off",
        "/settings — your saved defaults (cookies help is in there too)",
        "/tools — trim, compress, extract audio… (or just send me a video/audio file)",
        "/queue — what's running",
        "/history — what you've downloaded",
        "/cancel — stop your current download",
    ]
    if is_owner_or_admin(update.effective_user.id):
        lines.append(f"/{config.OWNER_ADMIN_COMMAND} — admin panel")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def queue_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate(update, context):
        return
    await _delete_quietly(update.message)
    await update.message.reply_text(job_manager.active_summary(update.effective_user.id), parse_mode=ParseMode.HTML)


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate(update, context):
        return
    await _delete_quietly(update.message)
    count = job_manager.cancel_all_for_user(update.effective_user.id)
    await update.message.reply_text(messages.CANCELLED if count else messages.NOTHING_TO_CANCEL)


async def update_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in config.ADMIN_USER_IDS:
        return
    msg = await update.message.reply_text("Checking for tool updates…")
    summary = await run_update_once(context.bot, notify_admins=False)
    await msg.edit_text(summary, parse_mode=ParseMode.HTML)


async def potcheck_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in config.ADMIN_USER_IDS:
        return
    test_url = context.args[0] if context.args else "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    msg = await update.message.reply_text("Checking PO Token provider status…")

    cmd = ["yt-dlp", "-v", "--simulate", "--no-warnings"]
    if config.BGUTIL_POT_URL:
        cmd += ["--extractor-args", f"youtubepot-bgutilhttp:base_url={config.BGUTIL_POT_URL}"]
    cmd.append(test_url)

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    text = out.decode(errors="ignore")
    pot_lines = [l for l in text.splitlines() if "pot" in l.lower() or "po token" in l.lower()]
    report = "\n".join(pot_lines[:20]) or "No PO Token debug lines found at all — plugin isn't loading."
    await msg.edit_text(f"<pre>{report[:3500]}</pre>", parse_mode=ParseMode.HTML)


# ---------- cookies (now surfaced from within /settings, see settings_callback) ----------

COOKIES_HELP = (
    "<b>Using cookies for logged-in content</b>\n\n"
    "Some sites ask to confirm you're not a bot, or need a login for "
    "private content. Fix: export your browser's cookies and send the "
    "file here.\n\n"
    "1. Install a \"cookies.txt\" export extension for your browser\n"
    "2. For YouTube: open a <b>private/incognito window</b>, log in, then "
    "open youtube.com/robots.txt in that same tab and export cookies from "
    "there. Close the window right after. (YouTube rotates cookies from "
    "normal tabs, which silently breaks the exported file.)\n"
    "3. Send me the exported <code>.txt</code> file right here\n\n"
    "4. Another site later? Send its file too: only that site's cookies are "
    "replaced, the rest are kept.\n\n"
    "• This is private to you — every person using this bot has their "
    "own cookies file, and no one else can see or use yours.\n\n"
    "• Using your main account's cookies for automated downloads can "
    "occasionally get that account rate-limited. A secondary account is safer."
)


async def cookies_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate(update, context):
        return
    await _delete_quietly(update.message)
    sites = list_sites(_cookie_path(update.effective_user.id))
    await update.message.reply_text(COOKIES_HELP + "\n\n" + cookies_menu.cookies_list_text(sites), parse_mode=ParseMode.HTML,
                                    reply_markup=cookies_menu.cookies_menu(sites, back="nav|home"))


def _cookie_path(user_id: int) -> Path:
    return Path(config.COOKIES_DIR) / f"{user_id}.txt"


async def cookies_file_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A cookies.txt upload is MERGED into the person's file: it replaces the cookies of the sites it contains
    and keeps the others (sending Instagram's cookies must not wipe YouTube's)."""
    if not await gate(update, context):
        return
    doc = update.message.document
    user_id = update.effective_user.id
    Path(config.COOKIES_DIR).mkdir(parents=True, exist_ok=True)
    dest = _cookie_path(user_id)
    upload = dest.with_suffix(".upload")

    try:
        tg_file = await doc.get_file()
        await tg_file.download_to_drive(custom_path=str(upload))
        drop_server_copy(getattr(tg_file, "file_path", None))      # cookies are secrets: don't leave a copy behind
    except Exception:  # noqa: BLE001
        log.exception("Couldn't fetch the uploaded cookies file")
        upload.unlink(missing_ok=True)
        hint = ""
        if config.LOCAL_BOT_API_URL:
            hint = (
                "\n\nThis usually means the bot container can't reach the local Bot API "
                "server's storage - check that docker-compose.yml mounts the same "
                "<code>bot_api_data</code> volume into both the <code>telegram-bot-api</code> "
                "and <code>bot</code> services."
            )
        await update.message.reply_text(
            f"✕ Couldn't read that file — try sending it again.{hint}", parse_mode=ParseMode.HTML,
        )
        return

    try:
        check = inspect_cookie_file(upload)
        merged = merge_upload(dest, upload)
    finally:
        upload.unlink(missing_ok=True)
    if merged is None:
        await update.message.reply_text(
            "✕ That doesn't look like a cookies.txt export (Netscape format), so nothing was changed. "
            "Use a \"cookies.txt\" browser extension and send the file again. /cookies has the steps.")
        return

    update_setting(user_id, "cookies_enabled", True)
    touched = merged.added + merged.replaced
    names = ", ".join(site_label(site) for site in touched)
    kept = ", ".join(site_label(site) for site in merged.kept)
    if check["youtube"] and not check["logged_in"]:
        note = (
            "Saved — but there's no YouTube login in this file, so it "
            "won't help with YouTube. Log in first, then export again "
            "from a private window. /cookies has the steps."
        )
    elif check["expired"]:
        note = (
            "Saved — but the login in this file has already expired, so it "
            "won't work. Log in again, export from a private window, and "
            "send the file again. /cookies has the steps."
        )
    elif merged.no_login:
        note = (f"Saved — but there's no {', '.join(site_label(site) for site in merged.no_login)} login in this file "
                f"(you were probably logged out when exporting), so it may not help. Log in, export again, and send it.")
    else:
        note = f"Cookies saved for {names} — just for you."
    if kept:
        note += f"\nYour other cookies ({kept}) were kept."
    await update.message.reply_text(note)


async def _show_cookies_screen(query, user_id: int) -> None:
    sites = list_sites(_cookie_path(user_id))
    await query.edit_message_text(COOKIES_HELP + "\n\n" + cookies_menu.cookies_list_text(sites), parse_mode=ParseMode.HTML,
                                  reply_markup=cookies_menu.cookies_menu(sites))


# ---------- settings menu ----------

def settings_title() -> str:
    return f"{config.OWNER_EMOJI} <b>Your settings</b>"


async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate(update, context):
        return
    await _delete_quietly(update.message)
    s = get_settings(update.effective_user.id)
    await update.message.reply_text(settings_title(), parse_mode=ParseMode.HTML,
                                     reply_markup=main_menu(s))


async def adhd_on_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate(update, context):
        return
    update_setting(update.effective_user.id, "adhd_mode", True)
    await update.message.reply_text(
        "🧠⚡ ADHD Mode is ON. Send a link — you'll get the file, best "
        "quality, no questions asked. /adhd_off to turn it back off."
    )


async def adhd_off_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate(update, context):
        return
    update_setting(update.effective_user.id, "adhd_mode", False)
    await update.message.reply_text("🧠 ADHD Mode is off. You'll get the quality picker again.")


async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not await gate_callback(update, context):
        return
    user_id = update.effective_user.id
    data = query.data or ""

    if data.startswith("nav|"):
        screen = data.split("|", 1)[1]
        pending_setting_input.pop(user_id, None)
        pending_section_input.pop(user_id, None)
        pending_tool_input.pop(user_id, None)

        if screen == "cookies":
            await _show_cookies_screen(query, user_id)
            return

        if screen == "home":
            await query.edit_message_text(messages.WELCOME, parse_mode=ParseMode.HTML, reply_markup=start_menu())
            return

        if screen in ADVANCED_TEXT_FIELDS:
            pending_setting_input[user_id] = screen
            field = ADVANCED_TEXT_FIELDS[screen]
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("← Cancel", callback_data="nav|advanced")]])
            await query.edit_message_text(field["prompt"], reply_markup=kb)
            return

        s = get_settings(user_id)
        screens = {
            "main": (main_menu, settings_title()),
            "look": (look_menu, look_title(s)),
            "bars": (bars_menu, bars_title()),
            "advanced": (advanced_menu, "⚙️ Advanced settings"),
            "reset": (confirm_reset_menu, "Reset ALL your settings to default?"),
        }
        if screen in screens:
            builder, title = screens[screen]
            markup = builder() if builder is confirm_reset_menu else builder(s)
            await query.edit_message_text(title, parse_mode=ParseMode.HTML, reply_markup=markup)
        return

    if data.startswith("ck|"):
        parts = data.split("|", 2)
        if len(parts) == 3 and parts[1] == "rm":
            remove_site(_cookie_path(user_id), parts[2])
        await _show_cookies_screen(query, user_id)
        return

    if data.startswith("s|"):
        _, key, value = data.split("|", 2)
        if key == "__reset__":
            reset_settings(user_id)
        elif key in {"embed_thumbnail", "embed_metadata", "sponsorblock", "use_archive",
                     "embed_subtitles", "cookies_enabled", "adhd_mode", "show_sizes"}:
            update_setting(user_id, key, value == "1")
        else:
            update_setting(user_id, key, value)

        s = get_settings(user_id)
        if key == "bar_style":
            # stay on the picker so the ✓ moves and they can compare styles
            await query.edit_message_text(bars_title(), parse_mode=ParseMode.HTML, reply_markup=bars_menu(s))
            return
        if key == "show_sizes":
            await query.edit_message_text(look_title(s), parse_mode=ParseMode.HTML, reply_markup=look_menu(s))
            return
        await query.edit_message_text(settings_title(), parse_mode=ParseMode.HTML,
                                       reply_markup=main_menu(s))


# ---------- owner admin panel ----------

async def owner_admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner_or_admin(update.effective_user.id):
        await update.message.reply_text("✕ Not authorized.")
        return
    await update.message.reply_text(
        admin_menu.panel_title(), parse_mode=ParseMode.HTML, reply_markup=admin_menu.main_panel()
    )


async def admin_panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    if not is_owner_or_admin(user_id):
        await query.edit_message_text("✕ Not authorized.")
        return

    action = (query.data or "").split("|", 1)[1]

    if action == "main":
        await query.edit_message_text(admin_menu.panel_title(), parse_mode=ParseMode.HTML,
                                       reply_markup=admin_menu.main_panel())
    elif action == "toggle_mode":
        ac.set_mode("private" if ac.get_mode() == "public" else "public")
        await query.edit_message_text(admin_menu.panel_title(), parse_mode=ParseMode.HTML,
                                       reply_markup=admin_menu.main_panel())
    elif action == "users":
        ids = ac.list_allowed_users()
        body = "\n".join(f"• <code>{i}</code>" for i in ids[:20]) or "No one added yet."
        text = f"{admin_menu.panel_title()}\n\nAllowed users ({len(ids)})\n{body}"
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=admin_menu.users_panel())
    elif action == "add_user":
        pending_admin_input[user_id] = "add_user"
        await query.edit_message_text("Send the numeric Telegram user ID to add.",
                                       reply_markup=admin_menu.back_only())
    elif action == "remove_user":
        pending_admin_input[user_id] = "remove_user"
        await query.edit_message_text("Send the numeric Telegram user ID to remove.",
                                       reply_markup=admin_menu.back_only())
    elif action == "join":
        channel = ac.get_force_join_channel()
        text = f"{admin_menu.panel_title()}\n\nForce-join channel\nCurrently: {channel or 'off'}"
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=admin_menu.join_panel(channel))
    elif action == "set_channel":
        pending_admin_input[user_id] = "set_channel"
        await query.edit_message_text("Send the channel username (e.g. @mychannel).",
                                       reply_markup=admin_menu.back_only())
    elif action == "clear_channel":
        ac.set_force_join_channel("")
        await query.edit_message_text(admin_menu.panel_title(), parse_mode=ParseMode.HTML,
                                       reply_markup=admin_menu.main_panel())
    elif action == "stats":
        total, success, failed = ac.download_stats()
        text = (f"{admin_menu.panel_title()}\n\nStats\n"
                f"Known users: {ac.known_user_count()}\nMode: {ac.get_mode()}\n"
                f"Downloads: {total} total ({success} ok, {failed} failed)")
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=admin_menu.back_only())
    elif action == "activity":
        total, success, failed = ac.download_stats()
        rows = ac.recent_downloads(10)
        icon = {"success": "✓", "failed": "✕", "cancelled": "•"}
        lines = [f"{admin_menu.panel_title()}", "",
                 f"Recent activity ({total} total, {success} ok, {failed} failed)"]
        for uid, url, status, _at in rows:
            lines.append(f"{icon.get(status, '•')} <code>{uid}</code> — {url[:40]}")
        if not rows:
            lines.append("Nothing yet.")
        await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.HTML,
                                       reply_markup=admin_menu.back_only())
    elif action == "all_users":
        rows = ac.list_known_users(30)
        body = "\n".join(f"• <code>{uid}</code> (since {at[:10]})" for uid, at in rows) or "No one yet."
        text = f"{admin_menu.panel_title()}\n\nAll known users ({len(rows)} shown)\n{body}"
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=admin_menu.back_only())
    elif action in ("warp", "warp_ip", "warp_go"):
        await _admin_warp(query, action)
    elif action == "close":
        try:
            await query.message.delete()
        except Exception:  # noqa: BLE001
            pass


async def _admin_warp(query, action: str) -> None:
    """The WARP screen: status + "Change address" (with a warning when downloads are running)."""
    running = job_manager.active_count()

    async def show(notice: str = "") -> None:
        proxy = warp_control.proxy_url()
        reachable = bool(proxy) and await asyncio.to_thread(tcp_alive, proxy)
        ip = await warp_control.current_ip(proxy) if reachable else None
        await query.edit_message_text(
            admin_menu.warp_text(bool(proxy), reachable, ip, warp_control.configured(), running, notice),
            parse_mode=ParseMode.HTML, reply_markup=admin_menu.warp_panel(warp_control.configured()))

    if action == "warp":
        await show()
        return
    if action == "warp_ip" and running:
        await query.edit_message_text(admin_menu.warp_confirm_text(running), parse_mode=ParseMode.HTML,
                                      reply_markup=admin_menu.warp_confirm_panel())
        return
    await query.edit_message_text(f"{admin_menu.panel_title()}\n\n🔄 Changing the WARP address… (up to a minute)",
                                  parse_mode=ParseMode.HTML)
    result = await warp_control.rotate()
    if result.ok and result.new_ip:
        notice = f"✓ {esc(result.message)}\n<code>{esc(result.old_ip or '?')}</code> → <code>{esc(result.new_ip)}</code>"
    else:
        notice = f"✕ {esc(result.message)}"
    await show(notice)


async def handle_admin_text_input(update: Update, user_id: int, text: str) -> None:
    action = pending_admin_input.pop(user_id)
    if action in ("add_user", "remove_user"):
        cleaned = text.strip()
        if not cleaned.lstrip("-").isdigit():
            await update.message.reply_text("That's not a numeric user ID — open the panel and try again.")
            return
        target_id = int(cleaned)
        if action == "add_user":
            ac.add_allowed_user(target_id)
            await update.message.reply_text(f"✓ Added <code>{target_id}</code>.", parse_mode=ParseMode.HTML)
        else:
            ac.remove_allowed_user(target_id)
            await update.message.reply_text(f"✓ Removed <code>{target_id}</code>.", parse_mode=ParseMode.HTML)
    elif action == "set_channel":
        channel = text.strip()
        if not channel.startswith("@"):
            channel = f"@{channel}"
        ac.set_force_join_channel(channel)
        await update.message.reply_text(f"✓ Force-join set to {channel}.")


async def handle_setting_text_input(update: Update, user_id: int, text: str) -> None:
    screen = pending_setting_input.pop(user_id)
    field = ADVANCED_TEXT_FIELDS[screen]
    try:
        value, confirmation = field["parser"](text)
    except ValueError as exc:
        # Bad input - keep waiting rather than silently dropping back to
        # the menu, so a typo doesn't make the person start over.
        pending_setting_input[user_id] = screen
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("← Cancel", callback_data="nav|advanced")]])
        await update.message.reply_text(str(exc), reply_markup=kb)
        return

    update_setting(user_id, field["setting_key"], value)
    s = get_settings(user_id)
    await update.message.reply_text(f"✓ {confirmation}", parse_mode=ParseMode.HTML,
                                     reply_markup=advanced_menu(s))


# ---------- misc buttons (from /start) ----------

async def misc_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not await gate_callback(update, context):
        return
    action = (query.data or "").split("|", 1)[1]
    if action == "queue":
        text = job_manager.active_summary(update.effective_user.id)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("← Back", callback_data="nav|home")]])
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    elif action == "tools":
        await query.edit_message_text(tools_menu.TOOLS_INTRO, parse_mode=ParseMode.HTML,
                                       reply_markup=tools_menu.tools_intro_menu())
    elif action in ("history", "history_home"):
        # Same screen, but "← Back" must return to wherever it was opened from.
        origin = ORIGIN_HOME if action == "history_home" else ORIGIN_SETTINGS
        text, markup = _render_history_page(update.effective_user.id, 1, origin)
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup,
                                       link_preview_options=_NO_PREVIEW)


# Every history entry is a link; without this Telegram would unfurl a big
# preview card for the first one on each page.
_NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


def _render_history_page(user_id: int, page: int, origin: str = ORIGIN_HOME) -> tuple[str, InlineKeyboardMarkup]:
    total = ac.count_user_downloads(user_id)
    total_pages = max(1, -(-total // PAGE_SIZE))   # ceiling division
    page = max(1, min(page, total_pages))
    rows = ac.list_user_downloads(user_id, (page - 1) * PAGE_SIZE, PAGE_SIZE)
    return history_text(rows, page, total_pages, total), history_menu(page, total_pages, origin)


def _hist_origin(parts: list[str], index: int) -> str:
    """Origin flag from a hist| callback; buttons on messages sent before
    this existed have none, so fall back to the start screen."""
    value = parts[index] if len(parts) > index else ""
    return value if value in (ORIGIN_HOME, ORIGIN_SETTINGS) else ORIGIN_HOME


async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate(update, context):
        return
    await _delete_quietly(update.message)
    text, markup = _render_history_page(update.effective_user.id, 1, ORIGIN_HOME)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup,
                                     link_preview_options=_NO_PREVIEW)


async def history_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not await gate_callback(update, context):
        return
    user_id = update.effective_user.id
    parts = (query.data or "").split("|")
    action = parts[1] if len(parts) > 1 else ""

    if action == "close":
        if _hist_origin(parts, 2) == ORIGIN_SETTINGS:
            s = get_settings(user_id)
            await query.edit_message_text(settings_title(), parse_mode=ParseMode.HTML, reply_markup=main_menu(s))
        else:
            await query.edit_message_text(messages.WELCOME, parse_mode=ParseMode.HTML, reply_markup=start_menu())
        return

    if action == "page":
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
        text, markup = _render_history_page(user_id, page, _hist_origin(parts, 3))
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup,
                                       link_preview_options=_NO_PREVIEW)
        return

    if action == "clear":
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
        await query.edit_message_reply_markup(reply_markup=confirm_clear_menu(page, _hist_origin(parts, 3)))
        return

    if action == "clear_yes":
        deleted = ac.clear_user_downloads(user_id)
        note = f"🗑 Cleared {deleted} entr{'y' if deleted == 1 else 'ies'}." if deleted else "Nothing to clear."
        text, markup = _render_history_page(user_id, 1, _hist_origin(parts, 2))
        await query.edit_message_text(f"{note}\n\n{text}", parse_mode=ParseMode.HTML, reply_markup=markup,
                                       link_preview_options=_NO_PREVIEW)


# ---------- clip sections editor ----------

async def _show_screen(bot, chat_id: int, message_id: int, is_photo: bool, text: str, markup) -> None:
    """Replace the quality-picker message's text and buttons. That message is a
    photo with a caption when the link had a thumbnail, plain text otherwise.
    Telegram rejects an edit that changes nothing ("message is not modified" -
    e.g. tapping the already-selected format); that's harmless, so swallow it."""
    try:
        if is_photo:
            await bot.edit_message_caption(chat_id=chat_id, message_id=message_id, caption=text,
                                            parse_mode=ParseMode.HTML, reply_markup=markup)
        else:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id,
                                         parse_mode=ParseMode.HTML, reply_markup=markup)
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise


def _section_context(rid: str, user_id: int):
    """(url, probe, draft) for this link's sections editor, or None if the link
    expired (restart / the 2-hour sweep) or isn't this person's. The draft is
    created on first use."""
    entry = pending_links.get(rid)
    probe_result = pending_probes.get(rid)
    if not entry or entry[0] != user_id or not probe_result or not probe_result.duration:
        return None
    _touch_rid(rid)
    draft = pending_sections.get(rid)
    if draft is None:
        draft = pending_sections[rid] = SectionDraft()
    return entry[1], probe_result, draft


def _active_sections(rid: str) -> tuple[list[tuple[float, float]], bool]:
    """(sections the person has chosen for this link, merge them?). Empty when
    none - the normal case, in which every download button behaves as before."""
    draft = pending_sections.get(rid)
    probe_result = pending_probes.get(rid)
    if not draft or not probe_result or not probe_result.duration:
        return [], False
    try:
        sections = draft.active(probe_result.duration)
    except SectionError:
        return [], False       # values are validated as they're typed, so this shouldn't happen
    return sections, bool(draft.merge and len(sections) > 1)


def _active_subs(rid: str) -> SubChoice | None:
    choice = pending_subs.get(rid)
    return choice if choice and choice.langs else None


def _quality_caption(rid: str, probe_result: ProbeResult) -> str:
    """The quality menu's caption: the title, plus the chosen sections so it is
    obvious the buttons below download only those parts."""
    caption = esc(probe_result.title) if probe_result.title else messages.PICK_OPTION
    sections, merge = _active_sections(rid)
    subs = _active_subs(rid)
    if sections:
        caption += "\n\n" + sections_summary(sections, merge)
        if subs:
            # Subtitles are fetched for the whole video, so on a clip they would be out of sync.
            caption += "\n◧ Subtitles are skipped for time ranges."
    elif subs:
        caption += "\n\n◧ Subtitles: " + esc(subtitle_summary(subs, probe_result.subtitles))
    return caption


def _prefill_start_time(rid: str, url: str, probe_result: ProbeResult) -> None:
    """A link like youtu.be/ID?t=1h30m means "start here": pre-fill that as a
    section from there to the end. It shows in the menu's caption and on its
    button, and one tap on the section's remove button in the editor drops it."""
    start_at = start_time_from_url(url)
    if not start_at or not probe_result.duration or start_at >= probe_result.duration:
        return
    draft = SectionDraft()
    try:
        draft.set_value(0, "start", start_at, probe_result.duration)
    except SectionError:
        return
    pending_sections[rid] = draft


def _editor_markup(rid: str, draft: SectionDraft, probe_result: ProbeResult):
    return editor_menu(draft, rid, len(_active_sections(rid)[0]), bool(probe_result.chapters))


def _size_labels(rid: str, probe_result: ProbeResult, user_id: int) -> dict:
    """Estimated sizes for the quality buttons ({} = show none): nothing when
    the person turned sizes off, or the site announced none. With sections
    chosen the numbers shrink to the share of the video being downloaded."""
    if not probe_result.sizes or not get_settings(user_id).get("show_sizes", True):
        return {}
    sections, _ = _active_sections(rid)
    scale = 1.0
    if sections and probe_result.duration:
        scale = sum(end - start for start, end in sections) / probe_result.duration
    return size_labels(probe_result.sizes, scale)


def _main_menu_for(rid: str, probe_result: ProbeResult, user_id: int):
    return quality_menu(probe_result, rid, len(_active_sections(rid)[0]), _size_labels(rid, probe_result, user_id))


def _more_menu_for(rid: str, probe_result: ProbeResult, user_id: int):
    subs = _active_subs(rid)
    return extended_video_menu(probe_result, rid, len(_active_sections(rid)[0]),
                               _size_labels(rid, probe_result, user_id), len(subs.langs) if subs else 0)


async def sections_callback(context: ContextTypes.DEFAULT_TYPE, query, user_id: int, parts: list[str],
                            rid: str, is_photo: bool) -> None:
    """Every dl|sec|... button. Errors are shown as a warning line inside the
    editor rather than a popup: the query was already answered once, and
    Telegram only honours the first answer."""
    sub = parts[2] if len(parts) > 3 else ""
    chat_id, message_id = query.message.chat_id, query.message.message_id
    pending_section_input.pop(user_id, None)   # any button abandons a half-typed time (start/end re-arm it)

    async def show(text: str, markup) -> None:
        await _show_screen(context.bot, chat_id, message_id, is_photo, text, markup)

    ctx = _section_context(rid, user_id)
    if ctx is None:
        await show("This link expired — send it again.", None)
        return
    _, probe_result, draft = ctx
    duration = probe_result.duration

    async def editor(notice: str = "") -> None:
        await show(editor_text(draft, duration, probe_result.title, notice), _editor_markup(rid, draft, probe_result))

    async def chapters_screen(page: int, notice: str = "") -> None:
        pages = chapter_pages(probe_result.chapters)
        page = max(0, min(page, pages - 1))
        await show(chapters_text(probe_result.title, duration, page, pages, notice),
                   chapters_menu(probe_result.chapters, draft, rid, page))

    def number_at(position: int) -> int | None:
        return int(parts[position]) if len(parts) > position + 1 and parts[position].isdigit() else None

    if sub in ("open", "edit"):
        await editor()

    elif sub == "add":
        try:
            draft.add_row()
        except SectionError as exc:
            await editor(str(exc))
        else:
            await editor()

    elif sub in ("start", "end"):
        index = number_at(3)
        if index is None or index >= len(draft.rows):
            await editor("That section no longer exists.")
            return
        pending_setting_input.pop(user_id, None)   # one free-text capture at a time
        pending_section_input[user_id] = {
            "rid": rid, "field": sub, "index": index, "chat_id": chat_id, "message_id": message_id,
            "is_photo": is_photo, "at": time.time(),
        }
        number = index + 1 if len(draft.rows) > 1 else None
        await show(prompt_text(sub, duration, section_number=number), prompt_menu(sub, index, rid))

    elif sub == "clear" and len(parts) > 5 and parts[3] in ("start", "end") and parts[4].isdigit():
        try:
            draft.clear_value(int(parts[4]), parts[3])
        except SectionError as exc:
            await editor(str(exc))
        else:
            await editor()

    elif sub == "del" and number_at(3) is not None:
        try:
            draft.remove_row(number_at(3))
        except SectionError as exc:
            await editor(str(exc))
        else:
            await editor()

    elif sub == "chap" and probe_result.chapters:
        await chapters_screen(number_at(3) or 0)

    elif sub == "ch" and probe_result.chapters and number_at(3) is not None:
        index, page = number_at(3), number_at(4) or 0
        if index >= len(probe_result.chapters):
            await chapters_screen(page, "That chapter no longer exists.")
            return
        _title, start, end = probe_result.chapters[index]
        try:
            draft.toggle_range(start, end, duration)
        except SectionError as exc:
            await chapters_screen(page, str(exc))
        else:
            await chapters_screen(page)

    elif sub == "merge" and len(parts) > 4:
        draft.merge = parts[3] == "on"
        await editor()

    elif sub == "back":
        # Back to the quality menu - its buttons now download these sections.
        await show(_quality_caption(rid, probe_result), _main_menu_for(rid, probe_result, user_id))


async def subtitles_callback(context: ContextTypes.DEFAULT_TYPE, query, user_id: int, parts: list[str],
                             rid: str, is_photo: bool) -> None:
    """Every dl|sub|... button (see ui/subtitle_menu.py)."""
    sub = parts[2] if len(parts) > 3 else ""
    chat_id, message_id = query.message.chat_id, query.message.message_id

    async def show(text: str, markup) -> None:
        await _show_screen(context.bot, chat_id, message_id, is_photo, text, markup)

    entry, probe_result = pending_links.get(rid), pending_probes.get(rid)
    if not entry or entry[0] != user_id or not probe_result or not probe_result.subtitles:
        await show("This link expired — send it again.", None)
        return
    _touch_rid(rid)
    tracks = probe_result.subtitles
    choice = pending_subs.setdefault(rid, SubChoice())

    def number(position: int, default: int = 0) -> int:
        return int(parts[position]) if len(parts) > position + 1 and parts[position].isdigit() else default

    async def screen(page: int, notice: str = "") -> None:
        page = sub_clamp_page(tracks, page)
        await show(subtitles_text(choice, tracks, page, probe_result.title, notice),
                   subtitles_menu(choice, tracks, page, rid))

    if sub == "open":
        await screen(number(3))
    elif sub == "t":
        index, page = number(3, -1), number(4)
        if not 0 <= index < len(tracks):
            await screen(page, "That language is no longer listed.")
            return
        try:
            choice.toggle(tracks[index].code)
        except SubtitleError as exc:
            await screen(page, str(exc))
        else:
            await screen(page)
    elif sub == "m" and len(parts) > 4:
        try:
            choice.toggle_option(parts[3])
        except SubtitleError as exc:
            await screen(number(4), str(exc))
        else:
            await screen(number(4))
    elif sub == "clr":
        choice.langs.clear()
        await screen(0)


async def handle_section_text_input(update: Update, user_id: int, text: str) -> None:
    """The person typed a timestamp after tapping a Start / End button."""
    state = pending_section_input[user_id]
    rid, field, index = state["rid"], state["field"], state["index"]
    await _delete_quietly(update.message)   # keep the chat tidy: the editor message is the UI

    async def show(text_: str, markup) -> None:
        await _show_screen(update.get_bot(), state["chat_id"], state["message_id"], state["is_photo"],
                           text_, markup)

    ctx = _section_context(rid, user_id)
    if ctx is None:
        pending_section_input.pop(user_id, None)
        await show("This link expired — send it again.", None)
        return
    _, probe_result, draft = ctx
    duration = probe_result.duration

    try:
        draft.set_value(index, field, parse_timestamp(text), duration)
    except SectionError as exc:
        # Keep waiting on the same prompt, with the reason - a typo shouldn't
        # send the person back to the start.
        number = index + 1 if len(draft.rows) > 1 else None
        await show(prompt_text(field, duration, str(exc), number), prompt_menu(field, index, rid))
        return

    pending_section_input.pop(user_id, None)
    await show(editor_text(draft, duration, probe_result.title), _editor_markup(rid, draft, probe_result))



# ---------- the toolbox: a video / audio file sent to the bot ----------

MAX_STORED_UPLOADS_PER_USER = 3


def _incoming_media(message):
    """(telegram file object, a name for it) when the message carries something the toolbox can work on."""
    if message.video:
        return message.video, message.video.file_name or "video.mp4"
    if message.animation:
        return message.animation, message.animation.file_name or "animation.mp4"
    if message.video_note:
        return message.video_note, "video_note.mp4"
    if message.audio:
        audio = message.audio
        return audio, audio.file_name or f"{audio.title or 'audio'}.mp3"
    if message.voice:
        return message.voice, "voice.ogg"
    document = message.document
    if document and tools.looks_like_media(document.file_name or "", document.mime_type):
        return document, document.file_name or "file"
    return None


async def media_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate(update, context):
        return
    found = _incoming_media(update.message)
    if found is None:
        return
    media, name = found
    user_id = update.effective_user.id
    if media.file_size and media.file_size > config.MAX_FILE_SIZE_BYTES:
        await update.message.reply_text("That file is bigger than the 2 GB I can handle.")
        return

    # A person can keep a few uploads waiting; the oldest ones make room (and free their disk space).
    waiting = tools.store.for_user(user_id)
    for old in waiting[:max(0, len(waiting) - (MAX_STORED_UPLOADS_PER_USER - 1))]:
        tools.store.discard(old)

    rid = new_rid()
    _touch_rid(rid)
    status = await update.message.reply_text("🧰 Receiving the file…")
    suffix = Path(name).suffix.lower()[:8] or ".bin"
    path = tools.store.folder(rid) / f"original{suffix}"
    try:
        telegram_file = await media.get_file()
        await telegram_file.download_to_drive(custom_path=path, read_timeout=600)
        drop_server_copy(getattr(telegram_file, "file_path", None))     # our copy is enough; free the server's
        info = await tools.probe(path)
    except tools.ToolError as exc:
        tools.store.discard(rid)
        await status.edit_text(f"⚠ {esc(str(exc))}", parse_mode=ParseMode.HTML)
        return
    except Exception:  # noqa: BLE001
        log.warning("Could not receive an uploaded file", exc_info=True)
        tools.store.discard(rid)
        await status.edit_text("⚠ I couldn't download that file from Telegram. Try sending it again.")
        return
    tools.store.add(rid, user_id, path, name, info)
    await status.edit_text(tools_menu.toolbox_text(name, info), parse_mode=ParseMode.HTML,
                           reply_markup=tools_menu.toolbox_menu(info, rid, burn_ok=tools.burn_allowed(info.duration)))


def _toolbox_screen(item, rid: str, notice: str = ""):
    return tools_menu.toolbox_screen(item, rid, notice)


async def _edit(bot, chat_id: int, message_id: int, text: str, markup=None) -> None:
    try:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, parse_mode=ParseMode.HTML,
                                    reply_markup=markup)
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise


async def _start_tool(bot, user_id: int, chat_id: int, message_id: int, rid: str, item, tool: str, **params) -> bool:
    """Queue a toolbox job for the stored file. False = already working on it."""
    if job_manager.is_active(rid):
        return False
    settings = dict(get_settings(user_id))
    settings.update(tool=tool, tool_input=str(item.path), tool_name=item.name, tool_duration=item.info.duration,
                    tool_has_video=item.info.has_video, tool_height=item.info.height, adhd_mode=False, **params)
    if item.srt is not None:
        settings["tool_srt"] = str(item.srt)
    url = f"tool://{tool}"
    last_download[rid] = (user_id, url, settings)
    _touch_rid(rid)
    await _edit(bot, chat_id, message_id, messages.QUEUED, queued_menu(rid, url))
    await job_manager.enqueue(rid, user_id, chat_id, url, settings, message_id, title=item.name)
    return True


async def tools_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not await gate_callback(update, context):
        return
    user_id = update.effective_user.id
    parts = (query.data or "").split("|")
    action = parts[1] if len(parts) > 1 else ""
    rid = parts[-1] if len(parts) > 2 else ""
    chat_id, message_id = query.message.chat_id, query.message.message_id
    pending_tool_input.pop(user_id, None)           # any button abandons a half-typed answer (trim/gif/srt re-arm it)

    if action == "x":
        tools.store.discard(rid)
        tool_drafts.pop(rid, None)
        await _delete_quietly(query.message)
        return

    item = tools.store.get(rid, user_id)
    if item is None:
        await query.edit_message_text("This file expired — send it again.")
        return
    info = item.info
    _touch_rid(rid)

    async def show(text: str, markup) -> None:
        await _edit(context.bot, chat_id, message_id, text, markup)

    async def go(tool: str, **params) -> None:
        if not await _start_tool(context.bot, user_id, chat_id, message_id, rid, item, tool, **params):
            text, markup = _toolbox_screen(item, rid, "Already working on this file — wait for it to finish.")
            await show(text, markup)

    if action == "home":
        text, markup = _toolbox_screen(item, rid)
        await show(text, markup)
    elif action == "trim":
        pending_tool_input[user_id] = {"rid": rid, "kind": "trim", "chat_id": chat_id, "message_id": message_id,
                                       "at": time.time()}
        await show(tools_menu.trim_prompt(info), tools_menu.prompt_menu(rid))
    elif action == "trimgo" and len(parts) > 3:
        draft = tool_drafts.get(rid)
        if not draft:
            await show(tools_menu.trim_prompt(info, "Send the times again."), tools_menu.prompt_menu(rid))
            pending_tool_input[user_id] = {"rid": rid, "kind": "trim", "chat_id": chat_id, "message_id": message_id,
                                           "at": time.time()}
            return
        await go("trim", start=draft["start"], end=draft["end"], exact=parts[2] == "exact")
    elif action == "aud" and len(parts) > 3:
        if parts[2] == "menu":
            await show(tools_menu.audio_text(item.name, info), tools_menu.audio_menu(rid))
        elif parts[2] in tools.AUDIO_FORMATS:
            await go("audio", audio_format=parts[2])
    elif action == "cmpm":
        await show(tools_menu.compress_text(item.name, info), tools_menu.compress_menu(info, rid))
    elif action == "cmp" and len(parts) > 3:
        try:
            target = int(parts[2])
            if target not in tools.COMPRESS_TARGETS_MB:
                raise ValueError
            tools.plan_compress(info.duration, target, info.height)       # refuse a hopeless size BEFORE queueing
        except tools.ToolError as exc:
            await show(tools_menu.compress_text(item.name, info, str(exc)), tools_menu.compress_menu(info, rid))
            return
        except ValueError:
            return
        await go("compress", target_mb=target)
    elif action == "gif":
        pending_tool_input[user_id] = {"rid": rid, "kind": "gif", "chat_id": chat_id, "message_id": message_id,
                                       "at": time.time()}
        await show(tools_menu.gif_prompt(info), tools_menu.prompt_menu(rid))
    elif action == "strip":
        await go("strip")
    elif action == "burn":
        if not tools.burn_allowed(info.duration):
            text, markup = _toolbox_screen(item, rid, "That video is too long to burn subtitles into.")
            await show(text, markup)
            return
        pending_tool_input[user_id] = {"rid": rid, "kind": "srt", "chat_id": chat_id, "message_id": message_id,
                                       "at": time.time()}
        await show(tools_menu.burn_prompt(info), tools_menu.prompt_menu(rid))


async def handle_tool_text_input(update: Update, user_id: int, text: str) -> None:
    """The person typed the times a trim or a GIF asked for."""
    state = pending_tool_input[user_id]
    rid, kind = state["rid"], state["kind"]
    chat_id, message_id = state["chat_id"], state["message_id"]
    bot = update.get_bot()
    await _delete_quietly(update.message)            # the toolbox message is the UI
    item = tools.store.get(rid, user_id)
    if item is None:
        pending_tool_input.pop(user_id, None)
        await _edit(bot, chat_id, message_id, "This file expired — send it again.")
        return
    info = item.info
    try:
        if kind == "trim":
            start, end = tools.parse_range(text, info.duration)
            tool_drafts[rid] = {"start": start, "end": end}
            pending_tool_input.pop(user_id, None)
            await _edit(bot, chat_id, message_id,
                        tools_menu.trim_mode_text(tools_menu.clock(start), tools_menu.clock(end), info.has_video),
                        tools_menu.trim_mode_menu(rid, info.has_video))
        else:
            start, length = tools.parse_gif(text, info.duration)
            pending_tool_input.pop(user_id, None)
            if not await _start_tool(bot, user_id, chat_id, message_id, rid, item, "gif", start=start, length=length):
                text_, markup_ = _toolbox_screen(item, rid, "Already working on this file — wait for it to finish.")
                await _edit(bot, chat_id, message_id, text_, markup_)
    except tools.ToolError as exc:
        prompt = tools_menu.trim_prompt if kind == "trim" else tools_menu.gif_prompt
        await _edit(bot, chat_id, message_id, prompt(info, str(exc)), tools_menu.prompt_menu(rid))   # keep waiting


async def srt_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A .srt file: it is the answer to "Burn subtitles", otherwise a hint about how to use one."""
    if not await gate(update, context):
        return
    user_id = update.effective_user.id
    state = pending_tool_input.get(user_id)
    if not state or state["kind"] != "srt" or time.time() - state["at"] > SECTION_INPUT_TTL_SECONDS:
        pending_tool_input.pop(user_id, None)
        await update.message.reply_text("To burn subtitles into a video, send the video first and pick "
                                        "“Burn subtitles”. For a download, use More options → Subtitles.")
        return
    rid, chat_id, message_id = state["rid"], state["chat_id"], state["message_id"]
    item = tools.store.get(rid, user_id)
    bot = context.bot
    await _delete_quietly(update.message)
    if item is None:
        pending_tool_input.pop(user_id, None)
        await _edit(bot, chat_id, message_id, "This file expired — send it again.")
        return
    document = update.message.document
    if document.file_size and document.file_size > 5_000_000:
        await _edit(bot, chat_id, message_id, tools_menu.burn_prompt(item.info, "That subtitle file is too big."),
                    tools_menu.prompt_menu(rid))
        return
    srt_path = tools.store.folder(rid) / "subs.srt"
    try:
        telegram_file = await document.get_file()
        await telegram_file.download_to_drive(custom_path=srt_path)
        drop_server_copy(getattr(telegram_file, "file_path", None))
        tools.normalize_srt(srt_path)
    except tools.ToolError as exc:
        await _edit(bot, chat_id, message_id, tools_menu.burn_prompt(item.info, str(exc)), tools_menu.prompt_menu(rid))
        return
    except Exception:  # noqa: BLE001
        log.warning("Could not receive a subtitle file", exc_info=True)
        await _edit(bot, chat_id, message_id, tools_menu.burn_prompt(item.info, "I couldn't download that file."),
                    tools_menu.prompt_menu(rid))
        return
    pending_tool_input.pop(user_id, None)
    item.srt = srt_path
    await _start_tool(bot, user_id, chat_id, message_id, rid, item, "burn")


# ---------- batches: a playlist, or several links in one message ----------

_URL_TRAILING_PUNCTUATION = ".,;:!?)]}>\"'"


def _unique_urls(text: str) -> list[str]:
    """Every link in a message, in order, without repeats. Chat text puts
    punctuation right after a link ("see https://x.com/a, then..."), which
    would otherwise become part of the URL."""
    seen: set[str] = set()
    urls = []
    for raw in URL_RE.findall(text):
        url = raw.rstrip(_URL_TRAILING_PUNCTUATION)
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def _batch_settings(quality: str, user_id: int) -> dict:
    settings = dict(get_settings(user_id))
    if quality in ("mp3", "opus"):
        settings["mode"], settings["audio_format"] = "audio", quality
    else:
        settings["mode"], settings["quality"] = "video", quality
    return settings


def _batch_settings_for(base: dict):
    def settings_for(url: str) -> dict:
        settings = dict(base)
        if tool_order_for(url)[0] == "spotify":
            settings["mode"] = "audio"        # Spotify is only ever audio, whatever was picked for the rest
        return settings
    return settings_for


async def _open_picker(bot, batch: Batch, status_msg, notice: str = "") -> None:
    """Show the selection list, reusing the "checking…" message if there is one."""
    text, markup = batch_menu.picker_text(batch, 0, notice), batch_menu.picker_menu(batch, 0)
    if status_msg is not None:
        await status_msg.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        batch.status_message_id = status_msg.message_id
    else:
        sent = await bot.send_message(batch.chat_id, text, parse_mode=ParseMode.HTML, reply_markup=markup)
        batch.status_message_id = sent.message_id
    pending_batches[batch.bid] = batch
    _touch_rid(batch.bid)


async def start_playlist_picker(bot, user_id: int, chat_id: int, url: str, status_msg) -> None:
    try:
        title, entries = await list_playlist(url, user_id)
    except ListingError as exc:
        await status_msg.edit_text(str(exc))
        return
    batch = Batch(bid=new_rid(), user_id=user_id, chat_id=chat_id, status_message_id=status_msg.message_id,
                  items=[BatchItem(e.url, e.title, e.duration) for e in entries], title=title, kind="playlist")
    notice = f"Showing the first {MAX_LISTED} videos." if len(entries) >= MAX_LISTED else ""
    await _open_picker(bot, batch, status_msg, notice)


async def start_links_picker(bot, user_id: int, chat_id: int, urls: list[str]) -> None:
    limit = batch_menu.MAX_BATCH_DOWNLOAD
    notice = f"Only the first {limit} links are used." if len(urls) > limit else ""
    urls = urls[:limit]
    status_msg = await bot.send_message(chat_id, f"Checking {len(urls)} links…")
    lookup = [u for u in urls if tool_order_for(u)[0] == "ytdlp"]       # titles only make sense where yt-dlp is the tool
    found = {e.url: e for e in await quick_titles(lookup, user_id)} if lookup else {}
    entries = [found.get(u) or Entry(u, short_label(u)) for u in urls]
    batch = Batch(bid=new_rid(), user_id=user_id, chat_id=chat_id, status_message_id=status_msg.message_id,
                  items=[BatchItem(e.url, e.title, e.duration) for e in entries], kind="links")
    await _open_picker(bot, batch, status_msg, notice)


def _select_all(batch: Batch) -> str:
    limit = batch_menu.MAX_BATCH_DOWNLOAD
    batch.selected = set(range(min(len(batch.items), limit)))
    return f"Selected the first {limit} - the most one batch downloads." if len(batch.items) > limit else ""


async def batch_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not await gate_callback(update, context):
        return
    user_id = update.effective_user.id
    parts = (query.data or "").split("|")
    action = parts[1] if len(parts) > 1 else ""
    batch = pending_batches.get(parts[-1]) if len(parts) > 2 else None

    async def show(text: str, markup) -> None:
        await _show_screen(context.bot, query.message.chat_id, query.message.message_id, False, text, markup)

    if batch is None or batch.user_id != user_id:
        await show("This list expired — send the links again.", None)
        return
    _touch_rid(batch.bid)
    running = bool(batch.run_indices)

    def number(position: int, default: int = 0) -> int:
        return int(parts[position]) if len(parts) > position + 1 and parts[position].isdigit() else default

    async def picker(page: int, notice: str = "") -> None:
        batch.page = batch_menu.clamp_page(batch, page)
        await show(batch_menu.picker_text(batch, batch.page, notice), batch_menu.picker_menu(batch, batch.page))

    async def quality_screen() -> None:
        await show(batch_menu.quality_text(batch), batch_menu.quality_menu(batch))

    # ---- while it is running or finished: only these buttons do anything
    if action == "cx":
        batch.cancel(job_manager)
        return
    if action == "rt":
        await batch.retry_failed(job_manager, _batch_settings_for(batch.settings))
        return
    if action in ("dm", "x"):
        batch.closed = True
        pending_batches.pop(batch.bid, None)
        await _delete_quietly(query.message)
        return
    if running:
        return                                          # a stale picker button on a batch that has already started

    # ---- the selection list
    if action == "t":
        index = number(2, -1)
        if not 0 <= index < len(batch.items):
            return
        if index in batch.selected:
            batch.selected.discard(index)
            await picker(number(3))
        elif len(batch.selected) >= batch_menu.MAX_BATCH_DOWNLOAD:
            await picker(number(3), f"You can select up to {batch_menu.MAX_BATCH_DOWNLOAD} at a time.")
        else:
            batch.selected.add(index)
            await picker(number(3))
    elif action == "p":
        await picker(number(2))
    elif action == "sa":
        await picker(number(2), _select_all(batch))
    elif action == "cl":
        batch.selected.clear()
        await picker(number(2))
    elif action == "q":
        if batch.selected:
            await quality_screen()
        else:
            await picker(batch.page, "Select at least one item first.")
    elif action == "qa":
        _select_all(batch)
        await quality_screen()
    elif action == "bk":
        await picker(batch.page)
    elif action == "go" and len(parts) > 3 and parts[2] in batch_menu.QUALITIES and batch.selected:
        batch.settings = _batch_settings(parts[2], user_id)
        await batch.start(job_manager, _batch_settings_for(batch.settings), sorted(batch.selected))


# ---------- link handling ----------

def _preview_title(rid: str) -> str:
    """Title from the quality-picker preview, if we probed one. It only seeds
    /history for jobs that never deliver a file (failed / cancelled); a
    successful job's file name replaces it. ADHD Mode never probes -> ""."""
    probe_result = pending_probes.get(rid)
    return probe_result.title if probe_result and probe_result.title else ""


async def handle_adhd_download(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str, rid: str,
                                user_id: int) -> None:
    """ADHD Mode: no probing, no quality picker, no confirmation - just
    queue the best-quality download straight away. Spotify links still
    need spotify_handler's own title lookup during the actual download
    (that's unavoidable - it has to search YouTube for a matching track),
    but skip our own confirmation menu for it too."""
    job_settings = dict(get_settings(user_id))
    if "spotify.com" in url:
        job_settings["mode"] = "audio"
    else:
        job_settings["mode"] = "video"
        job_settings["quality"] = "best"

    last_download[rid] = (user_id, url, job_settings)
    _touch_rid(rid)
    status_msg = await context.bot.send_message(
        update.effective_chat.id, messages.QUEUED, parse_mode=ParseMode.HTML, reply_markup=queued_menu(rid, url),
    )
    await job_manager.enqueue(rid, user_id, status_msg.chat_id, url, job_settings, status_msg.message_id)


async def handle_spotify_link(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str, rid: str) -> None:
    user_id = update.effective_user.id
    pending_links[rid] = (user_id, url)
    _touch_rid(rid)
    status_msg = await context.bot.send_message(
        update.effective_chat.id, "Checking via Spotify search…", reply_markup=queued_menu(rid, url),
    )
    info = await get_track_info(url)
    if rid in cancelled_pre_job_rids:
        cancelled_pre_job_rids.discard(rid)
        return

    title = (info or {}).get("title") or "Spotify track"
    thumbnail = (info or {}).get("thumbnail") or ""
    markup = spotify_menu(rid)
    caption = messages.with_link(esc(title), url)

    if thumbnail:
        try:
            await context.bot.send_photo(
                update.effective_chat.id, thumbnail, caption=caption,
                parse_mode=ParseMode.HTML, reply_markup=markup,
            )
            await _delete_quietly(status_msg)
            return
        except Exception:  # noqa: BLE001
            log.debug("Spotify thumbnail send failed, falling back to text", exc_info=True)

    try:
        await status_msg.edit_text(caption, parse_mode=ParseMode.HTML, reply_markup=markup)
    except Exception:  # noqa: BLE001
        log.warning("Falling back to plain text after HTML edit failed", exc_info=True)
        await status_msg.edit_text(messages.with_link(messages.PICK_OPTION, url), parse_mode=ParseMode.HTML,
                                    reply_markup=markup)


async def link_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    raw_text = (update.message.text or "").strip()

    if user_id in pending_admin_input:
        await handle_admin_text_input(update, user_id, raw_text)
        return

    if user_id in pending_setting_input:
        await handle_setting_text_input(update, user_id, raw_text)
        return

    tool_state = pending_tool_input.get(user_id)
    if tool_state and tool_state["kind"] in ("trim", "gif"):
        if time.time() - tool_state["at"] > SECTION_INPUT_TTL_SECONDS or URL_RE.search(raw_text):
            pending_tool_input.pop(user_id, None)      # stale, or they pasted a link: move on
        else:
            await handle_tool_text_input(update, user_id, raw_text)
            return

    section_state = pending_section_input.get(user_id)
    if section_state:
        if time.time() - section_state["at"] > SECTION_INPUT_TTL_SECONDS or URL_RE.search(raw_text):
            pending_section_input.pop(user_id, None)   # stale, or they pasted a new link: move on
        else:
            await handle_section_text_input(update, user_id, raw_text)
            return

    match = URL_RE.search(raw_text)
    if not match:
        return

    if not await gate(update, context):
        return

    url = match.group(0)

    # tidy the chat - the link itself doesn't need to stick around once we
    # have it; everything from here happens in one evolving message
    await _delete_quietly(update.message)

    rid = new_rid()

    if get_settings(user_id).get("adhd_mode"):
        await handle_adhd_download(update, context, url, rid, user_id)
        return

    urls = _unique_urls(raw_text)
    if len(urls) >= 2:
        await start_links_picker(context.bot, user_id, update.effective_chat.id, urls)
        return

    if "spotify.com" in url:
        await handle_spotify_link(update, context, url, rid)
        return

    order = tool_order_for(url)
    checking_text = f"Checking via {FRIENDLY_TOOL.get(order[0], order[0])}…"
    pending_links[rid] = (user_id, url)
    _touch_rid(rid)
    status_msg = await context.bot.send_message(
        update.effective_chat.id, checking_text, reply_markup=queued_menu(rid, url),
    )

    if order[0] == "ytdlp" and looks_like_playlist(url):
        # No point previewing "a video" that isn't one: list the playlist's items directly.
        pending_links.pop(rid, None)
        await start_playlist_picker(context.bot, user_id, update.effective_chat.id, url, status_msg)
        return

    # Preview with yt-dlp wherever it can be used - not only where it is the FIRST tool. X/Twitter and
    # Pinterest try gallery-dl first (right for image posts), but a post with a video deserves the same
    # quality picker, sizes and time ranges; an image post simply fails the preview and falls through
    # to the gallery-dl menu below, as before.
    probe_result = await probe(url, user_id) if "ytdlp" in order else None
    if rid in cancelled_pre_job_rids:
        cancelled_pre_job_rids.discard(rid)
        return

    if probe_result and cookie_health.cookies_in_use(user_id, get_settings(user_id), url):
        # A preview is a request with the person's cookies too: it feeds the same
        # "have they stopped working?" tracking as downloads.
        if probe_result.ok:
            cookie_health.watch.record_success(user_id)
        elif cookie_health.watch.record_failure(user_id, probe_result.error):
            await update.effective_chat.send_message(cookie_health.COOKIE_ALERT, parse_mode=ParseMode.HTML)

    if probe_result and probe_result.ok and probe_result.is_playlist:
        # A playlist link: list what is in it so the person can pick, instead of guessing.
        pending_links.pop(rid, None)
        await start_playlist_picker(context.bot, user_id, update.effective_chat.id, url, status_msg)
        return

    if probe_result and probe_result.ok and not probe_result.is_playlist:
        pending_probes[rid] = probe_result
        _touch_rid(rid)
        _prefill_start_time(rid, url, probe_result)
        caption = messages.with_link(_quality_caption(rid, probe_result), url)
        # Audio-only sources (SoundCloud, etc.) get audio formats only - see quality_menu.
        markup = _main_menu_for(rid, probe_result, user_id)
        if probe_result.thumbnail:
            try:
                await context.bot.send_photo(
                    update.effective_chat.id, probe_result.thumbnail,
                    caption=caption, parse_mode=ParseMode.HTML, reply_markup=markup,
                )
                # only delete the "checking..." message once the photo is
                # confirmed sent - otherwise we'd have nothing left to edit
                await _delete_quietly(status_msg)
                return
            except Exception:  # noqa: BLE001
                log.debug("Thumbnail send failed, falling back to text menu", exc_info=True)
        try:
            await status_msg.edit_text(caption, parse_mode=ParseMode.HTML, reply_markup=markup)
        except Exception:  # noqa: BLE001
            log.warning("Falling back to plain text after HTML edit failed", exc_info=True)
            await status_msg.edit_text(messages.with_link(messages.PICK_OPTION, url), parse_mode=ParseMode.HTML,
                                        reply_markup=markup)
        return

    # Primary probe failed (or this domain never uses yt-dlp first). For
    # domains where gallery-dl is also an option, try a best-effort
    # secondary probe just for a title/thumbnail preview - common on
    # Instagram, where yt-dlp's metadata fetch is blocked more often than
    # gallery-dl's. This is cosmetic only; it doesn't change what tool
    # actually downloads the content.
    gdl_info = None
    if "gallerydl" in order:
        gdl_info = await gallerydl_probe.probe(url)

    if gdl_info and (gdl_info.get("title") or gdl_info.get("thumbnail")):
        caption_text = esc(gdl_info["title"]) if gdl_info.get("title") else messages.PICK_OPTION
        if order[0] == "ytdlp" and not is_image_site(url):
            # yt-dlp is this site's first tool and it failed; gallery-dl only found a title/picture. Say so -
            # otherwise the missing quality/size/section buttons look like a bug.
            error = probe_result.error if probe_result else ""
            caption_text += "\n\n" + messages.preview_failed_note(error) + messages.preview_failed_reason(error)
        caption = messages.with_link(caption_text, url)
        markup = simple_menu(rid) if (order[0] != "ytdlp" or is_image_site(url)) else fallback_menu(rid)
        if gdl_info.get("thumbnail"):
            try:
                await context.bot.send_photo(
                    update.effective_chat.id, gdl_info["thumbnail"],
                    caption=caption, parse_mode=ParseMode.HTML, reply_markup=markup,
                )
                await _delete_quietly(status_msg)
                return
            except Exception:  # noqa: BLE001
                log.debug("gallery-dl thumbnail send failed, falling back to text menu", exc_info=True)
        try:
            await status_msg.edit_text(caption, parse_mode=ParseMode.HTML, reply_markup=markup)
        except Exception:  # noqa: BLE001
            log.warning("Falling back to plain text after HTML edit failed", exc_info=True)
            await status_msg.edit_text(messages.with_link(messages.PICK_OPTION, url), parse_mode=ParseMode.HTML,
                                        reply_markup=markup)
        return

    if order[0] != "ytdlp" or is_image_site(url):
        # Not a video the preview could read (or a site where posts are often just images): the plain menu.
        caption = messages.with_link(messages.PICK_OPTION, url)
        await status_msg.edit_text(caption, parse_mode=ParseMode.HTML, reply_markup=simple_menu(rid))
    else:
        error = probe_result.error if probe_result else ""
        note = messages.preview_failed_note(error) + messages.preview_failed_reason(error)
        caption = messages.with_link(note, url)
        await status_msg.edit_text(caption, parse_mode=ParseMode.HTML, reply_markup=fallback_menu(rid))


async def link_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not await gate_callback(update, context):
        return
    user_id = update.effective_user.id
    parts = (query.data or "").split("|")
    action = parts[1] if len(parts) > 1 else ""
    rid = parts[-1] if len(parts) > 2 else ""
    is_photo = bool(query.message.photo)

    async def set_text(text: str, markup=None) -> None:
        if is_photo:
            await query.edit_message_caption(caption=text, parse_mode=ParseMode.HTML, reply_markup=markup)
        else:
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)

    if action == "sec":
        await sections_callback(context, query, user_id, parts, rid, is_photo)
        return
    pending_section_input.pop(user_id, None)   # any other button abandons a half-typed time

    if action == "sub":
        await subtitles_callback(context, query, user_id, parts, rid, is_photo)
        return

    if action == "dismiss":
        await _delete_quietly(query.message)
        return

    if action == "cancel":
        if job_manager and job_manager.cancel(rid, user_id):
            return  # the job's own handler (queued or running) updates the message
        entry = pending_links.get(rid)
        if entry and entry[0] == user_id:
            url = entry[1]
            cancelled_pre_job_rids.add(rid)
            await set_text(messages.with_link(messages.CANCELLED, url), markup=redo_menu(rid, url))
        else:
            await set_text(messages.CANCELLED)
        return

    if action == "redo":
        entry = pending_links.get(rid)
        if not entry or entry[0] != user_id:
            await set_text("This link expired — send it again.")
            return
        url = entry[1]
        probe_result = pending_probes.get(rid)
        markup = _main_menu_for(rid, probe_result, user_id) if probe_result else fallback_menu(rid)
        title = _quality_caption(rid, probe_result) if probe_result else messages.PICK_OPTION
        await set_text(messages.with_link(title, url), markup=markup)
        return

    if action == "moreq":
        probe_result = pending_probes.get(rid)
        if not probe_result:
            await query.edit_message_reply_markup(reply_markup=fallback_menu(rid))
            return
        await set_text(_quality_caption(rid, probe_result), markup=_more_menu_for(rid, probe_result, user_id))
        return

    if action == "backq":
        probe_result = pending_probes.get(rid)
        if not probe_result:
            await query.edit_message_reply_markup(reply_markup=fallback_menu(rid))
            return
        await set_text(_quality_caption(rid, probe_result), markup=_main_menu_for(rid, probe_result, user_id))
        return

    if action == "retry":
        entry = last_download.get(rid)
        if not entry or entry[0] != user_id:
            await query.answer("Nothing to retry.", show_alert=True)
            return
        _, url, saved_settings = entry

        if job_manager.has_cached_video(rid, url):
            await set_text(messages.with_link("Sending from the local copy — no re-download needed…", url),
                            markup=queued_menu(rid, url))
            sent = await job_manager.send_cached_as_document(
                rid, query.message.chat_id, url, query.message.message_id,
            )
            if sent:
                return
            # cache vanished mid-flight - fall through to a fresh download

        await set_text(messages.with_link(messages.QUEUED, url), markup=queued_menu(rid, url))
        await job_manager.enqueue(
            rid, user_id, query.message.chat_id, url, _with_current_look(user_id, saved_settings), query.message.message_id,
            is_photo=is_photo, title=_preview_title(rid),
        )
        return

    if action == "asfile":
        entry = last_download.get(rid)
        if not entry or entry[0] != user_id:
            await query.answer("That download isn't available anymore.", show_alert=True)
            return
        _, url, saved_settings = entry
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:  # noqa: BLE001
            pass

        if job_manager.has_cached_video(rid, url):
            status_msg = await context.bot.send_message(
                query.message.chat_id, messages.with_link("Sending the file…", url), parse_mode=ParseMode.HTML,
            )
            sent = await job_manager.send_cached_as_document(
                rid, status_msg.chat_id, url, status_msg.message_id,
            )
            if sent:
                return
            # cache lookup raced or the file vanished - fall through to a fresh download

        status_msg = await context.bot.send_message(
            query.message.chat_id, messages.with_link("Re-fetching as a file…", url), parse_mode=ParseMode.HTML,
        )
        await job_manager.enqueue(
            rid, user_id, status_msg.chat_id, url, _with_current_look(user_id, saved_settings), status_msg.message_id,
            is_photo=False, force_document=True, title=_preview_title(rid),
        )
        return

    if action in ("video", "audio", "simple") and len(parts) == 4:
        preview_title = _preview_title(rid)   # must be read BEFORE the probe state is dropped
        clip_sections, clip_merge = _active_sections(rid)   # ditto: needs the probe's duration
        sub_choice = _active_subs(rid)
        had_preview = rid in pending_probes
        entry = pending_links.pop(rid, None)
        pending_probes.pop(rid, None)
        pending_sections.pop(rid, None)
        pending_subs.pop(rid, None)
        if not entry or entry[0] != user_id:
            # State was lost (restart, long gap, etc.) but the link is
            # still right there in the message - recover it instead of
            # dead-ending the person.
            source_text = query.message.caption or query.message.text or ""
            match = URL_RE.search(source_text)
            if match:
                url = match.group(0)
            else:
                markup = InlineKeyboardMarkup([[InlineKeyboardButton("↻ Try again", callback_data=f"dl|redo|{rid}")]])
                await set_text("This link expired — send it again.", markup=markup)
                return
        else:
            url = entry[1]

        value = parts[2]
        job_settings = dict(get_settings(user_id))
        if action == "video":
            job_settings["mode"] = "video"
            job_settings["quality"] = value
        elif action == "audio":
            job_settings["mode"] = "audio"
            if value == "mp3split":                    # one MP3 per chapter
                job_settings["audio_format"] = "mp3"
                job_settings["split_chapters"] = True
            else:
                job_settings["audio_format"] = value
        # "simple" (gallery/direct file): leave settings as-is
        if clip_sections and action in ("video", "audio"):
            # The person chose parts of the video in the editor: this button's
            # quality / format applies to just those parts.
            job_settings["sections"] = clip_sections
            job_settings["sections_merge"] = clip_merge
            job_settings.pop("split_chapters", None)       # the chosen sections already say which parts to take
        elif sub_choice and action == "video":
            job_settings.update(sub_choice.settings())     # sub_langs + sub_mode (never on a clip: wrong timing)
        if had_preview and tool_order_for(url)[0] != "ytdlp":
            job_settings["prefer_ytdlp"] = True            # what was chosen only means something to yt-dlp
        elif action == "simple" and is_image_site(url):
            job_settings["prefer_gallerydl"] = True        # no video preview: most likely an image post

        last_download[rid] = (user_id, url, job_settings)
        _touch_rid(rid)
        await set_text(messages.with_link(messages.QUEUED, url), markup=queued_menu(rid, url))
        await job_manager.enqueue(
            rid, user_id, query.message.chat_id, url, job_settings, query.message.message_id, is_photo=is_photo,
            title=preview_title,
        )


# ---------- app wiring ----------

def build_application() -> Application:
    builder = (
        ApplicationBuilder()
        .token(config.BOT_TOKEN)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        # Without this, PTB processes updates one at a time - one user's
        # link probe (a few seconds of network calls) would freeze the
        # bot for everyone else until it finished.
        .concurrent_updates(True)
    )
    if config.LOCAL_BOT_API_URL:
        builder = builder.base_url(f"{config.LOCAL_BOT_API_URL}/bot").base_file_url(
            f"{config.LOCAL_BOT_API_URL}/file/bot"
        ).local_mode(True)
    return builder.build()


def wait_for_local_api(url: str, timeout_seconds: int = 60) -> None:
    if not url:
        return
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            httpx.get(url, timeout=3)
            log.info("Local Bot API server is up at %s", url)
            return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(1.5)
    log.warning(
        "Local Bot API server didn't respond within %ss (last error: %s) - starting anyway.",
        timeout_seconds, last_error,
    )


def wait_for_pot_provider(url: str, timeout_seconds: int = 20) -> None:
    if not url:
        return
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            httpx.get(f"{url}/ping", timeout=3)
            log.info("PO Token provider is up at %s", url)
            return
        except Exception:  # noqa: BLE001
            time.sleep(1.5)
    log.warning("PO Token provider didn't respond within %ss - continuing anyway.", timeout_seconds)


async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Without this, PTB's default behavior for ANY unhandled exception in
    ANY handler is to log it server-side and tell the user nothing at all -
    which is exactly what happened with the cookies-upload crash: a full
    traceback in the logs, and total silence in the chat. This can't fix
    the underlying error, but it makes sure a person is never just left
    staring at a bot that stopped responding with no explanation."""
    log.error("Unhandled exception while processing update: %s", update, exc_info=context.error)
    if not isinstance(update, Update):
        return
    text = "✕ Something went wrong on my end. Try again in a moment."
    try:
        if update.callback_query:
            await update.callback_query.answer(text, show_alert=True)
        elif update.effective_message:
            await update.effective_message.reply_text(text)
    except Exception:  # noqa: BLE001
        log.debug("Couldn't even deliver the generic error message", exc_info=True)


async def post_init(application: Application) -> None:
    global job_manager
    sweep_orphaned_workspaces()
    job_manager = JobManager(application.bot, config.MAX_CONCURRENT_DOWNLOADS)
    job_manager.start()
    asyncio.create_task(run_update_once(application.bot, notify_admins=False))
    asyncio.create_task(daily_update_loop(application.bot, config.AUTO_UPDATE_HOUR_UTC))
    asyncio.create_task(_sweep_stale_link_state_loop())
    asyncio.create_task(housekeeping.loop(extra=tools.store.expire))
    log.info("%s is ready %s", config.OWNER_NAME, config.OWNER_EMOJI)


def _lower_priority() -> None:
    """Run at low CPU priority. ffmpeg and the other child processes inherit it, so a heavy conversion
    or exact clip cut can't make the host machine sluggish. Harmless where it isn't supported."""
    if config.PROCESS_NICE:
        try:
            os.nice(config.PROCESS_NICE)
        except (AttributeError, OSError):
            pass


def main() -> None:
    _lower_priority()
    if config.OWNER_USER_ID is None:
        log.warning(
            "OWNER_USER_ID is not set in .env — the admin panel and "
            "access-control privileges won't recognize an owner. Set it "
            "to her Telegram user ID (from @userinfobot)."
        )
    wait_for_local_api(config.LOCAL_BOT_API_URL, timeout_seconds=60)
    wait_for_pot_provider(config.BGUTIL_POT_URL, timeout_seconds=20)
    app = build_application()
    app.post_init = post_init
    app.add_error_handler(global_error_handler)

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(CommandHandler("tools", tools_cmd))
    app.add_handler(CommandHandler("adhd_on", adhd_on_cmd))
    app.add_handler(CommandHandler("adhd_off", adhd_off_cmd))
    app.add_handler(CommandHandler("queue", queue_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("update", update_cmd))
    app.add_handler(CommandHandler("potcheck", potcheck_cmd))
    app.add_handler(CommandHandler("cookies", cookies_cmd))
    app.add_handler(CommandHandler(config.OWNER_ADMIN_COMMAND, owner_admin_cmd))
    app.add_handler(MessageHandler(filters.Document.FileExtension("txt"), cookies_file_handler))
    app.add_handler(MessageHandler(filters.Document.FileExtension("srt"), srt_handler))
    app.add_handler(MessageHandler(
        filters.VIDEO | filters.AUDIO | filters.VOICE | filters.VIDEO_NOTE | filters.ANIMATION
        | (filters.Document.ALL & ~filters.Document.FileExtension("txt") & ~filters.Document.FileExtension("srt")),
        media_handler))
    app.add_handler(CallbackQueryHandler(admin_panel_callback, pattern=r"^adm\|"))
    app.add_handler(CallbackQueryHandler(force_join_callback, pattern=r"^fj\|"))
    app.add_handler(CallbackQueryHandler(misc_callback, pattern=r"^misc\|"))
    app.add_handler(CallbackQueryHandler(link_callback, pattern=r"^dl\|"))
    app.add_handler(CallbackQueryHandler(batch_callback, pattern=r"^bt\|"))
    app.add_handler(CallbackQueryHandler(tools_callback, pattern=r"^tl\|"))
    app.add_handler(CallbackQueryHandler(settings_callback, pattern=r"^(s\||nav\||ck\|)"))
    app.add_handler(CallbackQueryHandler(history_callback, pattern=r"^hist\|"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, link_handler))

    log.info("Owner admin command: /%s", config.OWNER_ADMIN_COMMAND)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
