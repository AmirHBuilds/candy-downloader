"""The bot token must never reach the logs - not in messages, not in tracebacks."""
import io
import logging
import unittest

from tests import _env  # noqa: F401

from utils.safe_logging import RedactingFormatter, install_safe_logging, redact  # noqa: E402

TOKEN = "8659318097:AAH-ExampleExampleExampleExample_12345"


class Redaction(unittest.TestCase):
    def test_token_in_a_bot_api_url_is_hidden_but_the_endpoint_stays_visible(self):
        text = f"POST http://telegram-bot-api:8081/bot{TOKEN}/getUpdates"
        out = redact(text)
        self.assertNotIn("AAH-Example", out)
        self.assertNotIn("8659318097", out)
        self.assertIn("/bot<redacted>/getUpdates", out)

    def test_a_bare_token_and_the_configured_secret_are_hidden(self):
        self.assertNotIn("AAH-Example", redact(f"token is {TOKEN}!"))
        self.assertEqual(redact("custom-secret-value here", secret="custom-secret-value"), "<redacted> here")

    def test_ordinary_text_is_untouched(self):
        for text in ("Job rid0000001 failed", "https://www.youtube.com/watch?v=QOZDKlpybZE",
                     "user 8659318097 sent a link", "ratio 12:30 at 10:45:00", ""):
            self.assertEqual(redact(text), text)


class Formatter(unittest.TestCase):
    def logger(self, level=logging.INFO):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(RedactingFormatter("%(levelname)s %(name)s: %(message)s", secret=TOKEN))
        log = logging.getLogger(f"test.safe.{id(stream)}")
        log.propagate = False
        log.setLevel(level)
        log.addHandler(handler)
        return log, stream

    def test_messages_and_arguments_are_scrubbed(self):
        log, stream = self.logger()
        log.info("calling %s", f"http://x/bot{TOKEN}/getMe")
        self.assertNotIn("AAH-Example", stream.getvalue())
        self.assertIn("INFO", stream.getvalue())

    def test_tracebacks_are_scrubbed_too(self):
        """A failing request echoes its URL in the exception text - invisible to a record filter."""
        log, stream = self.logger()
        try:
            raise RuntimeError(f"404 for url http://telegram-bot-api:8081/bot{TOKEN}/getFile")
        except RuntimeError:
            log.exception("download of a file failed")
        out = stream.getvalue()
        self.assertIn("Traceback", out)
        self.assertIn("RuntimeError", out)
        self.assertNotIn("AAH-Example", out)
        self.assertNotIn("8659318097", out)


class Install(unittest.TestCase):
    def test_install_quiets_httpx_and_redacts_through_the_root_handler(self):
        root = logging.getLogger()
        saved = (root.handlers[:], root.level, logging.getLogger("httpx").level)
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.handlers, root.level = [handler], logging.INFO
        self.addCleanup(lambda: (setattr(root, "handlers", saved[0]), setattr(root, "level", saved[1]),
                                 logging.getLogger("httpx").setLevel(saved[2])))
        install_safe_logging(TOKEN)

        logging.getLogger("httpx").info("HTTP Request: POST http://x/bot%s/getUpdates", TOKEN)   # the 10-second spam
        self.assertEqual(stream.getvalue(), "")
        logging.getLogger("candy.main").warning("oops %s", f"/bot{TOKEN}/x")
        self.assertIn("WARNING candy.main: oops /bot<redacted>/x", stream.getvalue())
        self.assertNotIn("AAH-Example", stream.getvalue())
        self.assertRegex(stream.getvalue(), r"^\d{4}-\d\d-\d\d")                                  # the date format survived


if __name__ == "__main__":
    unittest.main()
