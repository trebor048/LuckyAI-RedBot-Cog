import os
import time
import random
import asyncio
import logging
from typing import Dict, Optional, Any

import aiohttp
from redbot.core import commands

log = logging.getLogger("red.LuckyAICog.ai")

from ..providers import PROVIDER_ORDER, PROVIDER_BASE_URLS, FALLBACK_DEFAULT_MODELS, PROVIDER_LABELS


def get_provider_by_model(model: str) -> str:
    """
    Determine which provider a model belongs to based on its prefix.
    
    Args:
        model: Model identifier (e.g., "openai/gpt-4", "groq/llama-3.3-70b")
    
    Returns:
        Provider name (e.g., "openai", "groq")
    """
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


def get_actual_model_id(model: str, models_data: Optional[Dict[str, Any]]) -> str:
    """
    Get the actual model ID to send to the API.
    
    Handles model aliasing where a friendly name maps to an actual model ID.
    
    Args:
        model: Model identifier
        models_data: Dict mapping model names to metadata
    
    Returns:
        Actual model ID to use in API requests
    """
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


class AIService:
    """
    Handles AI API requests with automatic provider fallback and retry logic.
    
    DEVIATION FROM RED BEST PRACTICES:
    Uses direct HTTP requests instead of a Red-provided API abstraction.
    This is necessary because:
    - Red doesn't provide multi-provider AI abstraction
    - We need custom fallback logic across 7 different providers
    - Each provider has different endpoints and authentication methods
    """
    
    def __init__(self, bot: commands.Bot, config_obj: Any) -> None:
        self.bot = bot
        self._config = config_obj
        self._session: Optional[aiohttp.ClientSession] = None
        self._models_data: Dict[str, Any] = {}
        self._session_lock = asyncio.Lock()

    def set_models_data(self, data: Dict[str, Any]) -> None:
        """Set model metadata for aliasing."""
        self._models_data = data

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create HTTP session."""
        async with self._session_lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession()
            return self._session

    def _get_api_key(self, provider: str) -> str:
        """
        Get API key for a provider from Red's shared API tokens.
        
        DEVIATION FROM RED BEST PRACTICES:
        Uses Red's bot.get_shared_api_tokens() instead of env vars or config files.
        This is the correct Red pattern for storing sensitive credentials.
        """
        try:
            tokens = self.bot.get_shared_api_tokens(provider)
            if tokens:
                return tokens.get("api_key", "")
        except Exception as e:
            log.debug("Failed to get API tokens for %s: %s", provider, e)
        return ""

    async def _build_headers(self, provider: str) -> Dict[str, str]:
        """
        Build HTTP headers for a provider's API.
        
        Args:
            provider: Provider name
        
        Returns:
            Dict of HTTP headers
        
        Raises:
            ValueError: If no API key is configured
        """
        api_key = self._get_api_key(provider)
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
            if os.getenv("OPENROUTER_SITE_URL"):
                headers["HTTP-Referer"] = os.getenv("OPENROUTER_SITE_URL")
            if os.getenv("OPENROUTER_APP_NAME"):
                headers["X-Title"] = os.getenv("OPENROUTER_APP_NAME")
            if os.getenv("OPENROUTER_API_VERSION"):
                headers["X-API-Version"] = os.getenv("OPENROUTER_API_VERSION")
        return headers

    async def _test_endpoint(self, provider: str) -> Dict[str, Any]:
        """
        Test if an API key is valid for a provider.
        
        Args:
            provider: Provider name
        
        Returns:
            Dict with test results (status, latency, message)
        """
        api_key = self._get_api_key(provider)
        base_url = PROVIDER_BASE_URLS.get(provider)
        fallback_model = FALLBACK_DEFAULT_MODELS.get(provider)
        actual_model = get_actual_model_id(fallback_model, self._models_data)

        if not api_key:
            return {"name": PROVIDER_LABELS.get(provider, provider), "status": "not_configured", "latency": None, "message": "No API key found"}
        if not base_url or not fallback_model:
            return {"name": PROVIDER_LABELS.get(provider, provider), "status": "network_error", "latency": None, "message": "Provider not supported"}

        start = time.perf_counter()
        try:
            headers = await self._build_headers(provider)
            body = {"model": actual_model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5}
            session = await self._get_session()
            async with session.post(
                f"{base_url}/chat/completions", json=body, headers=headers,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                latency = int((time.perf_counter() - start) * 1000)
                if resp.status == 200:
                    _ = await resp.text()
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

    async def execute_request(self, payload: Dict[str, Any], model: str, max_retries: int = 3, 
                            context: str = "AI", timeout: int = 60) -> Dict[str, Any]:
        """
        Execute an AI API request with automatic retry and fallback.
        
        RETRY LOGIC:
        - Tries primary provider up to max_retries times with exponential backoff
        - On rate limit (429), respects Retry-After header
        - On auth error (401/403), skips to fallback immediately
        - Falls back to next provider in PROVIDER_ORDER if primary fails
        
        Args:
            payload: Request payload (messages, temperature, etc.)
            model: Model identifier
            max_retries: Number of retries for primary provider
            context: Context string for logging (e.g., "ROAST", "TLDR")
            timeout: Request timeout in seconds
        
        Returns:
            API response dict
        
        Raises:
            Exception: If all providers fail
        """
        provider = get_provider_by_model(model)
        base_url = PROVIDER_BASE_URLS.get(provider, PROVIDER_BASE_URLS["openrouter"])
        actual_model = get_actual_model_id(model, self._models_data)

        session = await self._get_session()
        try:
            headers = await self._build_headers(provider)
        except ValueError as e:
            log.warning("%s No API key for primary %s, trying fallback: %s", context, provider, e)
            fallback_result = await self._try_fallback(payload, provider, timeout)
            if fallback_result:
                return fallback_result
            raise

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
                        retry_after_raw = resp.headers.get("Retry-After", "5")
                        try:
                            retry_after = int(retry_after_raw)
                        except ValueError:
                            retry_after = 5
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
            except Exception as e:
                last_error = Exception(f"Unexpected error: {e}")

            if attempt < max_retries:
                backoff = min(2 ** (attempt - 1) * 2000, 30000)
                jitter = backoff * 0.3
                sleep_time = backoff + (jitter * (random.random() - 0.5))
                log.info("%s Retrying in %dms...", context, int(sleep_time))
                await asyncio.sleep(sleep_time / 1000)

        if not last_error:
            last_error = Exception(f"All {max_retries} attempts failed for {model}")

        fallback_result = await self._try_fallback(payload, provider, timeout)
        if fallback_result:
            return fallback_result

        log.error("%s All attempts failed: %s", context, last_error)
        raise last_error

    async def _try_fallback(self, payload: Dict[str, Any], failed_provider: str, timeout: int) -> Optional[Dict[str, Any]]:
        """
        Try fallback providers in order.
        
        FALLBACK ORDER (from providers/__init__.py):
        nvidia -> groq -> moonshot -> deepseek -> zai -> openrouter -> openai
        
        Args:
            payload: Request payload
            failed_provider: Provider that failed
            timeout: Request timeout
        
        Returns:
            API response if successful, None if all fallbacks fail
        """
        idx = PROVIDER_ORDER.index(failed_provider) if failed_provider in PROVIDER_ORDER else -1
        for provider in PROVIDER_ORDER[idx + 1:]:
            api_key = self._get_api_key(provider)
            if not api_key:
                continue
            fallback_model = FALLBACK_DEFAULT_MODELS.get(provider)
            if not fallback_model:
                continue
            base_url = PROVIDER_BASE_URLS.get(provider)
            session = await self._get_session()
            try:
                headers = await self._build_headers(provider)
            except ValueError:
                continue
            actual_model = get_actual_model_id(fallback_model, self._models_data)
            body = {**payload, "model": actual_model}
            try:
                log.info("FALLBACK Trying %s with %s", provider, actual_model)
                async with session.post(
                    f"{base_url}/chat/completions",
                    json=body,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        log.info("FALLBACK Succeeded with %s/%s", provider, actual_model)
                        return data
            except Exception:
                log.warning("FALLBACK %s failed, trying next...", provider)
        return None

    async def close(self) -> None:
        """Close HTTP session."""
        async with self._session_lock:
            if self._session and not self._session.closed:
                await self._session.close()
