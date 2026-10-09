from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from config import OWNER_NAME
from settings import access_control as ac


def panel_title() -> str:
    return f"⚙ <b>{OWNER_NAME}'s Admin Panel</b>"


def main_panel() -> InlineKeyboardMarkup:
    mode = ac.get_mode()
    channel = ac.get_force_join_channel()
    mode_label = "Public" if mode == "public" else "Private"
    join_label = f"Force-join: {channel}" if channel else "Force-join: off"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Access: {mode_label}", callback_data="adm|toggle_mode")],
        [InlineKeyboardButton("Allowed users", callback_data="adm|users")],
        [InlineKeyboardButton(join_label, callback_data="adm|join")],
        [InlineKeyboardButton("Stats", callback_data="adm|stats"),
         InlineKeyboardButton("Activity", callback_data="adm|activity")],
        [InlineKeyboardButton("All users", callback_data="adm|all_users"),
         InlineKeyboardButton("🌐 WARP", callback_data="adm|warp")],
        [InlineKeyboardButton("✕ Close", callback_data="adm|close")],
    ])


def users_panel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("+ Add user", callback_data="adm|add_user"),
         InlineKeyboardButton("− Remove user", callback_data="adm|remove_user")],
        [InlineKeyboardButton("← Back", callback_data="adm|main")],
    ])


def join_panel(channel: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("Set channel", callback_data="adm|set_channel")]]
    if channel:
        rows.append([InlineKeyboardButton("Turn off", callback_data="adm|clear_channel")])
    rows.append([InlineKeyboardButton("← Back", callback_data="adm|main")])
    return InlineKeyboardMarkup(rows)


def back_only() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("← Back", callback_data="adm|main")]])


def warp_text(proxy_known: bool, reachable: bool, ip: str | None, configured: bool, running_jobs: int,
              notice: str = "") -> str:
    lines = [panel_title(), "", "<b>WARP</b>"]
    if not proxy_known:
        lines.append("No WARP proxy is configured (YT_PROXIES is empty).")
    else:
        lines.append("Status: " + ("✓ reachable" if reachable else "✕ not reachable"))
        lines.append(f"Address: <code>{ip}</code>" if ip else "Address: unknown")
    lines.append(f"Downloads running: {running_jobs}")
    if not configured:
        lines += ["", "<i>The “change address” service isn't set up. By hand: "
                      "<code>docker compose up -d --force-recreate warp</code></i>"]
    if notice:
        lines += ["", notice]
    return "\n".join(lines)


def warp_panel(configured: bool) -> InlineKeyboardMarkup:
    rows = []
    if configured:
        rows.append([InlineKeyboardButton("🔄 Change address", callback_data="adm|warp_ip")])
    rows.append([InlineKeyboardButton("↻ Refresh", callback_data="adm|warp"),
                 InlineKeyboardButton("← Back", callback_data="adm|main")])
    return InlineKeyboardMarkup(rows)


def warp_confirm_text(running_jobs: int) -> str:
    return (f"{panel_title()}\n\n⚠ <b>{running_jobs}</b> download(s) are running or queued. Changing the WARP "
            f"address cuts any of them that are using WARP right now; they can be retried.\n\nChange it anyway?")


def warp_confirm_panel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("Change anyway", callback_data="adm|warp_go"),
                                  InlineKeyboardButton("Wait", callback_data="adm|warp")]])
