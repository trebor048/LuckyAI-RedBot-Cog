class RoastCommands:
    """Helper class for roast-related logic (not a Cog - commands live in RoasterCog)."""

    def __init__(self, bot, config, db, ai_service, cog_instance):
        self.bot = bot
        self.config = config
        self.db = db
        self.ai_service = ai_service
        self.cog = cog_instance

    # --- Passthrough to cog (delegates) ---

    async def _select_roast_style(self, prompt_key=None, random_mode=False, style_override=None):
        return await self.cog._select_roast_style(prompt_key, random_mode, style_override)

    async def _check_blacklist(self, guild_id, user_id):
        return await self.cog._check_blacklist(guild_id, user_id)

    async def _handle_typing(self, channel):
        return await self.cog._handle_typing(channel)

    async def _log_command(self, guild_id, user_id, command, success=True):
        return await self.cog._log_command(guild_id, user_id, command, success)

    @property
    def cooldowns(self):
        return self.cog.cooldowns

    # --- Helper logic ---

    async def get_roast_payload(self, ctx, user_id, target, style, model, message_fetch_mode, rand_mode, prompt_key):
        """Build the AI payload for roasting. Called from RoasterCog.roast command."""
        from ..utils import format_messages_for_roast
        messages = await self.db.get_messages(user_id, 200, message_fetch_mode, str(ctx.guild.id))
        if not messages:
            return None

        roast_style = await self._select_roast_style(prompt_key, rand_mode, style)
        formatted = format_messages_for_roast(messages)
        user_prompt = f'Roast "{target.name}" based on their messages. Be SHORT, MEAN, and SPECIFIC. No flowery language.\n\n{formatted}'

        async with self.config.guild(ctx.guild).all() as s:
            payload = {
                "messages": [
                    {"role": "system", "content": roast_style["systemPrompt"]},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": s.get("temperature", 1.0),
                "max_tokens": s.get("max_tokens", 4096),
                "top_p": s.get("top_p", 0.9),
                "frequency_penalty": s.get("frequency_penalty", 0.4),
                "presence_penalty": s.get("presence_penalty", 0.2),
            }
        return payload, model

    async def get_tldr_payload(self, ctx, count, style, model):
        """Build the AI payload for TLDR. Called from RoasterCog.tldr command."""
        from ..utils import TLDR_SYSTEM_PROMPT, GREENTEXT_SYSTEM_PROMPT, format_messages_for_tldr
        raw_messages = await self.db.get_channel_messages(str(ctx.channel.id), count)
        if not raw_messages:
            return None

        conv = format_messages_for_tldr(raw_messages, style)
        system = GREENTEXT_SYSTEM_PROMPT if style == "greentext" else TLDR_SYSTEM_PROMPT
        header = f"The following are the last {len(raw_messages)} messages from a Discord channel, ordered oldest to newest:\n\n"
        suffix = "\n\nTurn this conversation into a 4chan greentext story." if style == "greentext" else "\n\nSummarize this conversation as a TL;DR."

        payload = {
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": header + conv + suffix}],
            "temperature": 1.0 if style == "greentext" else 0.7,
            "max_tokens": 2048 if style == "greentext" else 1024,
        }
        return payload, model, style, len(raw_messages)