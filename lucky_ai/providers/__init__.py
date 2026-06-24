"""
Central provider registry for Lucky AI.
To add a new provider: add one entry to PROVIDERS dict.
To remove: delete one entry.
Everything else derives from this dict automatically.
API keys are stored via Red's shared API token system (not env vars).
"""

import sys

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
        "base_url": "https://api.deepseek.com/v1",
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
        "default_model": "openrouter/meta-llama/llama-3.1-70b-instruct",
    },
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "default_model": "openai/gpt-4-turbo",
    },
}

PROVIDER_ORDER = list(PROVIDERS.keys())
_DEFAULT_PROVIDER_ORDER = list(PROVIDER_ORDER)  # snapshot for reset

PROVIDER_LABELS = {k: v["label"] for k, v in PROVIDERS.items()}

PROVIDER_BASE_URLS = {k: v["base_url"] for k, v in PROVIDERS.items()}

FALLBACK_DEFAULT_MODELS = {k: v["default_model"] for k, v in PROVIDERS.items()}
DEFAULT_MODEL = PROVIDERS[PROVIDER_ORDER[0]]["default_model"]


def _dedupe_preserve_order(items):
    seen = set()
    result = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def get_default_model() -> str:
    return PROVIDERS[PROVIDER_ORDER[0]]["default_model"]


def _sync_default_model_exports() -> None:
    default_model = get_default_model()
    globals()["DEFAULT_MODEL"] = default_model
    for module_name in ("lucky_ai.core.cog", "lucky_ai.commands.setup", "lucky_ai.ui.settings"):
        module = sys.modules.get(module_name)
        if module is not None:
            setattr(module, "DEFAULT_MODEL", default_model)


def set_provider_order(order: list) -> None:
    """Override PROVIDER_ORDER in-place so all referencing modules see the change."""
    import logging
    log = logging.getLogger("red.lucky_ai")
    valid = _dedupe_preserve_order([p for p in order if p in PROVIDERS])
    for p in order:
        if p not in PROVIDERS:
            log.warning("Unknown provider '%s' in custom order, ignoring", p)
    # Append any missing providers at the end so nothing is lost
    for p in PROVIDER_ORDER:
        if p not in valid:
            valid.append(p)
    PROVIDER_ORDER[:] = valid
    _sync_default_model_exports()


def reset_provider_order() -> None:
    """Restore PROVIDER_ORDER to the default (registry) order."""
    PROVIDER_ORDER[:] = _DEFAULT_PROVIDER_ORDER
    _sync_default_model_exports()
