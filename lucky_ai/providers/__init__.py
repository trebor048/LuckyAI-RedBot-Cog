"""
Central provider registry for Lucky AI.
To add a new provider: add one entry to PROVIDERS dict.
To remove: delete one entry.
Everything else derives from this dict automatically.
API keys are stored via Red's shared API token system (not env vars).
"""

PROVIDERS = {
    "nvidia": {
        "label": "NVIDIA",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "default_model": "nvidia/nemotron-3-super-120b-a12b",
    },
    "groq": {
        "label": "Groq",
        "base_url": "https://api.groq.com/openai/v1",
        "default_model": "groq/llama-3.3-70b-versatile",
    },
    "moonshot": {
        "label": "Moonshot",
        "base_url": "https://api.moonshot.cn/v1",
        "default_model": "moonshot/kimi-k2.5",
    },
    "deepseek": {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com",
        "default_model": "deepseek/deepseek-reasoner",
    },
    "zai": {
        "label": "Zhipu AI",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "default_model": "zai/glm-5.1",
    },
    "openrouter": {
        "label": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "default_model": "openrouter/meta-llama/llama-3.3-70b-instruct",
    },
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "default_model": "openai/gpt-4-turbo",
    },
}

PROVIDER_ORDER = list(PROVIDERS.keys())

PROVIDER_LABELS = {k: v["label"] for k, v in PROVIDERS.items()}

PROVIDER_BASE_URLS = {k: v["base_url"] for k, v in PROVIDERS.items()}

FALLBACK_DEFAULT_MODELS = {k: v["default_model"] for k, v in PROVIDERS.items()}
