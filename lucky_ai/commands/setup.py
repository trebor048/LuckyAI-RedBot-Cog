import os
import re
import json
import asyncio
import logging

import discord
from discord.ui import View, Modal, Button, TextInput

from ..providers import PROVIDER_ORDER, PROVIDER_LABELS, PROVIDER_BASE_URLS, FALLBACK_DEFAULT_MODELS, PROVIDERS

log = logging.getLogger("red.LuckyAICog.setup")

SESSION_TIMEOUT = 900

DEFAULT_MODEL = PROVIDERS["nvidia"]["default_model"]


def _generate_models_from_providers():
    models = {}
    for pid, info in PROVIDERS.items():
        default = info.get("default_model", "")
        if not default:
            continue
        parts = default.split("/")
        actual = parts[-1] if len(parts) > 1 else parts[0]
        name = actual.replace("-", " ").title()
        model_key = default
        models[model_key] = {
            "name": f"{info['label']} - {name}",
            "provider": pid,
            "actualModelId": actual,
        }
    return models


CONFIG_JSON_TEMPLATE = {
    "MODELS": _generate_models_from_providers(),
    "PERSONALITIES": {
        "clinical": "Cold, dispassionate dissection. Use precise psychological terminology. Make them feel like a case study in failure.",
        "disappointed": "You're not angry - you're profoundly disappointed. Channel a parent who expected better. Emphasis on wasted potential.",
        "sarcastic": "Vicious sarcasm and mockery. Dripping with irony. Every line should read like it has an eye-roll attached.",
        "blunt": "Brutal, unfiltered truth. Say what everyone thinks but is too polite to say. No sugar-coating. Just damage.",
        "analytical": "Systematically dismantle them. Expose contradictions, hypocrisy, self-deception. Use logic as a weapon.",
        "default": "All-purpose devastation. Attack character, choices, and insecurities. Balanced mix of cruelty and specificity."
    }
}

def ensure_config_json(config_path):
    if not os.path.exists(config_path):
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        with open(config_path, "w") as f:
            json.dump(CONFIG_JSON_TEMPLATE, f, indent=2)
        log.info("Created default config at %s", config_path)
        return True
    return False


def get_model_options_for_provider(provider):
    models = []
    seen = set()
    default_model = FALLBACK_DEFAULT_MODELS.get(provider, "")
    if default_model:
        label = PROVIDER_LABELS.get(provider, provider)
        display_name = default_model.split("/")[-1] if "/" in default_model else default_model
        models.append((default_model, f"{label} default - {display_name}"))
        seen.add(default_model)
    for mid, info in CONFIG_JSON_TEMPLATE["MODELS"].items():
        if info.get("provider") == provider and mid not in seen:
            models.append((mid, info["name"]))
            seen.add(mid)
    return models


class ApiKeyModal(Modal):
    def __init__(self, provider, label, session_id, cog):
        super().__init__(title=f"{label} API Key", timeout=300)
        self.provider = provider
        self.session_id = session_id
        self.cog = cog
        self.add_item(
            TextInput(
                label=f"{label} API Key",
                placeholder=f"sk-... or leave empty to skip {label}",
                required=False,
                max_length=512,
                style=discord.TextStyle.short,
            )
        )

    async def on_submit(self, interaction: discord.Interaction):
        key = self.children[0].value.strip() if self.children[0].value else ""
        session = self.cog.setup_sessions.get(self.session_id)
        if not session:
            await interaction.response.send_message(":x: Session expired. Run setup again.", ephemeral=True)
            return
        had_key = bool(session["api_keys"].get(self.provider))
        session["api_keys"][self.provider] = key
        if key:
            await interaction.client.set_shared_api_tokens(self.provider, api_key=key)
            if not had_key:
                session["configured_count"] += 1
            log.info("SETUP %s API key set via Red shared API tokens", self.provider)
        else:
            if not had_key:
                session["skipped_count"] += 1
            log.info("SETUP %s API key skipped", self.provider)
        session["current_step"] += 1
        view = SetupView(self.cog, self.session_id, interaction.user.id, interaction.guild_id)
        embed = await view.build_embed()
        await interaction.response.edit_message(embed=embed, view=view)


class ModelSelectView(View):
    def __init__(self, cog, session_id, user_id, guild_id):
        super().__init__(timeout=SESSION_TIMEOUT)
        self.cog = cog
        self.session_id = session_id
        self.user_id = user_id
        self.guild_id = guild_id
        self._build_components()

    def _build_components(self):
        self.clear_items()
        session = self.cog.setup_sessions.get(self.session_id, {})
        configured = [p for p in PROVIDER_ORDER if session.get("api_keys", {}).get(p)]
        for provider in configured:
            label = PROVIDER_LABELS.get(provider, provider)
            self.add_item(ModelSelectButton(provider, label, self.session_id, self.cog, self.user_id, self.guild_id))
        self.add_item(SkipModelButton(self.session_id, self.cog, self.user_id, self.guild_id))

    async def build_embed(self):
        session = self.cog.setup_sessions.get(self.session_id, {})
        configured = [p for p in PROVIDER_ORDER if session.get("api_keys", {}).get(p)]
        embed = discord.Embed(
            title=":robot: Select Default Model",
            color=0x0099ff,
            description=(
                "Choose which model to use as the default for roasting.\n\n"
                "**Configured providers:**\n" +
                "\n".join(f":white_check_mark: {PROVIDER_LABELS.get(p, p)}" for p in configured) +
                "\n\nClick a provider to pick a specific model from it."
            ),
        )
        embed.set_footer(text="The default model can be changed later via /settings")
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(":x: Not your setup panel.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        self.cog.setup_sessions.pop(self.session_id, None)


class ModelSelectButton(Button):
    def __init__(self, provider, label, session_id, cog, user_id, guild_id):
        super().__init__(label=label, style=discord.ButtonStyle.primary, custom_id=f"setup_model_{provider}_{session_id}")
        self.provider = provider
        self.session_id = session_id
        self.cog = cog
        self.user_id = user_id
        self.guild_id = guild_id

    async def callback(self, interaction: discord.Interaction):
        models = get_model_options_for_provider(self.provider)
        if not models:
            await interaction.response.send_message(f":x: No known models for {PROVIDER_LABELS.get(self.provider, self.provider)}", ephemeral=True)
            return
        options = [
            discord.SelectOption(label=name[:100], value=mid, description=f"{self.provider}/{mid.split('/')[-1]}"[:100])
            for mid, name in models
        ]
        view = ModelPickView(self.user_id, timeout=SESSION_TIMEOUT)
        select = ModelPickSelect(options, self.provider, self.session_id, self.cog, self.user_id, self.guild_id)
        view.add_item(select)
        await interaction.response.edit_message(
            content=f"Select a model from {PROVIDER_LABELS.get(self.provider, self.provider)}:",
            embed=None, view=view,
        )


class ModelPickView(View):
    def __init__(self, user_id, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(":x: Not your setup panel.", ephemeral=True)
            return False
        return True


class ModelPickSelect(discord.ui.Select):
    def __init__(self, options, provider, session_id, cog, user_id, guild_id):
        super().__init__(
            placeholder="Pick a model...",
            options=options[:25],
            custom_id=f"setup_model_pick_{provider}_{session_id}",
        )
        self.provider = provider
        self.session_id = session_id
        self.cog = cog
        self.user_id = user_id
        self.guild_id = guild_id

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(":x: Not your setup panel.", ephemeral=True)
            return
        model_id = self.values[0]
        session = self.cog.setup_sessions.get(self.session_id)
        if not session:
            await interaction.response.send_message(":x: Session expired. Run setup again.", ephemeral=True)
            return
        session["default_model"] = model_id
        async with self.cog.config.guild_from_id(self.guild_id).all() as cfg:
            cfg["model"] = model_id
        log.info("SETUP Default model set to %s", model_id)
        session["current_step"] += 1
        view = SetupView(self.cog, self.session_id, self.user_id, self.guild_id)
        embed = await view.build_embed()
        await interaction.response.edit_message(embed=embed, view=view)


class SkipModelButton(Button):
    def __init__(self, session_id, cog, user_id, guild_id):
        super().__init__(label="\u23e9 Keep Default", style=discord.ButtonStyle.secondary, custom_id=f"setup_skip_model_{session_id}")
        self.session_id = session_id
        self.cog = cog
        self.user_id = user_id
        self.guild_id = guild_id

    async def callback(self, interaction: discord.Interaction):
        session = self.cog.setup_sessions.get(self.session_id)
        if not session:
            await interaction.response.send_message(":x: Session expired. Run setup again.", ephemeral=True)
            return
        session["current_step"] += 1
        view = SetupView(self.cog, self.session_id, self.user_id, self.guild_id)
        embed = await view.build_embed()
        await interaction.response.edit_message(embed=embed, view=view)


class ChannelSelectModal(Modal):
    def __init__(self, guild_id, session_id, cog):
        super().__init__(title="Select a channel", timeout=300)
        self.guild_id = guild_id
        self.session_id = session_id
        self.cog = cog
        self.add_item(
            TextInput(
                label="Channel ID or #mention",
                placeholder="Paste a channel ID or #mention (e.g. #general)",
                required=True,
                max_length=100,
            )
        )

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.children[0].value.strip()
        match = re.search(r"(\d{17,20})", raw)
        if not match:
            await interaction.response.send_message(":x: Could not find a channel ID in that input.", ephemeral=True)
            return
        channel_id = match.group(1)
        guild = interaction.client.get_guild(int(self.guild_id))
        channel = guild.get_channel(int(channel_id)) if guild else None
        if not channel:
            await interaction.response.send_message(":x: Channel not found in this server.", ephemeral=True)
            return
        session = self.cog.setup_sessions.get(self.session_id)
        if not session:
            await interaction.response.send_message(":x: Session expired. Run setup again.", ephemeral=True)
            return
        async with self.cog.config.guild_from_id(int(self.guild_id)).all() as cfg:
            sync = cfg.get("sync_channels", [])
            if channel_id not in sync:
                sync.append(channel_id)
                cfg["sync_channels"] = sync
        session["sync_channel_id"] = channel_id
        session["current_step"] += 1
        view = SetupView(self.cog, self.session_id, interaction.user.id, int(self.guild_id))
        embed = await view.build_embed()
        text = f":white_check_mark: Syncing {channel.mention}. Starting backfill..."
        await interaction.response.edit_message(content=text, embed=embed, view=view)
        task = asyncio.create_task(self.cog._do_backfill(guild, channel, 14, interaction.user))
        task.add_done_callback(lambda t: log.error("BACKFILL task failed: %s", t.exception()) if t.exception() else None)


class PickChannelButton(Button):
    def __init__(self, session_id, cog, user_id, guild_id):
        super().__init__(label="\U0001f4e1 Pick Channel", style=discord.ButtonStyle.primary, custom_id=f"setup_channel_{session_id}")
        self.session_id = session_id
        self.cog = cog
        self.user_id = user_id
        self.guild_id = guild_id

    async def callback(self, interaction: discord.Interaction):
        modal = ChannelSelectModal(self.guild_id, self.session_id, self.cog)
        await interaction.response.send_modal(modal)


class SkipChannelButton(Button):
    def __init__(self, session_id, cog, user_id, guild_id):
        super().__init__(label="\u23e9 Skip", style=discord.ButtonStyle.secondary, custom_id=f"setup_skip_channel_{session_id}")
        self.session_id = session_id
        self.cog = cog
        self.user_id = user_id
        self.guild_id = guild_id

    async def callback(self, interaction: discord.Interaction):
        session = self.cog.setup_sessions.get(self.session_id)
        if not session:
            return
        session["current_step"] += 1
        view = SetupView(self.cog, self.session_id, self.user_id, self.guild_id)
        embed = await view.build_embed()
        await interaction.response.edit_message(embed=embed, view=view)


class SetupView(View):
    def __init__(self, cog, session_id, user_id, guild_id):
        super().__init__(timeout=SESSION_TIMEOUT)
        self.cog = cog
        self.session_id = session_id
        self.user_id = user_id
        self.guild_id = guild_id
        self.session = cog.setup_sessions.get(session_id)
        if self.session is None:
            raise ValueError(f"Setup session {session_id} not found")
        self._build_components()

    def _build_components(self):
        self.clear_items()
        session = self.session
        if session["finished"]:
            return
        step = session["current_step"]
        provider_count = len(PROVIDER_ORDER)
        model_step = provider_count + 1
        channel_step = provider_count + 2
        test_step = provider_count + 3
        finish_step = provider_count + 4

        if step == 0:
            self.add_item(StartSetupButton(self.session_id, self.cog, self.user_id))
        elif step <= provider_count:
            provider = PROVIDER_ORDER[step - 1]
            label = PROVIDER_LABELS.get(provider, provider)
            self.add_item(EnterKeyButton(provider, label, self.session_id, self.cog, self.user_id))
            self.add_item(SkipKeyButton(provider, self.session_id, self.cog, self.user_id))
            self.add_item(CancelSetupButton(self.session_id, self.cog, self.user_id))
        elif step == model_step:
            self.add_item(ChooseModelButton(self.session_id, self.cog, self.user_id, self.guild_id))
            self.add_item(SkipModelButton(self.session_id, self.cog, self.user_id, self.guild_id))
        elif step == channel_step:
            self.add_item(PickChannelButton(self.session_id, self.cog, self.user_id, self.guild_id))
            self.add_item(SkipChannelButton(self.session_id, self.cog, self.user_id, self.guild_id))
        elif step == test_step:
            self.add_item(SaveAndTestButton(self.session_id, self.cog, self.user_id))
            self.add_item(SkipTestButton(self.session_id, self.cog, self.user_id))
        elif step == finish_step:
            self.add_item(FinishSetupButton(self.session_id, self.cog, self.user_id))
            self.add_item(SetupCloseButton(self.session_id, self.cog, self.user_id))

    async def build_embed(self):
        session = self.session
        step = session["current_step"]
        provider_count = len(PROVIDER_ORDER)
        model_step = provider_count + 1
        channel_step = provider_count + 2
        test_step = provider_count + 3
        finish_step = provider_count + 4
        total = finish_step

        embed = discord.Embed(color=0x0099ff)

        if step == 0:
            embed.title = "\U0001f680 Lucky AI Setup"
            embed.description = (
                "Welcome to the Lucky AI setup wizard!\n\n"
                "This will guide you through configuring AI provider API keys so the bot can "
                "generate roasts, TLDRs, and answer questions.\n\n"
                f"**Supported providers ({provider_count}):**\n" +
                "\n".join(f"{i+1}. {PROVIDER_LABELS.get(p, p)}" for i, p in enumerate(PROVIDER_ORDER)) +
                "\n\nYou can skip any provider you don't want to use.\n"
                "**At least one** must be configured for the bot to work."
            )
            embed.set_footer(text=f"Step 1/{total} - Click 'Start Setup' to begin")
        elif step <= provider_count:
            provider = PROVIDER_ORDER[step - 1]
            label = PROVIDER_LABELS.get(provider, provider)
            base_url = PROVIDER_BASE_URLS.get(provider, "")
            fallback = FALLBACK_DEFAULT_MODELS.get(provider, "")
            existing_key = session.get("api_keys", {}).get(provider, "")
            masked = existing_key[:6] + "..." + existing_key[-4:] if existing_key and len(existing_key) >= 10 else ""
            embed.title = f"Step {step}/{total} - {label}"
            embed.description = (
                f"Configure your **{label}** API key.\n\n"
                f"**Endpoint:** `{base_url}`\n"
                f"**Default model:** `{fallback}`\n"
                + (f"**Current key:** `{masked}`\n" if masked else "")
                + "\nClick **Enter Key** to provide your API key, "
                "or **Skip** this provider."
            )
            done = [p for p in PROVIDER_ORDER[:step]]
            progress = []
            for p in PROVIDER_ORDER:
                has = bool(session.get("api_keys", {}).get(p))
                if p in done:
                    progress.append(f"{'✅' if has else '❌'} {PROVIDER_LABELS.get(p, p)}")
                else:
                    progress.append(f"\u26ab {PROVIDER_LABELS.get(p, p)}")
            embed.add_field(name="Progress", value="\n".join(progress), inline=False)
            embed.set_footer(text=f"Configured: {session['configured_count']} / Skipped: {session['skipped_count']}")
        elif step == model_step:
            configured = [p for p in PROVIDER_ORDER if session.get("api_keys", {}).get(p)]
            embed.title = f"Step {step}/{total} - Choose Default Model"
            embed.description = (
                "Pick which AI model to use by default.\n\n"
                "**Configured providers:**\n" +
                ("\n".join(f"\u2705 {PROVIDER_LABELS.get(p, p)}" for p in configured) if configured else "None yet") +
                f"\n\nCurrent default: `{session.get('default_model', DEFAULT_MODEL)}`\n\n"
                "Click a provider button to pick a model from it, or **Keep Default**."
            )
            embed.set_footer(text=f"Step {step}/{total}")
        elif step == channel_step:
            p = session.get("prefix", "l")
            embed.title = f"Step {step}/{total} - Pick a Sync Channel"
            embed.description = (
                "Choose a channel to sync messages from.\n"
                "This lets the bot read chat history for roasts, TLDRs, and Q&A.\n\n"
                "Click **Pick Channel** to select one from a list, "
                f"or **Skip** to configure later with `{p}lconfig channels add #channel`."
            )
            chosen = session.get("sync_channel_id")
            if chosen:
                embed.add_field(name="Current Selection", value=f"<#{chosen}>", inline=False)
            embed.set_footer(text=f"Step {step}/{total}")
        elif step == test_step:
            configured = [p for p in PROVIDER_ORDER if session.get("api_keys", {}).get(p)]
            skipped = [p for p in PROVIDER_ORDER if not session.get("api_keys", {}).get(p)]
            embed.title = f"Step {step}/{total} - Test Endpoints"
            embed.description = (
                "All done! Let's verify the endpoints work.\n\n"
                f"**Default model:** `{session.get('default_model', DEFAULT_MODEL)}`\n"
                f"**Configured ({len(configured)}):** {' - '.join(PROVIDER_LABELS.get(p, p) for p in configured) or 'None'}\n"
                f"**Skipped ({len(skipped)}):** {' - '.join(PROVIDER_LABELS.get(p, p) for p in skipped) or 'None'}\n\n"
                "Click **Save & Test** to verify your API keys, or **Skip** to finish."
            )
            embed.set_footer(text=f"Step {step}/{total}")
        elif step == finish_step:
            test_results = session.get("test_results", [])
            embed.title = "\u2705 Setup Complete!"
            valid = sum(1 for r in test_results if r.get("status") == "valid")
            not_cfg = sum(1 for r in test_results if r.get("status") == "not_configured")
            sync_id = session.get("sync_channel_id")
            sync_line = f"\n**Sync channel:** <#{sync_id}>\n" if sync_id else "\n"
            embed.description = (
                f"**Default model:** `{session.get('default_model', DEFAULT_MODEL)}`"
                f"{sync_line}"
                f"**Endpoint results:** {valid} working, {not_cfg} skipped\n\n"
            )
            if test_results:
                lines = []
                for r in test_results:
                    status = r.get("status")
                    name = r.get("name", "?")
                    if status == "valid":
                        lines.append(f"\u2705 {name} - {r.get('latency', '?')}ms")
                    elif status == "not_configured":
                        lines.append(f"\u26aa {name} - Skipped")
                    elif status == "invalid":
                        lines.append(f"\u274c {name} - Invalid key")
                    elif status == "rate_limited":
                        lines.append(f"\u26a0 {name} - Rate limited (key may be valid)")
                    else:
                        lines.append(f"\u274c {name} - {r.get('message', 'Error')}")
                embed.add_field(name="Endpoint Results", value="\n".join(lines), inline=False)
            p = session.get("prefix", "l")
            embed.add_field(
                name="What's next?",
                value=(
                    f"- Use `{p}lhelp` to see all commands\n"
                    f"- Use `/lsettings` to change API keys, model, temperature, and styles\n"
                    f"- Use `{p}lconfig channels add #channel` to start syncing messages\n"
                    f"- Use `{p}lroast @user` to roast someone\n"
                    f"- Use `{p}ltldr 50` to summarize chat"
                ),
                inline=False,
            )
            embed.set_footer(text="Lucky AI is ready!")
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(":x: This isn't your setup panel.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        self.cog.setup_sessions.pop(self.session_id, None)


class StartSetupButton(Button):
    def __init__(self, session_id, cog, user_id):
        super().__init__(label="\U0001f680 Start Setup", style=discord.ButtonStyle.success, custom_id=f"setup_start_{session_id}")
        self.session_id = session_id
        self.cog = cog
        self.user_id = user_id

    async def callback(self, interaction: discord.Interaction):
        session = self.cog.setup_sessions.get(self.session_id)
        if not session:
            await interaction.response.send_message(":x: Session expired.", ephemeral=True)
            return
        session["current_step"] = 1
        view = SetupView(self.cog, self.session_id, self.user_id, interaction.guild_id)
        embed = await view.build_embed()
        await interaction.response.edit_message(embed=embed, view=view)


class EnterKeyButton(Button):
    def __init__(self, provider, label, session_id, cog, user_id):
        super().__init__(label="\U0001f511 Enter Key", style=discord.ButtonStyle.primary, custom_id=f"setup_key_{provider}_{session_id}")
        self.provider = provider
        self.label = label
        self.session_id = session_id
        self.cog = cog
        self.user_id = user_id

    async def callback(self, interaction: discord.Interaction):
        modal = ApiKeyModal(self.provider, self.label, self.session_id, self.cog)
        await interaction.response.send_modal(modal)


class SkipKeyButton(Button):
    def __init__(self, provider, session_id, cog, user_id):
        super().__init__(label="\u23e9 Skip", style=discord.ButtonStyle.secondary, custom_id=f"setup_skip_{provider}_{session_id}")
        self.provider = provider
        self.session_id = session_id
        self.cog = cog
        self.user_id = user_id

    async def callback(self, interaction: discord.Interaction):
        session = self.cog.setup_sessions.get(self.session_id)
        if not session:
            await interaction.response.send_message(":x: Session expired.", ephemeral=True)
            return
        session["current_step"] += 1
        session["skipped_count"] += 1
        view = SetupView(self.cog, self.session_id, self.user_id, interaction.guild_id)
        embed = await view.build_embed()
        await interaction.response.edit_message(embed=embed, view=view)


class CancelSetupButton(Button):
    def __init__(self, session_id, cog, user_id):
        super().__init__(label="✖ Cancel", style=discord.ButtonStyle.danger, custom_id=f"setup_cancel_{session_id}")
        self.session_id = session_id
        self.cog = cog
        self.user_id = user_id

    async def callback(self, interaction: discord.Interaction):
        self.cog.setup_sessions.pop(self.session_id, None)
        await interaction.response.edit_message(
            content=":x: Setup cancelled. Run `[p]lsetup` to start again.",
            embed=None, view=None,
        )


class ChooseModelButton(Button):
    def __init__(self, session_id, cog, user_id, guild_id):
        super().__init__(label="\U0001f916 Choose Model", style=discord.ButtonStyle.primary, custom_id=f"setup_choose_model_{session_id}")
        self.session_id = session_id
        self.cog = cog
        self.user_id = user_id
        self.guild_id = guild_id

    async def callback(self, interaction: discord.Interaction):
        view = ModelSelectView(self.cog, self.session_id, self.user_id, self.guild_id)
        embed = await view.build_embed()
        await interaction.response.edit_message(embed=embed, view=view)


class SaveAndTestButton(Button):
    def __init__(self, session_id, cog, user_id):
        super().__init__(label="\u2705 Save & Test Endpoints", style=discord.ButtonStyle.success, custom_id=f"setup_test_{session_id}")
        self.session_id = session_id
        self.cog = cog
        self.user_id = user_id

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        session = self.cog.setup_sessions.get(self.session_id)
        if not session:
            await interaction.edit_original_response(content=":x: Session expired. Run setup again.", embed=None, view=None)
            return

        embed = discord.Embed(
            color=0xffaa00,
            title=":rocket: Testing API Endpoints...",
            description="Please wait while we test each configured provider...",
        )
        await interaction.edit_original_response(embed=embed, view=None)

        for provider, key in session.get("api_keys", {}).items():
            if key:
                try:
                    await interaction.client.set_shared_api_tokens(provider, api_key=key)
                except Exception as e:
                    log.error("Failed to save API key for %s: %s", provider, e)

        results = []
        for provider in PROVIDER_ORDER:
            try:
                result = await self.cog.ai_service._test_endpoint(provider)
                results.append(result)
            except Exception as e:
                log.error("Test endpoint failed for %s: %s", provider, e)
                results.append({
                    "name": PROVIDER_LABELS.get(provider, provider),
                    "status": "network_error",
                    "latency": None,
                    "message": str(e),
                })

        session["test_results"] = results
        session["current_step"] += 1

        view = SetupView(self.cog, self.session_id, self.user_id, interaction.guild_id)
        embed = await view.build_embed()
        await interaction.edit_original_response(embed=embed, view=view)


class SkipTestButton(Button):
    def __init__(self, session_id, cog, user_id):
        super().__init__(label="\u23e9 Skip Testing", style=discord.ButtonStyle.secondary, custom_id=f"setup_skip_test_{session_id}")
        self.session_id = session_id
        self.cog = cog
        self.user_id = user_id

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        session = self.cog.setup_sessions.get(self.session_id)
        if not session:
            await interaction.edit_original_response(content=":x: Session expired.")
            return

        for provider, key in session.get("api_keys", {}).items():
            if key:
                try:
                    await interaction.client.set_shared_api_tokens(provider, api_key=key)
                except Exception as e:
                    log.error("Failed to save API key for %s: %s", provider, e)

        session["test_results"] = []
        session["current_step"] += 1
        view = SetupView(self.cog, self.session_id, self.user_id, interaction.guild_id)
        embed = await view.build_embed()
        await interaction.edit_original_response(embed=embed, view=view)


class FinishSetupButton(Button):
    def __init__(self, session_id, cog, user_id):
        super().__init__(label="\u2705 Finish", style=discord.ButtonStyle.success, custom_id=f"setup_finish_{session_id}")
        self.session_id = session_id
        self.cog = cog
        self.user_id = user_id

    async def callback(self, interaction: discord.Interaction):
        session = self.cog.setup_sessions.get(self.session_id)
        if not session:
            await interaction.response.send_message(":x: Session expired.", ephemeral=True)
            return
        session["finished"] = True
        p = session.get("prefix", "l")
        sync_id = session.get("sync_channel_id")
        sync_line = f"\n- Messages syncing from <#{sync_id}>\n" if sync_id else ""
        self.cog.setup_sessions.pop(self.session_id, None)
        await interaction.response.edit_message(
            content="\u2705 **Setup complete!** Lucky AI is ready to use.\n\n"
                    "Next steps:\n"
                    f"- `{p}lhelp` - See all commands{sync_line}"
                    f"- `/lsettings` - Change API keys, model, temperature, and styles\n"
                    f"- `{p}lroast @user` - Roast someone!\n"
                    f"- `{p}ltldr 50` - Summarize chat",
            embed=None, view=None,
        )


class SetupCloseButton(Button):
    def __init__(self, session_id, cog, user_id):
        super().__init__(label="✖ Close", style=discord.ButtonStyle.danger, custom_id=f"setup_close_{session_id}")
        self.session_id = session_id
        self.cog = cog
        self.user_id = user_id

    async def callback(self, interaction: discord.Interaction):
        self.cog.setup_sessions.pop(self.session_id, None)
        await interaction.response.edit_message(content=":white_check_mark: Setup closed.", embed=None, view=None)
