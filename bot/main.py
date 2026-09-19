import asyncio
import logging
import re
import time

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)

import config
from jobqueue.job_manager import JobManager
from settings import access_control as ac
from settings.user_settings import get_settings, update_setting, reset_settings
from ui import messages, admin_menu
from ui.quick_menu import (
    video_menu, extended_video_menu, simple_menu, fallback_menu, spotify_menu,
    queued_menu,
)
from ui.settings_menu import (
    main_menu, mode_menu, quality_menu, subs_menu, playlist_menu,
    advanced_menu, confirm_reset_menu, back_to_main,
)
from ui.start_menu import start_menu
from downloader.probe import probe, ProbeResult
from downloader import gallerydl_probe
from downloader.site_map import tool_order_for
from downloader.spotify_handler import get_track_info
from updater.auto_update import daily_update_loop, run_update_once
from utils.cleanup import sweep_orphaned_workspaces
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("candy.main")

URL_RE = re.compile(r"https?://\S+")

job_manager: JobManager | None = None
pending_links: dict[int, str] = {}                    # user_id -> url awaiting a quick-pick choice
pending_probes: dict[int, ProbeResult] = {}           # user_id -> probe result, for the "more options" submenu
pending_admin_input: dict[int, str] = {}              # user_id -> which admin panel field they're typing
last_download: dict[int, tuple[str, dict]] = {}       # user_id -> (url, settings) for "send as file"/"retry"


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


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate(update, context):
        return
    await _delete_quietly(update.message)
    lines = [
        f"{config.OWNER_EMOJI} <b>{config.OWNER_NAME}'s Downloader</b>",
        "Paste a link, pick from the buttons.",
        "",
        "/settings — your saved defaults (cookies help is in there too)",
        "/queue — what's running",
        "/cancel — stop your current download",
    ]
    if is_owner_or_admin(update.effective_user.id):
        lines.append(f"/{config.OWNER_ADMIN_COMMAND} — admin panel")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def queue_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate(update, context):
        return
    await _delete_quietly(update.message)
    await update.message.reply_text(job_manager.active_summary(), parse_mode=ParseMode.HTML)


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate(update, context):
        return
    await _delete_quietly(update.message)
    ok = job_manager.cancel_for_user(update.effective_user.id)
    await update.message.reply_text(messages.CANCELLED if ok else messages.NOTHING_TO_CANCEL)


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
    "2. While logged in on the site, export cookies for that domain\n"
    "3. Send me the exported <code>.txt</code> file right here\n\n"
    "• This is private to you — every person using this bot has their "
    "own cookies file, and no one else can see or use yours.\n\n"
    "• Using your main account's cookies for automated downloads can "
    "occasionally get that account rate-limited. A secondary account is safer."
)


async def cookies_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate(update, context):
        return
    await _delete_quietly(update.message)
    await update.message.reply_text(COOKIES_HELP, parse_mode=ParseMode.HTML)


async def cookies_file_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate(update, context):
        return
    doc = update.message.document
    user_id = update.effective_user.id
    Path(config.COOKIES_DIR).mkdir(parents=True, exist_ok=True)
    dest = Path(config.COOKIES_DIR) / f"{user_id}.txt"

    tg_file = await doc.get_file()
    await tg_file.download_to_drive(custom_path=str(dest))

    update_setting(user_id, "cookies_enabled", True)
    await update.message.reply_text("Cookies saved and turned on — just for you.")


# ---------- settings menu ----------

def settings_title() -> str:
    return f"{config.OWNER_EMOJI} <b>Your settings</b>"


async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate(update, context):
        return
    await _delete_quietly(update.message)
    s = get_settings(update.effective_user.id)
    await update.message.reply_text(settings_title(), parse_mode=ParseMode.HTML, reply_markup=main_menu(s))


async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    data = query.data or ""

    if data.startswith("nav|"):
        screen = data.split("|", 1)[1]

        if screen == "cookies":
            await query.edit_message_text(COOKIES_HELP, parse_mode=ParseMode.HTML, reply_markup=back_to_main())
            return

        if screen == "home":
            await query.edit_message_text(messages.WELCOME, parse_mode=ParseMode.HTML, reply_markup=start_menu())
            return

        s = get_settings(user_id)
        screens = {
            "main": (main_menu, settings_title()),
            "mode": (mode_menu, "Pick a mode"),
            "quality": (quality_menu, "Pick quality/format"),
            "subs": (subs_menu, "Subtitles"),
            "playlist": (playlist_menu, "Playlist handling"),
            "advanced": (advanced_menu, "Advanced settings"),
            "reset": (confirm_reset_menu, "Reset ALL your settings to default?"),
        }
        if screen in screens:
            builder, title = screens[screen]
            markup = builder(s) if builder not in (mode_menu, confirm_reset_menu) else builder()
            await query.edit_message_text(title, parse_mode=ParseMode.HTML, reply_markup=markup)
        return

    if data.startswith("s|"):
        _, key, value = data.split("|", 2)
        if key == "__reset__":
            reset_settings(user_id)
        elif key in {"embed_thumbnail", "embed_metadata", "sponsorblock", "use_archive",
                     "embed_subtitles", "cookies_enabled"}:
            update_setting(user_id, key, value == "1")
        else:
            update_setting(user_id, key, value)

        s = get_settings(user_id)
        await query.edit_message_text(settings_title(), parse_mode=ParseMode.HTML, reply_markup=main_menu(s))


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
    elif action == "close":
        try:
            await query.message.delete()
        except Exception:  # noqa: BLE001
            pass


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


# ---------- misc buttons (from /start) ----------

async def misc_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    action = (query.data or "").split("|", 1)[1]
    if action == "queue":
        text = job_manager.active_summary()
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("← Back", callback_data="nav|home")]])
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


# ---------- link handling ----------

async def handle_spotify_link(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str) -> None:
    user_id = update.effective_user.id
    status_msg = await context.bot.send_message(
        update.effective_chat.id, messages.with_link(messages.PROBING, url), parse_mode=ParseMode.HTML,
    )
    info = await get_track_info(url)
    pending_links[user_id] = url

    title = (info or {}).get("title") or "Spotify track"
    thumbnail = (info or {}).get("thumbnail") or ""
    markup = spotify_menu()
    caption = messages.with_link(title, url)

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

    await status_msg.edit_text(caption, parse_mode=ParseMode.HTML, reply_markup=markup)


async def link_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    raw_text = (update.message.text or "").strip()

    if user_id in pending_admin_input:
        await handle_admin_text_input(update, user_id, raw_text)
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

    if "spotify.com" in url:
        await handle_spotify_link(update, context, url)
        return

    order = tool_order_for(url)
    status_msg = await context.bot.send_message(
        update.effective_chat.id, messages.with_link(messages.PROBING, url), parse_mode=ParseMode.HTML,
    )

    probe_result = await probe(url) if order[0] == "ytdlp" else None
    pending_links[user_id] = url

    if probe_result and probe_result.ok and not probe_result.is_playlist:
        pending_probes[user_id] = probe_result
        caption = messages.with_link(probe_result.title or messages.PICK_OPTION, url)
        markup = video_menu(probe_result)
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
        await status_msg.edit_text(caption, parse_mode=ParseMode.HTML, reply_markup=markup)
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
        caption = messages.with_link(gdl_info.get("title") or messages.PICK_OPTION, url)
        markup = simple_menu() if order[0] != "ytdlp" else fallback_menu()
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
        await status_msg.edit_text(caption, parse_mode=ParseMode.HTML, reply_markup=markup)
        return

    if order[0] != "ytdlp":
        caption = messages.with_link(messages.PICK_OPTION, url)
        await status_msg.edit_text(caption, parse_mode=ParseMode.HTML, reply_markup=simple_menu())
    else:
        note = messages.preview_failed_note(probe_result.error if probe_result else "")
        caption = messages.with_link(note, url)
        await status_msg.edit_text(caption, parse_mode=ParseMode.HTML, reply_markup=fallback_menu())


async def link_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    parts = (query.data or "").split("|")
    action = parts[1] if len(parts) > 1 else ""
    is_photo = bool(query.message.photo)

    async def set_text(text: str, markup=None) -> None:
        if is_photo:
            await query.edit_message_caption(caption=text, parse_mode=ParseMode.HTML, reply_markup=markup)
        else:
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)

    if action == "cancel":
        if job_manager and job_manager.cancel_for_user(user_id):
            return  # the job's own handler (queued or running) updates the message
        url = pending_links.get(user_id)
        if url:
            markup = InlineKeyboardMarkup([[InlineKeyboardButton("↻ Try again", callback_data="dl|redo")]])
            await set_text(messages.with_link(messages.CANCELLED, url), markup=markup)
        else:
            await set_text(messages.CANCELLED)
        return

    if action == "redo":
        url = pending_links.get(user_id)
        if not url:
            await set_text("This link expired — send it again.")
            return
        probe_result = pending_probes.get(user_id)
        markup = video_menu(probe_result) if probe_result else fallback_menu()
        title = probe_result.title if probe_result and probe_result.title else messages.PICK_OPTION
        await set_text(messages.with_link(title, url), markup=markup)
        return

    if action == "moreq":
        probe_result = pending_probes.get(user_id)
        markup = extended_video_menu(probe_result) if probe_result else fallback_menu()
        await query.edit_message_reply_markup(reply_markup=markup)
        return

    if action == "backq":
        probe_result = pending_probes.get(user_id)
        markup = video_menu(probe_result) if probe_result else fallback_menu()
        await query.edit_message_reply_markup(reply_markup=markup)
        return

    if action == "retry":
        entry = last_download.get(user_id)
        if not entry:
            await query.answer("Nothing to retry.", show_alert=True)
            return
        url, saved_settings = entry
        await set_text(messages.with_link(messages.QUEUED, url), markup=queued_menu())
        await job_manager.enqueue(
            user_id, query.message.chat_id, url, dict(saved_settings), query.message.message_id,
            is_photo=is_photo,
        )
        return

    if action == "asfile":
        entry = last_download.get(user_id)
        if not entry:
            await query.answer("That download isn't available anymore.", show_alert=True)
            return
        url, saved_settings = entry
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:  # noqa: BLE001
            pass

        if job_manager.has_cached_video(user_id, url):
            status_msg = await context.bot.send_message(
                query.message.chat_id, messages.with_link("Sending the file…", url), parse_mode=ParseMode.HTML,
            )
            sent = await job_manager.send_cached_as_document(
                user_id, status_msg.chat_id, url, status_msg.message_id,
            )
            if sent:
                return
            # cache lookup raced or the file vanished - fall through to a fresh download

        status_msg = await context.bot.send_message(
            query.message.chat_id, messages.with_link("Re-fetching as a file…", url), parse_mode=ParseMode.HTML,
        )
        await job_manager.enqueue(
            user_id, status_msg.chat_id, url, dict(saved_settings), status_msg.message_id,
            is_photo=False, force_document=True,
        )
        return

    if action in ("video", "audio", "simple") and len(parts) == 3:
        url = pending_links.pop(user_id, None)
        pending_probes.pop(user_id, None)
        if not url:
            await set_text("This link expired — send it again.")
            return

        value = parts[2]
        job_settings = dict(get_settings(user_id))
        if action == "video":
            job_settings["mode"] = "video"
            job_settings["quality"] = value
        elif action == "audio":
            job_settings["mode"] = "audio"
            job_settings["audio_format"] = value
        # "simple" (gallery/direct file): leave settings as-is

        last_download[user_id] = (url, job_settings)
        await set_text(messages.with_link(messages.QUEUED, url), markup=queued_menu())
        await job_manager.enqueue(
            user_id, query.message.chat_id, url, job_settings, query.message.message_id, is_photo=is_photo,
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


async def post_init(application: Application) -> None:
    global job_manager
    sweep_orphaned_workspaces()
    job_manager = JobManager(application.bot, config.MAX_CONCURRENT_DOWNLOADS)
    job_manager.start()
    asyncio.create_task(run_update_once(application.bot, notify_admins=False))
    asyncio.create_task(daily_update_loop(application.bot, config.AUTO_UPDATE_HOUR_UTC))
    log.info("%s is ready %s", config.OWNER_NAME, config.OWNER_EMOJI)


def main() -> None:
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

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CommandHandler("queue", queue_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("update", update_cmd))
    app.add_handler(CommandHandler("potcheck", potcheck_cmd))
    app.add_handler(CommandHandler("cookies", cookies_cmd))
    app.add_handler(CommandHandler(config.OWNER_ADMIN_COMMAND, owner_admin_cmd))
    app.add_handler(MessageHandler(filters.Document.FileExtension("txt"), cookies_file_handler))
    app.add_handler(CallbackQueryHandler(admin_panel_callback, pattern=r"^adm\|"))
    app.add_handler(CallbackQueryHandler(force_join_callback, pattern=r"^fj\|"))
    app.add_handler(CallbackQueryHandler(misc_callback, pattern=r"^misc\|"))
    app.add_handler(CallbackQueryHandler(link_callback, pattern=r"^dl\|"))
    app.add_handler(CallbackQueryHandler(settings_callback, pattern=r"^(s\||nav\|)"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, link_handler))

    log.info("Owner admin command: /%s", config.OWNER_ADMIN_COMMAND)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
