# Lucky AI - Discord AI Cog for Red-DiscordBot

AI-powered roasting, TLDR summaries, chat Q&A, debate judging, and hot takes - all in one cog. Supports **7 AI providers** with automatic fallback.

---

## Features

| Command | Description |
|---------|-------------|
| `/roast @user` | Generate a personal AI roast from their message history |
| `/tldr [count] [style]` | Summarize the last N chat messages (normal or greentext) |
| `;ask <question>` | Chat with the AI using recent channel context |
| `;debate` | Judge the last argument in chat - picks a winner |
| `;htt fire` | Fire an automated "hot take" based on channel vibe |
| `/settings` | Interactive UI for model, temperature, styles, and fetch mode |
| `/setup` | Step-by-step wizard to configure API keys and test endpoints |
| `/optout` | Opt in/out of being roasted |
| `/stats` | View bot usage stats and health |
| `/config` | Manage sync channels, blacklist, admin role |

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
[p]setup
```

Or save API keys directly:

```
[p]set api openai api_key YOUR_KEY
[p]set api groq api_key YOUR_KEY
```

---

## Quick Start

1. **Install & load** the cog
2. Run **`/setup`** - the wizard walks you through API keys, endpoint testing, and model selection
3. Add a sync channel: **`/config channels add #general`**
4. Start roasting: **`/roast @user`**
5. Tune settings: **`/settings`**

---

## Prefix Commands

All prefix commands use the `;l` namespace to avoid conflicts with other bots.

| Prefix | Description |
|--------|-------------|
| `;lask <question>` | Ask the AI anything with channel context |
| `;ldebate` | Judge the most recent debate in chat |
| `;ltldr 50` | TLDR the last 50 messages |
| `;lgreentext 50` | 4chan-style greentext summary |
| `;lhtt on / off / fire` | Manage hot takes |
| `;ltypeon / ;ltypeoff` | Toggle typing indicator |

---

## Configuration

All per-guild settings are available through **`/settings`**:

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
/config channels add #channel
/config channels remove #channel
/config channels list
```

Synced messages are used for roasting, TLDR, debate, and ask commands.

---

## Data Privacy

- Users can opt out of being roasted with `/optout out`
- Opted-out users' messages are excluded from roast/analysis
- Users can be blacklisted by admins (`/config blacklist add @user`)
- Message data is stored only for explicitly configured sync channels

---

## Requirements

- Red-DiscordBot **3.5.0 - 3.6.0**
- `aiohttp>=3.9.0`
- `aiosqlite>=0.19.0`
