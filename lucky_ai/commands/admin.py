class AdminCommands:
    """Helper class for admin-related logic (not a Cog - commands live in LuckyAICog)."""

    def __init__(self, bot, config, db, ai_service, cog_instance):
        self.bot = bot
        self.config = config
        self.db = db
        self.ai_service = ai_service
        self.cog = cog_instance

    # --- Stats ---

    async def build_stats(self, ctx, verbose: bool = False):
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
            cmd_lines.append(f"`{ctx.clean_prefix}{r['command']}`: {r['cnt']} `{bars}` {pct}%")
        cmd_usage = "\n".join(cmd_lines) if cmd_lines else "No commands logged yet."
        total_line = f"\n**Total:** {total_cmds:,}"
        max_value_len = 1024
        if len(cmd_usage) + len(total_line) > max_value_len:
            trim_to = max_value_len - len(total_line) - 3
            cmd_usage = cmd_usage[:max(0, trim_to)] + "..."

        async with self.config.guild(ctx.guild).all() as cfg:
            hot_take_enabled = cfg.get("hot_take_enabled", False)
            provider_order = cfg.get("provider_order")

        payload = {
            "title": "📊 Bot Statistics",
            "color": 0x4DABF7,
            "fields": [
                {"name": "🗄️ Database", "value": f"Messages: **{db_stats.get('total_messages', 0):,}**\nGuilds: **{db_stats.get('total_guilds', 0)}**\nUsers: **{db_stats.get('total_users', 0)}**", "inline": True},
                {"name": "⏱️ Uptime", "value": uptime_str, "inline": True},
                {"name": "💓 Health", "value": f"Status: **healthy**\nGuilds: **{len(self.bot.guilds)}**\nLatency: **{round(self.bot.latency * 1000)}ms**", "inline": True},
                {"name": "📈 Command Usage (7 days)", "value": cmd_usage + total_line, "inline": False},
                {"name": "⚙️ Features", "value": f"Sync: **{'ON' if self.cog.message_sync_enabled else 'OFF'}**\nHot Takes: **{'ON' if hot_take_enabled else 'OFF'}**", "inline": False},
            ],
        }
        if verbose:
            metrics = self.ai_service.get_metrics() if hasattr(self.ai_service, "get_metrics") else {}
            effective_order = []
            if hasattr(self.ai_service, "get_effective_provider_order"):
                effective_order = await self.ai_service.get_effective_provider_order(
                    guild_id=ctx.guild.id,
                    configured_order=provider_order,
                )
            provider_lines = []
            for provider, vals in (metrics.get("providers", {}) or {}).items():
                probe = vals.get("probe_status", "unknown")
                circuit = " open" if vals.get("circuit_open") else ""
                provider_lines.append(
                    f"`{provider}` ok:{vals.get('ok',0)} fail:{vals.get('fail',0)} "
                    f"avg:{vals.get('avg_latency_ms',0)}ms probe:{probe}{circuit}"
                )
            if not provider_lines:
                provider_lines = ["No provider metrics yet."]
            payload["fields"].append(
                {
                    "name": "🧠 AI Runtime",
                    "value": (
                        f"Requests: **{metrics.get('requests', 0)}**\n"
                        f"Success: **{metrics.get('success', 0)}**\n"
                        f"Errors: **{metrics.get('errors', 0)}**\n"
                        f"Fallback Success: **{metrics.get('fallback_success', 0)}**"
                    ),
                    "inline": False,
                }
            )
            payload["fields"].append(
                {"name": "📡 Provider Health", "value": "\n".join(provider_lines)[:1000], "inline": False}
            )
            if effective_order:
                payload["fields"].append(
                    {
                        "name": "🔀 Learned Fallback Order",
                        "value": " → ".join(effective_order)[:1000],
                        "inline": False,
                    }
                )
        return payload
