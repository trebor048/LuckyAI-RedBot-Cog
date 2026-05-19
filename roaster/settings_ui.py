import json
import time
import asyncio
import logging
import os

import discord
from discord.ui import View, Modal, Select, Button, TextInput
from redbot.core import commands

from .ai_service import PROVIDER_ORDER
from .utils import PROVIDER_LABELS

log = logging.getLogger("red.RoasterCog.settings_ui")

PAGE_ORDER = ["model", "parameters", "fetch", "endpoints", "advanced"]

PAGE_TITLES = {
    "model": ("Model Selection", "🤖"),
    "parameters": ("Generation Parameters", ":gear:️"),
    "fetch": ("Message Fetching", "📨"),
    "endpoints": ("Test Endpoints", "🔍"),
    "advanced": ("Advanced Settings", ":wrench:"),
}

SESSION_TIMEOUT = 900


class ParamModal(Modal):
    def __init__(self, label, min_val, max_val, current, key, session_id, cog):
        super().__init__(title=f"Edit {label}", timeout=300)
        self.label = label
        self.key = key
        self.session_id = session_id
        self.cog = cog
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
        try:
            with open(config_path, "r") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        personalities = data.get("PERSONALITIES", {})
        personalities[style_name] = style_text
        data["PERSONALITIES"] = personalities
        with open(config_path, "w") as f:
            json.dump(data, f, indent=2)
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
        style_name = self.children[0].value.strip()
        style_prompt = self.children[1].value.strip()
        import re
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
        try:
            with open(config_path, "r") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        personalities = data.get("PERSONALITIES", {})
        personalities[style_name] = style_prompt
        data["PERSONALITIES"] = personalities
        with open(config_path, "w") as f:
            json.dump(data, f, indent=2)
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
        self.session = cog.settings_sessions.get(session_id, {
            "current_page": "model",
            "model_page": 0,
            "prompt_page": 0,
            "show_model_dropdown": False,
            "show_prompt_dropdown": False,
        })
        self._build_components()

    def _build_components(self):
        self.clear_items()
        page = self.session["current_page"]
        self._add_nav_buttons()
        if page == "model":
            self._add_model_components()
        elif page == "parameters":
            self._add_parameter_components()
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
        row2 = self._make_row()
        for param_key, param_info in [
            ("topk", "Top K"), ("freq", "Freq Penalty"), ("pres", "Pres Penalty"),
        ]:
            row2.add_item(ParamButton(param_key, param_info, self.session_id, self.cog, self.user_id))
        self.add_item(TogglePromptDropdownButton(self.session_id, self.cog, self.user_id))
        if self.session.get("show_prompt_dropdown"):
            self.add_item(PromptSelect(self.session_id, self.cog, self.user_id, self.session.get("prompt_page", 0)))
            self.add_item(PromptPageButton("◀ Styles", "prev", self.session_id, self.cog, self.user_id))
            self.add_item(PromptPageButton("Styles ▶", "next", self.session_id, self.cog, self.user_id))

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
                await self._build_model_embed(embed, cfg)
            elif page_key == "parameters":
                await self._build_parameter_embed(embed, cfg)
            elif page_key == "fetch":
                await self._build_fetch_embed(embed, cfg)
            elif page_key == "endpoints":
                await self._build_endpoints_embed(embed)
            elif page_key == "advanced":
                await self._build_advanced_embed(embed, cfg)
            return embed

    async def _build_model_embed(self, embed, cfg):
        model = cfg.get("model", "gpt-3.5-turbo")
        embed.add_field(name="🤖 Current Model", value=f"`{model}`", inline=False)
        embed.add_field(name="📡 Provider", value=PROVIDER_LABELS.get(model.split("/")[0] if "/" in model else model, model.split("/")[0] if "/" in model else model).capitalize(), inline=True)
        embed.add_field(name="📏 Context Length", value="`4096`", inline=True)

    async def _build_parameter_embed(self, embed, cfg):
        embed.color = 0x7c3aed
        param_lines = " • ".join([
            f"**Temp:** `{cfg.get('temperature', 0.7)}`",
            f"**Tokens:** `{cfg.get('max_tokens', 2000)}`",
            f"**Top P:** `{cfg.get('top_p', 1.0)}`",
            f"**Top K:** `{cfg.get('top_k', 40)}`",
            f"**Freq:** `{cfg.get('frequency_penalty', 0)}`",
            f"**Pres:** `{cfg.get('presence_penalty', 0)}`",
        ])
        embed.add_field(name=":gear:️ Generation Parameters", value=param_lines, inline=False)
        embed.add_field(name=":memo: Active Style", value=f"`{cfg.get('promptKey', 'base')}`", inline=False)

    async def _build_fetch_embed(self, embed, cfg):
        embed.color = 0x10a37f
        mode = cfg.get("messageFetchMode", "random")
        fetch_emoji = "📬" if mode == "recent" else "🎲"
        random_emoji = ":white_check_mark:" if cfg.get("randomMode", False) else ":x:"
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

    async def _build_endpoints_embed(self, embed):
        embed.color = 0x40a7d6
        embed.description = 'Click **"Test All Endpoints"** below to verify your API keys are working.'
        embed.add_field(
            name="🧪 Available Providers",
            value=" • ".join(p.capitalize() for p in PROVIDER_ORDER),
            inline=False,
        )
        embed.add_field(name="📡 Test Status", value=":hourglass_flowing_sand: Click the button to begin testing", inline=False)

    async def _build_advanced_embed(self, embed, cfg):
        embed.color = 0x6366f1
        settings_json = json.dumps(dict(cfg), indent=2)
        truncated = settings_json[:900] + ("..." if len(settings_json) > 900 else "")
        embed.add_field(name="📋 All Settings (JSON)", value=f"```json\n{truncated}\n```", inline=False)

    async def refresh(self, interaction: discord.Interaction):
        embed = await self.build_embed()
        self._build_components()
        await interaction.response.edit_message(embed=embed, view=self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(":x: This isn't your settings panel.", ephemeral=True)
            return False
        return True


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
            results = []
            for provider in PROVIDER_ORDER:
                result = await self._cog.ai_service._test_endpoint(provider)
                results.append(result)
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
                    text = f"Network error: {r.get('message', '')}"
                    icon = ":globe_with_meridians:"
                else:
                    text = r.get("message", "Unknown error")
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
            view = View(timeout=SESSION_TIMEOUT)
            view.add_item(NavButton(":arrows_counterclockwise: Test Again", "test_again", self._session_id, self._cog, self._user_id))
            view.add_item(NavButton("✖ Close", "close", self._session_id, self._cog, self._user_id))
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
        super().__init__(
            placeholder=f"Select a model... (Page {page + 1}/{total_pages})",
            options=options[:25],
            custom_id=f"settings_model_{session_id}_{page}",
        )

    def _load_models(self):
        config_path = os.path.join(self._cog.config_file_parent, "config", "config.json")
        try:
            with open(config_path, "r") as f:
                data = json.load(f)
            return data.get("MODELS", {})
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    async def callback(self, interaction: discord.Interaction):
        model_id = self.values[0]
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
        models = {}
        config_path = os.path.join(self._cog.config_file_parent, "config", "config.json")
        try:
            with open(config_path, "r") as f:
                data = json.load(f)
            models = data.get("MODELS", {})
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        total_pages = max(1, (len(models) + 24) // 25)
        if self._direction == "next" and current < total_pages - 1:
            session["model_page"] = current + 1
        elif self._direction == "prev" and current > 0:
            session["model_page"] = current - 1
        else:
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
            return
        guild_id = interaction.guild_id
        async with self._cog.config.guild_from_id(guild_id).all() as cfg:
            current = cfg.get(info["key"], info.get("min", 0))
        modal = ParamModal(info["label"], info["min"], info["max"], current, info["key"], self._session_id, self._cog)
        await interaction.response.send_modal(modal)


class TogglePromptDropdownButton(Button):
    def __init__(self, session_id, cog, user_id):
        super().__init__(label=":memo: Style", style=discord.ButtonStyle.primary, custom_id=f"settings_toggle_prompt_{session_id}")
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
            options.append(discord.SelectOption(label=":pencil2:️ Custom...", value="__custom__", description="Create a new style"))
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
        personalities = {}
        config_path = os.path.join(self._cog.config_file_parent, "config", "config.json")
        try:
            with open(config_path, "r") as f:
                data = json.load(f)
            personalities = data.get("PERSONALITIES", {})
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        total_pages = max(1, (len(personalities) + 23) // 24)
        if self._direction == "next" and current < total_pages - 1:
            session["prompt_page"] = current + 1
        elif self._direction == "prev" and current > 0:
            session["prompt_page"] = current - 1
        else:
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
        results = []
        for provider in PROVIDER_ORDER:
            result = await self._cog.ai_service._test_endpoint(provider)
            results.append(result)
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
                text = f"Network error: {r.get('message', '')}"
                icon = ":globe_with_meridians:"
            else:
                text = r.get("message", "Unknown error")
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
        view = View(timeout=SESSION_TIMEOUT)
        view.add_item(NavButton(":arrows_counterclockwise: Test Again", "test_again", self._session_id, self._cog, self._user_id))
        view.add_item(NavButton("✖ Close", "close", self._session_id, self._cog, self._user_id))
        await interaction.edit_original_response(embed=results_embed, view=view)


class ResetButton(Button):
    def __init__(self, session_id, cog, user_id):
        super().__init__(label="Reset to Defaults", style=discord.ButtonStyle.danger, custom_id=f"settings_reset_{session_id}")
        self._session_id = session_id
        self._cog = cog
        self._user_id = user_id

    async def callback(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        await self._cog.config.guild_from_id(guild_id).clear()
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


class RemoveStyleSelect(Select):
    def __init__(self, session_id, cog, user_id):
        self._session_id = session_id
        self._cog = cog
        self._user_id = user_id
        config_path = os.path.join(cog.config_file_parent, "config", "config.json")
        try:
            with open(config_path, "r") as f:
                data = json.load(f)
            personalities = data.get("PERSONALITIES", {})
        except (FileNotFoundError, json.JSONDecodeError):
            personalities = {}
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
            return
        style_key = self.values[0]
        config_path = os.path.join(self._cog.config_file_parent, "config", "config.json")
        try:
            with open(config_path, "r") as f:
                data = json.load(f)
            personalities = data.get("PERSONALITIES", {})
            if style_key in personalities:
                del personalities[style_key]
                data["PERSONALITIES"] = personalities
                with open(config_path, "w") as f:
                    json.dump(data, f, indent=2)
                log.info("SETTINGS_UI Style removed: %s", style_key)
        except (FileNotFoundError, json.JSONDecodeError):
            pass
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
        view = View(timeout=SESSION_TIMEOUT)
        view.add_item(RemoveStyleSelect(self._session_id, self._cog, self._user_id))
        await interaction.response.edit_message(view=view)
