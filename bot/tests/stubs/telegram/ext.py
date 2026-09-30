from telegram import make_dummy


def __getattr__(name):
    if name.startswith("__"):
        raise AttributeError(name)
    return make_dummy(name)
