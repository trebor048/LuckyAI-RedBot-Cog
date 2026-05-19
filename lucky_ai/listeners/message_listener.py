"""
Message listener module for Lucky AI cog.
Provides a background loop for hot take tracking and coordination.
Note: Message events are handled directly by LuckyAICog.on_message.
"""

import time
import logging

from discord.ext import tasks

log = logging.getLogger("red.LuckyAICog")


class MessageListener:
    """
    Handles hot take background loop coordination.
    Note: Message syncing and hot-take-on-message logic is in LuckyAICog.on_message.
    """

    def __init__(self, bot, db, config):
        self.bot = bot
        self.db = db
        self.config = config

    def cleanup(self):
        """Clean up resources when cog unloads."""
        pass

    def configure_hot_take(self, enabled, window_minutes=5, min_messages=10,
                           cooldown_minutes=120, probability=0.05, context_messages=100):
        """Configure hot take behavior from environment or settings."""
        # Config values are stored in LuckyAICog; we just expose this for API compatibility
        pass

    @tasks.loop(minutes=2)
    async def hot_take_loop(self, ai_service):
        """
        Periodic cleanup task. Actual hot-take-on-message logic lives in
        LuckyAICog._maybe_fire_hot_take() which is called from LuckyAICog.on_message.
        This loop can be extended in future for periodic cleanup or metrics.
        """
        # Hot-take-on-message is handled by LuckyAICog._maybe_fire_hot_take
        # This task exists to allow easy start/stop of the hot-take subsystem
        pass

    @hot_take_loop.before_loop
    async def before_hot_take_loop(self):
        """Wait for bot to be ready before starting the loop."""
        await self.bot.wait_until_ready()
