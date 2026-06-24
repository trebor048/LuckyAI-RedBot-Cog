# Lucky AI - Discord AI Cog for Red-DiscordBot

Lucky AI is a drop-in Red cog for AI roasts, TL;DRs, chat Q&A, hot takes, and server-specific chat analysis. Install it from GitHub, load it into Red, run setup, and it stores runtime data inside Red's cog data directory instead of the repository.

## What You Get

- `lroast @user [style]` for message-history roasts
- `ltldr [count] [style]` for summaries and greentext recaps
- `lask <question>` for general Q&A
- `lask 300 <question>` to fetch that many recent messages and send them to the AI as context
- `lhtt on|off|fire` for hot takes
- `lsettings` or `/lsettings` for interactive server settings
- `lsetup` for first-run setup
- `lconfig ...` for sync channels, blacklist, backfill, admin role, and enable/disable
- `loptout in|out` for per-user opt-out
- `lstats` for DB and health stats
- `lhelp` for the full command list

All commands also work without the Red prefix, so `lroast`, `ltldr`, `lask`, and the rest can be used directly if you prefer.

## Supported Providers

- NVIDIA
- Groq
- OpenAI
- Moonshot
- DeepSeek
- Zhipu AI
- OpenRouter

At least one provider API key is required. Keys are stored through Red's shared API token system, so they are bot-wide, not per guild.

## Install

```py
[p]repo add LuckyAI https://github.com/trebor048/LuckyAI-RedBot-Cog
[p]cog install LuckyAI lucky_ai
[p]load lucky_ai
```

Then run setup in the target server:

```py
[p]lsetup
```

If you want slash settings later, you can enable them, but it is optional because `lsettings` also works as a prefix command:

```py
[p]slash enable lsettings
[p]slash sync
```

## First Run

1. Load the cog.
2. Run `lsetup` in the server and channel you want to use first.
3. Add API keys in the setup wizard.
4. Finish setup.
5. Start using `lroast`, `ltldr`, and `lask`.

What setup does:

- verifies the selected provider before finishing
- enables live message sync
- adds the current channel to the sync list when the bot has permission
- starts a 14-day backfill for that channel when possible

## Configuration

`lsettings` is the main server settings UI. It uses four pages:

- Model and style
- Generation parameters
- API keys
- Advanced options

Notes:

- API keys are bot-wide shared tokens.
- Custom roast styles are stored per server.
- The current model and generation settings are per server.
- `lconfig` controls sync channels, backfill, blacklist, admin role, and the global enable toggle.

The `ask` command has two modes:

- `lask what is going on here?` asks a general question.
- `lask 300 what is going on here?` fetches 300 recent messages first and gives that context to the AI.

## Permissions and Intents

Lucky AI expects the bot to have:

- Message Content intent
- View Channel
- Send Messages
- Read Message History
- Embed Links
- Manage Messages
- Attach Files

The bot only stores message content from channels that admins explicitly add to sync.

## Data and Privacy

- Runtime data is stored in Red's cog data directory.
- Message storage is limited to configured sync channels.
- Users can opt out with `loptout out`.
- Opting out deletes that user's stored messages for the server and blocks future storage.
- Red data deletion requests are supported.
- Custom styles are per-server, not global.

## Troubleshooting

- No responses: check that provider API keys are set and valid.
- Nothing is syncing: confirm the channel is on the sync list and the bot has Read Message History.
- `lsettings` not showing as slash: use `lsettings` as a prefix command, or enable and sync the slash command.
- `lsetup` says the provider is not ready: update the key in `lsetup` or `lsettings` and try again.
- The bot looks disabled: check `lconfig toggle`, server permissions, and Red's cog-disable state.

## Requirements

- Red-DiscordBot 3.5+
- Python 3.8+
- `aiohttp`
- `aiosqlite`

## Development Checks

```powershell
python -m compileall lucky_ai tests
python -m unittest discover -v
ruff check lucky_ai tests
```
