import time
import json
import asyncio
import logging

import aiohttp

log = logging.getLogger("red.RoasterCog.ai")


PROVIDER_ORDER = ["nvidia", "groq", "moonshot", "zai", "deepseek", "openrouter", "openai"]

PROVIDER_BASE_URLS = {
    "nvidia": "https://integrate.api.nvidia.com/v1",
    "groq": "https://api.groq.com/openai/v1",
    "moonshot": "https://api.moonshot.cn/v1",
    "deepseek": "https://api.deepseek.com",
    "zai": "https://open.bigmodel.cn/api/paas/v4",
    "openrouter": "https://openrouter.ai/api/v1",
    "openai": "https://api.openai.com/v1",
}

PROVIDER_ENV_KEYS = {
    "nvidia": "NVIDIA_API_KEY",
    "groq": "GROQ_API_KEY",
    "moonshot": "MOONSHOT_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "zai": "ZAI_API_KEY",
    "openrouter": "OPENROUTER_KEY",
    "openai": "OPENAI_API_KEY",
}

FALLBACK_DEFAULT_MODELS = {
    "nvidia": "nvidia/nemotron-3-super-120b-a12b",
    "groq": "groq/llama-3.3-70b-versatile",
    "moonshot": "moonshot/kimi-k2.5",
    "zai": "zai/glm-5.1",
    "deepseek": "deepseek/deepseek-reasoner",
    "openrouter": "meta-llama/llama-3.3-70b-instruct",
    "openai": "gpt-4-turbo",
}

PROVIDER_LABELS = {
    "nvidia": "Nvidia",
    "groq": "Groq",
    "moonshot": "Moonshot",
    "deepseek": "DeepSeek",
    "zai": "Z-AI",
    "openrouter": "OpenRouter",
    "openai": "OpenAI",
}


def get_provider_by_model(model):
    if not model or not isinstance(model, str):
        return "openrouter"
    if model.startswith("groq/"):
        return "groq"
    if model.startswith("openai/") or model.startswith("gpt-"):
        return "openai"
    if model.startswith("nvidia/"):
        return "nvidia"
    if model.startswith("moonshot/") or model.startswith("kimi-"):
        return "moonshot"
    if model.startswith("deepseek/"):
        return "deepseek"
    if model.startswith("zai/") or model.startswith("glm-"):
        return "zai"
    return "openrouter"


def get_actual_model_id(model, models_data):
    if not model:
        return "gpt-3.5-turbo"
    if models_data and isinstance(models_data, dict) and model in models_data:
        actual = models_data[model].get("actualModelId") if isinstance(models_data[model], dict) else None
        if actual:
            return actual
    if "/" in model:
        parts = model.split("/")
        if len(parts) > 2:
            return "/".join(parts[1:])
        return parts[1]
    return model


def get_provider_api_key(config_func, provider):
    key = getattr(config_func, PROVIDER_ENV_KEYS.get(provider, ""), None)
    if key:
        return key
    import os
    key = os.getenv(PROVIDER_ENV_KEYS.get(provider, ""), "")
    if key:
        return key
    try:
        tokens = config_func.get_shared_api_tokens(provider)
        if tokens:
            return tokens.get("api_key", "")
    except Exception:
        pass
    return ""


class AIService:
    def __init__(self, bot, config_func):
        self.bot = bot
        self._config = config_func
        self._session = None
        self._models_data = {}

    def set_models_data(self, data):
        self._models_data = data

    async def _get_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _get_api_key(self, provider):
        env_key = PROVIDER_ENV_KEYS.get(provider)
        if not env_key:
            return ""
        import os
        key = os.getenv(env_key, "")
        if key:
            return key
        try:
            tokens = await self.bot.get_shared_api_tokens(provider)
            if tokens:
                return tokens.get("api_key", "")
        except Exception:
            pass
        return ""

    async def _build_headers(self, provider):
        api_key = await self._get_api_key(provider)
        if not api_key:
            raise ValueError(f"No API key configured for {provider}")
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if provider == "openrouter":
            headers["Accept-Encoding"] = "gzip, deflate, br"
            headers["Connection"] = "keep-alive"
            headers["Keep-Alive"] = "timeout=60"
            import os
            if os.getenv("OPENROUTER_SITE_URL"):
                headers["HTTP-Referer"] = os.getenv("OPENROUTER_SITE_URL")
            if os.getenv("OPENROUTER_APP_NAME"):
                headers["X-Title"] = os.getenv("OPENROUTER_APP_NAME")
            if os.getenv("OPENROUTER_API_VERSION"):
                headers["X-API-Version"] = os.getenv("OPENROUTER_API_VERSION")
        return headers

    async def _test_endpoint(self, provider):
        """Test if an API key is valid for a provider."""
        api_key = self._get_api_key(provider)
        base_url = PROVIDER_BASE_URLS.get(provider)
        fallback_model = FALLBACK_DEFAULT_MODELS.get(provider)

        if not api_key:
            return {"name": PROVIDER_LABELS.get(provider, provider), "status": "not_configured", "latency": None, "message": "No API key found"}
        if not base_url or not fallback_model:
            return {"name": PROVIDER_LABELS.get(provider, provider), "status": "network_error", "latency": None, "message": "Provider not supported"}

        start = time.perf_counter()
        try:
            headers = await self._build_headers(provider)
            body = {"model": fallback_model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5}
            session = await self._get_session()
            async with session.post(
                f"{base_url}/chat/completions", json=body, headers=headers,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                latency = int((time.perf_counter() - start) * 1000)
                if resp.status == 200:
                    return {"name": PROVIDER_LABELS.get(provider, provider), "status": "valid", "latency": latency, "message": f"Working ({fallback_model})"}
                elif resp.status == 401 or resp.status == 403:
                    return {"name": PROVIDER_LABELS.get(provider, provider), "status": "invalid", "latency": latency, "message": "Invalid API key"}
                elif resp.status == 429:
                    return {"name": PROVIDER_LABELS.get(provider, provider), "status": "rate_limited", "latency": latency, "message": "Rate limited (key may be valid)"}
                else:
                    return {"name": PROVIDER_LABELS.get(provider, provider), "status": "network_error", "latency": latency, "message": f"HTTP {resp.status}"}
        except asyncio.TimeoutError:
            return {"name": PROVIDER_LABELS.get(provider, provider), "status": "network_error", "latency": None, "message": "Request timed out"}
        except Exception as e:
            return {"name": PROVIDER_LABELS.get(provider, provider), "status": "network_error", "latency": None, "message": str(e)}

    async def execute_request(self, payload, model, max_retries=3, context="AI", timeout=60):
        provider = get_provider_by_model(model)
        base_url = PROVIDER_BASE_URLS.get(provider, PROVIDER_BASE_URLS["openrouter"])
        actual_model = get_actual_model_id(model, self._models_data)

        session = await self._get_session()
        headers = await self._build_headers(provider)
        body = {**payload, "model": actual_model}

        last_error = None
        for attempt in range(1, max_retries + 1):
            try:
                log.info("%s Attempt %d/%d with %s (actual: %s)", context, attempt, max_retries, model, actual_model)
                async with session.post(
                    f"{base_url}/chat/completions",
                    json=body,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        usage = data.get("usage", {})
                        log.info(
                            "%s %s | in:%d out:%d total:%d",
                            context, actual_model,
                            usage.get("prompt_tokens", 0),
                            usage.get("completion_tokens", 0),
                            usage.get("total_tokens", 0),
                        )
                        return data
                    text = await resp.text()
                    is_rate = resp.status == 429
                    is_4xx = 400 <= resp.status < 500
                    if is_rate and attempt < max_retries:
                        retry_after = int(resp.headers.get("Retry-After", 5))
                        log.warning("%s Rate limited on %s, retrying in %ds", context, provider, retry_after)
                        await asyncio.sleep(retry_after)
                        continue
                    if attempt == max_retries or (is_4xx and not is_rate):
                        msg = f"{provider} API Error (HTTP {resp.status}): {text[:200]}"
                        last_error = Exception(msg)
                        break
            except asyncio.TimeoutError:
                last_error = Exception(f"Request timed out after {timeout}s")
            except aiohttp.ClientError as e:
                last_error = Exception(f"Network error: {e}")

            if attempt < max_retries:
                backoff = min(2 ** (attempt - 1) * 2000, 30000)
                jitter = backoff * 0.3
                sleep_time = backoff + (jitter * (__import__("random").random() - 0.5))
                log.info("%s Retrying in %dms...", context, int(sleep_time))
                await asyncio.sleep(sleep_time / 1000)

        if not last_error:
            last_error = Exception(f"All {max_retries} attempts failed for {model}")

        fallback_result = await self._try_fallback(payload, provider, timeout)
        if fallback_result:
            return fallback_result

        log.error("%s All attempts failed: %s", context, last_error)
        raise last_error

    async def _try_fallback(self, payload, failed_provider, timeout):
        idx = PROVIDER_ORDER.index(failed_provider) if failed_provider in PROVIDER_ORDER else -1
        for provider in PROVIDER_ORDER[idx + 1:]:
            api_key = await self._get_api_key(provider)
            if not api_key:
                continue
            fallback_model = FALLBACK_DEFAULT_MODELS.get(provider)
            if not fallback_model:
                continue
            base_url = PROVIDER_BASE_URLS.get(provider)
            session = await self._get_session()
            headers = await self._build_headers(provider)
            body = {**payload, "model": fallback_model}
            try:
                log.info("FALLBACK Trying %s with %s", provider, fallback_model)
                async with session.post(
                    f"{base_url}/chat/completions",
                    json=body,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        log.info("FALLBACK Succeeded with %s/%s", provider, fallback_model)
                        return data
            except Exception:
                log.warning("FALLBACK %s failed, trying next...", provider)
        return None

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
