"""The bot-side half of web accounts: what /weblogin and the admin's /webaccount say. Pure text in, text out."""
from html import escape

from web import accounts

HELP = (
    "<b>Web accounts</b>\n"
    "<code>/webaccount add &lt;username&gt; [telegram_id] [admin]</code> — new account (a password is made for it)\n"
    "<code>/webaccount reset &lt;username&gt;</code> — new one-time password\n"
    "<code>/webaccount off|on &lt;username&gt;</code> — block / allow\n"
    "<code>/webaccount del &lt;username&gt;</code> — delete\n"
    "<code>/webaccount list</code>"
)


def credentials_text(account, password: str, created: bool, url: str) -> str:
    where = f"\nAddress: {escape(url)}" if url else ""
    return (f"🔑 <b>{'Your web account is ready' if created else 'New web password'}</b>{where}\n"
            f"Username: <code>{escape(account.username)}</code>\n"
            f"Password: <code>{escape(password)}</code>\n\n"
            "<i>It works once for signing in; you'll choose your own password right after. "
            "This message deletes itself in 2 minutes.</i>")


def _find(username: str):
    for account in accounts.list_accounts():
        if account.username.lower() == username.lower():
            return account
    return None


def admin_command(args: list[str], url: str = "") -> str:
    """Runs one /webaccount subcommand; returns the HTML reply."""
    if not args:
        return HELP
    action, rest = args[0].lower(), args[1:]
    try:
        if action == "list":
            lines = [f"• <code>{escape(a.username)}</code> · {a.role}"
                     + (f" · tg {a.telegram_id}" if a.telegram_id else "") + (" · ⛔ off" if a.disabled else "")
                     for a in accounts.list_accounts()]
            return "<b>Web accounts</b>\n" + ("\n".join(lines) if lines else "None yet.")
        if action not in ("add", "reset", "off", "on", "del") or not rest:
            return HELP
        if action == "add":
            telegram_id = next((int(x) for x in rest[1:] if x.isdigit()), None)
            role = "admin" if any(x.lower() == "admin" for x in rest[1:]) else "user"
            from web import security
            password = security.generate_password()
            account = accounts.create_account(rest[0], password, role=role, telegram_id=telegram_id, must_change=True)
            return credentials_text(account, password, True, url)
        target = _find(rest[0])
        if target is None:
            return "No account with that username."
        if action == "reset":
            from web import security
            password = security.generate_password()
            accounts.set_password(target.id, password, must_change=True)
            return credentials_text(target, password, False, url)
        if action in ("off", "on"):
            accounts.set_disabled(target.id, action == "off")
            return f"✓ <code>{escape(target.username)}</code> is now {'blocked' if action == 'off' else 'allowed'}."
        if action == "del":
            accounts.delete_account(target.id)
            return f"✓ Deleted <code>{escape(target.username)}</code>."
    except accounts.AccountError as exc:
        return f"✕ {escape(str(exc))}"
    return HELP
