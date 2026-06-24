import os
import time
import random
import asyncio
import logging
import hashlib
from typing import Dict, Optional, Any, List

import aiohttp
from redbot.core import commands

from ..providers import PROVIDER_ORDER, PROVIDER_BASE_URLS, FALLBACK_DEFAULT_MODELS, PROVIDER_LABELS

log = logging.getLogger("red.lucky_ai.ai")

_KNOWN_PROVIDER_PREFIXES = set(PROVIDER_BASE_URLS)
_PROBE_INTERVAL_SECONDS = 15 * 60
_PROBE_RESULT_TTL_MS = 30 * 60 * 1000
_LEARNING_SCORE_MIN = -20
_LEARNING_SCORE_MAX = 20


def parse_retry_after(value: Any, default: float = 5.0, maximum: float = 30.0) -> float:
    """Parse and clamp a Retry-After value so one provider cannot stall command fallback indefinitely."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(0.0, min(maximum, parsed))


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
    if model.startswith("openai/") or model.startswith("gpt-") or model.startswith("o1") or model.startswith("o3"):
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
    if isinstance(model, str):
        if model.startswith("nvidia/"):
            return model
        for provider in _KNOWN_PROVIDER_PREFIXES:
            prefix = f"{provider}/"
            if model.startswith(prefix):
                remainder = model[len(prefix):]
                if remainder:
                    return remainder
    if models_data and isinstance(models_data, dict) and model in models_data:
        entry = models_data[model]
        actual = entry.get("actualModelId") if isinstance(entry, dict) else None
        if actual:
            return actual
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
        self._provider_health: Dict[str, Dict[str, Any]] = {}
        self._probe_results: Dict[str, Dict[str, Any]] = {}
        self._health_monitor_task: Optional[asyncio.Task] = None
        self._guild_learning_locks: Dict[int, asyncio.Lock] = {}
        self._metrics: Dict[str, Any] = {
            "requests": 0,
            "success": 0,
            "errors": 0,
            "fallback_success": 0,
            "providers": {},
        }

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)

    def _provider_metrics(self, provider: str) -> Dict[str, Any]:
        providers = self._metrics.setdefault("providers", {})
        if provider not in providers:
            providers[provider] = {
                "ok": 0,
                "fail": 0,
                "latency_total_ms": 0,
                "latency_count": 0,
                "cb_skips": 0,
            }
        return providers[provider]

    def _record_provider_success(self, provider: str, latency_ms: Optional[int] = None) -> None:
        m = self._provider_metrics(provider)
        m["ok"] += 1
        if latency_ms is not None:
            m["latency_total_ms"] += max(0, int(latency_ms))
            m["latency_count"] += 1
        health = self._provider_health.setdefault(provider, {"fail_count": 0, "open_until_ms": 0})
        health["fail_count"] = 0
        health["open_until_ms"] = 0

    def _record_provider_failure(self, provider: str, weight: int = 1) -> None:
        m = self._provider_metrics(provider)
        m["fail"] += 1
        health = self._provider_health.setdefault(provider, {"fail_count": 0, "open_until_ms": 0})
        health["fail_count"] += max(1, int(weight))
        if health["fail_count"] >= 3:
            health["open_until_ms"] = self._now_ms() + (5 * 60 * 1000)
            health["fail_count"] = 0

    def _circuit_is_open(self, provider: str) -> bool:
        """Check if circuit breaker is OPEN for a provider (should be skipped).
        Returns True when the circuit is tripped, False when healthy."""
        health = self._provider_health.get(provider)
        if not health:
            return False
        return self._now_ms() < int(health.get("open_until_ms", 0))

    @staticmethod
    def _failure_weight_for_status(status: int) -> int:
        if status in {401, 403}:
            return 3
        if status in {408, 409, 425, 429} or status >= 500:
            return 1
        return 0

    @staticmethod
    def _normalize_provider_order(order: Optional[List[str]]) -> List[str]:
        normalized = []
        for provider in order or PROVIDER_ORDER:
            if provider in PROVIDER_BASE_URLS and provider not in normalized:
                normalized.append(provider)
        for provider in PROVIDER_ORDER:
            if provider not in normalized:
                normalized.append(provider)
        return normalized

    def _probe_status(self, provider: str) -> str:
        result = self._probe_results.get(provider) or {}
        checked_at = int(result.get("checked_at_ms", 0) or 0)
        if self._now_ms() - checked_at > _PROBE_RESULT_TTL_MS:
            return "unknown"
        return str(result.get("status", "unknown"))

    @staticmethod
    def _key_fingerprint(api_key: str) -> str:
        if not api_key:
            return ""
        return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]

    async def _mark_probe_invalid(
        self,
        provider: str,
        latency_ms: Optional[int] = None,
        key_fingerprint: Optional[str] = None,
    ) -> None:
        if key_fingerprint is None:
            key_fingerprint = self._key_fingerprint(await self._get_api_key(provider))
        self._probe_results[provider] = {
            "status": "invalid",
            "latency": latency_ms,
            "message": "Invalid API key",
            "checked_at_ms": self._now_ms(),
            "key_fingerprint": key_fingerprint,
        }

    async def _mark_probe_valid(
        self,
        provider: str,
        latency_ms: Optional[int] = None,
        key_fingerprint: Optional[str] = None,
    ) -> None:
        if key_fingerprint is None:
            key_fingerprint = self._key_fingerprint(await self._get_api_key(provider))
        self._probe_results[provider] = {
            "status": "valid",
            "latency": latency_ms,
            "message": "Working",
            "checked_at_ms": self._now_ms(),
            "key_fingerprint": key_fingerprint,
        }

    async def _load_guild_learning(self, guild_id: Optional[int]) -> Dict[str, Dict[str, Any]]:
        if guild_id is None or self._config is None:
            return {}
        try:
            raw = await self._config.guild_from_id(int(guild_id)).provider_learning()
        except Exception as e:
            log.debug("Could not load provider learning for guild %s: %s", guild_id, e)
            return {}
        if not isinstance(raw, dict):
            return {}
        return {
            provider: dict(values)
            for provider, values in raw.items()
            if provider in PROVIDER_BASE_URLS and isinstance(values, dict)
        }

    async def _record_guild_outcome(
        self,
        guild_id: Optional[int],
        provider: str,
        *,
        success: bool,
        weight: int = 1,
    ) -> None:
        if guild_id is None or self._config is None or provider not in PROVIDER_BASE_URLS:
            return
        guild_id = int(guild_id)
        lock = self._guild_learning_locks.setdefault(guild_id, asyncio.Lock())
        async with lock:
            learning = await self._load_guild_learning(guild_id)
            entry = learning.setdefault(provider, {})
            score = int(entry.get("score", 0) or 0)
            delta = max(1, int(weight)) * (2 if success else -3)
            entry["score"] = max(_LEARNING_SCORE_MIN, min(_LEARNING_SCORE_MAX, score + delta))
            entry["successes"] = int(entry.get("successes", 0) or 0) + (1 if success else 0)
            entry["failures"] = int(entry.get("failures", 0) or 0) + (0 if success else 1)
            entry["last_success_ms" if success else "last_failure_ms"] = self._now_ms()
            try:
                await self._config.guild_from_id(guild_id).provider_learning.set(learning)
            except Exception as e:
                log.warning("Could not persist provider learning for guild %s: %s", guild_id, e)

    async def get_effective_provider_order(
        self,
        guild_id: Optional[int] = None,
        configured_order: Optional[List[str]] = None,
        primary_provider: Optional[str] = None,
    ) -> List[str]:
        """Return viable providers ranked by guild outcomes, probes, and configured preference."""
        base_order = self._normalize_provider_order(configured_order)
        learning = await self._load_guild_learning(guild_id)
        base_index = {provider: index for index, provider in enumerate(base_order)}
        keys = await asyncio.gather(*(self._get_api_key(provider) for provider in base_order))
        has_key = dict(zip(base_order, (bool(key) for key in keys)))
        key_fingerprints = dict(
            zip(base_order, (self._key_fingerprint(key) for key in keys))
        )
        for provider in base_order:
            cached_fingerprint = (self._probe_results.get(provider) or {}).get("key_fingerprint")
            if cached_fingerprint != key_fingerprints[provider]:
                self._probe_results.pop(provider, None)

        def rank(provider: str) -> tuple:
            status = self._probe_status(provider)
            unavailable = (
                not has_key[provider]
                or status == "invalid"
                or self._circuit_is_open(provider)
            )
            network_penalty = 1 if status == "network_error" else 0
            entry = learning.get(provider) or {}
            score = int(entry.get("score", 0) or 0)
            last_success = int(entry.get("last_success_ms", 0) or 0)
            last_failure = int(entry.get("last_failure_ms", 0) or 0)
            if status == "valid" and last_failure > last_success:
                score = max(0, score)
            return (unavailable, network_penalty, -score, base_index[provider])

        ranked = sorted(base_order, key=rank)
        if primary_provider in ranked and not rank(primary_provider)[0]:
            ranked.remove(primary_provider)
            ranked.insert(0, primary_provider)
        return ranked

    async def refresh_provider_health(self) -> Dict[str, Dict[str, Any]]:
        """Probe every provider concurrently and cache key validity/availability."""
        providers = list(PROVIDER_ORDER)
        probe_started_at_ms = self._now_ms()
        results = await asyncio.gather(
            *(self._test_endpoint(provider) for provider in providers),
            return_exceptions=True,
        )
        now = self._now_ms()
        for provider, result in zip(providers, results):
            existing_checked_at = int(
                (self._probe_results.get(provider) or {}).get("checked_at_ms", 0) or 0
            )
            if existing_checked_at >= probe_started_at_ms:
                continue
            if isinstance(result, Exception):
                result = {"status": "network_error", "latency": None, "message": str(result)}
            cached = dict(result)
            cached["checked_at_ms"] = now
            cached.setdefault("key_fingerprint", "")
            self._probe_results[provider] = cached
            status = cached.get("status")
            if status == "valid":
                health = self._provider_health.setdefault(
                    provider, {"fail_count": 0, "open_until_ms": 0}
                )
                health["fail_count"] = 0
                health["open_until_ms"] = 0
        return {provider: dict(result) for provider, result in self._probe_results.items()}

    async def _health_monitor(self) -> None:
        while True:
            try:
                await self.refresh_provider_health()
                await asyncio.sleep(_PROBE_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("Provider health monitor failed: %s", e)
                await asyncio.sleep(60)

    def start_health_monitor(self) -> None:
        """Start the self-healing provider probe loop once."""
        if self._health_monitor_task is None or self._health_monitor_task.done():
            self._health_monitor_task = asyncio.create_task(self._health_monitor())

    def get_metrics(self) -> Dict[str, Any]:
        """Return a snapshot of request/provider telemetry for admin stats views."""
        providers = {}
        for provider in PROVIDER_ORDER:
            vals = self._metrics.get("providers", {}).get(provider, {})
            latency_count = vals.get("latency_count", 0) or 0
            avg_latency = int(vals.get("latency_total_ms", 0) / latency_count) if latency_count else 0
            providers[provider] = {
                "ok": vals.get("ok", 0),
                "fail": vals.get("fail", 0),
                "cb_skips": vals.get("cb_skips", 0),
                "avg_latency_ms": avg_latency,
                "probe_status": self._probe_status(provider),
                "circuit_open": self._circuit_is_open(provider),
            }
        return {
            "requests": self._metrics.get("requests", 0),
            "success": self._metrics.get("success", 0),
            "errors": self._metrics.get("errors", 0),
            "fallback_success": self._metrics.get("fallback_success", 0),
            "providers": providers,
        }

    def set_models_data(self, data: Dict[str, Any]) -> None:
        """Set model metadata for aliasing."""
        self._models_data = data

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create HTTP session."""
        async with self._session_lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession()
            return self._session

    async def _get_api_key(self, provider: str) -> str:
        """
        Get API key for a provider from Red's shared API tokens.
        
        DEVIATION FROM RED BEST PRACTICES:
        Uses Red's bot.get_shared_api_tokens() instead of env vars or config files.
        This is the correct Red pattern for storing sensitive credentials.
        
        Note: This is now async to properly await bot.get_shared_api_tokens().
        """
        try:
            tokens = await self.bot.get_shared_api_tokens(provider)
            if tokens:
                return tokens.get("api_key", "")
        except Exception as e:
            log.debug("Failed to get API tokens for %s: %s", provider, e)
        return ""

    async def get_configured_providers(self) -> List[str]:
        """Return providers that currently have a shared API key configured."""
        keys = await asyncio.gather(*(self._get_api_key(provider) for provider in PROVIDER_ORDER))
        return [provider for provider, key in zip(PROVIDER_ORDER, keys) if key]

    async def _build_headers(self, provider: str, api_key: Optional[str] = None) -> Dict[str, str]:
        """
        Build HTTP headers for a provider's API.
        
        Args:
            provider: Provider name
        
        Returns:
            Dict of HTTP headers
        
        Raises:
            ValueError: If no API key is configured
        """
        if api_key is None:
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
        api_key = await self._get_api_key(provider)
        key_fingerprint = self._key_fingerprint(api_key)
        base_url = PROVIDER_BASE_URLS.get(provider)
        fallback_model = FALLBACK_DEFAULT_MODELS.get(provider)
        actual_model = get_actual_model_id(fallback_model, self._models_data)

        def result(status: str, latency: Optional[int], message: str) -> Dict[str, Any]:
            return {
                "name": PROVIDER_LABELS.get(provider, provider),
                "status": status,
                "latency": latency,
                "message": message,
                "key_fingerprint": key_fingerprint,
            }

        if not api_key:
            return result("not_configured", None, "No API key found")
        if not base_url or not fallback_model:
            return result("network_error", None, "Provider not supported")

        start = time.perf_counter()
        try:
            headers = await self._build_headers(provider, api_key)
            body = {"model": actual_model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5}
            session = await self._get_session()
            async with session.post(
                f"{base_url}/chat/completions", json=body, headers=headers,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                latency = int((time.perf_counter() - start) * 1000)
                if resp.status == 200:
                    _ = await resp.text()
                    return result("valid", latency, f"Working ({fallback_model})")
                elif resp.status == 401 or resp.status == 403:
                    return result("invalid", latency, "Invalid API key")
                elif resp.status == 429:
                    return result("rate_limited", latency, "Rate limited (key may be valid)")
                else:
                    return result("network_error", latency, f"HTTP {resp.status}")
        except asyncio.TimeoutError:
            return result("network_error", None, "Request timed out")
        except Exception as e:
            return result("network_error", None, str(e))

    async def execute_request(
        self,
        payload: Dict[str, Any],
        model: str,
        max_retries: int = 3,
        context: str = "AI",
        timeout: int = 60,
        provider_order: Optional[List[str]] = None,
        guild_id: Optional[int] = None,
    ) -> Dict[str, Any]:
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
        effective_order = await self.get_effective_provider_order(
            guild_id=guild_id,
            configured_order=provider_order,
            primary_provider=provider,
        )
        base_url = PROVIDER_BASE_URLS.get(provider, PROVIDER_BASE_URLS["openrouter"])
        actual_model = get_actual_model_id(model, self._models_data)
        self._metrics["requests"] = self._metrics.get("requests", 0) + 1

        session = await self._get_session()
        if (
            self._circuit_is_open(provider)
            or self._probe_status(provider) in {"invalid", "not_configured"}
            or (effective_order and effective_order[0] != provider)
        ):
            self._provider_metrics(provider)["cb_skips"] += 1
            fallback_result = await self._try_fallback(
                payload, provider, timeout, provider_order=effective_order, guild_id=guild_id
            )
            if fallback_result:
                self._metrics["success"] = self._metrics.get("success", 0) + 1
                self._metrics["fallback_success"] = self._metrics.get("fallback_success", 0) + 1
                return fallback_result
            self._metrics["errors"] = self._metrics.get("errors", 0) + 1
            raise Exception(f"Provider {provider} temporarily unavailable (circuit open)")
        try:
            api_key = await self._get_api_key(provider)
            headers = await self._build_headers(provider, api_key)
        except ValueError as e:
            log.warning("%s No API key for primary %s, trying fallback: %s", context, provider, e)
            fallback_result = await self._try_fallback(
                payload, provider, timeout, provider_order=effective_order, guild_id=guild_id
            )
            if fallback_result:
                self._metrics["success"] = self._metrics.get("success", 0) + 1
                self._metrics["fallback_success"] = self._metrics.get("fallback_success", 0) + 1
                return fallback_result
            # Missing API key is a config issue, not a provider failure -- don't circuit-break
            self._metrics["errors"] = self._metrics.get("errors", 0) + 1
            raise

        body = {**payload, "model": actual_model}

        last_error = None
        provider_had_request_failure = False
        provider_failure_weight = 0
        for attempt in range(1, max_retries + 1):
            try:
                started = time.perf_counter()
                log.info("%s Attempt %d/%d with %s (actual: %s)", context, attempt, max_retries, model, actual_model)
                async with session.post(
                    f"{base_url}/chat/completions",
                    json=body,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        latency_ms = int((time.perf_counter() - started) * 1000)
                        self._record_provider_success(provider, latency_ms)
                        await self._mark_probe_valid(
                            provider,
                            latency_ms,
                            key_fingerprint=self._key_fingerprint(api_key),
                        )
                        await self._record_guild_outcome(guild_id, provider, success=True)
                        self._metrics["success"] = self._metrics.get("success", 0) + 1
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
                        retry_after = parse_retry_after(resp.headers.get("Retry-After"))
                        log.warning("%s Rate limited on %s, retrying in %.1fs", context, provider, retry_after)
                        await asyncio.sleep(retry_after)
                        continue
                    if attempt == max_retries or (is_4xx and not is_rate):
                        msg = f"{provider} API Error (HTTP {resp.status}): {text[:200]}"
                        last_error = Exception(msg)
                        provider_had_request_failure = True
                        provider_failure_weight = self._failure_weight_for_status(resp.status)
                        if provider_failure_weight == 3:
                            await self._mark_probe_invalid(
                                provider,
                                latency_ms=int((time.perf_counter() - started) * 1000),
                                key_fingerprint=self._key_fingerprint(api_key),
                            )
                        break
            except asyncio.TimeoutError:
                last_error = Exception(f"Request timed out after {timeout}s")
                provider_had_request_failure = True
                provider_failure_weight = 1
            except aiohttp.ClientError as e:
                last_error = Exception(f"Network error: {e}")
                provider_had_request_failure = True
                provider_failure_weight = 1
            except Exception as e:
                last_error = Exception(f"Unexpected error: {e}")
                provider_had_request_failure = True
                provider_failure_weight = 1

            if attempt < max_retries:
                backoff = min(2 ** (attempt - 1) * 2000, 30000)
                jitter = backoff * 0.3
                sleep_time = backoff + (jitter * (random.random() - 0.5))
                log.info("%s Retrying in %dms...", context, int(sleep_time))
                await asyncio.sleep(sleep_time / 1000)

        if not last_error:
            last_error = Exception(f"All {max_retries} attempts failed for {model}")
        if provider_had_request_failure and provider_failure_weight > 0:
            self._record_provider_failure(provider, weight=provider_failure_weight)
            await self._record_guild_outcome(
                guild_id, provider, success=False, weight=provider_failure_weight
            )

        fallback_result = await self._try_fallback(
            payload, provider, timeout, provider_order=effective_order, guild_id=guild_id
        )
        if fallback_result:
            self._metrics["success"] = self._metrics.get("success", 0) + 1
            self._metrics["fallback_success"] = self._metrics.get("fallback_success", 0) + 1
            return fallback_result

        log.error("%s All attempts and fallbacks exhausted: %s", context, last_error)
        self._metrics["errors"] = self._metrics.get("errors", 0) + 1
        raise Exception(f"{context}: All providers failed for model {model}. Last error: {last_error}")

    async def _try_fallback(
        self,
        payload: Dict[str, Any],
        failed_provider: str,
        timeout: int,
        provider_order: Optional[List[str]] = None,
        guild_id: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
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
        order = self._normalize_provider_order(provider_order)
        candidates = [provider for provider in order if provider != failed_provider]
        seen = set()
        for provider in candidates:
            if provider == failed_provider or provider in seen:
                continue
            seen.add(provider)
            if self._circuit_is_open(provider):
                self._provider_metrics(provider)["cb_skips"] += 1
                continue
            if self._probe_status(provider) in {"invalid", "not_configured"}:
                continue
            api_key = await self._get_api_key(provider)
            if not api_key:
                continue
            fallback_model = FALLBACK_DEFAULT_MODELS.get(provider)
            if not fallback_model:
                continue
            base_url = PROVIDER_BASE_URLS.get(provider)
            session = await self._get_session()
            try:
                headers = await self._build_headers(provider, api_key)
            except ValueError:
                continue
            actual_model = get_actual_model_id(fallback_model, self._models_data)
            body = {**payload, "model": actual_model}
            try:
                log.info("FALLBACK Trying %s with %s", provider, actual_model)
                started = time.perf_counter()
                async with session.post(
                    f"{base_url}/chat/completions",
                    json=body,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        latency_ms = int((time.perf_counter() - started) * 1000)
                        self._record_provider_success(provider, latency_ms)
                        await self._mark_probe_valid(
                            provider,
                            latency_ms,
                            key_fingerprint=self._key_fingerprint(api_key),
                        )
                        await self._record_guild_outcome(guild_id, provider, success=True)
                        log.info("FALLBACK Succeeded with %s/%s", provider, actual_model)
                        return data
                    weight = self._failure_weight_for_status(resp.status)
                    if weight == 3:
                        await self._mark_probe_invalid(
                            provider,
                            latency_ms=int((time.perf_counter() - started) * 1000),
                            key_fingerprint=self._key_fingerprint(api_key),
                        )
                    if weight > 0:
                        self._record_provider_failure(provider, weight=weight)
                        await self._record_guild_outcome(
                            guild_id, provider, success=False, weight=weight
                        )
            except Exception as e:
                self._record_provider_failure(provider)
                await self._record_guild_outcome(guild_id, provider, success=False)
                log.warning("FALLBACK %s failed (%s: %s), trying next...", provider, type(e).__name__, e)
        return None

    async def close(self) -> None:
        """Close HTTP session."""
        if self._health_monitor_task and not self._health_monitor_task.done():
            self._health_monitor_task.cancel()
            try:
                await self._health_monitor_task
            except asyncio.CancelledError:
                pass
        async with self._session_lock:
            if self._session and not self._session.closed:
                await self._session.close()
