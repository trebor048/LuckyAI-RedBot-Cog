import json
import time
import asyncio
import logging
import copy
import os

import discord
from discord.ui import View, Modal, Button, TextInput, Select

from ..providers import PROVIDER_ORDER, PROVIDER_LABELS, FALLBACK_DEFAULT_MODELS, PROVIDERS
from ..core.service import get_provider_by_model
from ..utils import normalize_string_iterable, set_shared_api_key

log = logging.getLogger("red.lucky_ai.setup")

SESSION_TIMEOUT = 900

DEFAULT_MODEL = PROVIDERS[PROVIDER_ORDER[0]]["default_model"]


def _generate_models_from_providers():
    models = {}
    for pid, info in PROVIDERS.items():
        default = info.get("default_model", "")
        if not default:
            continue
        parts = default.split("/")
        # Drop the provider prefix (e.g. "openrouter/"), but keep any remaining namespace path.
        # This matters for OpenRouter where model IDs are like "meta-llama/llama-3.3-70b-instruct".
        actual = "/".join(parts[1:]) if len(parts) > 1 else parts[0]
        display = parts[-1] if parts else actual
        name = display.replace("-", " ").title()
        model_key = default
        models[model_key] = {
            "name": f"{info['label']} - {name}",
            "provider": pid,
            "actualModelId": actual,
        }
    return models


CONFIG_JSON_TEMPLATE = {
    "MODELS": _generate_models_from_providers(),
}


def ensure_config_json(config_path):
    if not os.path.exists(config_path):
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        temp_path = f"{config_path}.tmp"
        with open(temp_path, "w") as f:
            json.dump(CONFIG_JSON_TEMPLATE, f, indent=2)
        os.replace(temp_path, config_path)
        log.info("Created default config at %s", config_path)
        return True
    try:
        with open(config_path, "r") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    changed = False
    for key, value in CONFIG_JSON_TEMPLATE.items():
        if key not in data or not isinstance(data.get(key), dict):
            data[key] = copy.deepcopy(value)
            changed = True
            continue
        for sub_key, sub_value in value.items():
            if sub_key not in data[key]:
                data[key][sub_key] = copy.deepcopy(sub_value)
                changed = True
    if changed:
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        temp_path = f"{config_path}.tmp"
        with open(temp_path, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(temp_path, config_path)
        log.info("Merged missing defaults into config at %s", config_path)
        return True
    return False


async def ensure_config_json_async(config_path):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, ensure_config_json, config_path)


async def _edit_setup_panel(interaction: discord.Interaction, cog, session_id, user_id, guild_id):
    try:
        view = SetupView(cog, session_id, user_id, guild_id)
    except ValueError:
        await interaction.response.send_message(":x: Session expired. Run setup again.", ephemeral=True)
        return False
    embed = await view.build_embed()
    await interaction.response.edit_message(embed=embed, view=view)
    return True


class ApiKeyModal(Modal):
    def __init__(self, provider, label, session_id, cog):
        super().__init__(title=f"{label} API Key", timeout=300)
        self.provider = provider
        self.session_id = session_id
        self.cog = cog
        self.add_item(
            TextInput(
                label=f"{label} API Key",
                placeholder="Paste your API key here...",
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
        try:
            await set_shared_api_key(interaction.client, self.provider, key)
        except Exception as e:
            log.error("SETUP Failed to update API key for %s: %s", self.provider, e)
            await interaction.response.send_message(":x: Failed to update API key. Check logs.", ephemeral=True)
            return
        if key:
            session["api_keys"][self.provider] = True
            log.info("SETUP %s API key saved", self.provider)
        else:
            session["api_keys"].pop(self.provider, None)
            log.info("SETUP %s API key cleared", self.provider)
        session["_last_accessed"] = time.time() * 1000
        await _edit_setup_panel(interaction, self.cog, self.session_id, interaction.user.id, interaction.guild_id)


class SetupView(View):
    """Single-page setup wizard: API keys + model selector on one screen."""

    def __init__(self, cog, session_id, user_id, guild_id):
        super().__init__(timeout=SESSION_TIMEOUT)
        self.cog = cog
        self.session_id = session_id
        self.user_id = user_id
        self.guild_id = guild_id
        self.session = cog.setup_sessions.get(session_id)
        if self.session is None:
            raise ValueError(f"Setup session {session_id} not found")
        self.session["_last_accessed"] = time.time() * 1000
        self._build_components()

    def _make_key_callback(self, provider, label):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.user_id:
                await interaction.response.send_message(":x: Not your setup panel.", ephemeral=True)
                return
            modal = ApiKeyModal(provider, label, self.session_id, self.cog)
            await interaction.response.send_modal(modal)
        return callback

    def _build_components(self):
        self.clear_items()
        session = self.session

        for provider in PROVIDER_ORDER:
            label = PROVIDER_LABELS.get(provider, provider)
            has_key = bool(session.get("api_keys", {}).get(provider))
            btn = Button(
                label=label,
                emoji='\U00002705' if has_key else '\U0001F511',
                style=discord.ButtonStyle.success if has_key else discord.ButtonStyle.secondary,
                custom_id=f"setup_key_{provider}_{self.session_id}",
            )
            btn.callback = self._make_key_callback(provider, label)
            self.add_item(btn)

        configured = [p for p in PROVIDER_ORDER if session.get("api_keys", {}).get(p)]
        options = []
        for p in configured:
            dm = FALLBACK_DEFAULT_MODELS.get(p, "")
            if dm:
                options.append(discord.SelectOption(
                    label=f"{PROVIDER_LABELS.get(p, p)}: {dm.split('/')[-1]}",
                    value=dm,
                ))
        if not options:
            options.append(discord.SelectOption(
                label=f"Global default: {DEFAULT_MODEL.split('/')[-1]}",
                value=DEFAULT_MODEL,
            ))
        available_values = {opt.value for opt in options}
        if session.get("default_model") not in available_values:
            session["default_model"] = options[0].value

        model_select = ModelSelect(options, self.session_id, self.cog, self.user_id, self.guild_id)
        self.add_item(model_select)

        finish = Button(
            label="Finish Setup",
            emoji='\U00002705',
            style=discord.ButtonStyle.success,
            custom_id=f"setup_finish_{self.session_id}",
            disabled=not configured,
        )

        async def finish_cb(interaction: discord.Interaction):
            if interaction.user.id != self.user_id:
                await interaction.response.send_message(":x: Not your setup panel.", ephemeral=True)
                return
            session = self.session
            session["_last_accessed"] = time.time() * 1000
            model = session.get("default_model", DEFAULT_MODEL)
            await interaction.response.defer()
            provider = get_provider_by_model(model)
            try:
                provider_result = await self.cog.ai_service._test_endpoint(provider)
            except Exception as e:
                log.error("SETUP Provider verification failed for %s: %s", provider, e)
                await interaction.followup.send(
                    f":x: **{PROVIDER_LABELS.get(provider, provider)}** could not be verified. "
                    "Update the API key and try Finish Setup again.",
                    ephemeral=True,
                )
                return
            provider_status = provider_result.get("status")
            if provider_status not in {"valid", "rate_limited"}:
                detail = provider_result.get("message", "The provider could not be verified.")
                await interaction.followup.send(
                    f":x: **{PROVIDER_LABELS.get(provider, provider)}** is not ready: {detail}\n"
                    "Update its API key and try Finish Setup again.",
                    ephemeral=True,
                )
                return

            guild = self.cog.bot.get_guild(self.guild_id)
            channel = None
            if guild and session.get("channel_id"):
                channel_id = session.get("channel_id")
                channel_lookup = getattr(guild, "get_channel_or_thread", None)
                channel = channel_lookup(channel_id) if callable(channel_lookup) else guild.get_channel(channel_id)
            channel_added = False
            channel_warning = ""
            async with self.cog.config.guild_from_id(self.guild_id).all() as cfg:
                cfg["model"] = model
                sync_channels = normalize_string_iterable(cfg.get("sync_channels", []))
                if channel and str(channel.id) not in sync_channels:
                    member = guild.me or guild.get_member(self.cog.bot.user.id)
                    permissions = channel.permissions_for(member) if member else None
                    if permissions and permissions.view_channel and permissions.read_message_history:
                        sync_channels.append(str(channel.id))
                        cfg["sync_channels"] = sync_channels
                        channel_added = True
                    else:
                        channel_warning = (
                            "\n:warning: I could not enable this channel because I need "
                            "**View Channel** and **Read Message History** there."
                        )
            sync_active = await self.cog._set_message_sync_enabled(True)
            if channel_added:
                await self.cog.db.update_sync_status(str(self.guild_id), str(channel.id))
                await self.cog.db.log_sync_operation(
                    str(self.guild_id),
                    str(channel.id),
                    "channel_add",
                    message_count=0,
                    triggered_by=str(interaction.user.id),
                )
            if not sync_active:
                channel_warning += (
                    "\n:warning: The `ENABLE_SYNC` environment variable is preventing live message storage."
                )
            await ensure_config_json_async(self.cog.config_json_path)
            self.cog.setup_sessions.pop(self.session_id, None)
            provider_count = len([p for p in PROVIDER_ORDER if session.get("api_keys", {}).get(p)])
            p = session.get("prefix", "l")
            channel_text = (
                f"**Sync channel:** {channel.mention} (starting a 14-day backfill)\n"
                if channel_added
                else "**Sync channel:** not configured; use `lconfig channels add #channel`\n"
            )
            await interaction.edit_original_response(
                content=(
                    ":white_check_mark: **Setup complete!** Lucky AI is ready to use.\n\n"
                    f"**Configured:** {provider_count} provider(s)\n"
                    f"**Default model:** `{model}`\n\n"
                    f"{channel_text}"
                    "**Next steps:**\n"
                    f"- `{p}lhelp` - See all commands\n"
                    f"- `{p}lroast @user` - Roast someone!\n"
                    f"- `{p}ltldr 50` - Summarize chat\n"
                    f"- `{p}lsettings` or `/lsettings` - Change API keys, model, temperature, and styles"
                    f"{channel_warning}"
                ),
                embed=None,
                view=None,
            )
            if channel_added:
                progress_msg = await interaction.followup.send(
                    f":arrows_counterclockwise: Backfilling the last 14 days of {channel.mention}...",
                    wait=True,
                )
                self.cog._start_backfill(guild, channel, 14, interaction.user, progress_msg)

        finish.callback = finish_cb
        self.add_item(finish)

    async def build_embed(self):
        session = self.session
        configured = [p for p in PROVIDER_ORDER if session.get("api_keys", {}).get(p)]
        if configured:
            status = "\n".join(f":white_check_mark: **{PROVIDER_LABELS.get(p, p)}**" for p in configured)
        else:
            status = "No providers configured yet. Click a 🔑 button above."
        embed = discord.Embed(
            title="🤖 Lucky AI Setup",
            color=0x0099ff,
            description=(
                "Configure your AI providers and pick a default model.\n"
                "You need **at least one** API key for the bot to work. Keys are bot-wide shared tokens, so changing one affects every guild using this bot.\n\n"
                "Finishing setup verifies the selected provider, enables message sync, and adds "
                "the current channel with a 14-day initial backfill when permissions allow.\n\n"
                "**Configured:**\n" + status +
                f"\n\n**Default model:** `{session.get('default_model', DEFAULT_MODEL)}`"
            ),
        )
        embed.set_footer(text="Click a provider button to add/edit its key. Pick a model below. Then Finish.")
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(":x: Not your setup panel.", ephemeral=True)
            return False
        session = self.cog.setup_sessions.get(self.session_id)
        if session is None:
            await interaction.response.send_message(":x: Session expired. Run setup again.", ephemeral=True)
            return False
        session["_last_accessed"] = time.time() * 1000
        return True

    async def on_timeout(self):
        self.cog.setup_sessions.pop(self.session_id, None)


class ModelSelect(Select):
    def __init__(self, options, session_id, cog, user_id, guild_id):
        super().__init__(
            placeholder="Pick a default model...",
            options=options[:25],
            custom_id=f"setup_model_{session_id}",
        )
        self.session_id = session_id
        self.cog = cog
        self.user_id = user_id
        self.guild_id = guild_id

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(":x: Not your setup panel.", ephemeral=True)
            return
        session = self.cog.setup_sessions.get(self.session_id)
        if not session:
            await interaction.response.send_message(":x: Session expired.", ephemeral=True)
            return
        session["default_model"] = self.values[0]
        session["_last_accessed"] = time.time() * 1000
        await _edit_setup_panel(interaction, self.cog, self.session_id, self.user_id, self.guild_id)
