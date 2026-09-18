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
        [InlineKeyboardButton("All users", callback_data="adm|all_users")],
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
