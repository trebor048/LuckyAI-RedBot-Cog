import json
import time
import asyncio
import logging
import os

import discord
from discord.ui import View, Modal, Select, Button, TextInput

from ..providers import PROVIDER_ORDER, PROVIDER_LABELS, PROVIDERS
from ..utils import DEFAULT_PERSONALITIES, set_shared_api_key

log = logging.getLogger("red.lucky_ai.settings_ui")

PAGE_ORDER = ["model", "parameters", "apikeys", "advanced"]

PAGE_TITLES = {
    "model": ("Model & Style", "🤖"),
    "parameters": ("Generation Parameters", "\u2699\ufe0f"),
    "apikeys": ("API Keys", "🔑"),
    "advanced": ("Advanced", "\U0001F527"),
}

SESSION_TIMEOUT = 900
MAX_STYLES_TOTAL = 24

_config_file_lock = asyncio.Lock()


def _mask_key(key):
    if not key or len(key) < 10:
        return ""
    return key[:6] + "..." + key[-4:]


def _merge_personalities(custom_personalities):
    personalities = dict(DEFAULT_PERSONALITIES)
    if isinstance(custom_personalities, dict):
        for key, value in custom_personalities.items():
            style_name = str(key).strip()
            style_text = str(value).strip() if value is not None else ""
            if not style_name or style_name in DEFAULT_PERSONALITIES or not style_text:
                continue
            personalities[style_name] = style_text
    return personalities


def _load_models_sync(cog):
    """Load model metadata synchronously (called once per settings session, not per render)."""
    models = {}
    config_path = cog.config_json_path
    try:
        with open(config_path, "r") as f:
            data = json.load(f)
        models.update(data.get("MODELS", {}))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    for pid, info in PROVIDERS.items():
        default = info.get("default_model", "")
        if default and default not in models:
            parts = default.split("/")
            actual = "/".join(parts[1:]) if len(parts) > 1 else parts[0]
            display = parts[-1] if parts else actual
            name = display.replace("-", " ").title()
            models[default] = {
                "name": f"{info['label']} - {name}",
                "provider": pid,
                "actualModelId": actual,
            }
    return models


async def _read_config_json(config_path):
    async with _config_file_lock:
        loop = asyncio.get_running_loop()

        def _read():
            try:
                with open(config_path, "r") as f:
                    return json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                return {}

        return await loop.run_in_executor(None, _read)


async def _write_config_json(config_path, data):
    async with _config_file_lock:
        loop = asyncio.get_running_loop()

        def _write():
            os.makedirs(os.path.dirname(config_path), exist_ok=True)
            temp_path = f"{config_path}.tmp"
            with open(temp_path, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(temp_path, config_path)

        await loop.run_in_executor(None, _write)


async def _edit_settings_panel(interaction: discord.Interaction, cog, session_id, user_id, guild_id, *, content=None):
    try:
        view = SettingsView(cog, session_id, user_id, guild_id)
    except ValueError:
        await interaction.response.send_message(":x: Session expired.", ephemeral=True)
        return False
    embed = await view.build_embed()
    if content is None:
        await interaction.response.edit_message(embed=embed, view=view)
    else:
        await interaction.response.edit_message(content=content, embed=embed, view=view)
    return True


async def _run_endpoint_tests(cog):
    async def _test(provider):
        try:
            return await cog.ai_service._test_endpoint(provider)
        except Exception as e:
            log.error("Test endpoint failed for %s: %s", provider, e)
            return {
                "name": PROVIDER_LABELS.get(provider, provider),
                "status": "network_error",
                "latency": None,
                "message": str(e),
            }

    return await asyncio.gather(*[_test(provider) for provider in PROVIDER_ORDER])


def _build_test_results_embed(results, cog, session_id, user_id):
    valid = sum(1 for r in results if r.get("status") == "valid")
    invalid = sum(1 for r in results if r.get("status") == "invalid")
    not_cfg = sum(1 for r in results if r.get("status") == "not_configured")
    rate_lim = sum(1 for r in results if r.get("status") == "rate_limited")
    errors = sum(1 for r in results if r.get("status") == "network_error")
    results_embed = discord.Embed(color=0x0099ff, title="🔍 API Endpoint Test Results")
    results_embed.set_footer(text=f"{valid} of {len(results)} providers valid")
    for r in results:
        status = r.get("status", "error")
        name = r.get("name", "Unknown")
        if status == "valid":
            text = f"Valid - {r.get('latency', '?')}ms"
            icon = ":white_check_mark:"
        elif status == "invalid":
            text = "Invalid API Key"
            icon = ":x:"
        elif status == "not_configured":
            text = "Not configured"
            icon = "⚪"
        elif status == "rate_limited":
            text = "Rate limited (key may be valid)"
            icon = ":warning:️"
        elif status == "network_error":
            text = f"Network error: {r.get('message', '')}"[:1024]
            icon = ":globe_with_meridians:"
        else:
            text = str(r.get("message", "Unknown error"))[:1024]
            icon = ":x:"
        results_embed.add_field(name=f"{icon} {name}", value=text, inline=True)
    summary_parts = [f":white_check_mark: Valid: {valid}"]
    if invalid:
        summary_parts.append(f":x: Invalid: {invalid}")
    if not_cfg:
        summary_parts.append(f"⚪ Not configured: {not_cfg}")
    if rate_lim:
        summary_parts.append(f":warning:️ Rate limited: {rate_lim}")
    if errors:
        summary_parts.append(f"🔴 Errors: {errors}")
    results_embed.add_field(name=":bar_chart: Summary", value="\n".join(summary_parts) or "No providers configured", inline=False)
    view = OwnerOnlyView(user_id, timeout=SESSION_TIMEOUT)
    view.add_item(NavButton("\U0001F504 Test Again", "test_again", session_id, cog, user_id))
    view.add_item(NavButton("✖ Close", "close", session_id, cog, user_id))
    return results_embed, view


class ParamModal(Modal):
    def __init__(self, label, min_val, max_val, current, key, session_id, cog):
        super().__init__(title=f"Edit {label}", timeout=300)
        self.label = label
        self.key = key
        self.session_id = session_id
        self.cog = cog
        self.min_val = min_val
        self.max_val = max_val
        self.add_item(
            TextInput(
                label=f"{label} ({min_val}-{max_val})",
                placeholder=f"Current: {current}",
                default=str(current),
                required=True,
                min_length=1,
                max_length=10,
            )
        )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            value_text = self.children[0].value.strip()
            if self.key in ("max_tokens", "top_k"):
                value = int(value_text)
            else:
                value = float(value_text)
        except ValueError:
            await interaction.response.send_message(":x: Invalid number", ephemeral=True)
            return

        if value < self.min_val or value > self.max_val:
            await interaction.response.send_message(
                f":x: Value must be between {self.min_val} and {self.max_val}.", ephemeral=True
            )
            return

        guild_id = interaction.guild_id
        async with self.cog.config.guild_from_id(guild_id).all() as cfg:
            cfg[self.key] = value
        log.info("SETTINGS_UI %s updated to %s", self.key, value)

        await _edit_settings_panel(interaction, self.cog, self.session_id, interaction.user.id, guild_id)


class AddStyleModal(Modal):
    def __init__(self, session_id, cog):
        super().__init__(title="Add a Roast Style", timeout=300)
        self.session_id = session_id
        self.cog = cog
        self.add_item(
            TextInput(
                label="Style key (e.g. my-custom-style)",
                placeholder="Lowercase, no spaces",
                required=True,
                min_length=2,
                max_length=64,
            )
        )
        self.add_item(
            TextInput(
                label="Style prompt (AI instructions)",
                style=discord.TextStyle.paragraph,
                required=True,
                max_length=2000,
            )
        )

    async def on_submit(self, interaction: discord.Interaction):
        import re
        style_name = self.children[0].value.strip()
        style_prompt = self.children[1].value.strip()
        if not re.match(r"^[a-z0-9][a-z0-9_-]*$", style_name):
            await interaction.response.send_message(
                ":x: Style key must start with a lowercase letter/number and contain only letters, numbers, hyphens, or underscores.",
                ephemeral=True,
            )
            return
        if not style_name or not style_prompt:
            await interaction.response.send_message(":x: Both name and prompt are required.", ephemeral=True)
            return
        guild_id = interaction.guild_id
        async with self.cog.config.guild_from_id(guild_id).all() as cfg:
            custom_personalities = dict(cfg.get("custom_personalities", {}))
            if style_name in DEFAULT_PERSONALITIES or style_name in custom_personalities:
                await interaction.response.send_message(
                    ":x: That style key already exists. Pick a different name.",
                    ephemeral=True,
                )
                return
            if len(custom_personalities) >= MAX_STYLES_TOTAL:
                await interaction.response.send_message(
                    f":x: Style limit reached ({MAX_STYLES_TOTAL} total). Remove an existing style before adding a new one.",
                    ephemeral=True,
                )
                return
            custom_personalities[style_name] = style_prompt
            cfg["custom_personalities"] = custom_personalities
            cfg["promptKey"] = style_name
        session = self.cog.settings_sessions.get(self.session_id)
        if session is not None:
            session["personalities"] = _merge_personalities(custom_personalities)
        log.info("SETTINGS_UI Style added: %s", style_name)
        await _edit_settings_panel(interaction, self.cog, self.session_id, interaction.user.id, guild_id)


class SettingsView(View):
    def __init__(self, cog, session_id, user_id, guild_id):
        super().__init__(timeout=SESSION_TIMEOUT)
        self.cog = cog
        self.session_id = session_id
        self.user_id = user_id
        self.guild_id = guild_id
        self.session = cog.settings_sessions.get(session_id)
        if self.session is None:
            raise ValueError(f"Settings session {session_id} not found")
        self.session["_last_accessed"] = time.time() * 1000
        if "models" not in self.session:
            self.session["models"] = _load_models_sync(self.cog)
        self._build_components()

    def _build_components(self):
        self.clear_items()
        self.add_item(PageSelect(self.session_id, self.cog, self.user_id, self.session["current_page"]))
        page = self.session["current_page"]
        if page == "model":
            self._add_model_components()
        elif page == "parameters":
            self._add_parameter_components()
        elif page == "apikeys":
            self._add_apikeys_components()
        elif page == "advanced":
            self._add_advanced_components()

    def _add_model_components(self):
        self.add_item(ModelSelect(self.session_id, self.cog, self.user_id))
        self.add_item(PromptSelect(self.session_id, self.cog, self.user_id))

    def _add_parameter_components(self):
        for param_key, param_info in [
            ("temp", "Temperature"), ("tokens", "Max Tokens"), ("topp", "Top P"),
        ]:
            self.add_item(ParamButton(param_key, param_info, self.session_id, self.cog, self.user_id))
        for param_key, param_info in [
            ("topk", "Top K"), ("freq", "Freq Penalty"), ("pres", "Pres Penalty"),
        ]:
            self.add_item(ParamButton(param_key, param_info, self.session_id, self.cog, self.user_id))

    def _add_apikeys_components(self):
        for provider in PROVIDER_ORDER:
            self.add_item(ApiKeyButton(provider, self.session_id, self.cog, self.user_id))

    def _add_advanced_components(self):
        self.add_item(FetchModeSelect(self.session_id, self.cog, self.user_id))
        self.add_item(ToggleRandomButton(self.session_id, self.cog, self.user_id))
        self.add_item(HotTakeConfigButton(self.session_id, self.cog, self.user_id))
        self.add_item(TestEndpointsButton(self.session_id, self.cog, self.user_id))
        self.add_item(AddStyleButton(self.session_id, self.cog, self.user_id))
        self.add_item(RemoveStyleButton(self.session_id, self.cog, self.user_id))
        self.add_item(ProviderOrderSelect(self.session_id, self.cog, self.user_id))
        self.add_item(MoveUpButton(self.session_id, self.cog, self.user_id))
        self.add_item(MoveDownButton(self.session_id, self.cog, self.user_id))
        self.add_item(SaveOrderButton(self.session_id, self.cog, self.user_id))
        self.add_item(ResetOrderButton(self.session_id, self.cog, self.user_id))
        self.add_item(ResetButton(self.session_id, self.cog, self.user_id))

    async def build_embed(self):
        page_key = self.session["current_page"]
        title, icon = PAGE_TITLES[page_key]
        async with self.cog.config.guild_from_id(self.guild_id).all() as cfg:
            embed = discord.Embed(
                title=f"{icon} {title}",
                color=0x0099ff,
            )
            embed.set_footer(text=f"Page {PAGE_ORDER.index(page_key) + 1}/{len(PAGE_ORDER)}")
            if page_key == "model":
                self._build_model_embed(embed, cfg)
            elif page_key == "parameters":
                self._build_parameter_embed(embed, cfg)
            elif page_key == "apikeys":
                await self._build_apikeys_embed(embed)
            elif page_key == "advanced":
                self._build_advanced_embed(embed, cfg)
            return embed

    def _build_model_embed(self, embed, cfg):
        model = cfg.get("model", PROVIDERS[PROVIDER_ORDER[0]]["default_model"])
        provider_key = model.split("/")[0] if "/" in model else model
        embed.add_field(name="🤖 Current Model", value=f"`{model}`", inline=False)
        embed.add_field(name="📡 Provider", value=PROVIDER_LABELS.get(provider_key, provider_key), inline=True)
        embed.add_field(name=":memo: Active Style", value=f"`{cfg.get('promptKey', 'base')}`", inline=True)
        embed.add_field(
            name="🧩 Style Scope",
            value="Built-in styles are shared. Custom styles are stored per server.",
            inline=False,
        )

    def _build_parameter_embed(self, embed, cfg):
        embed.color = 0x7c3aed
        param_lines = " - ".join([
            f"**Temp:** `{cfg.get('temperature', 1.0)}`",
            f"**Tokens:** `{cfg.get('max_tokens', 4096)}`",
            f"**Top P:** `{cfg.get('top_p', 0.9)}`",
            f"**Top K:** `{cfg.get('top_k', 40)}`",
            f"**Freq:** `{cfg.get('frequency_penalty', 0.4)}`",
            f"**Pres:** `{cfg.get('presence_penalty', 0.2)}`",
        ])
        embed.add_field(name=":gear:️ Generation Parameters", value=param_lines, inline=False)

    async def _build_apikeys_embed(self, embed):
        embed.color = 0xf59e0b
        embed.description = (
            "Click a provider below to **set, change, or remove** its API key.\n"
            "Keys are stored securely via Red's API token system and changing one affects every guild using this bot."
        )
        lines = []
        for p in PROVIDER_ORDER:
            try:
                tokens = await self.cog.bot.get_shared_api_tokens(p)
            except Exception as e:
                log.debug("SETTINGS_UI Failed to read API tokens for %s: %s", p, e)
                tokens = {}
            full_key = tokens.get("api_key", "") if tokens else ""
            has_key = bool(full_key)
            masked = _mask_key(full_key) if has_key else ""
            icon = ":white_check_mark:" if has_key else ":x:"
            label = PROVIDER_LABELS.get(p, p)
            parts = [f"{icon} **{label}**"]
            if masked:
                parts.append(f"`{masked}`")
            lines.append(" ".join(parts))
        embed.add_field(name="Provider Status", value="\n".join(lines), inline=False)

    def _build_advanced_embed(self, embed, cfg):
        embed.color = 0x6366f1
        mode = cfg.get("messageFetchMode", "random")
        fetch_emoji = "📬" if mode == "recent" else "🎲"
        random_emoji = ":white_check_mark:" if cfg.get("randomMode", False) else ":x:"
        embed.add_field(
            name=f"{fetch_emoji} Fetch Mode",
            value="**Recent** (latest messages)" if mode == "recent" else "**Random** (varied history)",
            inline=True,
        )
        embed.add_field(
            name=f"{random_emoji} Random Style",
            value="On" if cfg.get("randomMode") else "Off",
            inline=True,
        )
        raw_order = cfg.get("provider_order") or list(PROVIDER_ORDER)
        order = []
        seen = set()
        for provider in raw_order:
            if provider in PROVIDER_ORDER and provider not in seen:
                order.append(provider)
                seen.add(provider)
        for provider in PROVIDER_ORDER:
            if provider not in seen:
                order.append(provider)
                seen.add(provider)
        order_labels = " → ".join(PROVIDER_LABELS.get(p, p) for p in order)
        embed.add_field(
            name="🧭 Provider Order",
            value=order_labels[:1000] if order_labels else "Default registry order",
            inline=False,
        )
        embed.add_field(
            name="🔥 Hot Take Tuning",
            value=(
                f"Window: `{cfg.get('hot_take_window_minutes', 5)}m` | "
                f"Cooldown: `{cfg.get('hot_take_cooldown_minutes', 120)}m`\n"
                f"Min Msg: `{cfg.get('hot_take_min_messages', 10)}` | "
                f"Prob: `{cfg.get('hot_take_probability', 0.05)}`\n"
                f"Context Msg: `{cfg.get('hot_take_context_messages', 100)}`"
            ),
            inline=False,
        )
        settings_json = json.dumps(dict(cfg), indent=2)
        truncated = settings_json[:800] + ("..." if len(settings_json) > 800 else "")
        embed.add_field(name="📋 All Settings", value=f"```json\n{truncated}\n```", inline=False)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(":x: This isn't your settings panel.", ephemeral=True)
            return False
        session = self.cog.settings_sessions.get(self.session_id)
        if session is None:
            await interaction.response.send_message(":x: Session expired. Open `lsettings` or `/lsettings` again.", ephemeral=True)
            return False
        session["_last_accessed"] = time.time() * 1000
        return True

    async def on_timeout(self):
        self.cog.settings_sessions.pop(self.session_id, None)


class HotTakeConfigModal(Modal):
    def __init__(self, session_id, cog, current):
        super().__init__(title="Hot Take Settings", timeout=300)
        self._session_id = session_id
        self._cog = cog
        self.add_item(TextInput(label="Window Minutes (1-120)", default=str(current.get("hot_take_window_minutes", 5)), max_length=4))
        self.add_item(TextInput(label="Cooldown Minutes (1-1440)", default=str(current.get("hot_take_cooldown_minutes", 120)), max_length=5))
        self.add_item(TextInput(label="Min Messages (1-200)", default=str(current.get("hot_take_min_messages", 10)), max_length=4))
        self.add_item(TextInput(label="Probability (0.0-1.0)", default=str(current.get("hot_take_probability", 0.05)), max_length=6))
        self.add_item(TextInput(label="Context Messages (5-500)", default=str(current.get("hot_take_context_messages", 100)), max_length=4))

    async def on_submit(self, interaction: discord.Interaction):
        try:
            window = int(self.children[0].value.strip())
            cooldown = int(self.children[1].value.strip())
            min_msgs = int(self.children[2].value.strip())
            prob = float(self.children[3].value.strip())
            ctx_msgs = int(self.children[4].value.strip())
        except ValueError:
            await interaction.response.send_message(":x: Invalid hot-take setting values.", ephemeral=True)
            return
        if not (1 <= window <= 120 and 1 <= cooldown <= 1440 and 1 <= min_msgs <= 200 and 0 <= prob <= 1 and 5 <= ctx_msgs <= 500):
            await interaction.response.send_message(":x: One or more values are out of allowed range.", ephemeral=True)
            return
        guild_id = interaction.guild_id
        async with self._cog.config.guild_from_id(guild_id).all() as cfg:
            cfg["hot_take_window_minutes"] = window
            cfg["hot_take_cooldown_minutes"] = cooldown
            cfg["hot_take_min_messages"] = min_msgs
            cfg["hot_take_probability"] = prob
            cfg["hot_take_context_messages"] = ctx_msgs
        await _edit_settings_panel(interaction, self._cog, self._session_id, interaction.user.id, guild_id)


class HotTakeConfigButton(Button):
    def __init__(self, session_id, cog, user_id):
        super().__init__(label="🔥 Hot Take Config", style=discord.ButtonStyle.secondary, custom_id=f"settings_hotcfg_{session_id}")
        self._session_id = session_id
        self._cog = cog
        self._user_id = user_id

    async def callback(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        async with self._cog.config.guild_from_id(guild_id).all() as cfg:
            current = dict(cfg)
        modal = HotTakeConfigModal(self._session_id, self._cog, current)
        await interaction.response.send_modal(modal)


class PageSelect(Select):
    def __init__(self, session_id, cog, user_id, current_page):
        self._session_id = session_id
        self._cog = cog
        self._user_id = user_id
        options = []
        for key in PAGE_ORDER:
            title, icon = PAGE_TITLES[key]
            options.append(
                discord.SelectOption(
                    label=title, value=key, emoji=icon,
                    default=key == current_page,
                )
            )
        options.append(
            discord.SelectOption(label="✖ Close", value="close")
        )
        super().__init__(
            placeholder="Navigate to page...",
            options=options,
            custom_id=f"settings_pagesel_{session_id}",
        )

    async def callback(self, interaction: discord.Interaction):
        value = self.values[0]
        if value == "close":
            self._cog.settings_sessions.pop(self._session_id, None)
            await interaction.response.edit_message(
                content=":white_check_mark: Settings closed.",
                embed=None, view=None,
            )
            return
        session = self._cog.settings_sessions.get(self._session_id)
        if not session:
            await interaction.response.send_message(":x: Session expired.", ephemeral=True)
            return
        session["current_page"] = value
        session["_last_accessed"] = time.time() * 1000
        guild_id = interaction.guild_id
        await _edit_settings_panel(interaction, self._cog, self._session_id, self._user_id, guild_id)


class NavButton(Button):
    def __init__(self, label, action, session_id, cog, user_id):
        style = discord.ButtonStyle.danger if action == "close" else discord.ButtonStyle.secondary
        super().__init__(label=label, style=style, custom_id=f"settings_nav_{action}_{session_id}")
        self._action = action
        self._session_id = session_id
        self._cog = cog
        self._user_id = user_id

    async def callback(self, interaction: discord.Interaction):
        session = self._cog.settings_sessions.get(self._session_id)
        if not session:
            await interaction.response.send_message(":x: Session expired.", ephemeral=True)
            return
        if self._action == "close":
            self._cog.settings_sessions.pop(self._session_id, None)
            await interaction.response.edit_message(content=":white_check_mark: Settings closed.", embed=None, view=None)
            return
        if self._action == "test_again":
            await interaction.response.defer()
            embed = discord.Embed(
                color=0xffaa00,
                title="🔍 Testing API Endpoints...",
                description="Please wait while we test each provider...",
            )
            await interaction.edit_original_response(embed=embed, view=None)
            results = await _run_endpoint_tests(self._cog)
            results_embed, view = _build_test_results_embed(results, self._cog, self._session_id, self._user_id)
            await interaction.edit_original_response(embed=results_embed, view=view)
            return


class ModelSelect(Select):
    def __init__(self, session_id, cog, user_id):
        self._session_id = session_id
        self._cog = cog
        self._user_id = user_id
        session = cog.settings_sessions.get(session_id, {})
        models = session.get("models") or _load_models_sync(cog)
        model_ids = list(models.keys())
        options = [
            discord.SelectOption(
                label=m.get("name", mid)[:100],
                value=mid,
                description=m.get("provider", "unknown")[:100] or "unknown",
            )
            for mid in model_ids for m in [models.get(mid, {})]
        ]
        if not options:
            options = [discord.SelectOption(label="No models available", value="none")]
        super().__init__(
            placeholder="Select model...",
            options=options[:25],
            custom_id=f"settings_model_{session_id}",
        )

    async def callback(self, interaction: discord.Interaction):
        model_id = self.values[0]
        if model_id == "none":
            await interaction.response.send_message(":x: No model selected.", ephemeral=True)
            return
        guild_id = interaction.guild_id
        async with self._cog.config.guild_from_id(guild_id).all() as cfg:
            cfg["model"] = model_id
        log.info("SETTINGS_UI Model updated to %s", model_id)
        await _edit_settings_panel(interaction, self._cog, self._session_id, self._user_id, guild_id)


class ParamButton(Button):
    def __init__(self, param_key, label, session_id, cog, user_id):
        super().__init__(label=label, style=discord.ButtonStyle.primary, custom_id=f"settings_edit_{param_key}_{session_id}")
        self._param_key = param_key
        self._session_id = session_id
        self._cog = cog
        self._user_id = user_id

    async def callback(self, interaction: discord.Interaction):
        param_map = {
            "temp": {"label": "Temperature", "min": 0, "max": 2, "key": "temperature"},
            "tokens": {"label": "Max Tokens", "min": 100, "max": 8192, "key": "max_tokens"},
            "topp": {"label": "Top P", "min": 0, "max": 1, "key": "top_p"},
            "topk": {"label": "Top K", "min": 1, "max": 100, "key": "top_k"},
            "freq": {"label": "Frequency Penalty", "min": 0, "max": 2, "key": "frequency_penalty"},
            "pres": {"label": "Presence Penalty", "min": 0, "max": 2, "key": "presence_penalty"},
        }
        info = param_map.get(self._param_key)
        if not info:
            await interaction.response.send_message(":x: Unknown parameter.", ephemeral=True)
            return
        guild_id = interaction.guild_id
        async with self._cog.config.guild_from_id(guild_id).all() as cfg:
            current = cfg.get(info["key"], info.get("min", 0))
        modal = ParamModal(info["label"], info["min"], info["max"], current, info["key"], self._session_id, self._cog)
        await interaction.response.send_modal(modal)


class ApiKeySetModal(Modal):
    def __init__(self, provider, label, session_id, cog):
        super().__init__(title=f"{label} API Key", timeout=300)
        self.provider = provider
        self.session_id = session_id
        self.cog = cog
        self.add_item(
            TextInput(
                label=f"{label} API Key",
                placeholder="Paste your API key, or leave empty to clear it",
                required=False,
                max_length=512,
                style=discord.TextStyle.short,
            )
        )

    async def on_submit(self, interaction: discord.Interaction):
        key = (self.children[0].value or "").strip()
        try:
            if key:
                await set_shared_api_key(interaction.client, self.provider, key)
                text = f":white_check_mark: **{PROVIDER_LABELS.get(self.provider, self.provider)}** API key saved!"
            else:
                await set_shared_api_key(interaction.client, self.provider, "")
                text = f":wastebasket: **{PROVIDER_LABELS.get(self.provider, self.provider)}** API key cleared."
        except Exception as e:
            log.error("SETTINGS_UI Failed to update API key for %s: %s", self.provider, e)
            await interaction.response.send_message(":x: Failed to update API key. Check logs.", ephemeral=True)
            return
        log.info("SETTINGS_UI API key updated for %s", self.provider)
        guild_id = interaction.guild_id
        await _edit_settings_panel(
            interaction,
            self.cog,
            self.session_id,
            interaction.user.id,
            guild_id,
            content=text,
        )


class ApiKeyButton(Button):
    def __init__(self, provider, session_id, cog, user_id):
        label = PROVIDER_LABELS.get(provider, provider)
        super().__init__(label=label, style=discord.ButtonStyle.primary, custom_id=f"settings_apikey_{provider}_{session_id}")
        self.provider = provider
        self._session_id = session_id
        self._cog = cog
        self._user_id = user_id

    async def callback(self, interaction: discord.Interaction):
        modal = ApiKeySetModal(self.provider, PROVIDER_LABELS.get(self.provider, self.provider), self._session_id, self._cog)
        await interaction.response.send_modal(modal)


class PromptSelect(Select):
    def __init__(self, session_id, cog, user_id):
        self._session_id = session_id
        self._cog = cog
        self._user_id = user_id
        personalities = dict(cog.settings_sessions.get(session_id, {}).get("personalities", {}))
        if not personalities:
            personalities = dict(DEFAULT_PERSONALITIES)
        keys = list(personalities.keys())
        options = [
            discord.SelectOption(label=key.replace("_", " ").replace("-", " ").title(), value=key)
            for key in keys
        ]
        options.append(discord.SelectOption(label="\u270f\ufe0f Custom...", value="__custom__", description="Create a new style"))
        super().__init__(
            placeholder="Select style...",
            options=options[:25],
            custom_id=f"settings_prompt_{session_id}",
        )

    async def callback(self, interaction: discord.Interaction):
        selected = self.values[0]
        if selected == "__custom__":
            modal = AddStyleModal(self._session_id, self._cog)
            await interaction.response.send_modal(modal)
            return
        guild_id = interaction.guild_id
        async with self._cog.config.guild_from_id(guild_id).all() as cfg:
            cfg["promptKey"] = selected
        log.info("SETTINGS_UI Style set to %s", selected)
        await _edit_settings_panel(interaction, self._cog, self._session_id, self._user_id, guild_id)


class FetchModeSelect(Select):
    def __init__(self, session_id, cog, user_id):
        self._session_id = session_id
        self._cog = cog
        self._user_id = user_id
        super().__init__(
            placeholder="Select fetch mode...",
            options=[
                discord.SelectOption(label="Recent", value="recent", description="Most recent messages"),
                discord.SelectOption(label="Random", value="random", description="Messages from across history"),
            ],
            custom_id=f"settings_fetchmode_{session_id}",
        )

    async def callback(self, interaction: discord.Interaction):
        mode = self.values[0]
        guild_id = interaction.guild_id
        async with self._cog.config.guild_from_id(guild_id).all() as cfg:
            cfg["messageFetchMode"] = mode
        log.info("SETTINGS_UI Fetch mode updated to %s", mode)
        await _edit_settings_panel(interaction, self._cog, self._session_id, self._user_id, guild_id)


class ToggleRandomButton(Button):
    def __init__(self, session_id, cog, user_id):
        super().__init__(label="Toggle Random Style", style=discord.ButtonStyle.secondary, custom_id=f"settings_toggle_random_{session_id}")
        self._session_id = session_id
        self._cog = cog
        self._user_id = user_id

    async def callback(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        async with self._cog.config.guild_from_id(guild_id).all() as cfg:
            cfg["randomMode"] = not cfg.get("randomMode", False)
        log.info("SETTINGS_UI Random mode toggled")
        await _edit_settings_panel(interaction, self._cog, self._session_id, self._user_id, guild_id)


class TestEndpointsButton(Button):
    def __init__(self, session_id, cog, user_id):
        super().__init__(label="🔍 Test All Endpoints", style=discord.ButtonStyle.primary, custom_id=f"settings_test_endpoints_{session_id}")
        self._session_id = session_id
        self._cog = cog
        self._user_id = user_id

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        embed = discord.Embed(
            color=0xffaa00,
            title="🔍 Testing API Endpoints...",
            description="Please wait while we test each provider...",
        )
        await interaction.edit_original_response(embed=embed, view=None)
        results = await _run_endpoint_tests(self._cog)
        results_embed, view = _build_test_results_embed(results, self._cog, self._session_id, self._user_id)
        await interaction.edit_original_response(embed=results_embed, view=view)


class ResetButton(Button):
    def __init__(self, session_id, cog, user_id):
        super().__init__(label="Reset to Defaults", style=discord.ButtonStyle.danger, custom_id=f"settings_reset_{session_id}")
        self._session_id = session_id
        self._cog = cog
        self._user_id = user_id

    async def callback(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        try:
            await self._cog.config.guild_from_id(guild_id).clear()
        except Exception as e:
            log.error("Failed to reset settings: %s", e)
            await interaction.response.send_message(":x: Failed to reset settings.", ephemeral=True)
            return
        session = self._cog.settings_sessions.get(self._session_id)
        if session is not None:
            session["working_order"] = list(PROVIDER_ORDER)
            session.pop("selected_provider", None)
        log.info("SETTINGS_UI Settings reset to defaults")
        await _edit_settings_panel(interaction, self._cog, self._session_id, self._user_id, guild_id)


class AddStyleButton(Button):
    def __init__(self, session_id, cog, user_id):
        super().__init__(label="➕ Add Style", style=discord.ButtonStyle.success, custom_id=f"settings_addstyle_{session_id}")
        self._session_id = session_id
        self._cog = cog
        self._user_id = user_id

    async def callback(self, interaction: discord.Interaction):
        modal = AddStyleModal(self._session_id, self._cog)
        await interaction.response.send_modal(modal)


class OwnerOnlyView(View):
    def __init__(self, user_id, timeout=900):
        super().__init__(timeout=timeout)
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(":x: This isn't your settings panel.", ephemeral=True)
            return False
        return True


class RemoveStyleSelect(Select):
    def __init__(self, session_id, cog, user_id, personalities):
        self._session_id = session_id
        self._cog = cog
        self._user_id = user_id
        options = [
            discord.SelectOption(label=key.capitalize(), value=key, description=(personalities[key] or "")[:100])
            for key in personalities
        ]
        super().__init__(
            placeholder="Select a style to remove...",
            options=options[:25] if options else [discord.SelectOption(label="None available", value="none")],
            custom_id=f"settings_doremove_{session_id}",
        )

    async def callback(self, interaction: discord.Interaction):
        if not self.values or self.values[0] == "none":
            await interaction.response.send_message("No style selected.", ephemeral=True)
            return
        style_key = self.values[0]
        guild_id = interaction.guild_id
        async with self._cog.config.guild_from_id(guild_id).all() as cfg:
            custom_personalities = dict(cfg.get("custom_personalities", {}))
            if style_key in custom_personalities:
                del custom_personalities[style_key]
                cfg["custom_personalities"] = custom_personalities
                if cfg.get("promptKey") == style_key:
                    cfg["promptKey"] = "blunt" if "blunt" in DEFAULT_PERSONALITIES else next(iter(DEFAULT_PERSONALITIES))
                session = self._cog.settings_sessions.get(self._session_id)
                if session is not None:
                    session["personalities"] = _merge_personalities(custom_personalities)
                log.info("SETTINGS_UI Style removed: %s", style_key)
        await _edit_settings_panel(interaction, self._cog, self._session_id, self._user_id, interaction.guild_id)


class RemoveStyleButton(Button):
    def __init__(self, session_id, cog, user_id):
        super().__init__(label="➖ Remove Style", style=discord.ButtonStyle.danger, custom_id=f"settings_removestyle_{session_id}")
        self._session_id = session_id
        self._cog = cog
        self._user_id = user_id

    async def callback(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        async with self._cog.config.guild_from_id(guild_id).all() as cfg:
            personalities = {
                key: value
                for key, value in dict(cfg.get("custom_personalities", {})).items()
                if key not in DEFAULT_PERSONALITIES
            }
        view = OwnerOnlyView(self._user_id, timeout=SESSION_TIMEOUT)
        view.add_item(RemoveStyleSelect(self._session_id, self._cog, self._user_id, personalities))
        embed = discord.Embed(
            color=0x6366f1,
            title=":wrench: Remove a Custom Roast Style",
            description="Select a custom style to remove. Built-in styles are always available.",
        )
        await interaction.response.edit_message(embed=embed, view=view)


class ProviderOrderSelect(Select):
    def __init__(self, session_id, cog, user_id):
        self._session_id = session_id
        self._cog = cog
        self._user_id = user_id
        session = cog.settings_sessions.get(session_id, {})
        working = session.get("working_order", list(PROVIDER_ORDER))
        options = []
        for p in working:
            label = PROVIDER_LABELS.get(p, p)
            options.append(discord.SelectOption(
                label=label, value=p,
                description=f"Position {working.index(p) + 1}",
            ))
        super().__init__(
            placeholder="Select a provider to move...",
            options=options[:25] if options else [discord.SelectOption(label="None", value="none")],
            custom_id=f"settings_ordsel_{session_id}",
        )

    async def callback(self, interaction: discord.Interaction):
        session = self._cog.settings_sessions.get(self._session_id)
        if not session:
            await interaction.response.send_message(":x: Session expired.", ephemeral=True)
            return
        session["selected_provider"] = self.values[0]
        session.setdefault("working_order", list(PROVIDER_ORDER))
        await _edit_settings_panel(interaction, self._cog, self._session_id, self._user_id, interaction.guild_id)


class MoveUpButton(Button):
    def __init__(self, session_id, cog, user_id):
        super().__init__(label="^ Move Up", style=discord.ButtonStyle.primary, custom_id=f"settings_mvup_{session_id}")
        self._session_id = session_id
        self._cog = cog
        self._user_id = user_id

    async def callback(self, interaction: discord.Interaction):
        session = self._cog.settings_sessions.get(self._session_id)
        if not session:
            await interaction.response.send_message(":x: Session expired.", ephemeral=True)
            return
        working = session.get("working_order", list(PROVIDER_ORDER))
        selected = session.get("selected_provider")
        if not selected or selected not in working:
            await interaction.response.send_message(":x: Select a provider first.", ephemeral=True)
            return
        idx = working.index(selected)
        if idx == 0:
            await interaction.response.send_message(":x: Already at the top.", ephemeral=True)
            return
        working[idx], working[idx - 1] = working[idx - 1], working[idx]
        session["working_order"] = working
        await _edit_settings_panel(interaction, self._cog, self._session_id, self._user_id, interaction.guild_id)


class MoveDownButton(Button):
    def __init__(self, session_id, cog, user_id):
        super().__init__(label="v Move Down", style=discord.ButtonStyle.primary, custom_id=f"settings_mvdown_{session_id}")
        self._session_id = session_id
        self._cog = cog
        self._user_id = user_id

    async def callback(self, interaction: discord.Interaction):
        session = self._cog.settings_sessions.get(self._session_id)
        if not session:
            await interaction.response.send_message(":x: Session expired.", ephemeral=True)
            return
        working = session.get("working_order", list(PROVIDER_ORDER))
        selected = session.get("selected_provider")
        if not selected or selected not in working:
            await interaction.response.send_message(":x: Select a provider first.", ephemeral=True)
            return
        idx = working.index(selected)
        if idx == len(working) - 1:
            await interaction.response.send_message(":x: Already at the bottom.", ephemeral=True)
            return
        working[idx], working[idx + 1] = working[idx + 1], working[idx]
        session["working_order"] = working
        await _edit_settings_panel(interaction, self._cog, self._session_id, self._user_id, interaction.guild_id)


class SaveOrderButton(Button):
    def __init__(self, session_id, cog, user_id):
        super().__init__(label="💾 Save Order", style=discord.ButtonStyle.success, custom_id=f"settings_saveord_{session_id}")
        self._session_id = session_id
        self._cog = cog
        self._user_id = user_id

    async def callback(self, interaction: discord.Interaction):
        session = self._cog.settings_sessions.get(self._session_id)
        if not session:
            await interaction.response.send_message(":x: Session expired.", ephemeral=True)
            return
        working = [p for p in session.get("working_order", list(PROVIDER_ORDER)) if p in PROVIDER_ORDER]
        seen = set()
        working = [p for p in working if not (p in seen or seen.add(p))]
        for p in PROVIDER_ORDER:
            if p not in seen:
                working.append(p)
                seen.add(p)
        async with self._cog.config.guild_from_id(interaction.guild_id).all() as cfg:
            cfg["provider_order"] = working
        session.pop("selected_provider", None)
        log.info("SETTINGS_UI Provider order saved: %s", working)
        await _edit_settings_panel(interaction, self._cog, self._session_id, self._user_id, interaction.guild_id)


class ResetOrderButton(Button):
    def __init__(self, session_id, cog, user_id):
        super().__init__(label="↺ Reset to Default", style=discord.ButtonStyle.secondary, custom_id=f"settings_rstord_{session_id}")
        self._session_id = session_id
        self._cog = cog
        self._user_id = user_id

    async def callback(self, interaction: discord.Interaction):
        session = self._cog.settings_sessions.get(self._session_id)
        if not session:
            await interaction.response.send_message(":x: Session expired.", ephemeral=True)
            return
        async with self._cog.config.guild_from_id(interaction.guild_id).all() as cfg:
            cfg["provider_order"] = None
        session["working_order"] = list(PROVIDER_ORDER)
        session.pop("selected_provider", None)
        log.info("SETTINGS_UI Provider order reset to default")
        await _edit_settings_panel(interaction, self._cog, self._session_id, self._user_id, interaction.guild_id)
