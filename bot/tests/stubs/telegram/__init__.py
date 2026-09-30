"""
Test double for python-telegram-bot: just enough for main.py and
jobqueue/job_manager.py to IMPORT and for tests to drive their handlers with
fake objects. Types the tests inspect (buttons/markup, errors, ParseMode) are
real; every other name resolves to an inert dummy class.
"""


class _Meta(type):
    def __getattr__(cls, name):          # e.g. ContextTypes.DEFAULT_TYPE, filters.TEXT
        if name.startswith("__"):
            raise AttributeError(name)
        return make_dummy(name)


def make_dummy(name):
    return _Meta(name, (), {"__init__": lambda self, *a, **k: None,
                            "__call__": lambda self, *a, **k: make_dummy("call")(),
                            "__or__": lambda self, other: self, "__and__": lambda self, other: self,
                            "__invert__": lambda self: self})


def __getattr__(name):                    # module level: `from telegram import Anything`
    if name.startswith("__"):
        raise AttributeError(name)
    return make_dummy(name)


class InlineKeyboardButton:
    def __init__(self, text, callback_data=None, url=None, copy_text=None, **kw):
        self.text, self.callback_data, self.url, self.copy_text = text, callback_data, url, copy_text


class InlineKeyboardMarkup:
    def __init__(self, inline_keyboard):
        self.inline_keyboard = [list(row) for row in inline_keyboard]

    def buttons(self):
        return [b for row in self.inline_keyboard for b in row]


class CopyTextButton:
    def __init__(self, text):
        self.text = text


class InputFile:
    def __init__(self, obj, filename=None, **kw):
        self.filename = filename


class LinkPreviewOptions:
    def __init__(self, **kw):
        self.kw = kw
