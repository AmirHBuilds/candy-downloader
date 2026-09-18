from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def start_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚙ Settings", callback_data="nav|main"),
         InlineKeyboardButton("▤ Queue", callback_data="misc|queue")],
    ])
