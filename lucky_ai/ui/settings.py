import json
import time
import asyncio
import logging
import os

import discord
from discord.ui import View, Modal, Select, Button, TextInput

from ..providers import PROVIDER_ORDER, PROVIDER_LABELS, PROVIDERS

log = logging.getLogger("red.LuckyAICog.settings_ui")

PAGE_ORDER = ["model", "parameters", "apikeys", "fetch", "endpoints", "advanced"]

PAGE_TITLES = {
    "model": ("Model Selection", "🤖"),
    "parameters": ("Generation Parameters", "⚙️"),
    "apikeys": ("API Keys", "🔑"),
    "fetch": ("Message Fetching", "📨"),
    "endpoints": ("Test Endpoints", "🔍"),
    "advanced": ("Advanced Settings", "🔧"),
}

SESSION_TIMEOUT = 900

_config_file_lock = asyncio.Lock()


def _mask_key(key):
    if not key or len(key) < 10:
        return ""
    return key[:6] + "..." + key[-4:]


async def _read_config_json(config_path):
    async with _config_file_lock:
        loop = asyncio.get_event_loop()

        def _read():
            try:
                with open(config_path, "r") as f:
                    return json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                return {}

        return await loop.run_in_executor(None, _read)


async def _write_config_json(config_path, data):
    async with _config_file_lock:
        loop = asyncio.get_event_loop()

        def _write():
            with open(config_path, "w") as f:
                json.dump(data, f, indent=2)

        await loop.run_in_executor(None, _write)


async def _run_endpoint_tests(cog):
    results = []
    for provider in PROVIDER_ORDER:
        try:
            result = await cog.ai_service._test_endpoint(provider)
            results.append(result)
        except Exception as e:
            log.error("Test endpoint failed for %s: %s", provider, e)
            results.append({
                "name": PROVIDER_LABELS.get(provider, provider),
                "status": "network_error",
                "latency": None,
                "message": str(e),
            })
    return results


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
            icon = "✅"
        elif status == "invalid":
            text = "Invalid API Key"
            icon = "❌"
        elif status == "not_configured":
            text = "Not configured"
            icon = "⚪"
        elif status == "rate_limited":
            text = "Rate limited (key may be valid)"
            icon = "⚠️"
        elif status == "network_error":
            text = f"Network error: {r.get('message', '')}"
            icon = "🌐"
        else:
            text = r.get("message", "Unknown error")
            icon = "❌"
        results_embed.add_field(name=f"{icon} {name}", value=text, inline=True)
    summary_parts = [f"✅ Valid: {valid}"]
    if invalid:
        summary_parts.append(f"❌ Invalid: {invalid}")
    if not_cfg:
        summary_parts.append(f"⚪ Not configured: {not_cfg}")
    if rate_lim:
        summary_parts.append(f"⚠️ Rate limited: {rate_lim}")
    if errors:
        summary_parts.append(f"🔴 Errors: {errors}")
    results_embed.add_field(name="📊 Summary", value="\n".join(summary_parts) or "No providers configured", inline=False)
    view = View(timeout=SESSION_TIMEOUT)
    view.add_item(NavButton("🔄 Test Again", "test_again", session_id, cog, user_id))
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

        view = SettingsView(self.cog, self.session_id, interaction.user.id, guild_id)
        view.session["current_page"] = "parameters"
        embed = await view.build_embed()
        await interaction.response.edit_message(embed=embed, view=view)


class NewStyleModal(Modal):
    def __init__(self, session_id, cog):
        super().__init__(title="New Style", timeout=300)
        self.session_id = session_id
        self.cog = cog
        self.add_item(
            TextInput(
                label="Style guidance",
                placeholder="e.g. Focus on their terrible taste in music and failed life choices",
                style=discord.TextStyle.paragraph,
                required=True,
                max_length=1000,
            )
        )

    async def on_submit(self, interaction: discord.Interaction):
        style_text = self.children[0].value.strip()
        if not style_text:
            await interaction.response.send_message(":x: Style text is required", ephemeral=True)
            return
        guild_id = interaction.guild_id
        style_name = f"custom-{int(time.time() * 1000)}"
        config_path = os.path.join(self.cog.config_file_parent, "config", "config.json")
        data = await _read_config_json(config_path)
        personalities = data.get("PERSONALITIES", {})
        personalities[style_name] = style_text
        data["PERSONALITIES"] = personalities
        await _write_config_json(config_path, data)
        async with self.cog.config.guild_from_id(guild_id).all() as cfg:
            cfg["promptKey"] = style_name
        log.info("SETTINGS_UI New style created: %s", style_name)

        view = SettingsView(self.cog, self.session_id, interaction.user.id, guild_id)
        view.session["current_page"] = "parameters"
        embed = await view.build_embed()
        await interaction.response.edit_message(embed=embed, view=view)


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
        config_path = os.path.join(self.cog.config_file_parent, "config", "config.json")
        data = await _read_config_json(config_path)
        personalities = data.get("PERSONALITIES", {})
        personalities[style_name] = style_prompt
        data["PERSONALITIES"] = personalities
        await _write_config_json(config_path, data)
        log.info("SETTINGS_UI Style added: %s", style_name)

        guild_id = interaction.guild_id
        view = SettingsView(self.cog, self.session_id, interaction.user.id, guild_id)
        view.session["current_page"] = "advanced"
        embed = await view.build_embed()
        await interaction.response.edit_message(embed=embed, view=view)


class SettingsView(View):
    def __init__(self, cog, session_id, user_id, guild_id):
        super().__init__(timeout=SESSION_TIMEOUT)
        self.cog = cog
        self.session_id = session_id
        self.user_id = user_id
        self.guild_id = guild_id
        self.session = cog.settings_sessions.get(session_id)
        if self.session is None:
            self.session = {
                "current_page": "model",
                "model_page": 0,
                "prompt_page": 0,
                "show_model_dropdown": False,
                "show_prompt_dropdown": False,
            }
            cog.settings_sessions[session_id] = self.session
        self._build_components()

    def _build_components(self):
        self.clear_items()
        page = self.session["current_page"]
        self._add_nav_buttons()
        if page == "model":
            self._add_model_components()
        elif page == "parameters":
            self._add_parameter_components()
        elif page == "apikeys":
            self._add_apikeys_components()
        elif page == "fetch":
            self._add_fetch_components()
        elif page == "endpoints":
            self._add_endpoints_components()
        elif page == "advanced":
            self._add_advanced_components()

    def _add_nav_buttons(self):
        page_index = PAGE_ORDER.index(self.session["current_page"])
        if page_index > 0:
            self.add_item(NavButton("◀ Page", "prev", self.session_id, self.cog, self.user_id))
        self.add_item(NavButton("✖ Close", "close", self.session_id, self.cog, self.user_id))
        if page_index < len(PAGE_ORDER) - 1:
            self.add_item(NavButton("Page ▶", "next", self.session_id, self.cog, self.user_id))

    def _add_model_components(self):
        self.add_item(ToggleModelDropdownButton(self.session_id, self.cog, self.user_id))
        if self.session.get("show_model_dropdown"):
            self.add_item(ModelSelect(self.session_id, self.cog, self.user_id, self.session.get("model_page", 0)))
            self.add_item(ModelPageButton("◀ Models", "prev", self.session_id, self.cog, self.user_id))
            self.add_item(ModelPageButton("Models ▶", "next", self.session_id, self.cog, self.user_id))

    def _add_parameter_components(self):
        for param_key, param_info in [
            ("temp", "Temperature"), ("tokens", "Max Tokens"), ("topp", "Top P"),
        ]:
            self.add_item(ParamButton(param_key, param_info, self.session_id, self.cog, self.user_id))
        for param_key, param_info in [
            ("topk", "Top K"), ("freq", "Freq Penalty"), ("pres", "Pres Penalty"),
        ]:
            self.add_item(ParamButton(param_key, param_info, self.session_id, self.cog, self.user_id))
        self.add_item(TogglePromptDropdownButton(self.session_id, self.cog, self.user_id))
        if self.session.get("show_prompt_dropdown"):
            self.add_item(PromptSelect(self.session_id, self.cog, self.user_id, self.session.get("prompt_page", 0)))
            self.add_item(PromptPageButton("◀ Styles", "prev", self.session_id, self.cog, self.user_id))
            self.add_item(PromptPageButton("Styles ▶", "next", self.session_id, self.cog, self.user_id))

    def _add_apikeys_components(self):
        for provider in PROVIDER_ORDER:
            self.add_item(ApiKeyButton(provider, self.session_id, self.cog, self.user_id))

    def _add_fetch_components(self):
        self.add_item(FetchModeSelect(self.session_id, self.cog, self.user_id))
        self.add_item(ToggleRandomButton(self.session_id, self.cog, self.user_id))

    def _add_endpoints_components(self):
        self.add_item(TestEndpointsButton(self.session_id, self.cog, self.user_id))

    def _add_advanced_components(self):
        self.add_item(ResetButton(self.session_id, self.cog, self.user_id))
        self.add_item(AddStyleButton(self.session_id, self.cog, self.user_id))
        self.add_item(RemoveStyleButton(self.session_id, self.cog, self.user_id))

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
            elif page_key == "fetch":
                self._build_fetch_embed(embed, cfg)
            elif page_key == "apikeys":
                self._build_apikeys_embed(embed)
            elif page_key == "endpoints":
                self._build_endpoints_embed(embed)
            elif page_key == "advanced":
                self._build_advanced_embed(embed, cfg)
            return embed

    def _build_model_embed(self, embed, cfg):
        model = cfg.get("model", PROVIDERS["nvidia"]["default_model"])
        provider_key = model.split("/")[0] if "/" in model else model
        embed.add_field(name="🤖 Current Model", value=f"`{model}`", inline=False)
        embed.add_field(name="📡 Provider", value=PROVIDER_LABELS.get(provider_key, provider_key), inline=True)
        embed.add_field(name="📏 Context Length", value="Varies by model", inline=True)

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
        embed.add_field(name="⚙️ Generation Parameters", value=param_lines, inline=False)
        embed.add_field(name="📝 Active Style", value=f"`{cfg.get('promptKey', 'base')}`", inline=False)

    def _build_fetch_embed(self, embed, cfg):
        embed.color = 0x10a37f
        mode = cfg.get("messageFetchMode", "random")
        fetch_emoji = "📬" if mode == "recent" else "🎲"
        random_emoji = "✅" if cfg.get("randomMode", False) else "❌"
        embed.add_field(
            name=f"{fetch_emoji} Fetch Mode",
            value="**Recent** (latest messages)" if mode == "recent" else "**Random** (varied history)",
            inline=False,
        )
        embed.add_field(
            name=f"{random_emoji} Random Style",
            value="Picks random user from message pool" if cfg.get("randomMode") else "Selects based on message order",
            inline=False,
        )

    def _build_apikeys_embed(self, embed):
        embed.color = 0xf59e0b
        embed.description = "Click a provider below to **set, change, or remove** its API key.\nKeys are stored securely via Red's API token system."
        lines = []
        for p in PROVIDER_ORDER:
            tokens = self.cog.bot.get_shared_api_tokens(p)
            full_key = tokens.get("api_key", "") if tokens else ""
            has_key = bool(full_key)
            masked = _mask_key(full_key) if has_key else ""
            icon = "✅" if has_key else "❌"
            label = PROVIDER_LABELS.get(p, p)
            parts = [f"{icon} **{label}**"]
            if masked:
                parts.append(f"`{masked}`")
            lines.append(" ".join(parts))
        embed.add_field(name="Provider Status", value="\n".join(lines), inline=False)
        embed.add_field(name="🔑 Quick Actions", value="Configured keys are tested automatically.\nUse the **Endpoints** page to verify they work.", inline=False)
        embed.set_footer(text="API keys are stored in Red's shared API token storage")

    def _build_endpoints_embed(self, embed):
        embed.color = 0x40a7d6
        embed.description = 'Click **"Test All Endpoints"** below to verify your API keys are working.'
        embed.add_field(
            name="🧪 Available Providers",
            value=" - ".join(p.capitalize() for p in PROVIDER_ORDER),
            inline=False,
        )
        embed.add_field(name="📡 Test Status", value="⏳ Click the button to begin testing", inline=False)

    def _build_advanced_embed(self, embed, cfg):
        embed.color = 0x6366f1
        settings_json = json.dumps(dict(cfg), indent=2)
        truncated = settings_json[:900] + ("..." if len(settings_json) > 900 else "")
        embed.add_field(name="📋 All Settings (JSON)", value=f"```json\n{truncated}\n```", inline=False)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(":x: This isn't your settings panel.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        self.cog.settings_sessions.pop(self.session_id, None)


class NavButton(Button):
    def __init__(self, label, action, session_id, cog, user_id):
        style = discord.ButtonStyle.danger if action == "close" else discord.ButtonStyle.secondary
        super().__init__(label=label, style=style, custom_id=f"settings_{action}_{session_id}")
        self._action = action
        self._session_id = session_id
        self._cog = cog
        self._user_id = user_id

    async def callback(self, interaction: discord.Interaction):
        session = self._cog.settings_sessions.get(self._session_id)
        if not session:
            await interaction.response.send_message(":x: Session expired. Run /settings again.", ephemeral=True)
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
        page_index = PAGE_ORDER.index(session["current_page"])
        if self._action == "next" and page_index < len(PAGE_ORDER) - 1:
            session["current_page"] = PAGE_ORDER[page_index + 1]
        elif self._action == "prev" and page_index > 0:
            session["current_page"] = PAGE_ORDER[page_index - 1]
        session["model_page"] = 0
        session["show_model_dropdown"] = False
        session["show_prompt_dropdown"] = False
        view = SettingsView(self._cog, self._session_id, self._user_id, interaction.guild_id)
        embed = await view.build_embed()
        await interaction.response.edit_message(embed=embed, view=view)


class ToggleModelDropdownButton(Button):
    def __init__(self, session_id, cog, user_id):
        super().__init__(label="🔍 Change Model", style=discord.ButtonStyle.primary, custom_id=f"settings_toggle_model_{session_id}")
        self._session_id = session_id
        self._cog = cog
        self._user_id = user_id

    async def callback(self, interaction: discord.Interaction):
        session = self._cog.settings_sessions.get(self._session_id)
        if not session:
            await interaction.response.send_message(":x: Session expired.", ephemeral=True)
            return
        session["show_model_dropdown"] = not session.get("show_model_dropdown", False)
        view = SettingsView(self._cog, self._session_id, self._user_id, interaction.guild_id)
        embed = await view.build_embed()
        await interaction.response.edit_message(embed=embed, view=view)


class ModelSelect(Select):
    def __init__(self, session_id, cog, user_id, page=0):
        self._session_id = session_id
        self._cog = cog
        self._user_id = user_id
        self._page = page
        models = self._load_models()
        model_ids = list(models.keys())
        page_size = 25
        start = page * page_size
        end = start + page_size
        page_ids = model_ids[start:end]
        total_pages = max(1, (len(model_ids) + page_size - 1) // page_size)
        options = [
            discord.SelectOption(
                label=m.get("name", mid)[:100],
                value=mid,
                description=m.get("provider", "unknown")[:100] or "unknown",
            )
            for mid in page_ids for m in [models.get(mid, {})]
        ]
        if not options:
            options = [discord.SelectOption(label="No models available", value="none")]
        super().__init__(
            placeholder=f"Select a model... (Page {page + 1}/{total_pages})",
            options=options[:25],
            custom_id=f"settings_model_{session_id}_{page}",
        )

    def _load_models(self):
        models = {}
        config_path = os.path.join(self._cog.config_file_parent, "config", "config.json")
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
                actual = parts[-1] if len(parts) > 1 else parts[0]
                name = actual.replace("-", " ").title()
                models[default] = {
                    "name": f"{info['label']} - {name}",
                    "provider": pid,
                    "actualModelId": actual,
                }
        return models

    async def callback(self, interaction: discord.Interaction):
        model_id = self.values[0]
        if model_id == "none":
            await interaction.response.send_message(":x: No model selected.", ephemeral=True)
            return
        guild_id = interaction.guild_id
        async with self._cog.config.guild_from_id(guild_id).all() as cfg:
            cfg["model"] = model_id
        log.info("SETTINGS_UI Model updated to %s", model_id)
        session = self._cog.settings_sessions.get(self._session_id)
        if session:
            session["show_model_dropdown"] = False
        view = SettingsView(self._cog, self._session_id, self._user_id, guild_id)
        embed = await view.build_embed()
        await interaction.response.edit_message(embed=embed, view=view)


class ModelPageButton(Button):
    def __init__(self, label, direction, session_id, cog, user_id):
        super().__init__(label=label, style=discord.ButtonStyle.secondary, custom_id=f"settings_model_{direction}_{session_id}")
        self._direction = direction
        self._session_id = session_id
        self._cog = cog
        self._user_id = user_id

    async def callback(self, interaction: discord.Interaction):
        session = self._cog.settings_sessions.get(self._session_id)
        if not session:
            await interaction.response.send_message(":x: Session expired.", ephemeral=True)
            return
        current = session.get("model_page", 0)
        config_path = os.path.join(self._cog.config_file_parent, "config", "config.json")
        data = await _read_config_json(config_path)
        models = data.get("MODELS", {})
        total_pages = max(1, (len(models) + 24) // 25)
        if self._direction == "next" and current < total_pages - 1:
            session["model_page"] = current + 1
        elif self._direction == "prev" and current > 0:
            session["model_page"] = current - 1
        else:
            await interaction.response.send_message("No more pages in that direction.", ephemeral=True)
            return
        view = SettingsView(self._cog, self._session_id, self._user_id, interaction.guild_id)
        embed = await view.build_embed()
        await interaction.response.edit_message(embed=embed, view=view)


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
        if key:
            await interaction.client.set_shared_api_tokens(self.provider, api_key=key)
            text = f":white_check_mark: **{PROVIDER_LABELS.get(self.provider, self.provider)}** API key saved!"
        else:
            await interaction.client.set_shared_api_tokens(self.provider, api_key="")
            text = f":wastebasket: **{PROVIDER_LABELS.get(self.provider, self.provider)}** API key cleared."
        log.info("SETTINGS_UI API key updated for %s", self.provider)
        guild_id = interaction.guild_id
        view = SettingsView(self.cog, self.session_id, interaction.user.id, guild_id)
        embed = await view.build_embed()
        await interaction.response.edit_message(content=text, embed=embed, view=view)


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


class TogglePromptDropdownButton(Button):
    def __init__(self, session_id, cog, user_id):
        super().__init__(label="\U0001f4dd Style", style=discord.ButtonStyle.primary, custom_id=f"settings_toggle_prompt_{session_id}")
        self._session_id = session_id
        self._cog = cog
        self._user_id = user_id

    async def callback(self, interaction: discord.Interaction):
        session = self._cog.settings_sessions.get(self._session_id)
        if not session:
            await interaction.response.send_message(":x: Session expired.", ephemeral=True)
            return
        session["show_prompt_dropdown"] = not session.get("show_prompt_dropdown", False)
        view = SettingsView(self._cog, self._session_id, self._user_id, interaction.guild_id)
        embed = await view.build_embed()
        await interaction.response.edit_message(embed=embed, view=view)


class PromptSelect(Select):
    def __init__(self, session_id, cog, user_id, page=0):
        self._session_id = session_id
        self._cog = cog
        self._user_id = user_id
        self._page = page
        personalities = self._load_personalities()
        keys = list(personalities.keys())
        page_size = 24
        start = page * page_size
        end = start + page_size
        page_keys = keys[start:end]
        total_pages = max(1, (len(keys) + page_size - 1) // page_size)
        options = [
            discord.SelectOption(label=key.capitalize(), value=key)
            for key in page_keys
        ]
        if page == max(0, total_pages - 1):
            options.append(discord.SelectOption(label="✏️ Custom...", value="__custom__", description="Create a new style"))
        super().__init__(
            placeholder=f"Select a style... (Page {page + 1}/{total_pages})",
            options=options[:25],
            custom_id=f"settings_prompt_{session_id}_{page}",
        )

    def _load_personalities(self):
        config_path = os.path.join(self._cog.config_file_parent, "config", "config.json")
        try:
            with open(config_path, "r") as f:
                data = json.load(f)
            return data.get("PERSONALITIES", {})
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    async def callback(self, interaction: discord.Interaction):
        selected = self.values[0]
        if selected == "__custom__":
            modal = NewStyleModal(self._session_id, self._cog)
            await interaction.response.send_modal(modal)
            return
        guild_id = interaction.guild_id
        async with self._cog.config.guild_from_id(guild_id).all() as cfg:
            cfg["promptKey"] = selected
        log.info("SETTINGS_UI Style set to %s", selected)
        session = self._cog.settings_sessions.get(self._session_id)
        if session:
            session["show_prompt_dropdown"] = False
        view = SettingsView(self._cog, self._session_id, self._user_id, guild_id)
        embed = await view.build_embed()
        await interaction.response.edit_message(embed=embed, view=view)


class PromptPageButton(Button):
    def __init__(self, label, direction, session_id, cog, user_id):
        super().__init__(label=label, style=discord.ButtonStyle.secondary, custom_id=f"settings_prompt_{direction}_{session_id}")
        self._direction = direction
        self._session_id = session_id
        self._cog = cog
        self._user_id = user_id

    async def callback(self, interaction: discord.Interaction):
        session = self._cog.settings_sessions.get(self._session_id)
        if not session:
            await interaction.response.send_message(":x: Session expired.", ephemeral=True)
            return
        current = session.get("prompt_page", 0)
        config_path = os.path.join(self._cog.config_file_parent, "config", "config.json")
        data = await _read_config_json(config_path)
        personalities = data.get("PERSONALITIES", {})
        total_pages = max(1, (len(personalities) + 23) // 24)
        if self._direction == "next" and current < total_pages - 1:
            session["prompt_page"] = current + 1
        elif self._direction == "prev" and current > 0:
            session["prompt_page"] = current - 1
        else:
            await interaction.response.send_message("No more pages in that direction.", ephemeral=True)
            return
        view = SettingsView(self._cog, self._session_id, self._user_id, interaction.guild_id)
        embed = await view.build_embed()
        await interaction.response.edit_message(embed=embed, view=view)


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
        view = SettingsView(self._cog, self._session_id, self._user_id, guild_id)
        embed = await view.build_embed()
        await interaction.response.edit_message(embed=embed, view=view)


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
        view = SettingsView(self._cog, self._session_id, self._user_id, guild_id)
        embed = await view.build_embed()
        await interaction.response.edit_message(embed=embed, view=view)


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
        log.info("SETTINGS_UI Settings reset to defaults")
        view = SettingsView(self._cog, self._session_id, self._user_id, guild_id)
        embed = await view.build_embed()
        await interaction.response.edit_message(embed=embed, view=view)


class AddStyleButton(Button):
    def __init__(self, session_id, cog, user_id):
        super().__init__(label="➕ Add Style", style=discord.ButtonStyle.success, custom_id=f"settings_addstyle_{session_id}")
        self._session_id = session_id
        self._cog = cog
        self._user_id = user_id

    async def callback(self, interaction: discord.Interaction):
        modal = AddStyleModal(self._session_id, self._cog)
        await interaction.response.send_modal(modal)


class StyleRemoveView(View):
    def __init__(self, user_id, *args, **kwargs):
        super().__init__(*args, **kwargs)
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
        config_path = os.path.join(self._cog.config_file_parent, "config", "config.json")
        data = await _read_config_json(config_path)
        personalities = data.get("PERSONALITIES", {})
        if style_key in personalities:
            del personalities[style_key]
            data["PERSONALITIES"] = personalities
            await _write_config_json(config_path, data)
            log.info("SETTINGS_UI Style removed: %s", style_key)
        view = SettingsView(self._cog, self._session_id, self._user_id, interaction.guild_id)
        embed = await view.build_embed()
        await interaction.response.edit_message(embed=embed, view=view)


class RemoveStyleButton(Button):
    def __init__(self, session_id, cog, user_id):
        super().__init__(label="➖ Remove Style", style=discord.ButtonStyle.danger, custom_id=f"settings_removestyle_{session_id}")
        self._session_id = session_id
        self._cog = cog
        self._user_id = user_id

    async def callback(self, interaction: discord.Interaction):
        config_path = os.path.join(self._cog.config_file_parent, "config", "config.json")
        data = await _read_config_json(config_path)
        personalities = data.get("PERSONALITIES", {})
        view = StyleRemoveView(self._user_id, timeout=SESSION_TIMEOUT)
        view.add_item(RemoveStyleSelect(self._session_id, self._cog, self._user_id, personalities))
        await interaction.response.edit_message(view=view)
