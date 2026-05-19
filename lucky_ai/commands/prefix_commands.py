"""
Prefix commands module for LuckyAICog.
All commands use the ;l prefix to avoid conflicts with other bots.
Handles: ;lhelp, ;ltldr, ;lgreentext, ;lask, ;ldebate, ;lhtt, ;ltypeon, ;ltypeoff
"""

import time
import re
import math
import logging
import base64
import aiohttp

import discord

from ..utils import (
    ASK_SYSTEM_PROMPT, DEBATE_SYSTEM_PROMPT, HOT_TAKE_PROMPT,
    TLDR_SYSTEM_PROMPT, GREENTEXT_SYSTEM_PROMPT,
    parse_debate_response, sanitize_output, format_messages_for_tldr,
    AskError,
)

log = logging.getLogger("red.LuckyAICog.prefix")

TLDR_MIN_MESSAGES = 10
MAX_MESSAGE_COUNT = 500


class PrefixCommands:
    """
    Handles all prefix commands for the LuckyAICog.
    Accepts cog instance to access helper methods and state.
    """

    def __init__(self, bot, config, db, ai_service, cog_instance):
        self.bot = bot
        self.config = config
        self.db = db
        self.ai_service = ai_service
        self.cog = cog_instance

    # =========================================================================
    # Public entry point - called from cog's on_message
    # =========================================================================

    async def handle_message(self, message):
        """
        Main entry point. Check if message is a prefix command and handle it.
        Returns True if handled (stop further processing), False otherwise.
        """
        if message.author.bot:
            return False
        if not message.guild:
            return False

        content = message.content.strip()

        # ;lhelp
        if content == ";lhelp":
            ctx = await self.bot.get_context(message)
            await self.cog.roasthelp(ctx)
            return True

        # ;ltldr and ;lgreentext
        tldr_match = re.match(r"^;l(tldr|greentext)\s+(\d+)$", content, re.IGNORECASE)
        if tldr_match:
            async with self.config.guild(message.guild).all() as cfg:
                if not cfg.get("enabled", True):
                    return True
            count = int(tldr_match.group(2))
            style = "greentext" if tldr_match.group(1).lower() == "greentext" else "normal"
            if count < TLDR_MIN_MESSAGES or count > MAX_MESSAGE_COUNT:
                await self._send_and_delete(
                    message.channel,
                    f":x: Message count must be between {TLDR_MIN_MESSAGES} and {MAX_MESSAGE_COUNT}.",
                    delete_after=6,
                    delete_original=message,
                )
                return True
            await self._do_tldr(message, count, style)
            return True

        # ;lhtt on/off/fire
        htt_match = re.match(r"^;lhtt\s+(on|off|fire)$", content, re.IGNORECASE)
        if htt_match:
            await self._do_htt(message)
            return True

        # ;lask
        if content.startswith(";lask"):
            async with self.config.guild(message.guild).all() as cfg:
                if not cfg.get("enabled", True):
                    return True
            await self._do_ask(message)
            return True

        # ;ldebate
        if content.startswith(";ldebate"):
            async with self.config.guild(message.guild).all() as cfg:
                if not cfg.get("enabled", True):
                    return True
            await self._do_debate(message)
            return True

        # ;ltypeon / ;ltypeoff
        if content in (";ltypeon", ";ltypeoff"):
            await self._do_typeonoff(message)
            return True

        return False

    # =========================================================================
    # TLDR / Greentext
    # =========================================================================

    async def _do_tldr(self, message, count, style):
        """Handle ;ltldr N and ;lgreentext N commands with cooldown."""
        author_id = str(message.author.id)
        cooldown = self.cooldowns.check(author_id, 300000, "tldr")
        if cooldown["active"]:
            await self._send_and_delete(
                message.channel,
                f":hourglass_flowing_sand: Wait {cooldown['remaining_sec']}s",
                delete_after=5,
                delete_original=message,
            )
            return
        try:
            async with self.config.guild(message.guild).all() as cfg:
                model = cfg.get("model", "nvidia/qwen/qwen3.5-122b-a10b")

            messages = await self.db.get_channel_messages(str(message.channel.id), count)
            if not messages:
                await self._send_and_delete(
                    message.channel,
                    ":x: No messages found. The channel may not be synced yet.",
                    delete_after=6,
                    delete_original=message,
                )
                return

            conversation = format_messages_for_tldr(messages, style)
            system_prompt = GREENTEXT_SYSTEM_PROMPT if style == "greentext" else TLDR_SYSTEM_PROMPT
            header = f"The following are the last {len(messages)} messages from a Discord channel, ordered oldest to newest:\n\n"
            suffix = "\n\nTurn this conversation into a 4chan greentext story." if style == "greentext" else "\n\nSummarize this conversation as a TL;DR."
            user_prompt = header + conversation + suffix

            payload = {
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 1.0 if style == "greentext" else 0.7,
                "max_tokens": 2048 if style == "greentext" else 1024,
            }

            await self._handle_typing(message.channel)
            resp = await self.ai_service.execute_request(payload, model, context="TLDR", timeout=90)
            text = sanitize_output(resp["choices"][0]["message"]["content"])

            embed = discord.Embed(
                color=0x51CF66 if style == "greentext" else 0x4DABF7,
                title="> Greentext" if style == "greentext" else "\U0001f9e0 TL;DR Summary",
                description=text[:4090] + "..." if len(text) > 4090 else text,
            )
            embed.set_footer(text=f"Requested by {message.author.name}")
            embed.timestamp = discord.utils.utcnow()

            await message.channel.send(embed=embed)

            try:
                await message.delete()
            except (discord.Forbidden, discord.HTTPException):
                pass

            await self._log_command(message.guild.id, message.author.id, style if style == "greentext" else "tldr")

        except Exception as e:
            log.error("TLDR_PREFIX Error: %s", e)
            await self._send_and_delete(
                message.channel,
                f":x: Failed to generate TL;DR: {e}",
                delete_after=6,
                delete_original=message,
            )

    # =========================================================================
    # ;lask command

    async def _do_ask(self, message):
        """Handle ;lask command with cooldown, context, and image support."""
        ctx = await self.bot.get_context(message)
        author_id = str(message.author.id)
        guild_id = str(message.guild.id)

        # Check cooldown (10s admin, 60s normal)
        is_admin = await self._require_admin(ctx)
        cd_ms = 10000 if is_admin else 60000
        cooldown = self.cooldowns.check(author_id, cd_ms, "ask")
        if cooldown["active"]:
            await self._send_and_delete(
                message.channel,
                f":hourglass_flowing_sand: Wait {cooldown['remaining_sec']}s",
                delete_after=5,
            )
            return

        raw = message.content.strip()
        args = raw[5:].strip()
        attach = message.attachments[0] if message.attachments else None
        has_image = attach and attach.content_type and attach.content_type.startswith("image/")

        if not args and not has_image:
            await self._send_and_delete(
                message.channel,
                "Usage: `;lask <question>` or attach an image",
                delete_after=6,
                delete_original=message,
            )
            return

        try:
            await self._handle_typing(message.channel)

            async with self.config.guild(message.guild).all() as cfg:
                if not cfg.get("enabled", True):
                    await self._send_and_delete(
                        message.channel,
                        ":x: Bot is disabled.",
                        delete_after=5,
                        delete_original=message,
                    )
                    return

            # Build context from last 25 channel messages
            bot_user_id = str(self.bot.user.id)
            ctx_msgs = await self.db.get_channel_messages(str(message.channel.id), 25)
            context_lines = []

            for m in (ctx_msgs or []):
                if str(m.get("id", "")) == str(message.id):
                    continue
                if m.get("author_id") == bot_user_id:
                    continue
                name = m.get("author_tag") or m.get("author_id", "Someone")
                content = (m.get("content", "") or "")[:250]
                context_lines.append(f"{name}: {content}")

            ctx_text = "\n".join(context_lines[-25:])
            user_name = message.author.display_name or message.author.name

            if ctx_text:
                prompt = f"Recent chat:\n{ctx_text}\n---\n{user_name}: {args or '[image]'}\n\nAnswer:"
            else:
                prompt = f"{user_name}: {args or '[image]'}\n\nAnswer:"

            if has_image:
                async with aiohttp.ClientSession() as session:
                    async with session.get(attach.url) as resp:
                        if resp.status == 200:
                            buf = await resp.read()
                            b64 = base64.b64encode(buf).decode()
                            mime = attach.content_type or "image/jpeg"
                            content_parts = [
                                {"type": "text", "text": prompt},
                                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                            ]
                            msgs = [{"role": "user", "content": content_parts}]
                        else:
                            msgs = [{"role": "user", "content": prompt}]
            else:
                msgs = [{"role": "user", "content": prompt}]

            # Select model based on whether image is present
            async with self.config.all() as gcfg:
                model = gcfg.get("ask_vision_model") if has_image else gcfg.get("ask_model")

            payload = {
                "messages": [{"role": "system", "content": ASK_SYSTEM_PROMPT}, *msgs],
                "temperature": 0.85,
                "max_tokens": 600,
            }

            resp = await self.ai_service.execute_request(payload, model, context="ASK", timeout=45, max_retries=2)
            text = resp["choices"][0]["message"]["content"]

            # Reply with chunks (max 1990 chars each)
            chunks = [text[i:i+1990] for i in range(0, len(text), 1990)] if len(text) > 1990 else [text]
            for chunk in chunks:
                await message.reply(chunk, allowed_mentions=discord.AllowedMentions(replied_user=False))

            try:
                await message.delete()
            except (discord.Forbidden, discord.HTTPException):
                pass

            await self._log_command(guild_id, author_id, "ask")

        except AskError as e:
            log.error("ASK AskError: %s - %s", e.code, e.message)
            await self._send_and_delete(message.channel, f":x: {e.message}", delete_after=8)
            if e.should_log:
                await self._log_command(guild_id, author_id, "ask", False)

        except Exception as e:
            log.error("ASK Error: %s", e)
            await self._send_and_delete(message.channel, f":x: {e}", delete_after=8)
            await self._log_command(guild_id, author_id, "ask", False)

    # =========================================================================
    # ;ldebate command

    async def _do_debate(self, message):
        """Handle ;ldebate command with cooldown and participant validation."""
        ctx = await self.bot.get_context(message)
        author_id = str(message.author.id)
        guild_id = str(message.guild.id)

        # Check cooldown (20s admin, 120s normal)
        is_admin = await self._require_admin(ctx)
        cd_ms = 20000 if is_admin else 120000
        cooldown = self.cooldowns.check(author_id, cd_ms, "debate")
        if cooldown["active"]:
            await self._send_and_delete(
                message.channel,
                f":hourglass_flowing_sand: Wait {cooldown['remaining_sec']}s",
                delete_after=5,
            )
            return

        try:
            msgs = await self.db.get_channel_messages(str(message.channel.id), 50)
            if not msgs or len(msgs) < 2:
                await self._send_and_delete(
                    message.channel,
                    "Thinking Nothing to debate here. Start an argument first.",
                    delete_after=6,
                    delete_original=message,
                )
                return

            # Filter by opt-out users
            opt_out_ids = await self.db.get_opt_out_user_ids(str(message.guild.id))
            opt_set = set(opt_out_ids)
            filtered = [m for m in msgs if m.get("author_id") not in opt_set]

            if len(filtered) < 2:
                await self._send_and_delete(
                    message.channel,
                    ":x: Not enough participants after filtering opt-outs.",
                    delete_after=6,
                    delete_original=message,
                )
                return

            # Build context and count participants
            participants = set()
            ctx_text = ""

            for m in filtered[-50:]:
                name = m.get("author_tag") or m.get("author_id", "Someone")
                content = (m.get("content", "") or "")[:300]
                ctx_text += f"{name}: {content}\n"
                participants.add(name)

            if len(participants) < 2:
                await self._send_and_delete(
                    message.channel,
                    ":rolling_eyes: You can't debate yourself. Grab a friend.",
                    delete_after=6,
                    delete_original=message,
                )
                return

            if len(participants) > 10:
                await self._send_and_delete(
                    message.channel,
                    ":shrug: Too many cooks. Narrow it down to 2 sides.",
                    delete_after=6,
                    delete_original=message,
                )
                return

            await self._handle_typing(message.channel)

            prompt = f"Recent chat:\n{ctx_text}\n\nJudge this debate:"

            async with self.config.all() as gcfg:
                model = gcfg.get("ask_model", "deepseek/deepseek-reasoner")

            payload = {
                "messages": [
                    {"role": "system", "content": DEBATE_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.85,
                "max_tokens": 800,
            }

            resp = await self.ai_service.execute_request(payload, model, context="DEBATE", timeout=45, max_retries=2)
            text = resp["choices"][0]["message"]["content"]
            parsed = parse_debate_response(text)

            if not parsed.get("winner") or (not parsed.get("sideA") and not parsed.get("sideB")):
                await self._send_and_delete(
                    message.channel,
                    ":thought_balloon: This isn't really a debate, just vibes. Pick a side and argue.",
                    delete_after=6,
                    delete_original=message,
                )
                return

            # Build embed with emoji field names matching original requirements
            is_a_win = parsed.get("winner", "").upper().startswith("A")
            embed = discord.Embed(
                title=f"\u2696\uFE0F Debate: {parsed.get('topic', 'Conversation Analysis')}",
                color=0x00ff00 if is_a_win else 0xff0000,
            )

            if parsed.get("sideA"):
                parts = parsed["sideA"].split("\u2014\u2014", 1)
                field_name = f"\uD83C\uDFDB\uFE0F Side A: {parts[0].strip()}"
                field_value = parts[1].strip() if len(parts) > 1 else parsed["sideA"]
                embed.add_field(name=field_name, value=field_value, inline=True)

            if parsed.get("sideB"):
                parts = parsed["sideB"].split("\u2014\u2014", 1)
                field_name = f"\uD83C\uDFDB\uFE0F Side B: {parts[0].strip()}"
                field_value = parts[1].strip() if len(parts) > 1 else parsed["sideB"]
                embed.add_field(name=field_name, value=field_value, inline=True)

            embed.add_field(name="\uD83C\uDFC6 Verdict", value=parsed.get("verdict", "Inconclusive"), inline=False)
            embed.add_field(name="\uD83D\uDC80 Loser Take", value=parsed.get("loserTake", "No arguments found."), inline=False)

            if parsed.get("score"):
                embed.add_field(name="\uD83D\uDCCA Score", value=parsed["score"], inline=False)

            embed.set_footer(text=f"Requested by {message.author.name}")

            await message.reply(embed=embed, allowed_mentions=discord.AllowedMentions(replied_user=False))

            try:
                await message.delete()
            except (discord.Forbidden, discord.HTTPException):
                pass

            await self._log_command(message.guild.id, message.author.id, "debate")

        except Exception as e:
            log.error("DEBATE Error: %s", e)
            await self._send_and_delete(
                message.channel,
                ":boom: Something broke. Try again.",
                delete_after=6,
                delete_original=message,
            )
            await self._log_command(message.guild.id, message.author.id, "debate", False)

    # =========================================================================
    # ;lhtt on/off/fire

    async def _do_htt(self, message):
        """Handle ;lhtt on/off/fire commands. Admin only."""
        if not message.guild:
            return

        content = message.content.strip()
        match = re.match(r"^;lhtt\s+(on|off|fire)$", content, re.IGNORECASE)
        if not match:
            return

        action = match.group(1).lower()
        guild_id = str(message.guild.id)

        # Admin check
        ctx = await self.bot.get_context(message)
        is_admin = await self._require_admin(ctx)
        if not is_admin:
            await self._send_and_delete(
                message.channel,
                ":x: Admin only.",
                delete_after=3,
                ephemeral=True,
                delete_original=message,
            )
            return

        if action == "on":
            self.cog.hot_take_enabled = True
            await self.db.save_hot_take_enabled(True)
            msg = await message.reply(
                ":white_check_mark: Hot Takes enabled!",
                allowed_mentions=discord.AllowedMentions(replied_user=False),
            )
            try:
                await msg.delete(delay=3)
            except (discord.Forbidden, discord.HTTPException):
                pass

        elif action == "off":
            self.cog.hot_take_enabled = False
            await self.db.save_hot_take_enabled(False)
            msg = await message.reply(
                "🚫 Hot Takes disabled!",
                allowed_mentions=discord.AllowedMentions(replied_user=False),
            )
            try:
                await msg.delete(delay=3)
            except (discord.Forbidden, discord.HTTPException):
                pass

        elif action == "fire":
            channel_id = str(message.channel.id)
            allowed = await self._get_hot_take_channels(guild_id)

            if channel_id not in allowed:
                await self._send_and_delete(
                    message.channel,
                    ":x: This channel is not configured for hot takes.",
                    delete_after=5,
                    delete_original=message,
                )
                return

            reply = await message.reply(
                ":fire: Firing hot take...",
                allowed_mentions=discord.AllowedMentions(replied_user=False),
            )

            try:
                ctx_msgs = await self.db.get_channel_messages(channel_id, self.cog.hot_take_config["context_messages"])
                if not ctx_msgs:
                    await reply.edit(content=":x: No messages available for context.")
                    return

                conversation = self._format_hot_take_context(ctx_msgs)

                async with self.config.guild(message.guild).all() as cfg:
                    model = cfg.get("model", "nvidia/qwen/qwen3.5-122b-a10b")

                payload = {
                    "messages": [{"role": "user", "content": HOT_TAKE_PROMPT.format(conversation=conversation)}],
                    "temperature": 0.9,
                    "max_tokens": 500,
                }

                resp = await self.ai_service.execute_request(payload, model, context="HOT_TAKE")
                text = sanitize_output(resp["choices"][0]["message"]["content"])

                await reply.delete()
                await message.channel.send(text)
                self.cog.hot_take_cooldowns[channel_id] = time.time() * 1000
                await self.db.log_hot_take(guild_id, channel_id, text, len(ctx_msgs), model, 0)

            except Exception as e:
                try:
                    await reply.edit(content=f":x: Failed to generate hot take: {e}")
                except (discord.Forbidden, discord.HTTPException):
                    pass

    # =========================================================================
    # ;ltypeon and ;ltypeoff

    async def _do_typeonoff(self, message):
        """Handle ;ltypeon and ;ltypeoff commands. Admin only."""
        if not message.guild:
            return

        content = message.content.strip()
        if content not in (";ltypeon", ";ltypeoff"):
            return

        # Admin check
        ctx = await self.bot.get_context(message)
        is_admin = await self._require_admin(ctx)
        if not is_admin:
            await self._send_and_delete(
                message.channel,
                ":x: Admin only.",
                delete_after=3,
                delete_original=message,
            )
            return

        enabled = content == ";ltypeon"

        # Persist to guild config
        async with self.config.guild(message.guild).all() as cfg:
            cfg["typing_enabled"] = enabled

        # Send response with exact emojis as required
        response = ":white_check_mark: Typing indicator ON" if enabled else "🚫 Typing indicator OFF"
        msg = await message.reply(response, allowed_mentions=discord.AllowedMentions(replied_user=False))

        try:
            await msg.delete(delay=3)
        except (discord.Forbidden, discord.HTTPException):
            pass

        try:
            await message.delete()
        except (discord.Forbidden, discord.HTTPException):
            pass

    # =========================================================================
    # Helper methods (delegated to cog instance)
    # =========================================================================

    async def _require_admin(self, ctx):
        """Check if user has admin permission (via cog helper)."""
        return await self.cog._require_admin(ctx)

    async def _handle_typing(self, channel):
        """Send typing indicator (via cog helper)."""
        return await self.cog._handle_typing(channel)

    async def _log_command(self, guild_id, user_id, command, success=True):
        """Log command usage (via cog helper)."""
        await self.cog._log_command(guild_id, user_id, command, success)

    @property
    def cooldowns(self):
        """Access the cog's cooldown tracker."""
        return self.cog.cooldowns

    async def _get_hot_take_channels(self, guild_id):
        """Get allowed hot take channels (via cog helper)."""
        return await self.cog._get_hot_take_channels(guild_id)

    def _format_hot_take_context(self, messages):
        """Format messages for hot take generation (via cog helper)."""
        return self.cog._format_hot_take_context(messages)

    # =========================================================================
    # Utility methods
    # =========================================================================

    async def _send_and_delete(
        self,
        channel,
        content,
        delete_after=None,
        delete_original=None,
        ephemeral=False,
    ):
        """
        Send a message and optionally delete it and/or the original message.
        For ephemeral messages, send with flags=64.
        """
        try:
            if ephemeral:
                msg = await channel.send(content, flags=64)
            else:
                msg = await channel.send(content)

            if delete_after:
                try:
                    await msg.delete(delay=delete_after)
                except (discord.Forbidden, discord.HTTPException):
                    pass

            if delete_original:
                try:
                    await delete_original.delete()
                except (discord.Forbidden, discord.HTTPException):
                    pass

            return msg

        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning("_send_and_delete failed: %s", e)
            return None
