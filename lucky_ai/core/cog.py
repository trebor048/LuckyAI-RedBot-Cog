import os
import json
import time
import math
import asyncio
import logging
import random
import base64
import mimetypes
import shutil
from pathlib import Path
from typing import Dict, Optional, Any

import aiohttp
import discord
from discord.ext.commands.view import StringView
from redbot.core import commands, Config
from redbot.core.data_manager import cog_data_path

from ..providers import PROVIDER_ORDER, PROVIDERS
from ..utils import (
    BASE_ROAST_PROMPT, TLDR_SYSTEM_PROMPT, GREENTEXT_SYSTEM_PROMPT,
    ASK_SYSTEM_PROMPT, HOT_TAKE_PROMPT,
    COLORS, sanitize_output,
    normalize_string_iterable,
    format_messages_for_roast, format_messages_for_tldr,
)

from ..database.manager import MessageDB
from .service import AIService
from .ask import build_ask_context, find_image_attachments, parse_ask_flags
from .backfill import backfill_task_key, format_backfill_status
from .command_utils import find_suggestions, suggest_choice
from ..ui.settings import SettingsView
from ..commands.admin import AdminCommands
from ..commands.setup import SetupView, ensure_config_json_async

log = logging.getLogger("red.lucky_ai")

TLDR_MIN_MESSAGES = 10
MAX_MESSAGE_COUNT = 500

COOLDOWN_PRUNE_INTERVAL = 60000


def lucky_admin():
    """Allow bot owners, server administrators, and the configured Lucky AI admin role."""
    async def predicate(ctx: commands.Context) -> bool:
        cog = ctx.cog
        return bool(cog and hasattr(cog, "_require_admin") and await cog._require_admin(ctx))

    return commands.check(predicate)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


_SAFE_MENTIONS = discord.AllowedMentions(users=False, everyone=False, roles=False)
_NO_PING_MENTIONS = discord.AllowedMentions(users=False, everyone=False, roles=False, replied_user=False)

_UNPREFIXED_COMMANDS = {
    "lroast", "ltldr", "lgreentext", "lask", "loptout",
    "lsetup", "lsettings", "lconfig", "lcfg", "lstats", "lhelp", "lcommands", "lhtt",
}

_FIRST_PROVIDER = PROVIDER_ORDER[0]
DEFAULT_MODEL = PROVIDERS[_FIRST_PROVIDER]["default_model"]


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

    def remove(self, user_id: str, command_key: str = "default") -> None:
        """Remove an active cooldown entry (used when a command fails)."""
        self._prune()
        key = f"{user_id}:{command_key}"
        self._cooldowns.pop(key, None)



class _SyncConfirmView(discord.ui.View):
    """Confirmation dialog shown when adding a sync channel while sync is disabled."""

    def __init__(self, cog, ctx, channel):
        super().__init__(timeout=60)
        self.cog = cog
        self.ctx = ctx
        self.channel = channel

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(":x: Only the command author can use this.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Enable Sync & Add Channel", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog._set_message_sync_enabled(True)
        status_text = ":white_check_mark: Sync enabled. Adding channel..."
        if not self.cog.message_sync_enabled:
            status_text = (
                ":warning: Sync was saved, but `ENABLE_SYNC` is currently preventing live message storage. "
                "Adding channel..."
            )
        await interaction.response.edit_message(
            content=status_text,
            embed=None,
            view=None,
        )
        guild_id = str(self.ctx.guild.id)
        channel_id = str(self.channel.id)
        await self.cog._do_add_sync_channel(self.ctx, self.channel, guild_id, channel_id)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content=":x: Cancelled.",
            embed=None,
            view=None,
        )


class LuckyAICogBase(commands.Cog):
    """AI-powered roast bot with message sync, TLDR, ask, and hot take features."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.data_path = cog_data_path(self)
        self.data_path.mkdir(parents=True, exist_ok=True)
        self.config_json_path = str(self.data_path / "config.json")
        self._legacy_package_path = Path(__file__).resolve().parents[1]
        self._legacy_repo_path = Path(__file__).resolve().parents[2]

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
            "custom_personalities": {},
            "messageFetchMode": "random",
            "randomMode": False,
            "enabled": True,
            "admin_role": None,
            "sync_channels": [],
            "provider_order": None,
            "provider_learning": {},
            "hot_take_enabled": os.getenv("ENABLE_HOT_TAKE", "").lower() == "true",
            "ask_model": None,
            "ask_vision_model": None,
            "hot_take_window_minutes": _env_int("HOT_TAKE_WINDOW_MINUTES", 5),
            "hot_take_cooldown_minutes": _env_int("HOT_TAKE_COOLDOWN_MINUTES", 120),
            "hot_take_min_messages": _env_int("HOT_TAKE_MIN_MESSAGES", 10),
            "hot_take_probability": _env_float("HOT_TAKE_PROBABILITY", 0.05),
            "hot_take_context_messages": _env_int("HOT_TAKE_CONTEXT_MESSAGES", 100),
        }
        self.config.register_guild(**default_guild)
        self.config.register_global(
            ask_model=DEFAULT_MODEL,
            ask_vision_model=DEFAULT_MODEL,
            sync_enabled=False,
        )

        configured_db_path = os.getenv("DB_FILE")
        db_path = configured_db_path or str(self.data_path / "messages.db")
        self._uses_default_db_path = not configured_db_path
        self.db = MessageDB(db_path)
        self.ai_service = AIService(self.bot, self.config)

        # Session management with TTL cleanup
        self.settings_sessions: Dict[str, Dict[str, Any]] = {}
        self.setup_sessions: Dict[str, Dict[str, Any]] = {}
        self._session_counter: int = 0
        self._session_cleanup_task: Optional[asyncio.Task] = None
        self._lsettings_app_command = None
        self.cooldowns = CooldownTracker()

        self.hot_take_enabled = os.getenv("ENABLE_HOT_TAKE", "").lower() == "true"
        self.hot_take_channel_activity: Dict[str, list] = {}
        self.hot_take_cooldowns: Dict[str, float] = {}
        self.backfill_tasks: Dict[str, asyncio.Task] = {}
        self.backfill_progress: Dict[str, Dict[str, Any]] = {}
        self.hot_take_config = {
            "window_minutes": _env_int("HOT_TAKE_WINDOW_MINUTES", 5),
            "cooldown_minutes": _env_int("HOT_TAKE_COOLDOWN_MINUTES", 120),
            "min_messages": _env_int("HOT_TAKE_MIN_MESSAGES", 10),
            "probability": _env_float("HOT_TAKE_PROBABILITY", 0.05),
            "context_messages": _env_int("HOT_TAKE_CONTEXT_MESSAGES", 100),
        }
        raw_hot_take_channels = os.getenv("HOT_TAKE_CHANNELS", "")
        self.hot_take_channels_extra = [c.strip() for c in raw_hot_take_channels.split(",") if c.strip()] if raw_hot_take_channels else []

        self.message_sync_enabled = False  # Will be loaded from config in cog_load; env var takes precedence

        self.admin_cmds = AdminCommands(self.bot, self.config, self.db, self.ai_service, self)

    def _migrate_legacy_runtime_files(self) -> None:
        """Copy legacy package-adjacent runtime files into Red's per-cog data directory."""
        migrations = [(self._legacy_repo_path / "config" / "config.json", Path(self.config_json_path))]
        if self._uses_default_db_path:
            migrations.append((self._legacy_package_path / "messages.db", Path(self.db.db_path)))
        for source, target in migrations:
            if target.exists() or not source.is_file():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            log.info("Migrated legacy runtime data from %s to %s", source, target)

    async def cog_load(self) -> None:
        """Initialize cog resources and start background tasks."""
        await asyncio.get_running_loop().run_in_executor(None, self._migrate_legacy_runtime_files)
        await self.db.initialize()
        await ensure_config_json_async(self.config_json_path)
        self._load_models_data()
        # Load persisted sync_enabled; env var overrides for backwards compatibility
        raw_sync = os.getenv("ENABLE_SYNC")
        cfg_sync = await self.config.sync_enabled()
        self.message_sync_enabled = cfg_sync if raw_sync is None or raw_sync == "" else raw_sync.lower() == "true"
        self._register_lsettings_cmd()
        self.ai_service.start_health_monitor()
        
        # Start session cleanup task to prevent memory accumulation
        self._session_cleanup_task = asyncio.create_task(self._cleanup_expired_sessions())
        
        log.info("Lucky AI cog loaded")

    async def cog_unload(self) -> None:
        """Clean up cog resources and cancel background tasks."""
        self._unregister_lsettings_cmd()
        pending_backfills = []
        for task in list(self.backfill_tasks.values()):
            if task and not task.done():
                task.cancel()
                pending_backfills.append(task)
        if pending_backfills:
            await asyncio.gather(*pending_backfills, return_exceptions=True)
        self.backfill_tasks.clear()
        
        # Cancel session cleanup task
        if self._session_cleanup_task and not self._session_cleanup_task.done():
            self._session_cleanup_task.cancel()
            try:
                await self._session_cleanup_task
            except asyncio.CancelledError:
                pass
        
        await self.db.close()
        await self.ai_service.close()

    async def red_delete_data_for_user(self, *, requester, user_id: int) -> None:
        """Delete all SQLite data directly associated with a Discord user."""
        await self.db.delete_user_data(user_id)

    async def _is_disabled_in_guild(self, guild: discord.Guild) -> bool:
        """Return Red's core cog-disable state without breaking older compatible Red versions."""
        check = getattr(self.bot, "cog_disabled_in_guild", None)
        if not callable(check):
            return False
        try:
            return bool(await check(self, guild))
        except Exception as e:
            log.warning("Could not read Red cog-disable state for guild %s: %s", guild.id, e)
            return False

    async def _set_message_sync_enabled(self, enabled: bool) -> bool:
        """Persist live-sync state while honoring an explicit legacy environment override."""
        await self.config.sync_enabled.set(bool(enabled))
        raw_sync = os.getenv("ENABLE_SYNC")
        self.message_sync_enabled = bool(enabled) if raw_sync is None or raw_sync == "" else raw_sync.lower() == "true"
        return self.message_sync_enabled

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
                    if now - session.get("_last_accessed", 0) > SESSION_TIMEOUT_MS
                ]
                for sid in expired_settings:
                    del self.settings_sessions[sid]
                    log.debug("Cleaned up expired settings session: %s", sid)
                
                # Clean up setup sessions
                expired_setup = [
                    sid for sid, session in self.setup_sessions.items()
                    if now - session.get("_last_accessed", 0) > SESSION_TIMEOUT_MS
                ]
                for sid in expired_setup:
                    del self.setup_sessions[sid]
                    log.debug("Cleaned up expired setup session: %s", sid)

                # Prune stale backfill progress entries (completed/failed/cancelled older than 2 hours)
                stale_keys = []
                for key, info in self.backfill_progress.items():
                    status = str(info.get("status", ""))
                    finished_at = info.get("finished_at")
                    if status in {"completed", "failed", "cancelled"} and isinstance(finished_at, (int, float)):
                        if (time.time() - float(finished_at)) > 7200:
                            stale_keys.append(key)
                for key in stale_keys:
                    self.backfill_progress.pop(key, None)
                
                if expired_settings or expired_setup:
                    log.info("Session cleanup: removed %d settings, %d setup sessions",
                            len(expired_settings), len(expired_setup))

                # Deterministic pruning of stale hot-take channel activity (complements 1% probabilistic)
                stale_hot_take_channels = [
                    ch for ch, activity in self.hot_take_channel_activity.items()
                    if not activity or (now - activity[-1]) > 3_600_000  # 1 hour
                ]
                for ch in stale_hot_take_channels:
                    self.hot_take_channel_activity.pop(ch, None)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("Error in session cleanup task: %s", e)

    @commands.Cog.listener()
    async def on_command_error(self, ctx: commands.Context, error: Exception):
        if isinstance(error, commands.CommandNotFound):
            unknown = str(error).partition('"')[2].partition('"')[0]
            if unknown.lower().startswith("l"):
                await self._suggest_command(ctx.message, unknown, prefix=ctx.clean_prefix)
            return
        if not ctx.command or ctx.command.cog is not self:
            return
        command_name = ctx.command.qualified_name
        signature = ctx.command.signature
        usage = f"`{ctx.clean_prefix}{command_name}{f' {signature}' if signature else ''}`"
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
            await ctx.send(f":x: Missing `{error.param.name}`. Usage: {usage}")
        elif isinstance(error, commands.BadArgument):
            await ctx.send(f":x: {error}\nUsage: {usage}")
        elif isinstance(error, commands.UserInputError):
            await ctx.send(f":x: Usage: {usage}")
        elif isinstance(error, commands.CheckFailure):
            await ctx.send(":x: You don't have permission to use that command.")
        else:
            log.error("Unhandled error in %s: %s", command_name, error)
            await ctx.send(f":x: Something went wrong running `{command_name}`.")

    def _load_models_data(self) -> None:
        """Load model metadata from config.json into AI service."""
        try:
            with open(self.config_json_path, "r") as f:
                data = json.load(f)
            models = data.get("MODELS", {})
            self.ai_service.set_models_data(models)
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    async def _load_personalities(self, guild: Optional[discord.Guild] = None) -> Dict[str, str]:
        """
        Load personality styles from the built-in defaults, legacy config.json data, and guild settings.
        
        Returns:
            Dict mapping personality name to description
        """
        personalities: Dict[str, str] = dict(DEFAULT_PERSONALITIES)
        loop = asyncio.get_running_loop()

        def _read() -> Dict[str, Any]:
            try:
                with open(self.config_json_path, "r") as f:
                    return json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                return {}

        data = await loop.run_in_executor(None, _read)
        legacy_personalities = data.get("PERSONALITIES", {})
        if isinstance(legacy_personalities, dict):
            for key, value in legacy_personalities.items():
                style_name = str(key).strip()
                style_text = str(value).strip() if value is not None else ""
                if not style_name or style_name in personalities or not style_text:
                    continue
                personalities[style_name] = style_text

        if guild is not None:
            async with self.config.guild(guild).all() as cfg:
                custom_personalities = cfg.get("custom_personalities", {})
            if isinstance(custom_personalities, dict):
                for key, value in custom_personalities.items():
                    style_name = str(key).strip()
                    style_text = str(value).strip() if value is not None else ""
                    if not style_name or style_name in DEFAULT_PERSONALITIES or not style_text:
                        continue
                    personalities[style_name] = style_text

        return personalities

    def _register_lsettings_cmd(self) -> None:
        """Register /lsettings slash command dynamically."""
        from discord import app_commands

        @app_commands.command(
            name="lsettings",
            description="Open interactive settings UI for model, temperature, API keys, and styles.",
        )
        @app_commands.guild_only()
        async def lsettings_cmd(interaction: discord.Interaction) -> None:
            if not interaction.guild:
                await interaction.response.send_message(":x: This command only works in servers.", ephemeral=True)
                return
            if await self._is_disabled_in_guild(interaction.guild):
                await interaction.response.send_message(":x: Lucky AI is disabled in this server.", ephemeral=True)
                return
            is_admin = await self.bot.is_owner(interaction.user) or interaction.user.guild_permissions.administrator
            if not is_admin:
                async with self.config.guild(interaction.guild).all() as cfg:
                    admin_role_id = cfg.get("admin_role")
                    if admin_role_id:
                        try:
                            role = interaction.guild.get_role(int(admin_role_id))
                        except (TypeError, ValueError):
                            role = None
                        if role and role in interaction.user.roles:
                            is_admin = True
            if not is_admin:
                await interaction.response.send_message(":x: You need administrator permissions.", ephemeral=True)
                return
            embed, view = await self._build_settings_panel(interaction.user.id, interaction.guild)
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

        try:
            self.bot.tree.add_command(lsettings_cmd)
        except app_commands.CommandAlreadyRegistered:
            log.warning("Could not register /lsettings because another application command already uses that name")
            return
        self._lsettings_app_command = lsettings_cmd

    def _unregister_lsettings_cmd(self) -> None:
        """Unregister /lsettings slash command."""
        if self._lsettings_app_command is None:
            return
        self.bot.tree.remove_command(self._lsettings_app_command.name)
        self._lsettings_app_command = None

    async def _build_settings_panel(self, user_id: int, guild: discord.Guild):
        """Create a fresh settings session and render its initial panel."""
        session_id = self._create_session(user_id, guild.id)
        personalities = await self._load_personalities(guild)
        async with self.config.guild(guild).all() as cfg:
            self.settings_sessions[session_id]["personalities"] = personalities
            self.settings_sessions[session_id]["working_order"] = (
                self._normalize_provider_order(cfg.get("provider_order")) or list(PROVIDER_ORDER)
            )
        view = SettingsView(self, session_id, user_id, guild.id)
        return await view.build_embed(), view

    async def _select_roast_style(self, prompt_key: Optional[str] = None, random_mode: bool = False, 
                                  style_override: Optional[str] = None, guild: Optional[discord.Guild] = None) -> Dict[str, str]:
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
        personalities = await self._load_personalities(guild)
        if personalities:
            keys = list(personalities.keys())
            if random_mode:
                selected = random.choice(keys)
            elif prompt_key and prompt_key in personalities:
                selected = prompt_key
            else:
                selected = keys[0]
            style_text = personalities.get(selected) or ""
            return {"name": selected, "systemPrompt": BASE_ROAST_PROMPT + "\n\nStyle guidance: " + style_text}
        return {"name": "base", "systemPrompt": BASE_ROAST_PROMPT}

    def _create_session(self, user_id: int, guild_id: int) -> str:
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
            "_last_accessed": time.time() * 1000,
        }
        return session_id

    async def _check_blacklist(self, guild_id: str, user_id: str) -> bool:
        """Check if a user is blacklisted in a guild."""
        return await self.db.is_blacklisted(guild_id, user_id)

    async def _check_opt_out(self, guild_id: str, user_id: str) -> bool:
        """Check if a user has opted out of stored-message AI analysis in a guild."""
        return await self.db.get_user_opt_out(user_id, guild_id)

    @staticmethod
    def _normalize_provider_order(order: Any) -> Optional[list]:
        """Validate and normalize a provider order list."""
        if not isinstance(order, list):
            return None
        valid = []
        seen = set()
        for provider in order:
            if provider in PROVIDERS and provider not in seen:
                valid.append(provider)
                seen.add(provider)
        if not valid:
            return None
        for p in PROVIDER_ORDER:
            if p not in valid:
                valid.append(p)
        return valid

    async def _get_guild_provider_order(self, guild: discord.Guild) -> Optional[list]:
        """Get guild-specific provider order, normalized for fallback execution."""
        async with self.config.guild(guild).all() as cfg:
            return self._normalize_provider_order(cfg.get("provider_order"))

    async def _get_hot_take_runtime_cfg(self, guild: discord.Guild) -> Dict[str, Any]:
        """Build effective hot-take settings with per-guild overrides."""
        def _as_int(value: Any, default: int, min_v: int, max_v: int) -> int:
            try:
                val = int(value)
            except (TypeError, ValueError):
                return default
            return max(min_v, min(max_v, val))

        def _as_float(value: Any, default: float, min_v: float, max_v: float) -> float:
            try:
                val = float(value)
            except (TypeError, ValueError):
                return default
            return max(min_v, min(max_v, val))

        async with self.config.guild(guild).all() as cfg:
            return {
                "window_minutes": _as_int(cfg.get("hot_take_window_minutes"), self.hot_take_config["window_minutes"], 1, 120),
                "cooldown_minutes": _as_int(cfg.get("hot_take_cooldown_minutes"), self.hot_take_config["cooldown_minutes"], 1, 1440),
                "min_messages": _as_int(cfg.get("hot_take_min_messages"), self.hot_take_config["min_messages"], 1, 200),
                "probability": _as_float(cfg.get("hot_take_probability"), self.hot_take_config["probability"], 0.0, 1.0),
                "context_messages": _as_int(cfg.get("hot_take_context_messages"), self.hot_take_config["context_messages"], 5, 500),
            }

    async def _is_hot_take_enabled_for_guild(self, guild: discord.Guild) -> bool:
        """Check if hot takes are enabled for a specific guild."""
        async with self.config.guild(guild).all() as cfg:
            return bool(cfg.get("hot_take_enabled", self.hot_take_enabled))

    async def _require_admin(self, ctx: commands.Context) -> bool:
        """
        Check if user has admin permissions (server admin or custom admin role).
        
        Args:
            ctx: Command context
        
        Returns:
            True if user is admin, False otherwise
        """
        if await self.bot.is_owner(ctx.author):
            return True
        if not ctx.guild:
            return False
        if ctx.author.guild_permissions.administrator:
            return True
        async with self.config.guild(ctx.guild).all() as cfg:
            admin_role_id = cfg.get("admin_role")
            if admin_role_id:
                try:
                    role = ctx.guild.get_role(int(admin_role_id))
                except (TypeError, ValueError):
                    role = None
                if role and role in ctx.author.roles:
                    return True
        return False

    async def _log_command(self, guild_id, user_id, command: str, success: bool = True) -> None:
        """Log command usage to database."""
        await self.db.log_command_usage(str(guild_id), str(user_id), command, success)

    async def _ensure_ai_configured(self, ctx: commands.Context) -> bool:
        """Give a recoverable setup instruction instead of silently failing with no provider keys."""
        if await self.ai_service.get_configured_providers():
            return True
        await ctx.send(
            f":x: No AI provider is configured yet. A server admin can run `{ctx.clean_prefix}lsetup`."
        )
        return False

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
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item, str):
                    parts.append(item)
            return "\n".join(p for p in parts if p)
        return ""

    def _format_hot_take_context(self, messages, limit: Optional[int] = None):
        msgs = sorted([m for m in messages if (m.get("content") or "").strip()], key=lambda x: x.get("timestamp", 0))
        lines = []
        context_limit = limit if limit is not None else self.hot_take_config["context_messages"]
        for m in msgs[-context_limit:]:
            name = m.get("author_tag") or m.get("author_id", "Unknown")
            lines.append(f"[{name}]: {m.get('content', '')}")
        conversation = "\n".join(lines)
        if len(conversation) > 8000:
            conversation = "[...older messages truncated]\n" + conversation[-8000:]
        return conversation

    async def _maybe_fire_hot_take(self, message):
        if not message.guild or message.author.bot:
            return
        if not await self._is_hot_take_enabled_for_guild(message.guild):
            return
        if not message.channel or message.channel.type != discord.ChannelType.text:
            return
        channel_id = str(message.channel.id)
        guild_id = str(message.guild.id)

        allowed = await self._get_hot_take_channels(guild_id)
        if channel_id not in allowed:
            return
        hot_take_cfg = await self._get_hot_take_runtime_cfg(message.guild)

        now = time.time() * 1000
        window_ms = hot_take_cfg["window_minutes"] * 60 * 1000
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

        min_msgs = hot_take_cfg["min_messages"]
        if len(activity) < min_msgs:
            return

        cooldown_ms = hot_take_cfg["cooldown_minutes"] * 60 * 1000
        last_fire = self.hot_take_cooldowns.get(channel_id, 0)
        if now - last_fire < cooldown_ms:
            return

        prob = hot_take_cfg["probability"]
        if random.random() > prob:
            return
        if not await self.ai_service.get_configured_providers():
            return

        try:
            ctx_msgs = await self.db.get_channel_messages(channel_id, hot_take_cfg["context_messages"])
            if not ctx_msgs:
                return
            conversation = self._format_hot_take_context(ctx_msgs, hot_take_cfg["context_messages"])
            async with self.config.guild(message.guild).all() as cfg:
                model = cfg.get("model", DEFAULT_MODEL)
            provider_order = await self._get_guild_provider_order(message.guild)
            payload = {
                "messages": [{"role": "user", "content": HOT_TAKE_PROMPT.replace("{conversation}", conversation)}],
                "temperature": 0.9,
                "max_tokens": 500,
            }
            resp = await self.ai_service.execute_request(
                payload,
                model,
                context="HOT_TAKE",
                timeout=60,
                provider_order=provider_order,
                guild_id=message.guild.id,
            )
            text = sanitize_output(self._extract_ai_response(resp))
            if text:
                await message.channel.send(text, allowed_mentions=_SAFE_MENTIONS)
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
        merged = normalize_string_iterable(sync_channels + self.hot_take_channels_extra)
        return merged

    @staticmethod
    async def _delete_after_delay(msg, delay: int):
        """Delete a message after a delay in seconds."""
        await asyncio.sleep(delay)
        try:
            await msg.delete()
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            pass


class _TldrExpandView(discord.ui.View):
    """Button to show the full TLDR text when truncated."""
    def __init__(self, full_text: str, author_id: int):
        super().__init__(timeout=60)
        self.full_text = full_text
        self.author_id = author_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(":x: Only the requester can expand this.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Show More", style=discord.ButtonStyle.secondary, emoji="\U0001f4d6")
    async def show_more(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = interaction.message.embeds[0] if interaction.message.embeds else None
        if embed:
            embed.description = self.full_text[:4090]
            await interaction.response.edit_message(embed=embed, view=None)
        else:
            await interaction.response.edit_message(content=self.full_text[:2000], view=None)


class LuckyAICog(LuckyAICogBase):

    def _is_command_content(self, content: str, prefixes) -> bool:
        """Return whether content is a prefixed or supported bare Lucky AI command."""
        stripped = (content or "").strip()
        if not stripped:
            return False
        if any(prefix and stripped.startswith(prefix) for prefix in prefixes):
            return True
        command_name = stripped.split(maxsplit=1)[0].lower()
        if command_name in _UNPREFIXED_COMMANDS:
            return True
        return command_name.startswith("l") and bool(
            find_suggestions(command_name, self._command_candidate_map(), limit=1, cutoff=0.7)
        )

    def _command_candidate_map(self) -> Dict[str, str]:
        """Map command names and aliases to their full user-facing command path."""
        candidates = {}
        for command in self.bot.walk_commands():
            if command.cog is not self:
                continue
            qualified = command.qualified_name
            candidates[qualified.lower()] = qualified
            candidates[command.name.lower()] = qualified
            parent_name = command.parent.qualified_name if command.parent else ""
            for alias in command.aliases:
                display = f"{parent_name} {alias}".strip()
                candidates[alias.lower()] = display
                candidates[display.lower()] = display
        return candidates

    async def _sync_message_if_eligible(self, message, sync_channels, is_command: bool) -> None:
        """Insert, update, or remove one message according to the current sync and privacy rules."""
        if not self.message_sync_enabled or str(message.channel.id) not in sync_channels:
            return
        guild_id = str(message.guild.id)
        author_id = str(message.author.id)
        if is_command or await self._check_opt_out(guild_id, author_id):
            await self.db.delete_message(str(message.id))
            return
        await self.db.save_message(
            {
                "id": str(message.id),
                "author": {"id": author_id, "tag": str(message.author), "name": message.author.name},
                "channel": {"id": str(message.channel.id)},
                "content": message.content,
                "timestamp": int(message.created_at.timestamp() * 1000),
                "guild_id": guild_id,
            }
        )

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot:
            return
        if not message.guild:
            return
        if not message.channel:
            return
        if await self._is_disabled_in_guild(message.guild):
            return

        # Check if bot is enabled in this guild
        async with self.config.guild(message.guild).all() as cfg:
            enabled = cfg.get("enabled", True)
            sync_channels = normalize_string_iterable(cfg.get("sync_channels", []))

        if not enabled:
            await self._try_unprefixed_command(message)
            return

        # Skip command messages from being saved to history
        content = message.content.strip()
        prefixes = await self.bot.get_valid_prefixes(message.guild)
        is_command = self._is_command_content(content, prefixes)

        await self._sync_message_if_eligible(message, sync_channels, is_command)

        if not is_command:
            await self._maybe_fire_hot_take(message)

        await self._try_unprefixed_command(message)

    @commands.Cog.listener()
    async def on_message_edit(self, _before, after):
        """Keep stored context aligned with message edits without rerunning commands or hot takes."""
        if after.author.bot or not after.guild or not after.channel:
            return
        if await self._is_disabled_in_guild(after.guild):
            return
        async with self.config.guild(after.guild).all() as cfg:
            if not cfg.get("enabled", True):
                return
            sync_channels = normalize_string_iterable(cfg.get("sync_channels", []))
        prefixes = await self.bot.get_valid_prefixes(after.guild)
        await self._sync_message_if_eligible(
            after,
            sync_channels,
            self._is_command_content(after.content, prefixes),
        )

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload):
        """Remove deleted Discord messages from local AI context storage."""
        if payload.guild_id is not None:
            await self.db.delete_message(str(payload.message_id))

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload):
        """Remove bulk-deleted Discord messages from local AI context storage."""
        if payload.guild_id is not None:
            await self.db.delete_messages(payload.message_ids)

    async def _suggest_command(self, message: discord.Message, typed: str, prefix: str = "") -> None:
        """Suggest similar commands when a typo is detected, then auto-delete."""
        suggestions = find_suggestions(typed, self._command_candidate_map())
        if not suggestions:
            return
        formatted = [f"`{prefix}{name}`" for name in suggestions]
        suggestion = f"Did you mean {formatted[0]}?"
        if len(formatted) > 1:
            suggestion += f" Or {formatted[1]}?"
        msg = await message.channel.send(suggestion, allowed_mentions=_SAFE_MENTIONS)
        asyncio.create_task(self._delete_after_delay(msg, 5))

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
            if cmd_name.startswith("l"):
                await self._suggest_command(message, cmd_name)
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
        if not await self._ensure_ai_configured(ctx):
            return

        async with self.config.guild(ctx.guild).all() as cfg:
            model = cfg.get("model", DEFAULT_MODEL)
            message_fetch_mode = cfg.get("messageFetchMode", "random")
            rand_mode = cfg.get("randomMode", False)
            prompt_key = cfg.get("promptKey")
            provider_order = self._normalize_provider_order(cfg.get("provider_order"))

        try:
            messages = await self.db.get_messages(user_id, 200, message_fetch_mode, guild_id)
            if not messages:
                await ctx.send(f":x: <@{user_id}> has no message history in this server.")
                return
            if not await self._require_admin(ctx):
                cooldown = self.cooldowns.check(author_id, 30000, "roast")
                if cooldown["active"]:
                    msg = await ctx.send(f":hourglass_flowing_sand: Wait {cooldown['remaining_sec']}s")
                    asyncio.create_task(self._delete_after_delay(msg, 4))
                    return

            roast_style = await self._select_roast_style(prompt_key, rand_mode, style, guild=ctx.guild)
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

            async with ctx.typing():
                resp = await self.ai_service.execute_request(
                    payload, model, context="ROAST", provider_order=provider_order, guild_id=ctx.guild.id
                )
            text = sanitize_output(self._extract_ai_response(resp)) or f"I was going to roast {target.name}, but even my AI has standards. Nice try though!"
            usage = resp.get("usage", {})
            truncated = text[:4090] + "..." if len(text) > 4090 else text
            embed = discord.Embed(
                color=COLORS["ROAST"],
                title=f":fire: ROAST OF {target.display_name} :fire:",
                description=truncated,
            )
            embed.set_footer(text=f"Tokens used: {usage.get('total_tokens', '?')}" if usage else "Roast complete")
            embed.timestamp = discord.utils.utcnow()
            await ctx.send(embed=embed, allowed_mentions=_SAFE_MENTIONS)
            await self.db.update_roast_count(user_id)
            await self._log_command(guild_id, ctx.author.id, "roast")
        except Exception as e:
            log.error("ROAST Error: %s", e)
            self.cooldowns.remove(author_id, "roast")
            await self._log_command(guild_id, ctx.author.id, "roast", False)

    @commands.command(name="ltldr", aliases=["lgreentext"])
    async def ltldr(self, ctx: commands.Context, messages: int = 200, style: str = "normal"):
        """Summarize recent chat as a TL;DR. Style can be "normal" or "greentext"."""
        if not ctx.guild:
            await ctx.send(":x: This command only works in servers.")
            return
        style = (style or "normal").lower().strip()
        if style not in {"normal", "greentext"}:
            suggestion = suggest_choice(style, ("normal", "greentext"))
            hint = f" Did you mean `{suggestion}`?" if suggestion else ""
            await ctx.send(f":x: Style must be `normal` or `greentext`.{hint}")
            return
        if messages < TLDR_MIN_MESSAGES or messages > MAX_MESSAGE_COUNT:
            await ctx.send(f":x: Message count must be between {TLDR_MIN_MESSAGES} and {MAX_MESSAGE_COUNT}.")
            return
        async with self.config.guild(ctx.guild).all() as cfg:
            if not cfg.get("enabled", True):
                await ctx.send(":x: Bot is currently disabled in this server.")
                return
            model = cfg.get("model", DEFAULT_MODEL)
            provider_order = self._normalize_provider_order(cfg.get("provider_order"))
        if not await self._ensure_ai_configured(ctx):
            return
        try:
            raw_messages = await self.db.get_channel_messages(str(ctx.channel.id), messages)
            raw_messages = [m for m in raw_messages if m.get("id") != str(ctx.message.id)]
            if not raw_messages:
                await ctx.send(":x: No messages found. The channel may not be synced yet.")
                return
            if not await self._require_admin(ctx):
                cooldown = self.cooldowns.check(str(ctx.author.id), 300000, "tldr")
                if cooldown["active"]:
                    msg = await ctx.send(f":hourglass_flowing_sand: Wait {cooldown['remaining_sec']}s")
                    asyncio.create_task(self._delete_after_delay(msg, 4))
                    return
            conv = format_messages_for_tldr(raw_messages)
            system = GREENTEXT_SYSTEM_PROMPT if style == "greentext" else TLDR_SYSTEM_PROMPT
            header = f"The following are the last {len(raw_messages)} messages from a Discord channel, ordered oldest to newest:\n\n"
            suffix = "\n\nTurn this conversation into a 4chan greentext story." if style == "greentext" else "\n\nSummarize this conversation as a TL;DR."
            msg_count = len(raw_messages)
            if msg_count <= 50:
                mt = 512
            elif msg_count <= 100:
                mt = 1024
            elif msg_count <= 300:
                mt = 1536
            else:
                mt = 2048
            payload = {
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": header + conv + suffix}],
                "temperature": 1.0 if style == "greentext" else 0.7,
                "max_tokens": mt,
            }
            async with ctx.typing():
                resp = await self.ai_service.execute_request(
                    payload, model, context="TLDR", timeout=90, provider_order=provider_order, guild_id=ctx.guild.id
                )
            text = sanitize_output(self._extract_ai_response(resp))
            if not text:
                self.cooldowns.remove(str(ctx.author.id), "tldr")
                await self._log_command(ctx.guild.id, ctx.author.id, "tldr", False)
                return
            embed = discord.Embed(
                color=COLORS["SUCCESS"] if style == "greentext" else COLORS["INFO"],
                title="> Greentext" if style == "greentext" else "🧠 TL;DR Summary",
                description=text[:4090] if len(text) > 4090 else text,
            )
            embed.set_footer(text=f"Requested by {ctx.author.name}")
            embed.timestamp = discord.utils.utcnow()
            if len(text) > 1500:
                view = _TldrExpandView(text, ctx.author.id)
                await ctx.send(embed=embed, view=view, allowed_mentions=_SAFE_MENTIONS)
            else:
                await ctx.send(embed=embed, allowed_mentions=_SAFE_MENTIONS)
            await self._log_command(ctx.guild.id, ctx.author.id, "tldr")
        except Exception as e:
            log.error("TLDR Error: %s", e)
            self.cooldowns.remove(str(ctx.author.id), "tldr")
            await self._log_command(ctx.guild.id, ctx.author.id, "tldr", False)

    @commands.command(name="loptout")
    async def loptout(self, ctx: commands.Context, action: str):
        """Opt in or out of stored-message AI analysis. Use "in" to opt in, "out" to opt out."""
        if not ctx.guild:
            await ctx.send(":x: This command only works in servers.")
            return
        action = (action or "").lower().strip()
        if action not in ("in", "out"):
            suggestion = suggest_choice(action, ("in", "out"))
            hint = f" Did you mean `{suggestion}`?" if suggestion else ""
            await ctx.send(f":x: Use `in` or `out`.{hint}")
            return
        opted = action == "out"
        deleted = await self.db.set_user_opt_out(str(ctx.author.id), str(ctx.guild.id), opted)
        status = "opted out" if opted else "opted in"
        deleted_text = f" Removed **{deleted}** stored message(s)." if opted and deleted else ""
        await ctx.send(f":white_check_mark: You have {status} of AI analysis in this server.{deleted_text}")

    @commands.group(name="lconfig", aliases=["lcfg"], invoke_without_command=True)
    @lucky_admin()
    async def lconfig(self, ctx: commands.Context):
        """Manage sync channels, blacklist, admin role, toggle, and backfill. Use subcommands for details."""
        subcommands = ("channels", "blacklist", "admin_role", "toggle", "backfill", "backfill_cancel", "backfill_status")
        suggestion = suggest_choice(ctx.subcommand_passed or "", subcommands)
        if suggestion:
            await ctx.send(f"Did you mean `{ctx.clean_prefix}lconfig {suggestion}`?")
            return
        await ctx.send(f"Use `{ctx.clean_prefix}lhelp` to see the available `lconfig` subcommands.")

    @lconfig.group(name="channels", invoke_without_command=True)
    async def lconfig_channels(self, ctx: commands.Context):
        """Manage message sync channels. Use add/remove/list subcommands."""
        suggestion = suggest_choice(ctx.subcommand_passed or "", ("add", "remove", "list"))
        if suggestion:
            await ctx.send(f"Did you mean `{ctx.clean_prefix}lconfig channels {suggestion}`?")
            return
        await ctx.send(
            f"Usage: `{ctx.clean_prefix}lconfig channels add|remove|list [#channel]`"
        )

    @lconfig_channels.command(name="add")
    async def lconfig_channels_add(self, ctx: commands.Context, channel: discord.TextChannel):
        """Add a channel for message syncing. Starts backfill on the last 14 days."""
        if not ctx.guild:
            return
        guild_id = str(ctx.guild.id)
        channel_id = str(channel.id)
        async with self.config.guild(ctx.guild).all() as cfg:
            sync = normalize_string_iterable(cfg.get("sync_channels", []))
            if channel_id in sync:
                await ctx.send(":x: Channel already enabled for syncing.")
                return
            if len(sync) >= 45:
                await ctx.send(":x: Channel limit reached (max 45).")
                return

        if not self.message_sync_enabled:
            view = _SyncConfirmView(self, ctx, channel)
            embed = discord.Embed(
                color=0xf1c40f,
                title=":warning: Message Sync is Off",
                description=(
                    "Adding a sync channel requires message syncing to be **enabled**.\n\n"
                    "This will store messages from configured channels in the local database "
                    "to power `lroast`, `ltldr`, and other features.\n\n"
                    "Enable syncing and add this channel?"
                ),
            )
            await ctx.send(embed=embed, view=view)
            return

        await self._do_add_sync_channel(ctx, channel, guild_id, channel_id)

    async def _do_add_sync_channel(self, ctx, channel, guild_id, channel_id):
        """Inner logic to add a sync channel (called after sync confirmation)."""
        task_key = backfill_task_key(ctx.guild.id, channel.id)
        existing = self.backfill_tasks.get(task_key)
        if existing and not existing.done():
            await ctx.send(":x: A backfill is already running for this channel.")
            return
        async with self.config.guild(ctx.guild).all() as cfg:
            sync = normalize_string_iterable(cfg.get("sync_channels", []))
            if channel_id in sync:
                await ctx.send(":x: Channel already enabled for syncing.")
                return
            sync.append(channel_id)
            cfg["sync_channels"] = sync
        await self.db.update_sync_status(guild_id, channel_id)
        await self.db.log_sync_operation(guild_id, channel_id, "channel_add", message_count=0, triggered_by=str(ctx.author.id))
        progress_msg = await ctx.send(
            f":white_check_mark: Added {channel.mention} as a sync channel. Starting initial backfill..."
        )
        self._start_backfill(ctx.guild, channel, 14, ctx.author, progress_msg)

    def _start_backfill(self, guild, channel, days, author, progress_msg=None):
        """Start one tracked backfill task, returning None when one is already active."""
        task_key = backfill_task_key(guild.id, channel.id)
        existing = self.backfill_tasks.get(task_key)
        if existing and not existing.done():
            return None
        task = asyncio.create_task(self._do_backfill(guild, channel, days, author, progress_msg))
        self.backfill_tasks[task_key] = task

        def _cleanup_done(done_task: asyncio.Task):
            self.backfill_tasks.pop(task_key, None)
            if not done_task.cancelled() and done_task.exception():
                log.error("Backfill task %s failed: %s", task_key, done_task.exception())

        task.add_done_callback(_cleanup_done)
        return task

    @lconfig_channels.command(name="remove")
    async def lconfig_channels_remove(self, ctx: commands.Context, channel: discord.TextChannel):
        """Remove a sync channel and delete its stored messages."""
        if not ctx.guild:
            return
        channel_id = str(channel.id)
        async with self.config.guild(ctx.guild).all() as cfg:
            sync = normalize_string_iterable(cfg.get("sync_channels", []))
            if channel_id not in sync:
                await ctx.send(":x: Channel not enabled for syncing.")
                return
            sync.remove(channel_id)
            cfg["sync_channels"] = sync
        task_key = backfill_task_key(ctx.guild.id, channel.id)
        task = self.backfill_tasks.get(task_key)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.backfill_progress[task_key] = {
                **self.backfill_progress.get(task_key, {}),
                "status": "cancelled",
                "finished_at": time.time(),
            }
            self.backfill_tasks.pop(task_key, None)
        await self.db.delete_sync_status(str(ctx.guild.id), channel_id)
        deleted = await self.db.delete_channel_messages(str(ctx.guild.id), channel_id)
        await ctx.send(f":white_check_mark: Removed {channel.mention} from sync channels ({deleted} messages deleted).")

    @lconfig_channels.command(name="list")
    async def lconfig_channels_list(self, ctx: commands.Context):
        """List all channels currently configured for message syncing."""
        if not ctx.guild:
            return
        async with self.config.guild(ctx.guild).all() as cfg:
            sync = normalize_string_iterable(cfg.get("sync_channels", []))
        if not sync:
            await ctx.send(":x: No sync channels configured.")
            return
        lines = [f"- <#{ch}>" for ch in sync]
        pages = []
        current = []
        current_len = 0
        for line in lines:
            line_len = len(line) + 1
            if current and current_len + line_len > 3500:
                pages.append("\n".join(current))
                current = []
                current_len = 0
            current.append(line)
            current_len += line_len
        if current:
            pages.append("\n".join(current))
        total_pages = len(pages)
        for idx, page in enumerate(pages, start=1):
            title = ":clipboard: Sync Channels"
            if total_pages > 1:
                title += f" ({idx}/{total_pages})"
            embed = discord.Embed(title=title, color=0x0099ff, description=page)
            await ctx.send(embed=embed)

    @lconfig.group(name="blacklist", invoke_without_command=True)
    async def lconfig_blacklist(self, ctx: commands.Context):
        """Manage blacklisted users. Use add/remove/list subcommands."""
        suggestion = suggest_choice(ctx.subcommand_passed or "", ("add", "remove", "list"))
        if suggestion:
            await ctx.send(f"Did you mean `{ctx.clean_prefix}lconfig blacklist {suggestion}`?")
            return
        await ctx.send(
            f"Usage: `{ctx.clean_prefix}lconfig blacklist add|remove|list [@user]`"
        )

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
        lines = []
        for e in entries:
            reason = str(e.get("reason") or "").strip().replace("\n", " ")
            if len(reason) > 150:
                reason = reason[:147] + "..."
            lines.append(f"- <@{e['user_id']}>{' - ' + reason if reason else ''}")
        pages = []
        current = []
        current_len = 0
        for line in lines:
            line_len = len(line) + 1
            if current and current_len + line_len > 3500:
                pages.append("\n".join(current))
                current = []
                current_len = 0
            current.append(line)
            current_len += line_len
        if current:
            pages.append("\n".join(current))
        total_pages = len(pages)
        for idx, page in enumerate(pages, start=1):
            title = ":clipboard: Blacklisted Users"
            if total_pages > 1:
                title += f" ({idx}/{total_pages})"
            embed = discord.Embed(title=title, color=0x0099ff, description=page)
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
        if days is not None and days <= 0:
            await ctx.send(":x: `days` must be greater than 0.")
            return
        channel = channel or ctx.channel
        task_key = backfill_task_key(ctx.guild.id, channel.id)
        existing = self.backfill_tasks.get(task_key)
        if existing and not existing.done():
            await ctx.send(":x: A backfill is already running for this channel.")
            return
        progress_msg = await ctx.send(
            f":arrows_counterclockwise: Starting {f'{days}-day' if days else 'full'} backfill of {channel.mention}..."
        )
        self._start_backfill(ctx.guild, channel, days, ctx.author, progress_msg)

    @lconfig.command(name="backfill_cancel")
    async def lconfig_backfill_cancel(self, ctx: commands.Context, channel: discord.TextChannel = None):
        """Cancel a running backfill for a channel (defaults to current channel)."""
        if not ctx.guild:
            return
        channel = channel or ctx.channel
        task_key = backfill_task_key(ctx.guild.id, channel.id)
        task = self.backfill_tasks.get(task_key)
        if not task or task.done():
            await ctx.send(":x: No active backfill for that channel.")
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.backfill_progress[task_key] = {
            **self.backfill_progress.get(task_key, {}),
            "status": "cancelled",
            "finished_at": time.time(),
        }
        self.backfill_tasks.pop(task_key, None)
        await ctx.send(f":warning: Cancellation requested for backfill in {channel.mention}.")

    @lconfig.command(name="backfill_status")
    async def lconfig_backfill_status(self, ctx: commands.Context, channel: discord.TextChannel = None):
        """Show current backfill progress for a channel."""
        if not ctx.guild:
            return
        channel = channel or ctx.channel
        task_key = backfill_task_key(ctx.guild.id, channel.id)
        p = self.backfill_progress.get(task_key)
        task = self.backfill_tasks.get(task_key)
        if not p:
            await ctx.send(":x: No backfill status found for that channel.")
            return
        running = bool(task and not task.done())
        await ctx.send(format_backfill_status(channel.id, p, running))

    async def _do_backfill(self, guild, channel, days, author, progress_msg=None):
        from datetime import timedelta
        guild_id = str(guild.id)
        channel_id = str(channel.id)
        task_key = backfill_task_key(guild.id, channel.id)
        after = discord.utils.utcnow() - timedelta(days=days) if days else None
        total_synced = 0
        processed = 0
        started_at = time.time()
        self.backfill_progress[task_key] = {
            "status": "running",
            "processed": 0,
            "synced": 0,
            "started_at": started_at,
        }
        last_progress_update = 0.0
        pending_messages = []
        try:
            prefixes = await self.bot.get_valid_prefixes(guild)
            opt_out_ids = set(await self.db.get_opt_out_user_ids(guild_id))
            async for msg in channel.history(limit=None, after=after, oldest_first=True):
                processed += 1
                should_tick = processed <= 100 or processed % 100 == 0
                if msg.author.bot:
                    if should_tick:
                        self.backfill_progress[task_key]["processed"] = processed
                    continue
                author_id = str(msg.author.id)
                if author_id in opt_out_ids or self._is_command_content(msg.content, prefixes):
                    if should_tick:
                        self.backfill_progress[task_key]["processed"] = processed
                    continue
                pending_messages.append({
                    "id": str(msg.id),
                    "author": {"id": author_id, "tag": str(msg.author), "name": msg.author.name},
                    "channel": {"id": channel_id},
                    "content": msg.content,
                    "timestamp": int(msg.created_at.timestamp() * 1000),
                    "guild_id": guild_id,
                })
                if len(pending_messages) >= 100:
                    total_synced += await self.db.save_message_batch(pending_messages)
                    pending_messages.clear()
                if should_tick:
                    self.backfill_progress[task_key]["processed"] = processed
                    self.backfill_progress[task_key]["synced"] = total_synced
                    now = time.time()
                    if progress_msg and (now - last_progress_update >= 3):
                        try:
                            await progress_msg.edit(
                                content=(
                                    f":arrows_counterclockwise: Backfilling {channel.mention}...\n"
                                    f"Processed: **{processed}** | Synced: **{total_synced}**"
                                )
                            )
                            last_progress_update = now
                        except Exception:
                            pass
            if pending_messages:
                total_synced += await self.db.save_message_batch(pending_messages)
            await self.db.update_sync_status(guild_id, channel_id)
            await self.db.log_sync_operation(guild_id, channel_id, "backfill", message_count=total_synced, triggered_by=str(author.id))
            self.backfill_progress[task_key] = {
                "status": "completed",
                "processed": processed,
                "synced": total_synced,
                "started_at": started_at,
                "finished_at": time.time(),
            }
            if progress_msg:
                try:
                    await progress_msg.edit(
                        content=(
                            f":white_check_mark: Backfill complete for {channel.mention}.\n"
                            f"Processed: **{processed}** | Synced: **{total_synced}**"
                        )
                    )
                except Exception:
                    pass
            log.info("BACKFILL Complete for %s: %d messages", channel_id, total_synced)
        except asyncio.CancelledError:
            self.backfill_progress[task_key] = {
                "status": "cancelled",
                "processed": processed,
                "synced": total_synced,
                "started_at": started_at,
                "finished_at": time.time(),
            }
            if progress_msg:
                try:
                    await progress_msg.edit(
                        content=(
                            f":warning: Backfill cancelled for {channel.mention}.\n"
                            f"Processed: **{processed}** | Synced: **{total_synced}**"
                        )
                    )
                except Exception:
                    pass
            raise
        except Exception as e:
            self.backfill_progress[task_key] = {
                "status": "failed",
                "processed": processed,
                "synced": total_synced,
                "started_at": started_at,
                "finished_at": time.time(),
                "error": str(e),
            }
            if progress_msg:
                try:
                    await progress_msg.edit(content=f":x: Backfill failed for {channel.mention}. Check the bot logs.")
                except Exception:
                    pass
            log.error("BACKFILL Error for %s: %s", channel_id, e)

    @commands.command(name="lstats")
    @lucky_admin()
    async def lstats(self, ctx: commands.Context, detail: str = "normal"):
        """View bot statistics, database size, uptime, and command usage."""
        if not ctx.guild:
            return
        try:
            normalized = (detail or "normal").lower().strip()
            if normalized not in {"normal", "v", "verbose", "full", "debug"}:
                await ctx.send(":x: Usage: `lstats [normal|verbose]`")
                return
            verbose = normalized in {"v", "verbose", "full", "debug"}
            stats_data = await self.admin_cmds.build_stats(ctx, verbose=verbose)
            embed = discord.Embed(color=stats_data["color"], title=stats_data["title"])
            for field in stats_data["fields"]:
                embed.add_field(name=field["name"], value=field["value"], inline=field.get("inline", True))
            embed.timestamp = discord.utils.utcnow()
            await ctx.send(embed=embed)
        except Exception as e:
            log.error("STATS Error: %s", e)
            await ctx.send(":x: Failed to retrieve statistics.")

    @commands.command(name="lsetup")
    @lucky_admin()
    async def lsetup(self, ctx: commands.Context) -> None:
        """Run the interactive setup wizard to configure API keys and default model."""
        if not ctx.guild:
            await ctx.send(":x: This command only works in servers.")
            return

        self._session_counter += 1
        session_id = str(self._session_counter)

        await ensure_config_json_async(self.config_json_path)
        async with self.config.guild(ctx.guild).all() as cfg:
            current_model = cfg.get("model", DEFAULT_MODEL)

        self.setup_sessions[session_id] = {
            "api_keys": {},
            "default_model": current_model,
            "prefix": ctx.clean_prefix,
            "channel_id": ctx.channel.id,
            "_last_accessed": time.time() * 1000,
        }
        for provider in PROVIDER_ORDER:
            try:
                tokens = await self.bot.get_shared_api_tokens(provider)
            except Exception:
                tokens = {}
            key = (tokens or {}).get("api_key")
            if key:
                self.setup_sessions[session_id]["api_keys"][provider] = True

        view = SetupView(self, session_id, ctx.author.id, ctx.guild.id)
        embed = await view.build_embed()
        await ctx.send(embed=embed, view=view)

    @commands.command(name="lsettings")
    @lucky_admin()
    async def lsettings(self, ctx: commands.Context) -> None:
        """Open the interactive settings UI without requiring slash-command setup."""
        if not ctx.guild:
            await ctx.send(":x: This command only works in servers.")
            return
        embed, view = await self._build_settings_panel(ctx.author.id, ctx.guild)
        await ctx.send(embed=embed, view=view)

    @commands.command(name="lhelp", aliases=["lcommands"])
    async def lhelp(self, ctx: commands.Context):
        """Show the list of available commands."""
        p = ctx.clean_prefix
        embed = discord.Embed(
            color=0x0099ff,
            title="\U0001f916 Lucky AI - Commands",
            description=f"Core commands use `{p}`. Also supports unprefixed `l...` commands (example: `lroast @user`).",
        )
        embed.add_field(
            name=f"\U0001f3af `{p}lroast @user [style]`",
            value="Roast a user from their stored server message history. Cooldown: 30s (admin bypass).",
            inline=False,
        )
        embed.add_field(
            name=f"\U0001f9e0 `{p}ltldr [count] [style]`",
            value="Summarize recent messages. Count: 10-500. Style: `normal` or `greentext`. Cooldown: 300s.",
            inline=False,
        )
        embed.add_field(
            name=f"\U0001f4ac `{p}lask [count] <question>`",
            value=(
                "Ask a general question, or add a leading count (1-500) to analyze that many recent messages. "
                "Supports image attachments and `--with-context`. Cooldown: 60s (10s admin)."
            ),
            inline=False,
        )
        embed.add_field(
            name=f":no_entry_sign: `{p}loptout <in|out>`",
            value="Opt in/out of stored-message AI analysis for this server.",
            inline=False,
        )
        embed.add_field(
            name=f"\U0001f525 `{p}lhtt on|off|fire` (Admin)",
            value="Enable/disable hot takes or fire one immediately.",
            inline=False,
        )
        embed.add_field(
            name=f":wrench: `{p}lsettings` or `/lsettings` (Admin)",
            value="Interactive settings UI. `lsettings` works immediately; `/lsettings` works after slash commands are enabled/synced.",
            inline=False,
        )
        embed.add_field(
            name=f":gear: `{p}lconfig ...` (Admin)",
            value=(
                "Config subcommands:\n"
                f"`{p}lconfig channels add|remove|list [#channel]`\n"
                f"`{p}lconfig blacklist add|remove|list [@user]`\n"
                f"`{p}lconfig admin_role <@role>`\n"
                f"`{p}lconfig toggle <true|false>`\n"
                f"`{p}lconfig backfill [#channel] [days]`\n"
                f"`{p}lconfig backfill_status [#channel]`\n"
                f"`{p}lconfig backfill_cancel [#channel]`"
            ),
            inline=False,
        )
        embed.add_field(
            name=f"\U0001f680 `{p}lsetup` (Admin)",
            value="Single-page setup wizard that validates a provider, enables sync, adds the current channel, and backfills 14 days when possible.",
            inline=False,
        )
        embed.add_field(
            name=f"\U0001f4ca `{p}lstats [normal|verbose]` (Admin)",
            value="Show usage, DB, and health stats.",
            inline=False,
        )
        embed.add_field(name=f":question: `{p}lhelp`", value="Show this command reference.", inline=False)
        embed.set_footer(text="Lucky AI")
        embed.timestamp = discord.utils.utcnow()
        await ctx.send(embed=embed)

    @commands.command(name="lask")
    async def lask(self, ctx: commands.Context, *, question: str = None):
        """Ask a general question, or provide a leading count to analyze recent chat."""
        if not ctx.guild:
            await ctx.send(":x: This command only works in servers.")
            return
        async with self.config.guild(ctx.guild).all() as cfg:
            if not cfg.get("enabled", True):
                await ctx.send(":x: Bot is currently disabled in this server.")
                return
        question, debug_context, context_count = parse_ask_flags(question)
        image_attachments = find_image_attachments(ctx.message.attachments or [])
        attach = image_attachments[0] if image_attachments else None
        if context_count is not None and not 1 <= context_count <= MAX_MESSAGE_COUNT:
            await ctx.send(f":x: Context count must be between 1 and {MAX_MESSAGE_COUNT}.")
            return
        if not question and not ctx.message.attachments:
            await ctx.send(f"Usage: `{ctx.clean_prefix}lask [count] <question>` or attach an image")
            return
        if not question and not attach:
            await ctx.send(":x: Attach an image file or include a text question.")
            return
        if not await self._ensure_ai_configured(ctx):
            return
        author_id = str(ctx.author.id)
        guild_id = str(ctx.guild.id)
        async with self.config.guild(ctx.guild).all() as cfg:
            provider_order = self._normalize_provider_order(cfg.get("provider_order"))
        try:
            bot_user_id = str(self.bot.user.id)
            explicit_context = context_count is not None
            fetch_count = context_count if explicit_context else 10
            ctx_msgs = await self.db.get_channel_messages(str(ctx.channel.id), fetch_count)
            ctx_msgs = [m for m in ctx_msgs if m.get("id") != str(ctx.message.id)]
            chosen_context_lines = build_ask_context(
                ctx_msgs,
                question,
                bot_user_id,
                explicit_context=explicit_context,
            )
            if explicit_context and not chosen_context_lines:
                await ctx.send(":x: No synced messages are available in this channel yet.")
                return
            ctx_text = "\n".join(chosen_context_lines)
            user_name = ctx.author.display_name or ctx.author.name
            prompt_text_only = f"{user_name}: {question or ''}\n\nAnswer:"
            prompt_with_image = f"{user_name}: {question or '[image]'}\n\nAnswer:"
            if ctx_text:
                prompt = f"Recent chat:\n{ctx_text}\n---\n{prompt_with_image}"
                prompt_without_image = f"Recent chat:\n{ctx_text}\n---\n{prompt_text_only}"
            else:
                prompt = prompt_with_image
                prompt_without_image = prompt_text_only
            has_image = bool(attach)
            if has_image:
                if attach.size and attach.size > 8 * 1024 * 1024:
                    await ctx.send(":x: Image is too large (max 8MB).")
                    await self._log_command(guild_id, author_id, "ask", False)
                    return
                session = await self.ai_service._get_session()
                async with session.get(attach.url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        response_mime = (resp.headers.get("Content-Type", "") or "").lower()
                        if response_mime and not response_mime.startswith("image/"):
                            if not question:
                                await ctx.send(":x: Attachment URL did not return an image.")
                                await self._log_command(guild_id, author_id, "ask", False)
                                return
                            has_image = False
                            msgs = [{"role": "user", "content": prompt}]
                        else:
                            buf = await resp.content.read((8 * 1024 * 1024) + 1)
                            if len(buf) > 8 * 1024 * 1024:
                                await ctx.send(":x: Image is too large (max 8MB).")
                                await self._log_command(guild_id, author_id, "ask", False)
                                return
                            b64 = (await asyncio.get_running_loop().run_in_executor(None, base64.b64encode, buf)).decode()
                            guessed_mime = mimetypes.guess_type(getattr(attach, "filename", "") or "")[0]
                            mime_candidates = (
                                response_mime.split(";", 1)[0],
                                getattr(attach, "content_type", None),
                                guessed_mime,
                            )
                            mime = next(
                                (candidate for candidate in mime_candidates if candidate and candidate.startswith("image/")),
                                "image/jpeg",
                            )
                            content_parts = [
                                {"type": "text", "text": prompt},
                                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                            ]
                            msgs = [{"role": "user", "content": content_parts}]
                    else:
                        if not question:
                            await ctx.send(":x: Could not retrieve the image attachment.")
                            await self._log_command(guild_id, author_id, "ask", False)
                            return
                        has_image = False
                        msgs = [{"role": "user", "content": prompt_without_image}]
            else:
                msgs = [{"role": "user", "content": prompt_without_image}]
            async with self.config.guild(ctx.guild).all() as cfg:
                guild_ask_model = cfg.get("ask_model")
                guild_vision_model = cfg.get("ask_vision_model")
            global_model = await self.config.ask_model()
            global_vision_model = await self.config.ask_vision_model()
            model = (
                guild_vision_model or global_vision_model
                if has_image
                else guild_ask_model or global_model
            )
            payload = {"messages": [{"role": "system", "content": ASK_SYSTEM_PROMPT}, *msgs], "temperature": 0.85, "max_tokens": 600}
            is_admin = await self._require_admin(ctx)
            cooldown = self.cooldowns.check(author_id, 10000 if is_admin else 60000, "ask")
            if cooldown["active"]:
                msg = await ctx.send(f":hourglass_flowing_sand: Wait {cooldown['remaining_sec']}s")
                asyncio.create_task(self._delete_after_delay(msg, 4))
                return
            async with ctx.typing():
                resp = await self.ai_service.execute_request(
                    payload, model, context="ASK", timeout=45, max_retries=2, provider_order=provider_order,
                    guild_id=ctx.guild.id,
                )
            text = sanitize_output(self._extract_ai_response(resp))
            if not text:
                await ctx.send(":x: AI returned an empty response.")
                await self._log_command(guild_id, author_id, "ask", False)
                return
            if debug_context:
                if chosen_context_lines:
                    preview = "\n".join(chosen_context_lines)
                    preview = preview[:1900] + ("..." if len(preview) > 1900 else "")
                    await ctx.send(f"**Context used ({len(chosen_context_lines)} lines):**\n```text\n{preview}\n```")
                else:
                    await ctx.send("**Context used:** none")
            chunks = [text[i:i+1990] for i in range(0, len(text), 1990)] if len(text) > 1990 else [text]
            for chunk in chunks:
                await ctx.send(chunk, allowed_mentions=_SAFE_MENTIONS)
            await self._log_command(guild_id, author_id, "ask")
        except Exception as e:
            log.error("ASK Error: %s", e)
            self.cooldowns.remove(author_id, "ask")
            await self._log_command(guild_id, author_id, "ask", False)

    @commands.command(name="lhtt")
    @lucky_admin()
    async def lhtt(self, ctx: commands.Context, action: str):
        """Manage hot takes. Use "on", "off", or "fire" to manually trigger one."""
        if not ctx.guild:
            return
        action = (action or "").lower().strip()
        guild_id = str(ctx.guild.id)
        if action == "on":
            async with self.config.guild(ctx.guild).all() as cfg:
                cfg["hot_take_enabled"] = True
            await ctx.send(":white_check_mark: Hot Takes enabled!")
        elif action == "off":
            async with self.config.guild(ctx.guild).all() as cfg:
                cfg["hot_take_enabled"] = False
            await ctx.send("Hot Takes disabled!")
        elif action == "fire":
            if not await self._ensure_ai_configured(ctx):
                return
            channel_id = str(ctx.channel.id)
            allowed = await self._get_hot_take_channels(guild_id)
            if channel_id not in allowed:
                await ctx.send(":x: This channel is not configured for hot takes.")
                return
            msg = await ctx.send(":fire: Firing hot take...")
            asyncio.create_task(self._delete_after_delay(msg, 2))
            try:
                hot_take_cfg = await self._get_hot_take_runtime_cfg(ctx.guild)
                ctx_msgs = await self.db.get_channel_messages(channel_id, hot_take_cfg["context_messages"])
                if not ctx_msgs:
                    await ctx.send(":x: No messages available for context.")
                    return
                conversation = self._format_hot_take_context(ctx_msgs, hot_take_cfg["context_messages"])
                async with self.config.guild(ctx.guild).all() as cfg:
                    model = cfg.get("model", DEFAULT_MODEL)
                    provider_order = self._normalize_provider_order(cfg.get("provider_order"))
                payload = {
                    "messages": [{"role": "user", "content": HOT_TAKE_PROMPT.replace("{conversation}", conversation)}],
                    "temperature": 0.9,
                    "max_tokens": 500,
                }
                async with ctx.typing():
                    resp = await self.ai_service.execute_request(
                        payload, model, context="HOT_TAKE", provider_order=provider_order, guild_id=ctx.guild.id
                    )
                text = sanitize_output(self._extract_ai_response(resp))
                if text:
                    await ctx.send(text, allowed_mentions=_SAFE_MENTIONS)
                    self.hot_take_cooldowns[channel_id] = time.time() * 1000
                    await self.db.log_hot_take(guild_id, channel_id, text, len(ctx_msgs), model, 0)
                else:
                    await ctx.send(":x: AI returned empty response.")
            except Exception as e:
                log.error("HOT_TAKE Error: %s", e)
                await ctx.send(":x: Failed to generate hot take.")
        else:
            suggestion = suggest_choice(action, ("on", "off", "fire"))
            hint = f" Did you mean `{suggestion}`?" if suggestion else ""
            await ctx.send(
                f"Usage: `{ctx.clean_prefix}lhtt on` / `{ctx.clean_prefix}lhtt off` / "
                f"`{ctx.clean_prefix}lhtt fire`.{hint}"
            )
