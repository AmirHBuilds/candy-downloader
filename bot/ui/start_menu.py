from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def start_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚙ Settings", callback_data="nav|main"),
         InlineKeyboardButton("▤ Queue", callback_data="misc|queue")],
        [InlineKeyboardButton("📜 History", callback_data="misc|history_home"),
         InlineKeyboardButton("🧰 Tools", callback_data="misc|tools")],
    ])
