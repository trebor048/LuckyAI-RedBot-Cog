import os
import json
import time
import math
import asyncio
import logging
import re
import random

import discord
from redbot.core import commands, Config, checks
from redbot.core.utils.mod import get_audit_reason

from .utils import (
    BASE_ROAST_PROMPT, TLDR_SYSTEM_PROMPT, GREENTEXT_SYSTEM_PROMPT,
    ASK_SYSTEM_PROMPT, DEBATE_SYSTEM_PROMPT, HOT_TAKE_PROMPT,
    DEFAULT_SETTINGS, PROVIDER_ORDER, FALLBACK_DEFAULT_MODELS,
    COLORS, sanitize_input, sanitize_output, generate_content_hash,
    format_messages_for_roast, format_messages_for_tldr,
    parse_debate_response, make_cooldown_message,
)
from .ai_service import PROVIDER_ENV_KEYS
from .db import MessageDB
from .ai_service import AIService, get_provider_by_model, get_actual_model_id
from .settings_ui import SettingsView
from .commands.roast_commands import RoastCommands
from .commands.admin_commands import AdminCommands
from .commands.setup_wizard import SetupView, ensure_config_json
from .listeners.message_listener import MessageListener

log = logging.getLogger("red.LuckyAICog")

TLDR_MIN_MESSAGES = 10
MAX_MESSAGE_COUNT = 500
DEFAULT_MESSAGE_COUNT = 200

COOLDOWN_PRUNE_INTERVAL = 60000

class CooldownTracker:
    def __init__(self):
        self._cooldowns = {}
        self._last_pruned = time.time() * 1000

    def _prune(self):
        now = time.time() * 1000
        if now - self._last_pruned < COOLDOWN_PRUNE_INTERVAL:
            return
        expired = [k for k, v in self._cooldowns.items() if v <= now]
        for k in expired:
            del self._cooldowns[k]
        self._last_pruned = now

    def check(self, user_id, cooldown_ms, command_key="default"):
        self._prune()
        now = time.time() * 1000
        key = f"{user_id}:{command_key}"
        expires = self._cooldowns.get(key)
        if expires and now < expires:
            remaining_ms = expires - now
            remaining_sec = math.ceil(remaining_ms / 1000)
            return {"active": True, "remaining_sec": remaining_sec, "reset_at": expires}
        self._cooldowns[key] = now + cooldown_ms
        return {"active": False, "remaining_sec": 0, "reset_at": None}

    def set(self, user_id, cooldown_ms, command_key="default"):
        self._cooldowns[f"{user_id}:{command_key}"] = (time.time() * 1000) + cooldown_ms


class LuckyAICog(commands.Cog):
    """AI-powered roast bot with message sync, TLDR, debate, ask, and hot take features."""

    def __init__(self, bot):
        self.bot = bot
        self.config_file_parent = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")

        self.config = Config.get_conf(self, identifier=1380332243402227772, force_registration=True)
        default_guild = {
            "model": "nvidia/qwen/qwen3.5-122b-a10b",
            "temperature": 1.0,
            "max_tokens": 4096,
            "top_p": 0.9,
            "top_k": 40,
            "frequency_penalty": 0.4,
            "presence_penalty": 0.2,
            "promptKey": "blunt",
            "messageFetchMode": "random",
            "randomMode": False,
            "enabled": True,
            "admin_role": None,
            "typing_enabled": True,
            "sync_channels": [],
        }
        self.config.register_guild(**default_guild)
        self.config.register_global(ask_model="deepseek/deepseek-reasoner", ask_vision_model="openai/gpt-4o-mini")

        db_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
        db_path = os.getenv("DB_FILE", os.path.join(db_dir, "messages.db"))
        self.db = MessageDB(db_path)
        self.ai_service = AIService(self.bot, self.config)

        self.settings_sessions = {}
        self.setup_sessions = {}
        self._session_counter = 0
        self.cooldowns = CooldownTracker()

        self.hot_take_enabled = os.getenv("ENABLE_HOT_TAKE", "").lower() == "true"
        self.hot_take_channel_activity = {}
        self.hot_take_cooldowns = {}
        self.hot_take_config = {
            "window_minutes": int(os.getenv("HOT_TAKE_WINDOW_MINUTES", "5")),
            "cooldown_minutes": int(os.getenv("HOT_TAKE_COOLDOWN_MINUTES", "120")),
            "min_messages": int(os.getenv("HOT_TAKE_MIN_MESSAGES", "10")),
            "probability": float(os.getenv("HOT_TAKE_PROBABILITY", "0.05")),
            "context_messages": int(os.getenv("HOT_TAKE_CONTEXT_MESSAGES", "100")),
        }
        self.hot_take_channels_extra = os.getenv("HOT_TAKE_CHANNELS", "").split(",") if os.getenv("HOT_TAKE_CHANNELS") else []

        self.message_sync_enabled = os.getenv("ENABLE_SYNC", "").lower() == "true"
        self._typing_tasks = {}
        self._prefix_listeners = {}

        # Instantiate helper classes (logic modules, not Cogs)
        self.roast_cmds = RoastCommands(self.bot, self.config, self.db, self.ai_service, self)
        self.admin_cmds = AdminCommands(self.bot, self.config, self.db, self.ai_service, self)
        # MessageListener handles message sync and hot-takes
        self.msg_listener = MessageListener(self.bot, self.db, self.config)
        self.msg_listener.configure_hot_take(
            self.hot_take_enabled,
            self.hot_take_config.get("window_minutes", 5),
            self.hot_take_config.get("min_messages", 10),
            self.hot_take_config.get("cooldown_minutes", 120),
            self.hot_take_config.get("probability", 0.05),
            self.hot_take_config.get("context_messages", 100),
        )

    async def cog_load(self):
        await self.db.initialize()
        config_path = os.path.join(self.config_file_parent, "config", "config.json")
        ensure_config_json(config_path)
        self._load_models_data()
        persisted = await self.db.get_hot_take_enabled()
        if persisted is not None:
            self.hot_take_enabled = persisted
        # Start the hot_take_loop task if enabled
        if self.hot_take_enabled:
            try:
                self.msg_listener.hot_take_loop.start(self.ai_service)
            except RuntimeError:
                pass  # Already running
        log.info(
            "Lucky AI loaded. Run `;lsetup` to configure API keys, `;lhelp` for commands."
        )

    async def cog_unload(self):
        # Stop hot_take_loop if running
        if self.msg_listener.hot_take_loop.is_running():
            self.msg_listener.hot_take_loop.cancel()
        self.msg_listener.cleanup()
        await self.db.close()
        await self.ai_service.close()

    def _load_models_data(self):
        config_path = os.path.join(self.config_file_parent, "config", "config.json")
        try:
            with open(config_path, "r") as f:
                data = json.load(f)
            models = data.get("MODELS", {})
            self.ai_service.set_models_data(models)
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    def _load_personalities(self):
        config_path = os.path.join(self.config_file_parent, "config", "config.json")
        try:
            with open(config_path, "r") as f:
                data = json.load(f)
            return data.get("PERSONALITIES", {})
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _select_roast_style(self, prompt_key=None, random_mode=False, style_override=None):
        if style_override:
            return {"name": "command-override", "systemPrompt": BASE_ROAST_PROMPT + "\n\nStyle guidance: " + style_override[:1000]}
        personalities = self._load_personalities()
        if personalities:
            keys = list(personalities.keys())
            if random_mode:
                import random
                selected = random.choice(keys)
            elif prompt_key and prompt_key in personalities:
                selected = prompt_key
            else:
                selected = keys[0]
            style_text = personalities[selected]
            return {"name": selected, "systemPrompt": BASE_ROAST_PROMPT + "\n\nStyle guidance: " + style_text}
        return {"name": "base", "systemPrompt": BASE_ROAST_PROMPT}

    def _create_session(self, user_id, guild_id):
        self._session_counter += 1
        session_id = str(self._session_counter)
        self.settings_sessions[session_id] = {
            "user_id": user_id,
            "guild_id": guild_id,
            "current_page": "model",
            "model_page": 0,
            "prompt_page": 0,
            "show_model_dropdown": False,
            "show_prompt_dropdown": False,
        }
        return session_id

    async def _check_enabled(self, ctx):
        async with self.config.guild(ctx.guild).all() as cfg:
            return cfg.get("enabled", True)

    async def _check_blacklist(self, guild_id, user_id):
        return await self.db.is_blacklisted(guild_id, user_id)

    async def _require_admin(self, ctx):
        if ctx.author.guild_permissions.administrator:
            return True
        async with self.config.guild(ctx.guild).all() as cfg:
            admin_role_id = cfg.get("admin_role")
            if admin_role_id:
                role = ctx.guild.get_role(int(admin_role_id))
                if role and role in ctx.author.roles:
                    return True
        return False

    async def _log_command(self, guild_id, user_id, command, success=True):
        await self.db.log_command_usage(guild_id, user_id, command, success)

    def _format_hot_take_context(self, messages):
        msgs = sorted([m for m in messages if m.get("content", "").strip()], key=lambda x: x.get("timestamp", 0))
        lines = []
        for m in msgs[-self.hot_take_config["context_messages"]:]:
            name = m.get("author_tag") or m.get("author_id", "Unknown")
            lines.append(f"[{name}]: {m['content']}")
        conversation = "\n".join(lines)
        if len(conversation) > 8000:
            conversation = conversation[:8000] + "\n[...truncated]"
        return conversation

    async def _maybe_fire_hot_take(self, message):
        if not self.hot_take_enabled:
            return
        if not message.guild or message.author.bot:
            return
        if not message.channel or message.channel.type != discord.ChannelType.text:
            return
        channel_id = str(message.channel.id)
        guild_id = str(message.guild.id)

        allowed = await self._get_hot_take_channels(guild_id)
        if channel_id not in allowed:
            return

        now = time.time() * 1000
        window_ms = self.hot_take_config["window_minutes"] * 60 * 1000
        activity = self.hot_take_channel_activity.get(channel_id, [])
        activity.append(now)
        activity = [t for t in activity if now - t < window_ms]
        self.hot_take_channel_activity[channel_id] = activity

        min_msgs = self.hot_take_config["min_messages"]
        if len(activity) < min_msgs:
            return

        cooldown_ms = self.hot_take_config["cooldown_minutes"] * 60 * 1000
        last_fire = self.hot_take_cooldowns.get(channel_id, 0)
        if now - last_fire < cooldown_ms:
            return

        prob = self.hot_take_config["probability"]
        if random.random() > prob:
            return

        try:
            ctx_msgs = await self.db.get_channel_messages(channel_id, self.hot_take_config["context_messages"])
            if not ctx_msgs:
                return
            conversation = self._format_hot_take_context(ctx_msgs)
            async with self.config.guild(message.guild).all() as cfg:
                model = cfg.get("model", "nvidia/qwen/qwen3.5-122b-a10b")
            payload = {
                "messages": [{"role": "user", "content": HOT_TAKE_PROMPT.format(conversation=conversation)}],
                "temperature": 0.9,
                "max_tokens": 500,
            }
            resp = await self.ai_service.execute_request(payload, model, context="HOT_TAKE", timeout=60)
            text = sanitize_output(resp["choices"][0]["message"]["content"])
            await message.channel.send(text)
            self.hot_take_cooldowns[channel_id] = now
            await self.db.log_hot_take(guild_id, channel_id, text, len(ctx_msgs), model, 0)
        except Exception as e:
            log.error("HOT_TAKE Error in %s: %s", channel_id, e)

    async def _get_hot_take_channels(self, guild_id):
        guild = self.bot.get_guild(int(guild_id))
        if not guild:
            return []
        async with self.config.guild(guild).all() as cfg:
            sync_channels = cfg.get("sync_channels", [])
        merged = list(set(sync_channels + self.hot_take_channels_extra))
        return merged

    async def _handle_typing(self, channel):
        try:
            await channel.trigger_typing()
        except (discord.Forbidden, discord.HTTPException):
            pass

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot:
            return
        if not message.guild:
            return

        guild_id = str(message.guild.id)
        channel_id = str(message.channel.id)
        author_id = str(message.author.id)

        async with self.config.guild(message.guild).all() as cfg:
            sync_channels = cfg.get("sync_channels", [])

        if sync_channels and channel_id in sync_channels:
            msg = {
                "id": str(message.id),
                "author": {"id": author_id, "tag": str(message.author), "name": message.author.name},
                "channel": {"id": channel_id},
                "content": message.content,
                "timestamp": int(time.time() * 1000),
                "guild_id": guild_id,
            }
            await self.db.save_message(msg)

        await self._maybe_fire_hot_take(message)

    async def _auto_delete(self, cmd_msg, resp_msg, delay=5):
        try:
            await cmd_msg.delete()
        except (discord.Forbidden, discord.HTTPException):
            pass
        try:
            await resp_msg.delete(delay=delay)
        except (discord.Forbidden, discord.HTTPException):
            pass



    @commands.command(name="lroast")
    async def lroast(self, ctx: commands.Context, user: discord.User = None, *, style: str = None):
        await ctx.defer()
        if not ctx.guild:
            await ctx.send(":x: This command only works in servers.")
            return
        target = user or ctx.author
        guild_id = str(ctx.guild.id)
        user_id = str(target.id)
        author_id = str(ctx.author.id)

        is_admin = await self._require_admin(ctx)
        if not is_admin:
            cooldown = self.cooldowns.check(author_id, 30000, "roast")
            if cooldown["active"]:
                await ctx.send(f":hourglass_flowing_sand: Wait {cooldown['remaining_sec']}s")
                return

        async with self.config.guild(ctx.guild).all() as cfg:
            if not cfg.get("enabled", True):
                await ctx.send(":x: Bot is currently disabled in this server.")
                return

        if await self._check_blacklist(guild_id, user_id):
            await ctx.send(f":x: <@{user_id}> is blacklisted from being roasted in this server.")
            return

        async with self.config.guild(ctx.guild).all() as cfg:
            typing_enabled = cfg.get("typing_enabled", True)
            model = cfg.get("model", "nvidia/qwen/qwen3.5-122b-a10b")
            message_fetch_mode = cfg.get("messageFetchMode", "random")
            rand_mode = cfg.get("randomMode", False)
            prompt_key = cfg.get("promptKey")

        if typing_enabled:
            await self._handle_typing(ctx.channel)

        try:
            messages = await self.db.get_messages(user_id, 200, message_fetch_mode, guild_id)
            if not messages:
                await ctx.send(f":x: <@{user_id}> has no message history in this server.")
                return

            roast_style = self._select_roast_style(prompt_key, rand_mode, style)
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

            resp = await self.ai_service.execute_request(payload, model, context="ROAST")
            text = sanitize_output(resp["choices"][0]["message"]["content"]) or f"I was going to roast {target.name}, but even my AI has standards. Nice try though!"
            usage = resp.get("usage", {})
            truncated = text[:4090] + "..." if len(text) > 4090 else text
            embed = discord.Embed(
                color=COLORS["ROAST"],
                title=":fire: ROAST :fire:",
                description=truncated,
            )
            embed.set_footer(text=f"Tokens used: {usage.get('total_tokens', '?')}" if usage else "Roast complete")
            embed.timestamp = discord.utils.utcnow()
            await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions(everyone=False, roles=False))
            await self.db.update_roast_count(user_id)
            await self._log_command(guild_id, ctx.author.id, "roast")
        except Exception as e:
            log.error("ROAST Error: %s", e)
            await ctx.send(f":x: Failed to generate roast: {e}")
            await self._log_command(guild_id, ctx.author.id, "roast", False)

    @commands.command(name="ltldr")
    async def ltldr(self, ctx: commands.Context, messages: int = 200, style: str = "normal"):
        await ctx.defer()
        if not ctx.guild:
            await ctx.send(":x: This command only works in servers.")
            return
        if messages < TLDR_MIN_MESSAGES or messages > MAX_MESSAGE_COUNT:
            await ctx.send(f":x: Message count must be between {TLDR_MIN_MESSAGES} and {MAX_MESSAGE_COUNT}.")
            return
        is_admin = await self._require_admin(ctx)
        if not is_admin:
            cooldown = self.cooldowns.check(str(ctx.author.id), 300000, "tldr")
            if cooldown["active"]:
                await ctx.send(f":hourglass_flowing_sand: Wait {cooldown['remaining_sec']}s")
                return
        async with self.config.guild(ctx.guild).all() as cfg:
            if not cfg.get("enabled", True):
                await ctx.send(":x: Bot is currently disabled in this server.")
                return
            model = cfg.get("model", "nvidia/qwen/qwen3.5-122b-a10b")
            typing_enabled = cfg.get("typing_enabled", True)
        if typing_enabled:
            await self._handle_typing(ctx.channel)
        try:
            raw_messages = await self.db.get_channel_messages(str(ctx.channel.id), messages)
            if not raw_messages:
                await ctx.send(":x: No messages found. The channel may not be synced yet.")
                return
            conv = format_messages_for_tldr(raw_messages, style)
            system = GREENTEXT_SYSTEM_PROMPT if style == "greentext" else TLDR_SYSTEM_PROMPT
            header = f"The following are the last {len(raw_messages)} messages from a Discord channel, ordered oldest to newest:\n\n"
            suffix = "\n\nTurn this conversation into a 4chan greentext story." if style == "greentext" else "\n\nSummarize this conversation as a TL;DR."
            payload = {
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": header + conv + suffix}],
                "temperature": 1.0 if style == "greentext" else 0.7,
                "max_tokens": 2048 if style == "greentext" else 1024,
            }
            resp = await self.ai_service.execute_request(payload, model, context="TLDR", timeout=90)
            text = sanitize_output(resp["choices"][0]["message"]["content"])
            embed = discord.Embed(
                color=COLORS["SUCCESS"] if style == "greentext" else COLORS["INFO"],
                title="> Greentext" if style == "greentext" else "🧠 TL;DR Summary",
                description=text[:4090] if len(text) > 4090 else text,
            )
            embed.set_footer(text=f"Requested by {ctx.author.name}")
            embed.timestamp = discord.utils.utcnow()
            await ctx.send(embed=embed)
            await self._log_command(ctx.guild.id, ctx.author.id, "tldr")
        except Exception as e:
            log.error("TLDR Error: %s", e)
            await ctx.send(f":x: Failed to generate TL;DR: {e}")
            await self._log_command(ctx.guild.id, ctx.author.id, "tldr", False)

    @commands.command(name="loptout")
    async def loptout(self, ctx: commands.Context, action: str):
        await ctx.defer(ephemeral=True)
        if not ctx.guild:
            await ctx.send(":x: This command only works in servers.")
            return
        if action not in ("in", "out"):
            await ctx.send(":x: Use `in` or `out`.")
            return
        opted = action == "out"
        await self.db.set_user_opt_out(str(ctx.author.id), str(ctx.guild.id), opted)
        status = "opted out" if opted else "opted in"
        await ctx.send(f":white_check_mark: You have {status} of roasting.")

    @commands.command(name="lsettings")
    @checks.admin_or_permissions(administrator=True)
    async def lsettings(self, ctx: commands.Context):
        await ctx.defer(ephemeral=True)
        if not ctx.guild:
            await ctx.send(":x: This command only works in servers.")
            return
        await self.admin_cmds.open_settings(ctx)

    @commands.group(name="lconfig", aliases=["lcfg"])
    @checks.admin_or_permissions(administrator=True)
    async def lconfig(self, ctx: commands.Context):
        pass

    @lconfig.group(name="channels")
    async def lconfig_channels(self, ctx: commands.Context):
        pass

    @lconfig_channels.command(name="add")
    async def lconfig_channels_add(self, ctx: commands.Context, channel: discord.TextChannel):
        await ctx.defer(ephemeral=True)
        if not ctx.guild:
            return
        guild_id = str(ctx.guild.id)
        channel_id = str(channel.id)
        async with self.config.guild(ctx.guild).all() as cfg:
            sync = cfg.get("sync_channels", [])
            if channel_id in sync:
                await ctx.send(":x: Channel already enabled for syncing.")
                return
            if len(sync) >= 45:
                await ctx.send(f":x: Channel limit approaching ({len(sync)}/45).")
                return
            sync.append(channel_id)
            cfg["sync_channels"] = sync
        await self.db.update_sync_status(guild_id, channel_id)
        await self.db.log_sync_operation(guild_id, channel_id, "channel_add", triggered_by=str(ctx.author.id))
        await ctx.send(f":white_check_mark: Added {channel.mention} as a sync channel. Starting initial backfill...")
        asyncio.create_task(self._do_backfill(ctx.guild, channel, 14, ctx.author))

    @lconfig_channels.command(name="remove")
    async def lconfig_channels_remove(self, ctx: commands.Context, channel: discord.TextChannel):
        await ctx.defer(ephemeral=True)
        if not ctx.guild:
            return
        channel_id = str(channel.id)
        async with self.config.guild(ctx.guild).all() as cfg:
            sync = cfg.get("sync_channels", [])
            if channel_id not in sync:
                await ctx.send(":x: Channel not enabled for syncing.")
                return
            sync.remove(channel_id)
            cfg["sync_channels"] = sync
        await self.db.delete_sync_status(str(ctx.guild.id), channel_id)
        deleted = await self.db.delete_channel_messages(str(ctx.guild.id), channel_id)
        await ctx.send(f":white_check_mark: Removed {channel.mention} from sync channels ({deleted} messages deleted).")

    @lconfig_channels.command(name="list")
    async def lconfig_channels_list(self, ctx: commands.Context):
        await ctx.defer(ephemeral=True)
        if not ctx.guild:
            return
        async with self.config.guild(ctx.guild).all() as cfg:
            sync = cfg.get("sync_channels", [])
        if not sync:
            await ctx.send(":x: No sync channels configured.")
            return
        lines = [f"- <#{ch}>" for ch in sync]
        embed = discord.Embed(title=":clipboard: Sync Channels", color=0x0099ff, description="\n".join(lines))
        await ctx.send(embed=embed, ephemeral=True)

    @lconfig.group(name="blacklist")
    async def lconfig_blacklist(self, ctx: commands.Context):
        pass

    @lconfig_blacklist.command(name="add")
    async def lconfig_blacklist_add(self, ctx: commands.Context, user: discord.User):
        await ctx.defer(ephemeral=True)
        if not ctx.guild:
            return
        await self.db.add_to_blacklist(str(ctx.guild.id), str(user.id), str(ctx.author.id))
        await ctx.send(f":white_check_mark: Added {user.mention} to the blacklist.")

    @lconfig_blacklist.command(name="remove")
    async def lconfig_blacklist_remove(self, ctx: commands.Context, user: discord.User):
        await ctx.defer(ephemeral=True)
        if not ctx.guild:
            return
        await self.db.remove_from_blacklist(str(ctx.guild.id), str(user.id))
        await ctx.send(f":white_check_mark: Removed {user.mention} from the blacklist.")

    @lconfig_blacklist.command(name="list")
    async def lconfig_blacklist_list(self, ctx: commands.Context):
        await ctx.defer(ephemeral=True)
        if not ctx.guild:
            return
        entries = await self.db.get_blacklist(str(ctx.guild.id))
        if not entries:
            await ctx.send(":x: No blacklisted users.")
            return
        lines = [f"- <@{e['user_id']}>{' - ' + e['reason'] if e.get('reason') else ''}" for e in entries]
        embed = discord.Embed(title=":clipboard: Blacklisted Users", color=0x0099ff, description="\n".join(lines))
        await ctx.send(embed=embed, ephemeral=True)

    @lconfig.command(name="admin_role")
    async def lconfig_admin_role(self, ctx: commands.Context, role: discord.Role = None):
        await ctx.defer(ephemeral=True)
        if not ctx.guild:
            return
        async with self.config.guild(ctx.guild).all() as cfg:
            cfg["admin_role"] = str(role.id) if role else None
        text = role.mention if role else "server administrators only"
        await ctx.send(f":white_check_mark: Admin role set to {text}.")

    @lconfig.command(name="toggle")
    async def lconfig_toggle(self, ctx: commands.Context, enabled: bool):
        await ctx.defer(ephemeral=True)
        if not ctx.guild:
            return
        async with self.config.guild(ctx.guild).all() as cfg:
            cfg["enabled"] = enabled
        await ctx.send(f":white_check_mark: Bot {'enabled' if enabled else 'disabled'} for this server.")

    @lconfig.command(name="backfill")
    async def lconfig_backfill(self, ctx: commands.Context, channel: discord.TextChannel = None, days: int = None):
        await ctx.defer(ephemeral=True)
        if not ctx.guild:
            return
        channel = channel or ctx.channel
        await ctx.send(f":arrows_counterclockwise: Starting {f'{days}-day' if days else 'full'} backfill of {channel.mention}...")
        await self._do_backfill(ctx.guild, channel, days, ctx.author)

    async def _do_backfill(self, guild, channel, days, author):
        from datetime import datetime, timedelta
        guild_id = str(guild.id)
        channel_id = str(channel.id)
        after = datetime.utcnow() - timedelta(days=days) if days else None
        total_synced = 0
        try:
            async for msg in channel.history(limit=None, after=after, oldest_first=True):
                m = {
                    "id": str(msg.id),
                    "author": {"id": str(msg.author.id), "tag": str(msg.author), "name": msg.author.name},
                    "channel": {"id": channel_id},
                    "content": msg.content,
                    "timestamp": int(msg.created_at.timestamp() * 1000),
                    "guild_id": guild_id,
                }
                await self.db.save_message(m)
                total_synced += 1
            await self.db.update_sync_status(guild_id, channel_id)
            await self.db.log_sync_operation(guild_id, channel_id, "backfill", total_synced, triggered_by=str(author.id))
            log.info("BACKFILL Complete for %s: %d messages", channel_id, total_synced)
        except Exception as e:
            log.error("BACKFILL Error for %s: %s", channel_id, e)

    @commands.command(name="lstats")
    @checks.admin_or_permissions(administrator=True)
    async def lstats(self, ctx: commands.Context):
        await ctx.defer(ephemeral=True)
        if not ctx.guild:
            return
        try:
            stats_data = await self.admin_cmds.build_stats(ctx)
            embed = discord.Embed(color=stats_data["color"], title=stats_data["title"])
            for field in stats_data["fields"]:
                embed.add_field(name=field["name"], value=field["value"], inline=field.get("inline", True))
            embed.timestamp = discord.utils.utcnow()
            await ctx.send(embed=embed, ephemeral=True)
        except Exception as e:
            log.error("STATS Error: %s", e)
            await ctx.send(":x: Failed to retrieve statistics.")

    @commands.command(name="lsetup")
    @checks.admin_or_permissions(administrator=True)
    async def lsetup(self, ctx: commands.Context):
        await ctx.defer(ephemeral=True)
        if not ctx.guild:
            await ctx.send(":x: This command only works in servers.")
            return

        config_path = os.path.join(self.config_file_parent, "config", "config.json")
        ensure_config_json(config_path)

        self._session_counter += 1
        session_id = str(self._session_counter)
        api_keys = {}
        for provider in PROVIDER_ORDER:
            env_key = PROVIDER_ENV_KEYS.get(provider, "")
            existing = os.getenv(env_key, "")
            if existing:
                api_keys[provider] = existing

        self.setup_sessions[session_id] = {
            "current_step": 0,
            "api_keys": api_keys,
            "configured_count": len(api_keys),
            "skipped_count": 0,
            "default_model": "nvidia/qwen/qwen3.5-122b-a10b",
            "finished": False,
            "test_results": [],
        }

        view = SetupView(self, session_id, ctx.author.id, str(ctx.guild.id))
        embed = await view.build_embed()
        await ctx.send(embed=embed, view=view, ephemeral=True)

    @commands.command(name="lhelp", aliases=["lcommands"])
    async def lhelp(self, ctx: commands.Context):
        embed = discord.Embed(color=0x0099ff, title="\U0001f916 Lucky AI - Commands", description="AI-powered roasts, TL;DRs, and more. All commands use the `;l` prefix.")
        embed.add_field(name="\U0001f3af `;lroast @user`", value='Generate an AI roast based on message history.\nOptions: `style`: optional override', inline=False)
        embed.add_field(name="\U0001f9e0 `;ltldr [count] [style]`", value="Summarize the last N messages (10-500).\nStyle: `normal` or `greentext`\nAlias: `;lgreentext N`", inline=False)
        embed.add_field(name=":no_entry_sign: `;loptout <in|out>`", value="Opt in or out of being roasted.", inline=False)
        embed.add_field(name=":wrench: `;lsettings` (Admin)", value="Interactive UI for model, temperature, API keys, and styles.", inline=False)
        embed.add_field(name=":gear: `;lconfig` (Admin)", value="Manage sync channels, blacklist, admin role, toggle, backfill.\nSubcommands: `channels add/remove/list`, `blacklist add/remove/list`, `admin_role`, `toggle`, `backfill`", inline=False)
        embed.add_field(name="\U0001f4ca `;lstats` (Admin)", value="View bot statistics and health.", inline=False)
        embed.add_field(name="\U0001f680 `;lsetup` (Admin)", value="Interactive setup wizard for API keys and configuration.", inline=False)
        embed.add_field(name=":question: `;lhelp`", value="Show this help message.", inline=False)
        embed.add_field(name="More Commands", value="`;lask <question>` - Chat with AI\n`;ldebate` - Judge a debate\n`;lhtt on/off/fire` - Manage hot takes\n`;ltypeon`/`;ltypeoff` - Toggle typing indicator", inline=False)
        embed.set_footer(text="Lucky AI - Powered by AI")
        embed.timestamp = discord.utils.utcnow()
        await ctx.send(embed=embed)

    @commands.command(name="lask")
    async def lask(self, ctx: commands.Context, *, question: str = None):
        if not ctx.guild:
            await ctx.send(":x: This command only works in servers.")
            return
        if not question and not ctx.message.attachments:
            await ctx.send("Usage: `;lask <question>` or attach an image")
            return
        author_id = str(ctx.author.id)
        guild_id = str(ctx.guild.id)
        is_admin = await self._require_admin(ctx)
        cd_ms = 10000 if is_admin else 60000
        cooldown = self.cooldowns.check(author_id, cd_ms, "ask")
        if cooldown["active"]:
            await ctx.send(f":hourglass_flowing_sand: Wait {cooldown['remaining_sec']}s")
            return
        try:
            await self._handle_typing(ctx.channel)
            bot_user_id = str(self.bot.user.id)
            ctx_msgs = await self.db.get_channel_messages(str(ctx.channel.id), 25)
            context_lines = []
            for m in (ctx_msgs or []):
                if m.get("author_id") == bot_user_id:
                    continue
                name = m.get("author_tag") or m.get("author_id", "Someone")
                content = (m.get("content", "") or "")[:250]
                context_lines.append(f"{name}: {content}")
            ctx_text = "\n".join(context_lines[-25:])
            user_name = ctx.author.display_name or ctx.author.name
            if ctx_text:
                prompt = f"Recent chat:\n{ctx_text}\n---\n{user_name}: {question or '[image]'}\n\nAnswer:"
            else:
                prompt = f"{user_name}: {question or '[image]'}\n\nAnswer:"
            attach = ctx.message.attachments[0] if ctx.message.attachments else None
            has_image = attach and attach.content_type and attach.content_type.startswith("image/")
            if has_image:
                import aiohttp
                async with aiohttp.ClientSession() as session:
                    async with session.get(attach.url) as resp:
                        if resp.status == 200:
                            buf = await resp.read()
                            import base64
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
            async with self.config.all() as gcfg:
                model = gcfg.get("ask_vision_model") if has_image else gcfg.get("ask_model")
            payload = {"messages": [{"role": "system", "content": ASK_SYSTEM_PROMPT}, *msgs], "temperature": 0.85, "max_tokens": 600}
            resp = await self.ai_service.execute_request(payload, model, context="ASK", timeout=45, max_retries=2)
            text = resp["choices"][0]["message"]["content"]
            chunks = [text[i:i+1990] for i in range(0, len(text), 1990)] if len(text) > 1990 else [text]
            for chunk in chunks:
                await ctx.send(chunk)
            await self._log_command(guild_id, author_id, "ask")
        except Exception as e:
            log.error("ASK Error: %s", e)
            await ctx.send(f":x: {e}")
            await self._log_command(guild_id, author_id, "ask", False)

    @commands.command(name="ldebate")
    async def ldebate(self, ctx: commands.Context):
        if not ctx.guild:
            await ctx.send(":x: This command only works in servers.")
            return
        author_id = str(ctx.author.id)
        guild_id = str(ctx.guild.id)
        is_admin = await self._require_admin(ctx)
        cd_ms = 20000 if is_admin else 120000
        cooldown = self.cooldowns.check(author_id, cd_ms, "debate")
        if cooldown["active"]:
            await ctx.send(f":hourglass_flowing_sand: Wait {cooldown['remaining_sec']}s")
            return
        try:
            msgs = await self.db.get_channel_messages(str(ctx.channel.id), 50)
            if not msgs or len(msgs) < 2:
                await ctx.send("Nothing to debate here. Start an argument first.")
                return
            opt_out_ids = await self.db.get_opt_out_user_ids(str(ctx.guild.id))
            opt_set = set(opt_out_ids)
            filtered = [m for m in msgs if m.get("author_id") not in opt_set]
            if len(filtered) < 2:
                await ctx.send(":x: Not enough participants after filtering opt-outs.")
                return
            participants = set()
            ctx_text = ""
            for m in filtered[-50:]:
                name = m.get("author_tag") or m.get("author_id", "Someone")
                content = (m.get("content", "") or "")[:300]
                ctx_text += f"{name}: {content}\n"
                participants.add(name)
            if len(participants) < 2:
                await ctx.send("You can't debate yourself. Grab a friend.")
                return
            if len(participants) > 10:
                await ctx.send("Too many cooks. Narrow it down to 2 sides.")
                return
            await self._handle_typing(ctx.channel)
            prompt = f"Recent chat:\n{ctx_text}\n\nJudge this debate:"
            async with self.config.all() as gcfg:
                model = gcfg.get("ask_model", "deepseek/deepseek-reasoner")
            payload = {"messages": [{"role": "system", "content": DEBATE_SYSTEM_PROMPT}, {"role": "user", "content": prompt}], "temperature": 0.85, "max_tokens": 800}
            resp = await self.ai_service.execute_request(payload, model, context="DEBATE", timeout=45, max_retries=2)
            text = resp["choices"][0]["message"]["content"]
            parsed = parse_debate_response(text)
            if not parsed.get("winner") or (not parsed.get("sideA") and not parsed.get("sideB")):
                await ctx.send("This isn't really a debate, just vibes. Pick a side and argue.")
                return
            is_a_win = parsed.get("winner", "").upper().startswith("A")
            embed = discord.Embed(
                title=f"\u2696\uFE0F Debate: {parsed.get('topic', 'Conversation Analysis')}",
                color=0x00ff00 if is_a_win else 0xff0000,
            )
            if parsed.get("sideA"):
                parts = parsed["sideA"].split("\u2014\u2014", 1)
                embed.add_field(name=f"\U0001f3db\uFE0F Side A: {parts[0].strip() if parts else ''}", value=parts[1].strip() if len(parts) > 1 else parsed["sideA"], inline=True)
            if parsed.get("sideB"):
                parts = parsed["sideB"].split("\u2014\u2014", 1)
                embed.add_field(name=f"\U0001f3db\uFE0F Side B: {parts[0].strip() if parts else ''}", value=parts[1].strip() if len(parts) > 1 else parsed["sideB"], inline=True)
            embed.add_field(name="\U0001f3c6 Verdict", value=parsed.get("verdict", "Inconclusive"), inline=False)
            embed.add_field(name="\U0001f480 Loser Take", value=parsed.get("loserTake", "No arguments found."), inline=False)
            if parsed.get("score"):
                embed.add_field(name="\U0001f4ca Score", value=parsed["score"], inline=False)
            embed.set_footer(text=f"Requested by {ctx.author.name}")
            await ctx.send(embed=embed)
            await self._log_command(guild_id, author_id, "debate")
        except Exception as e:
            log.error("DEBATE Error: %s", e)
            await ctx.send("Something broke. Try again.")
            await self._log_command(guild_id, author_id, "debate", False)

    @commands.command(name="lhtt")
    @checks.admin_or_permissions(administrator=True)
    async def lhtt(self, ctx: commands.Context, action: str):
        if not ctx.guild:
            return
        guild_id = str(ctx.guild.id)
        if action == "on":
            self.hot_take_enabled = True
            await self.db.save_hot_take_enabled(True)
            await ctx.send(":white_check_mark: Hot Takes enabled!")
        elif action == "off":
            self.hot_take_enabled = False
            await self.db.save_hot_take_enabled(False)
            await ctx.send("Hot Takes disabled!")
        elif action == "fire":
            channel_id = str(ctx.channel.id)
            allowed = await self._get_hot_take_channels(guild_id)
            if channel_id not in allowed:
                await ctx.send(":x: This channel is not configured for hot takes.")
                return
            await ctx.send(":fire: Firing hot take...")
            try:
                ctx_msgs = await self.db.get_channel_messages(channel_id, self.hot_take_config["context_messages"])
                if not ctx_msgs:
                    await ctx.send(":x: No messages available for context.")
                    return
                conversation = self._format_hot_take_context(ctx_msgs)
                async with self.config.guild(ctx.guild).all() as cfg:
                    model = cfg.get("model", "nvidia/qwen/qwen3.5-122b-a10b")
                payload = {
                    "messages": [{"role": "user", "content": HOT_TAKE_PROMPT.format(conversation=conversation)}],
                    "temperature": 0.9,
                    "max_tokens": 500,
                }
                resp = await self.ai_service.execute_request(payload, model, context="HOT_TAKE")
                text = sanitize_output(resp["choices"][0]["message"]["content"])
                await ctx.send(text)
                self.hot_take_cooldowns[channel_id] = time.time() * 1000
                await self.db.log_hot_take(guild_id, channel_id, text, len(ctx_msgs), model, 0)
            except Exception as e:
                log.error("HOT_TAKE Error: %s", e)
                await ctx.send(f":x: Failed to generate hot take.")
        else:
            await ctx.send("Usage: `;lhtt on` / `;lhtt off` / `;lhtt fire`")

    @commands.command(name="ltypeon")
    @checks.admin_or_permissions(administrator=True)
    async def ltypeon(self, ctx: commands.Context):
        if not ctx.guild:
            return
        async with self.config.guild(ctx.guild).all() as cfg:
            cfg["typing_enabled"] = True
        await ctx.send(":white_check_mark: Typing indicator ON")

    @commands.command(name="ltypeoff")
    @checks.admin_or_permissions(administrator=True)
    async def ltypeoff(self, ctx: commands.Context):
        if not ctx.guild:
            return
        async with self.config.guild(ctx.guild).all() as cfg:
            cfg["typing_enabled"] = False
        await ctx.send("Typing indicator OFF")
