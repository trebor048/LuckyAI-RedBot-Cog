import os
import json
import time
import math
import asyncio
import logging
import random
from typing import Dict, Optional, Any

import discord
from discord.ext.commands.view import StringView
from redbot.core import commands, Config, checks

from ..providers import PROVIDER_ORDER, FALLBACK_DEFAULT_MODELS
from ..utils import (
    BASE_ROAST_PROMPT, TLDR_SYSTEM_PROMPT, GREENTEXT_SYSTEM_PROMPT,
    ASK_SYSTEM_PROMPT, DEBATE_SYSTEM_PROMPT, HOT_TAKE_PROMPT,
    COLORS, sanitize_output, generate_content_hash,
    format_messages_for_roast, format_messages_for_tldr,
    parse_debate_response,
)

from ..database.manager import MessageDB
from .service import AIService
from ..ui.settings import SettingsView
from ..commands.admin import AdminCommands
from ..commands.setup import SetupView, ensure_config_json
from ..listeners.messages import MessageListener

log = logging.getLogger("red.LuckyAICog")

TLDR_MIN_MESSAGES = 10
MAX_MESSAGE_COUNT = 500
DEFAULT_MESSAGE_COUNT = 200

COOLDOWN_PRUNE_INTERVAL = 60000

_UNPREFIXED_COMMANDS = {
    "lroast", "ltldr", "lgreentext", "lask", "ldebate", "loptout",
    "lsetup", "lconfig", "lstats", "lhelp", "lhtt",
    "ltypeon", "ltypeoff",
}

DEFAULT_MODEL = "nvidia/nemotron-3-super-120b-a12b"


class CooldownTracker:
    """
    Custom cooldown tracker for per-command granularity and admin bypass.
    
    DEVIATION FROM RED BEST PRACTICES:
    Red's @commands.cooldown() decorator is global and doesn't support per-command
    granularity or easy admin bypass. This custom implementation allows:
    - Different cooldowns for different commands (roast: 30s, tldr: 300s, etc.)
    - Admin bypass without decorator complexity
    - Fine-grained control over cooldown behavior
    
    Stores cooldowns in memory with automatic pruning every 60 seconds.
    """
    
    def __init__(self) -> None:
        self._cooldowns: Dict[str, float] = {}
        self._last_pruned: float = time.time() * 1000

    def _prune(self) -> None:
        """Remove expired cooldown entries to prevent memory accumulation."""
        now = time.time() * 1000
        if now - self._last_pruned < COOLDOWN_PRUNE_INTERVAL:
            return
        expired = [k for k, v in self._cooldowns.items() if v <= now]
        for k in expired:
            del self._cooldowns[k]
        self._last_pruned = now

    def check(self, user_id: str, cooldown_ms: int, command_key: str = "default") -> Dict[str, Any]:
        """
        Check if a user is on cooldown for a command.
        
        Args:
            user_id: Discord user ID
            cooldown_ms: Cooldown duration in milliseconds
            command_key: Command identifier (e.g., "roast", "tldr")
        
        Returns:
            Dict with keys:
            - active: bool - Whether cooldown is active
            - remaining_sec: int - Seconds remaining (0 if not active)
            - reset_at: float - Timestamp when cooldown expires (None if not active)
        """
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



class LuckyAICog(commands.Cog):
    """AI-powered roast bot with message sync, TLDR, debate, ask, and hot take features."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.config_file_parent = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")

        self.config = Config.get_conf(self, identifier=1380332243402227772, force_registration=True)
        default_guild = {
            "model": DEFAULT_MODEL,
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

        # Session management with TTL cleanup
        self.settings_sessions: Dict[str, Dict[str, Any]] = {}
        self.setup_sessions: Dict[str, Dict[str, Any]] = {}
        self._session_counter: int = 0
        self._session_cleanup_task: Optional[asyncio.Task] = None
        self.cooldowns = CooldownTracker()

        self.hot_take_enabled = os.getenv("ENABLE_HOT_TAKE", "").lower() == "true"
        self.hot_take_channel_activity: Dict[str, list] = {}
        self.hot_take_cooldowns: Dict[str, float] = {}
        self.hot_take_config = {
            "window_minutes": int(os.getenv("HOT_TAKE_WINDOW_MINUTES", "5")),
            "cooldown_minutes": int(os.getenv("HOT_TAKE_COOLDOWN_MINUTES", "120")),
            "min_messages": int(os.getenv("HOT_TAKE_MIN_MESSAGES", "10")),
            "probability": float(os.getenv("HOT_TAKE_PROBABILITY", "0.05")),
            "context_messages": int(os.getenv("HOT_TAKE_CONTEXT_MESSAGES", "100")),
        }
        raw_hot_take_channels = os.getenv("HOT_TAKE_CHANNELS", "")
        self.hot_take_channels_extra = [c.strip() for c in raw_hot_take_channels.split(",") if c.strip()] if raw_hot_take_channels else []

        self.message_sync_enabled = os.getenv("ENABLE_SYNC", "").lower() == "true"

        # Instantiate helper classes (logic modules, not Cogs)
        self.admin_cmds = AdminCommands(self.bot, self.config, self.db, self.ai_service, self)
        # MessageListener handles message sync and hot-takes
        self.msg_listener = MessageListener(self.bot, self.db, self.config)

    async def cog_load(self) -> None:
        """Initialize cog resources and start background tasks."""
        await self.db.initialize()
        config_path = os.path.join(self.config_file_parent, "config", "config.json")
        ensure_config_json(config_path)
        self._load_models_data()
        persisted = await self.db.get_hot_take_enabled()
        if persisted is not None:
            self.hot_take_enabled = persisted
        self._register_lsettings_cmd()
        
        # Start session cleanup task to prevent memory accumulation
        self._session_cleanup_task = asyncio.create_task(self._cleanup_expired_sessions())
        
        log.info("Lucky AI cog loaded")

    async def cog_unload(self) -> None:
        """Clean up cog resources and cancel background tasks."""
        self.msg_listener.cleanup()
        self._unregister_lsettings_cmd()
        
        # Cancel session cleanup task
        if self._session_cleanup_task and not self._session_cleanup_task.done():
            self._session_cleanup_task.cancel()
            try:
                await self._session_cleanup_task
            except asyncio.CancelledError:
                pass
        
        await self.db.close()
        await self.ai_service.close()

    async def _cleanup_expired_sessions(self) -> None:
        """
        Background task that periodically removes expired sessions.
        
        Sessions are marked as expired if they haven't been accessed in 15 minutes.
        This prevents memory accumulation from abandoned UI interactions.
        """
        SESSION_TIMEOUT_MS = 15 * 60 * 1000  # 15 minutes
        CLEANUP_INTERVAL = 5 * 60  # Check every 5 minutes
        
        while True:
            try:
                await asyncio.sleep(CLEANUP_INTERVAL)
                now = time.time() * 1000
                
                # Clean up settings sessions
                expired_settings = [
                    sid for sid, session in self.settings_sessions.items()
                    if now - session.get("_last_accessed", now) > SESSION_TIMEOUT_MS
                ]
                for sid in expired_settings:
                    del self.settings_sessions[sid]
                    log.debug("Cleaned up expired settings session: %s", sid)
                
                # Clean up setup sessions
                expired_setup = [
                    sid for sid, session in self.setup_sessions.items()
                    if now - session.get("_last_accessed", now) > SESSION_TIMEOUT_MS
                ]
                for sid in expired_setup:
                    del self.setup_sessions[sid]
                    log.debug("Cleaned up expired setup session: %s", sid)
                
                if expired_settings or expired_setup:
                    log.info("Session cleanup: removed %d settings, %d setup sessions", 
                            len(expired_settings), len(expired_setup))
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("Error in session cleanup task: %s", e)

    @commands.Cog.listener()
    async def on_command_error(self, ctx: commands.Context, error: Exception):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send(":x: You don't have the required permissions for that command.")
        elif isinstance(error, commands.BotMissingPermissions):
            missing = ", ".join(error.missing)
            await ctx.send(f":x: I need the `{missing}` permission to do that.")
        elif isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f":hourglass_flowing_sand: Command on cooldown. Try again in {error.retry_after:.1f}s.")
        elif isinstance(error, commands.NotOwner):
            await ctx.send(":x: This command is bot-owner only.")
        elif isinstance(error, commands.MaxConcurrencyReached):
            await ctx.send(":x: This command is already running. Wait for it to finish.")
        elif isinstance(error, commands.MissingRequiredArgument):
            usage = f"Usage: `{ctx.clean_prefix}{ctx.command.name} {ctx.command.signature}`"
            await ctx.send(f":x: Missing `{error.param.name}`. {usage}")
        elif isinstance(error, commands.BadArgument):
            usage = f"Usage: `{ctx.clean_prefix}{ctx.command.name} {ctx.command.signature}`"
            await ctx.send(f":x: {error}\n{usage}")
        elif isinstance(error, commands.UserInputError):
            usage = f"`{ctx.clean_prefix}{ctx.command.name} {ctx.command.signature}`"
            await ctx.send(f":x: {usage}")
        elif isinstance(error, commands.CheckFailure):
            await ctx.send(":x: You don't have permission to use that command.")
        elif isinstance(error, commands.CommandNotFound):
            return
        else:
            log.error("Unhandled error in %s: %s", ctx.command, error)
            await ctx.send(f":x: Something went wrong running `{ctx.command}`.")

    def _load_models_data(self) -> None:
        """Load model metadata from config.json into AI service."""
        config_path = os.path.join(self.config_file_parent, "config", "config.json")
        try:
            with open(config_path, "r") as f:
                data = json.load(f)
            models = data.get("MODELS", {})
            self.ai_service.set_models_data(models)
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    async def _load_personalities(self) -> Dict[str, str]:
        """
        Load personality styles from config.json.
        
        DEVIATION FROM RED BEST PRACTICES:
        Stores personality metadata in external config.json instead of Red's Config API.
        This allows easy editing of personalities without database access and enables
        quick iteration on roast styles without restarting the bot.
        
        Returns:
            Dict mapping personality name to description
        """
        config_path = os.path.join(self.config_file_parent, "config", "config.json")
        loop = asyncio.get_event_loop()

        def _read() -> Dict[str, Any]:
            try:
                with open(config_path, "r") as f:
                    return json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                return {}

        data = await loop.run_in_executor(None, _read)
        return data.get("PERSONALITIES", {})

    def _register_lsettings_cmd(self) -> None:
        """Register /lsettings slash command dynamically."""
        from discord import app_commands
        @self.bot.tree.command(
            name="lsettings",
            description="Open interactive settings UI for model, temperature, API keys, and styles.",
        )
        @app_commands.default_permissions(administrator=True)
        @app_commands.guild_only()
        async def lsettings_cmd(interaction: discord.Interaction) -> None:
            if not interaction.guild:
                await interaction.response.send_message(":x: This command only works in servers.", ephemeral=True)
                return
            is_admin = interaction.user.guild_permissions.administrator
            if not is_admin:
                async with self.config.guild(interaction.guild).all() as cfg:
                    admin_role_id = cfg.get("admin_role")
                    if admin_role_id:
                        role = interaction.guild.get_role(int(admin_role_id))
                        if role and role in interaction.user.roles:
                            is_admin = True
            if not is_admin:
                await interaction.response.send_message(":x: You need administrator permissions.", ephemeral=True)
                return
            session_id = self._create_session(interaction.user.id, str(interaction.guild.id))
            view = SettingsView(self, session_id, interaction.user.id, str(interaction.guild.id))
            embed = await view.build_embed()
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    def _unregister_lsettings_cmd(self) -> None:
        """Unregister /lsettings slash command."""
        self.bot.tree.remove_command("lsettings")

    async def _select_roast_style(self, prompt_key: Optional[str] = None, random_mode: bool = False, 
                                  style_override: Optional[str] = None) -> Dict[str, str]:
        """
        Select a roast style based on personality configuration.
        
        Args:
            prompt_key: Personality name to use
            random_mode: If True, randomly select from available personalities
            style_override: Custom style text to use instead of personality
        
        Returns:
            Dict with 'name' and 'systemPrompt' keys
        """
        if style_override:
            return {"name": "command-override", "systemPrompt": BASE_ROAST_PROMPT + "\n\nStyle guidance: " + style_override[:1000]}
        personalities = await self._load_personalities()
        if personalities:
            keys = list(personalities.keys())
            if random_mode:
                selected = random.choice(keys)
            elif prompt_key and prompt_key in personalities:
                selected = prompt_key
            else:
                selected = keys[0]
            style_text = personalities[selected]
            return {"name": selected, "systemPrompt": BASE_ROAST_PROMPT + "\n\nStyle guidance: " + style_text}
        return {"name": "base", "systemPrompt": BASE_ROAST_PROMPT}

    def _create_session(self, user_id: int, guild_id: str) -> str:
        """
        Create a new UI session for settings or setup.
        
        Args:
            user_id: Discord user ID
            guild_id: Discord guild ID
        
        Returns:
            Session ID string
        """
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
            "_last_accessed": time.time() * 1000,  # For session cleanup
        }
        return session_id

    async def _check_blacklist(self, guild_id: str, user_id: str) -> bool:
        """Check if a user is blacklisted in a guild."""
        return await self.db.is_blacklisted(guild_id, user_id)

    async def _check_opt_out(self, guild_id: str, user_id: str) -> bool:
        """Check if a user has opted out of being roasted in a guild."""
        return await self.db.get_user_opt_out(user_id, guild_id)

    async def _require_admin(self, ctx: commands.Context) -> bool:
        """
        Check if user has admin permissions (server admin or custom admin role).
        
        Args:
            ctx: Command context
        
        Returns:
            True if user is admin, False otherwise
        """
        if ctx.author.guild_permissions.administrator:
            return True
        async with self.config.guild(ctx.guild).all() as cfg:
            admin_role_id = cfg.get("admin_role")
            if admin_role_id:
                role = ctx.guild.get_role(int(admin_role_id))
                if role and role in ctx.author.roles:
                    return True
        return False

    async def _log_command(self, guild_id: int, user_id: int, command: str, success: bool = True) -> None:
        """Log command usage to database."""
        await self.db.log_command_usage(str(guild_id), str(user_id), command, success)

    @staticmethod
    def _extract_ai_response(data):
        """Safely extract text from an OpenAI-compatible chat completion response."""
        if not isinstance(data, dict):
            return ""
        choices = data.get("choices")
        if not choices or not isinstance(choices, list):
            return ""
        first = choices[0]
        if not isinstance(first, dict):
            return ""
        message = first.get("message")
        if not isinstance(message, dict):
            return ""
        return message.get("content") or ""

    def _format_hot_take_context(self, messages):
        msgs = sorted([m for m in messages if m.get("content", "").strip()], key=lambda x: x.get("timestamp", 0))
        lines = []
        for m in msgs[-self.hot_take_config["context_messages"]:]:
            name = m.get("author_tag") or m.get("author_id", "Unknown")
            lines.append(f"[{name}]: {m.get('content', '')}")
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

        # Prune stale channel activity entries occasionally
        if random.random() < 0.01:
            self.hot_take_channel_activity = {
                k: v for k, v in self.hot_take_channel_activity.items()
                if v and (now - v[-1]) < window_ms * 2
            }

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
                model = cfg.get("model", DEFAULT_MODEL)
            payload = {
                "messages": [{"role": "user", "content": HOT_TAKE_PROMPT.replace("{conversation}", conversation)}],
                "temperature": 0.9,
                "max_tokens": 500,
            }
            resp = await self.ai_service.execute_request(payload, model, context="HOT_TAKE", timeout=60)
            text = sanitize_output(self._extract_ai_response(resp))
            if text:
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
        except (discord.Forbidden, discord.HTTPException, AttributeError):
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

        # Check if bot is enabled in this guild
        async with self.config.guild(message.guild).all() as cfg:
            enabled = cfg.get("enabled", True)
            sync_channels = cfg.get("sync_channels", [])

        if not enabled:
            return

        # Skip command messages from being saved to history
        content = message.content.strip()
        is_command = False
        if content:
            prefixes = await self.bot.get_valid_prefixes(message.guild)
            for p in prefixes:
                if p and content.startswith(p):
                    is_command = True
                    break
            if not is_command:
                parts = content.split(maxsplit=1)
                if parts and parts[0].lower() in _UNPREFIXED_COMMANDS:
                    is_command = True

        if self.message_sync_enabled and sync_channels and channel_id in sync_channels and not is_command:
            if not await self._check_opt_out(guild_id, author_id):
                msg = {
                    "id": str(message.id),
                    "author": {"id": author_id, "tag": str(message.author), "name": message.author.name},
                    "channel": {"id": channel_id},
                    "content": message.content,
                    "timestamp": int(message.created_at.timestamp() * 1000),
                    "guild_id": guild_id,
                }
                await self.db.save_message(msg)

        await self._maybe_fire_hot_take(message)

        await self._try_unprefixed_command(message)

    async def _try_unprefixed_command(self, message: discord.Message) -> None:
        """
        Handle unprefixed commands (e.g., "lroast" instead of "[p]lroast").
        
        DEVIATION FROM RED BEST PRACTICES:
        Red's standard pattern requires a prefix for all commands. This implementation
        allows unprefixed commands for convenience, enabling users to type:
        - "lroast @user" instead of "[p]lroast @user"
        - "ltldr 50" instead of "[p]ltldr 50"
        
        This is intentional to improve UX for frequently-used commands. The implementation:
        - Only applies to commands in _UNPREFIXED_COMMANDS set
        - Skips messages that already have a prefix (no double-processing)
        - Respects the cog's command namespace (only this cog's commands)
        
        Args:
            message: Discord message to check
        """
        content = message.content.strip()
        if not content:
            return

        parts = content.split(maxsplit=1)
        if not parts:
            return
        cmd_name = parts[0].lower()

        if cmd_name not in _UNPREFIXED_COMMANDS:
            return

        # Skip if message already has a prefix (avoid double-processing)
        prefixes = await self.bot.get_valid_prefixes(message.guild)
        for p in prefixes:
            if p and content.startswith(p):
                return

        cmd = self.bot.get_command(cmd_name)
        if cmd is None or cmd.cog is not self:
            return

        ctx = await self.bot.get_context(message)
        ctx.prefix = ""
        ctx.command = cmd
        ctx.invoked_with = cmd_name
        ctx.valid = True

        rest = content[len(cmd_name):].lstrip()
        ctx.view = StringView(rest)
        ctx.args = [ctx]
        ctx.kwargs = {}

        await self.bot.invoke(ctx)

    @commands.command(name="lroast")
    async def lroast(self, ctx: commands.Context, user: discord.User = None, *, style: str = None) -> None:
        """Roast a user based on their message history. Defaults to yourself if no user specified."""
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

        if await self._check_opt_out(guild_id, user_id):
            await ctx.send(f":x: <@{user_id}> has opted out of being roasted.")
            return

        async with self.config.guild(ctx.guild).all() as cfg:
            typing_enabled = cfg.get("typing_enabled", True)
            model = cfg.get("model", DEFAULT_MODEL)
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

            resp = await self.ai_service.execute_request(payload, model, context="ROAST")
            text = sanitize_output(self._extract_ai_response(resp)) or f"I was going to roast {target.name}, but even my AI has standards. Nice try though!"
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

    @commands.command(name="ltldr", aliases=["lgreentext"])
    async def ltldr(self, ctx: commands.Context, messages: int = 200, style: str = "normal"):
        """Summarize recent chat as a TL;DR. Style can be "normal" or "greentext"."""
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
            model = cfg.get("model", DEFAULT_MODEL)
            typing_enabled = cfg.get("typing_enabled", True)
        if typing_enabled:
            await self._handle_typing(ctx.channel)
        try:
            raw_messages = await self.db.get_channel_messages(str(ctx.channel.id), messages)
            raw_messages = [m for m in raw_messages if m.get("id") != str(ctx.message.id)]
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
            text = sanitize_output(self._extract_ai_response(resp))
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
        """Opt in or out of being roasted. Use "in" to opt in, "out" to opt out."""
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

    @commands.group(name="lconfig", aliases=["lcfg"])
    @checks.admin_or_permissions(administrator=True)
    async def lconfig(self, ctx: commands.Context):
        """Manage sync channels, blacklist, admin role, toggle, and backfill. Use subcommands for details."""

    @lconfig.group(name="channels")
    async def lconfig_channels(self, ctx: commands.Context):
        """Manage message sync channels. Use add/remove/list subcommands."""

    @lconfig_channels.command(name="add")
    async def lconfig_channels_add(self, ctx: commands.Context, channel: discord.TextChannel):
        """Add a channel for message syncing. Starts backfill on the last 14 days."""
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
                await ctx.send(f":x: Channel limit reached (max 45).")
                return
            sync.append(channel_id)
            cfg["sync_channels"] = sync
        await self.db.update_sync_status(guild_id, channel_id)
        await self.db.log_sync_operation(guild_id, channel_id, "channel_add", message_count=0, triggered_by=str(ctx.author.id))
        await ctx.send(f":white_check_mark: Added {channel.mention} as a sync channel. Starting initial backfill...")
        task = asyncio.create_task(self._do_backfill(ctx.guild, channel, 14, ctx.author))
        task.add_done_callback(lambda t: log.error("BACKFILL task failed: %s", t.exception()) if t.exception() else None)

    @lconfig_channels.command(name="remove")
    async def lconfig_channels_remove(self, ctx: commands.Context, channel: discord.TextChannel):
        """Remove a sync channel and delete its stored messages."""
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
        """List all channels currently configured for message syncing."""
        if not ctx.guild:
            return
        async with self.config.guild(ctx.guild).all() as cfg:
            sync = cfg.get("sync_channels", [])
        if not sync:
            await ctx.send(":x: No sync channels configured.")
            return
        lines = [f"- <#{ch}>" for ch in sync]
        embed = discord.Embed(title=":clipboard: Sync Channels", color=0x0099ff, description="\n".join(lines))
        await ctx.send(embed=embed)

    @lconfig.group(name="blacklist")
    async def lconfig_blacklist(self, ctx: commands.Context):
        """Manage blacklisted users. Use add/remove/list subcommands."""

    @lconfig_blacklist.command(name="add")
    async def lconfig_blacklist_add(self, ctx: commands.Context, user: discord.User):
        """Add a user to the blacklist (prevents them from being roasted)."""
        if not ctx.guild:
            return
        await self.db.add_to_blacklist(str(ctx.guild.id), str(user.id), str(ctx.author.id))
        await ctx.send(f":white_check_mark: Added {user.mention} to the blacklist.")

    @lconfig_blacklist.command(name="remove")
    async def lconfig_blacklist_remove(self, ctx: commands.Context, user: discord.User):
        """Remove a user from the blacklist."""
        if not ctx.guild:
            return
        await self.db.remove_from_blacklist(str(ctx.guild.id), str(user.id))
        await ctx.send(f":white_check_mark: Removed {user.mention} from the blacklist.")

    @lconfig_blacklist.command(name="list")
    async def lconfig_blacklist_list(self, ctx: commands.Context):
        """List all blacklisted users in this server."""
        if not ctx.guild:
            return
        entries = await self.db.get_blacklist(str(ctx.guild.id))
        if not entries:
            await ctx.send(":x: No blacklisted users.")
            return
        lines = [f"- <@{e['user_id']}>{' - ' + e['reason'] if e.get('reason') else ''}" for e in entries]
        embed = discord.Embed(title=":clipboard: Blacklisted Users", color=0x0099ff, description="\n".join(lines))
        await ctx.send(embed=embed)

    @lconfig.command(name="admin_role")
    async def lconfig_admin_role(self, ctx: commands.Context, role: discord.Role = None):
        """Set the admin role that can use admin commands. Leave empty to restrict to server admins."""
        if not ctx.guild:
            return
        async with self.config.guild(ctx.guild).all() as cfg:
            cfg["admin_role"] = str(role.id) if role else None
        text = role.mention if role else "server administrators only"
        await ctx.send(f":white_check_mark: Admin role set to {text}.")

    @lconfig.command(name="toggle")
    async def lconfig_toggle(self, ctx: commands.Context, enabled: bool):
        """Enable or disable the bot for this server. Use true/false."""
        if not ctx.guild:
            return
        async with self.config.guild(ctx.guild).all() as cfg:
            cfg["enabled"] = enabled
        await ctx.send(f":white_check_mark: Bot {'enabled' if enabled else 'disabled'} for this server.")

    @lconfig.command(name="backfill")
    async def lconfig_backfill(self, ctx: commands.Context, channel: discord.TextChannel = None, days: int = None):
        """Backfill message history from a channel. Defaults to current channel, all history if days omitted."""
        if not ctx.guild:
            return
        channel = channel or ctx.channel
        await ctx.send(f":arrows_counterclockwise: Starting {f'{days}-day' if days else 'full'} backfill of {channel.mention}...")
        task = asyncio.create_task(self._do_backfill(ctx.guild, channel, days, ctx.author))
        task.add_done_callback(
            lambda t: asyncio.create_task(
                ctx.send(f":white_check_mark: Backfill of {channel.mention} completed.")
            ) if not t.exception() else log.error("BACKFILL task failed: %s", t.exception())
        )

    async def _do_backfill(self, guild, channel, days, author):
        from datetime import timedelta
        guild_id = str(guild.id)
        channel_id = str(channel.id)
        after = discord.utils.utcnow() - timedelta(days=days) if days else None
        total_synced = 0
        try:
            async for msg in channel.history(limit=None, after=after, oldest_first=True):
                if msg.author.bot:
                    continue
                author_id = str(msg.author.id)
                if await self._check_opt_out(guild_id, author_id):
                    continue
                m = {
                    "id": str(msg.id),
                    "author": {"id": author_id, "tag": str(msg.author), "name": msg.author.name},
                    "channel": {"id": channel_id},
                    "content": msg.content,
                    "timestamp": int(msg.created_at.timestamp() * 1000),
                    "guild_id": guild_id,
                }
                await self.db.save_message(m)
                total_synced += 1
            await self.db.update_sync_status(guild_id, channel_id)
            await self.db.log_sync_operation(guild_id, channel_id, "backfill", message_count=total_synced, triggered_by=str(author.id))
            log.info("BACKFILL Complete for %s: %d messages", channel_id, total_synced)
        except Exception as e:
            log.error("BACKFILL Error for %s: %s", channel_id, e)

    @commands.command(name="lstats")
    @checks.admin_or_permissions(administrator=True)
    async def lstats(self, ctx: commands.Context):
        """View bot statistics, database size, uptime, and command usage."""
        if not ctx.guild:
            return
        try:
            stats_data = await self.admin_cmds.build_stats(ctx)
            embed = discord.Embed(color=stats_data["color"], title=stats_data["title"])
            for field in stats_data["fields"]:
                embed.add_field(name=field["name"], value=field["value"], inline=field.get("inline", True))
            embed.timestamp = discord.utils.utcnow()
            await ctx.send(embed=embed)
        except Exception as e:
            log.error("STATS Error: %s", e)
            await ctx.send(":x: Failed to retrieve statistics.")

    @commands.command(name="lsetup")
    @checks.admin_or_permissions(administrator=True)
    async def lsetup(self, ctx: commands.Context) -> None:
        """Run the interactive setup wizard to configure API keys and default model."""
        if not ctx.guild:
            await ctx.send(":x: This command only works in servers.")
            return

        already_configured = []
        for provider in PROVIDER_ORDER:
            tokens = await self.bot.get_shared_api_tokens(provider)
            from_red = tokens.get("api_key", "") if tokens else ""
            if from_red:
                already_configured.append(provider)

        if already_configured:
            await ctx.send(
                ":warning: This server already has API keys configured.\n"
                "Running setup again will overwrite existing keys.\n"
                "Use `/lsettings` to change individual keys instead."
            )

        config_path = os.path.join(self.config_file_parent, "config", "config.json")
        ensure_config_json(config_path)

        self._session_counter += 1
        session_id = str(self._session_counter)
        api_keys = {}
        for provider in PROVIDER_ORDER:
            tokens = await self.bot.get_shared_api_tokens(provider)
            existing = tokens.get("api_key", "") if tokens else ""
            if existing:
                api_keys[provider] = existing

        self.setup_sessions[session_id] = {
            "current_step": 0,
            "api_keys": api_keys,
            "configured_count": len(api_keys),
            "skipped_count": 0,
            "default_model": DEFAULT_MODEL,
            "sync_channel_id": None,
            "finished": False,
            "test_results": [],
            "prefix": ctx.clean_prefix,
            "_last_accessed": time.time() * 1000,
        }

        view = SetupView(self, session_id, ctx.author.id, ctx.guild.id)
        embed = await view.build_embed()
        await ctx.send(embed=embed, view=view)

    @commands.command(name="lhelp", aliases=["lcommands"])
    async def lhelp(self, ctx: commands.Context):
        """Show the list of available commands."""
        p = ctx.clean_prefix
        embed = discord.Embed(color=0x0099ff, title="\U0001f916 Lucky AI - Commands", description=f"AI-powered roasts, TL;DRs, and more. All commands use the `{p}` prefix.")
        embed.add_field(name=f"\U0001f3af `{p}lroast @user`", value='Generate an AI roast based on message history.\nOptions: `style`: optional override', inline=False)
        embed.add_field(name=f"\U0001f9e0 `{p}ltldr [count] [style]`", value="Summarize the last N messages (10-500).\nStyle: `normal` or `greentext`", inline=False)
        embed.add_field(name=f":no_entry_sign: `{p}loptout <in|out>`", value="Opt in or out of being roasted.", inline=False)
        embed.add_field(name=f":wrench: `/lsettings` (Admin)", value="Interactive UI for model, temperature, API keys, and styles. (Slash command)", inline=False)
        embed.add_field(name=f":gear: `{p}lconfig` (Admin)", value="Manage sync channels, blacklist, admin role, toggle, backfill.\nSubcommands: `channels add/remove/list`, `blacklist add/remove/list`, `admin_role`, `toggle`, `backfill`", inline=False)
        embed.add_field(name=f"\U0001f4ca `{p}lstats` (Admin)", value="View bot statistics and health.", inline=False)
        embed.add_field(name=f"\U0001f680 `{p}lsetup` (Admin)", value="Interactive setup wizard for API keys and configuration.", inline=False)
        embed.add_field(name=f":question: `{p}lhelp`", value="Show this help message.", inline=False)
        embed.add_field(name="More Commands", value=f"`{p}lask <question>` - Chat with AI\n`{p}ldebate` - Judge a debate\n`{p}lhtt on/off/fire` - Manage hot takes\n`{p}ltypeon`/`{p}ltypeoff` - Toggle typing indicator", inline=False)
        embed.set_footer(text="Lucky AI - Powered by AI")
        embed.timestamp = discord.utils.utcnow()
        await ctx.send(embed=embed)

    @commands.command(name="lask")
    async def lask(self, ctx: commands.Context, *, question: str = None):
        """Ask the AI a question with chat context. Attach an image for vision support."""
        if not ctx.guild:
            await ctx.send(":x: This command only works in servers.")
            return
        if not question and not ctx.message.attachments:
            await ctx.send(f"Usage: `{ctx.clean_prefix}lask <question>` or attach an image")
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
            ctx_msgs = [m for m in ctx_msgs if m.get("id") != str(ctx.message.id)]
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
                session = await self.ai_service._get_session()
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
            text = sanitize_output(self._extract_ai_response(resp))
            if not text:
                await ctx.send(":x: AI returned an empty response.")
                await self._log_command(guild_id, author_id, "ask", False)
                return
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
        """Analyze recent chat as a debate and judge who won."""
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
            msgs = [m for m in msgs if m.get("id") != str(ctx.message.id)]
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
            text = self._extract_ai_response(resp)
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
        """Manage hot takes. Use "on", "off", or "fire" to manually trigger one."""
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
                    model = cfg.get("model", DEFAULT_MODEL)
                payload = {
                    "messages": [{"role": "user", "content": HOT_TAKE_PROMPT.replace("{conversation}", conversation)}],
                    "temperature": 0.9,
                    "max_tokens": 500,
                }
                resp = await self.ai_service.execute_request(payload, model, context="HOT_TAKE")
                text = sanitize_output(self._extract_ai_response(resp))
                if text:
                    await ctx.send(text)
                    self.hot_take_cooldowns[channel_id] = time.time() * 1000
                    await self.db.log_hot_take(guild_id, channel_id, text, len(ctx_msgs), model, 0)
                else:
                    await ctx.send(":x: AI returned empty response.")
            except Exception as e:
                log.error("HOT_TAKE Error: %s", e)
                await ctx.send(f":x: Failed to generate hot take.")
        else:
            await ctx.send(f"Usage: `{ctx.clean_prefix}lhtt on` / `{ctx.clean_prefix}lhtt off` / `{ctx.clean_prefix}lhtt fire`")

    @commands.command(name="ltypeon")
    @checks.admin_or_permissions(administrator=True)
    async def ltypeon(self, ctx: commands.Context):
        """Enable the typing indicator for AI commands."""
        if not ctx.guild:
            return
        async with self.config.guild(ctx.guild).all() as cfg:
            cfg["typing_enabled"] = True
        await ctx.send(":white_check_mark: Typing indicator ON")

    @commands.command(name="ltypeoff")
    @checks.admin_or_permissions(administrator=True)
    async def ltypeoff(self, ctx: commands.Context):
        """Disable the typing indicator for AI commands."""
        if not ctx.guild:
            return
        async with self.config.guild(ctx.guild).all() as cfg:
            cfg["typing_enabled"] = False
        await ctx.send("Typing indicator OFF")
