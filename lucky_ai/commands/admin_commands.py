import time
import discord

from ..settings_ui import SettingsView


class AdminCommands:
    """Helper class for admin-related logic (not a Cog - commands live in LuckyAICog)."""

    def __init__(self, bot, config, db, ai_service, cog_instance):
        self.bot = bot
        self.config = config
        self.db = db
        self.ai_service = ai_service
        self.cog = cog_instance

    # --- Passthrough to cog (delegates) ---

    async def _log_command(self, guild_id, user_id, command, success=True):
        return await self.cog._log_command(guild_id, user_id, command, success)

    async def _do_backfill(self, guild, channel, days, author):
        return await self.cog._do_backfill(guild, channel, days, author)

    # --- Settings UI ---

    async def open_settings(self, ctx):
        """Open the interactive settings UI for an admin."""
        session_id = self.cog._create_session(ctx.author.id, str(ctx.guild.id))
        view = SettingsView(self.cog, session_id, ctx.author.id, str(ctx.guild.id))
        embed = await view.build_embed()
        await ctx.send(embed=embed, view=view, ephemeral=True)

    # --- Stats ---

    async def build_stats(self, ctx):
        """Build the stats embed data. Returns embed dict or raises on error."""
        db_stats = await self.db.get_database_stats()
        uptime = time.time() - self.bot.uptime.timestamp() if self.bot.uptime else 0
        hours = int(uptime // 3600)
        minutes = int((uptime % 3600) // 60)
        seconds = int(uptime % 60)
        uptime_str = f"{hours}h {minutes}m {seconds}s"
        cmd_stats = await self.db.get_command_stats(str(ctx.guild.id), 7)
        total_cmds = sum(r["cnt"] for r in cmd_stats)
        cmd_lines = []
        for r in cmd_stats:
            pct = round((r["cnt"] / total_cmds * 100)) if total_cmds else 0
            bars = ":large_blue_diamond:" * (pct // 10) if pct else ""
            cmd_lines.append(f"`;{r['command']}:` {r['cnt']} `{bars}` {pct}%")
        cmd_usage = "\n".join(cmd_lines) if cmd_lines else "No commands logged yet."
        async with self.config.guild(ctx.guild).all() as cfg:
            typing_enabled = cfg.get("typing_enabled", True)
        return {
            "title": "📊 Bot Statistics",
            "color": 0x4DABF7,
            "fields": [
                {"name": "🗄️ Database", "value": f"Messages: **{db_stats['total_messages']:,}**\nGuilds: **{db_stats['total_guilds']}**\nUsers: **{db_stats['total_users']}**", "inline": True},
                {"name": "⏱️ Uptime", "value": uptime_str, "inline": True},
                {"name": "💓 Health", "value": f"Status: **healthy**\nGuilds: **{len(self.bot.guilds)}**\nLatency: **{round(self.bot.latency * 1000)}ms**", "inline": True},
                {"name": "📈 Command Usage (7 days)", "value": cmd_usage + f"\n**Total:** {total_cmds:,}", "inline": False},
                {"name": "⚙️ Features", "value": f"Typing Indicator: **{'ON :green_circle:' if typing_enabled else 'OFF :red_circle:'}**\nSync: **{'ON' if self.cog.message_sync_enabled else 'OFF'}**\nHot Takes: **{'ON' if self.cog.hot_take_enabled else 'OFF'}**", "inline": False},
            ],
        }

    # --- Config helpers ---

    async def add_sync_channel(self, guild_id, channel_id):
        """Add a channel to the sync list."""
        import asyncio
        # Read current channels
        guild = self.bot.get_guild(int(guild_id))
        if not guild:
            return False, "Guild not found."
        async with self.config.guild(guild).all() as cfg:
            sync = cfg.get("sync_channels", [])
            if channel_id in sync:
                return False, "Channel already in sync list."
            if len(sync) >= 45:
                return False, f"Channel limit reached ({len(sync)}/45)."
            sync.append(channel_id)
            cfg["sync_channels"] = sync
        await self.db.update_sync_status(guild_id, channel_id)
        await self.db.log_sync_operation(guild_id, channel_id, "channel_add", triggered_by="admin")
        return True, "Channel added. Starting backfill..."

    async def remove_sync_channel(self, guild_id, channel_id):
        """Remove a channel from the sync list."""
        guild = self.bot.get_guild(int(guild_id))
        if not guild:
            return False, "Guild not found."
        async with self.config.guild(guild).all() as cfg:
            sync = cfg.get("sync_channels", [])
            if channel_id not in sync:
                return False, "Channel not in sync list."
            sync.remove(channel_id)
            cfg["sync_channels"] = sync
        await self.db.delete_sync_status(guild_id, channel_id)
        deleted = await self.db.delete_channel_messages(guild_id, channel_id)
        return True, f"Channel removed. {deleted} messages deleted."

    async def get_sync_channels(self, guild_id):
        """Get list of sync channel IDs."""
        guild = self.bot.get_guild(int(guild_id))
        if not guild:
            return []
        async with self.config.guild(guild).all() as cfg:
            return cfg.get("sync_channels", [])

    async def add_blacklist(self, guild_id, user_id, triggered_by):
        """Blacklist a user."""
        await self.db.add_to_blacklist(guild_id, user_id, triggered_by)

    async def remove_blacklist(self, guild_id, user_id):
        """Remove user from blacklist."""
        await self.db.remove_from_blacklist(guild_id, user_id)

    async def get_blacklist(self, guild_id):
        """Get blacklist entries."""
        return await self.db.get_blacklist(guild_id)

    async def set_admin_role(self, guild_id, role_id):
        """Set the admin role ID."""
        guild = self.bot.get_guild(int(guild_id))
        if not guild:
            return False
        async with self.config.guild(guild).all() as cfg:
            cfg["admin_role"] = role_id
        return True

    async def toggle_enabled(self, guild_id, enabled):
        """Enable or disable the bot for a guild."""
        guild = self.bot.get_guild(int(guild_id))
        if not guild:
            return False
        async with self.config.guild(guild).all() as cfg:
            cfg["enabled"] = enabled
        return True
