import asyncio
import logging

import telegram

from core.filemanager import FileManager
from core.exceptions import InvalidJSONException


class _Notification:
    bot = None
    enabled = False
    channel_id = None
    token = None
    loop = None
    events = {}

    # Message categories that can be toggled individually via
    # notifications.notify_<category> in config.json. A missing key defaults to
    # True, so existing configs (and any future/uncategorised message) keep
    # sending everything unless the user explicitly opts out.
    CATEGORIES = ("startup", "crash", "session", "captcha", "village", "farm", "attack")

    def __init__(self):
        # Deliberately do NOT read config or build the bot here. In --world
        # workers this module is imported (twb.py) before FileManager's data dir
        # is pointed at the world, so an eager read would load the wrong (or a
        # missing) root config.json and permanently disable notifications. The
        # config is loaded lazily on the first send(), by which point the data
        # dir has been set by resolve_world_dir().
        pass

    def get_config(self, config=None):
        if config is None:
            try:
                config = FileManager.load_json_file("config.json")
            except InvalidJSONException:
                config = None
        if config:
            notification_config = config.get("notifications", {})
            self.enabled = notification_config.get("enabled", False)
            self.channel_id = notification_config.get("channel_id")
            self.token = notification_config.get("token")
            self.events = {
                cat: notification_config.get("notify_" + cat, True)
                for cat in self.CATEGORIES
            }
        else:
            self.enabled = False
            self.channel_id = None
            self.token = None
            self.events = {}

    def _ensure_bot(self):
        """(Re)load config and build the bot/loop on demand.

        Returns True when a bot is ready to send. Reading config on each call
        (rather than once at import) is what makes --world workers work: by the
        time the bot loop calls send(), FileManager points at worlds/<name>/, so
        we read that world's config.json rather than the missing root one. It
        also means the enabled flag and per-category toggles take effect live,
        without restarting the bot. The telegram bot object itself is built once
        and cached.
        """
        self.get_config()
        if not self.enabled or not self.token or not self.channel_id:
            return False
        if self.loop is None:
            self.loop = asyncio.new_event_loop()
        if self.bot is None:
            self.bot = telegram.Bot(token=self.token)
        return True

    def send(self, message, category=None):
        """Send a Telegram message. `category` (see CATEGORIES) lets the user
        mute this kind of message via notifications.notify_<category>; an
        unknown/None category always sends."""
        if not self._ensure_bot():
            return
        if category is not None and not self.events.get(category, True):
            return

        # Best-effort, like test() below: a notification must never be the thing
        # that takes the bot down. main()'s crash handler calls this while
        # already handling an exception, so a Telegram timeout raising here
        # escapes the retry loop entirely and the bot exits instead of
        # restarting - which is exactly what happened on 2026-08-03, when a
        # dropped game request crashed a cycle and telegram.error.TimedOut
        # turned that into a full stop.
        try:
            task = self.loop.create_task(self.send_async(message))
            self.loop.run_until_complete(task)
        except Exception as exc:
            logging.getLogger("Notification").warning(
                "Could not send notification (%s): %s", category, exc)

    async def send_async(self, message):
        await self.bot.send_message(chat_id=self.channel_id, text=message)

    def test(self, message="TWB test notification - your Telegram setup works!", config=None):
        """Send a one-off message using the *current* config, ignoring 'enabled'.

        Reads config fresh (or uses the passed-in `config` dict) and builds its
        own bot/loop, independent of the send() singleton state. The `config`
        override lets the world-aware web dashboard hand us the selected world's
        config, since the web process's FileManager is not world-aware. Returns
        (ok, error) so the dashboard's "Send test message" button can verify a
        Telegram setup without restarting the bot. Never raises.
        """
        self.get_config(config=config)
        if not self.token or not self.channel_id:
            return False, "Missing bot token or channel id"
        try:
            bot = telegram.Bot(token=self.token)
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(bot.send_message(chat_id=self.channel_id, text=message))
            finally:
                loop.close()
            return True, None
        except Exception as exc:
            return False, str(exc)


Notification = _Notification()
