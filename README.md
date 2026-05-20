# Lucky AI - Discord AI Cog for Red-DiscordBot

AI-powered roasting, TLDR summaries, chat Q&A, debate judging, and hot takes - all in one cog. Supports **7 AI providers** with automatic fallback.

---

## Features

All commands use the `l` prefix. `/lsettings` is available as a slash command for admin settings.

| Command | Description |
|---------|-------------|
| `lroast @user` | Generate a personal AI roast from their message history |
| `ltldr [count] [style]` | Summarize the last N chat messages (normal or greentext) |
| `lask <question>` | Chat with the AI using recent channel context |
| `ldebate` | Judge the last argument in chat - picks a winner |
| `lhtt fire` | Fire an automated "hot take" based on channel vibe |
| `/lsettings` | Interactive UI for model, temperature, styles, fetch mode, and API keys (slash command) |
| `lsetup` | Step-by-step wizard to configure API keys and test endpoints |
| `loptout` | Opt in/out of being roasted |
| `lstats` | View bot usage stats and health |
| `lhelp` | Show all commands |
| `lconfig` | Manage sync channels, blacklist, admin role |

---

## Supported AI Providers

- **NVIDIA** - `NVIDIA_API_KEY`
- **Groq** - `GROQ_API_KEY`
- **OpenAI** - `OPENAI_API_KEY`
- **Moonshot** - `MOONSHOT_API_KEY`
- **DeepSeek** - `DEEPSEEK_API_KEY`
- **Z-AI** - `ZAI_API_KEY`
- **OpenRouter** - `OPENROUTER_KEY`

At least one provider must be configured. The bot will automatically fall back to another provider if the primary fails.

---

## Installation

```py
[p]repo add LuckyAI https://github.com/trebor048/LuckyAI-RedBot-Cog "I agree"
[p]cog install LuckyAI lucky-ai
[p]load lucky-ai
```

Then run the interactive setup:

```
[lsetup
```

Or save API keys directly via Red's API system:

```
[lset api openai api_key YOUR_KEY
[lset api groq api_key YOUR_KEY
```

---

## Quick Start

1. **Install & load** the cog
2. Run **`lsetup`** - the wizard walks you through API keys, endpoint testing, and model selection
3. Add a sync channel: **`lconfig channels add #general`**
4. Start roasting: **`lroast @user`**
5. See all commands: **`lhelp`**
6. Tune settings: **`lsettings`**

---

## Configuration

All per-guild settings are available through **`lsettings`**:

- **API Keys** - set, change, or remove keys for any provider
- **Model** - pick from any provider's models
- **Temperature / Top P / Top K** - tune generation creativity
- **Frequency / Presence Penalty** - control repetition
- **Roast Styles** - switch between clinical, sarcastic, blunt, analytical, disappointed, or custom
- **Fetch Mode** - recent messages or random sampling
- **Admin Role** - restrict admin commands to a specific role

---

## Message Syncing

The cog stores messages in a local SQLite database (`messages.db`). To start syncing:

```
lconfig channels add #channel
lconfig channels remove #channel
lconfig channels list
```

Synced messages are used for roasting, TLDR, debate, and ask commands.

---

## Data Privacy

- Users can opt out of being roasted with `loptout out`
- Opted-out users' messages are excluded from roast/analysis
- Users can be blacklisted by admins (`lconfig blacklist add @user`)
- Message data is stored only for explicitly configured sync channels

---

## Requirements

- Red-DiscordBot **3.5.0 - 3.6.0**
- `aiohttp>=3.9.0`
- `aiosqlite>=0.19.0`
