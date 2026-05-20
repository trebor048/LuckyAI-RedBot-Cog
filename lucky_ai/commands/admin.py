class AdminCommands:
    """Helper class for admin-related logic (not a Cog - commands live in LuckyAICog)."""

    def __init__(self, bot, config, db, ai_service, cog_instance):
        self.bot = bot
        self.config = config
        self.db = db
        self.ai_service = ai_service
        self.cog = cog_instance

    # --- Stats ---

    async def build_stats(self, ctx):
        try:
            db_stats = await self.db.get_database_stats()
        except Exception as e:
            log = __import__("logging").getLogger("red.LuckyAICog")
            log.error("Failed to get database stats: %s", e)
            db_stats = {"total_messages": 0, "total_guilds": 0, "total_users": 0}

        try:
            cmd_stats = await self.db.get_command_stats(str(ctx.guild.id), 7)
        except Exception as e:
            log = __import__("logging").getLogger("red.LuckyAICog")
            log.error("Failed to get command stats: %s", e)
            cmd_stats = []

        uptime = 0
        if hasattr(self.bot, "uptime") and self.bot.uptime:
            try:
                uptime = __import__("time").time() - self.bot.uptime.timestamp()
            except Exception:
                uptime = 0
        hours = int(uptime // 3600)
        minutes = int((uptime % 3600) // 60)
        seconds = int(uptime % 60)
        uptime_str = f"{hours}h {minutes}m {seconds}s"

        total_cmds = sum(r["cnt"] for r in cmd_stats)
        cmd_lines = []
        for r in cmd_stats:
            pct = round((r["cnt"] / total_cmds * 100)) if total_cmds else 0
            bars = "🔷" * (pct // 10) if pct else ""
            cmd_lines.append(f"`;{r['command']}:` {r['cnt']} `{bars}` {pct}%")
        cmd_usage = "\n".join(cmd_lines) if cmd_lines else "No commands logged yet."
        if len(cmd_usage) > 1000:
            cmd_usage = cmd_usage[:997] + "..."

        async with self.config.guild(ctx.guild).all() as cfg:
            typing_enabled = cfg.get("typing_enabled", True)

        return {
            "title": "📊 Bot Statistics",
            "color": 0x4DABF7,
            "fields": [
                {"name": "🗄️ Database", "value": f"Messages: **{db_stats.get('total_messages', 0):,}**\nGuilds: **{db_stats.get('total_guilds', 0)}**\nUsers: **{db_stats.get('total_users', 0)}**", "inline": True},
                {"name": "⏱️ Uptime", "value": uptime_str, "inline": True},
                {"name": "💓 Health", "value": f"Status: **healthy**\nGuilds: **{len(self.bot.guilds)}**\nLatency: **{round(self.bot.latency * 1000)}ms**", "inline": True},
                {"name": "📈 Command Usage (7 days)", "value": cmd_usage + f"\n**Total:** {total_cmds:,}", "inline": False},
                {"name": "⚙️ Features", "value": f"Typing Indicator: **{'ON 🟢' if typing_enabled else 'OFF 🔴'}**\nSync: **{'ON' if self.cog.message_sync_enabled else 'OFF'}**\nHot Takes: **{'ON' if self.cog.hot_take_enabled else 'OFF'}**", "inline": False},
            ],
        }
