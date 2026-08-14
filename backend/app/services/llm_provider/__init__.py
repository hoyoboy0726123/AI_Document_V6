"""
LLM provider factory.

Resolution order (per kind, where kind ∈ {llm, embedding}):
  1. system_configs DB row `llm.provider` / `embedding.provider`
  2. .env `LLM_PROVIDER` / `EMBEDDING_PROVIDER`
  3. fallback "ollama"

The two kinds are independent — e.g. LLM on Gemini, embeddings on local Ollama.
"""
from __future__ import annotations

import logging
from threading import Lock
from typing import Optional

from ...core.config import settings
from ..ollama_client import OllamaClient
from .aihub_provider import AiHubProvider
from .base import LLMProvider
from .gemini_provider import GeminiProvider
from .ollama_provider import OllamaProvider

logger = logging.getLogger(__name__)

_llm_lock = Lock()
_embed_lock = Lock()
_llm_singleton: Optional[LLMProvider] = None
_embed_singleton: Optional[LLMProvider] = None
_cached_signature: dict = {}


def _signature() -> dict:
    """Build a config-fingerprint so we can rebuild singletons after admin saves."""
    return {
        "llm_provider": getattr(settings, "LLM_PROVIDER", "ollama"),
        # aihub 的模型名是 service/version，退回 OLLAMA_LLM_MODEL 沒有意義，
        # 還會讓指紋每次都變、singleton 反覆重建。
        "llm_model": (
            getattr(settings, "LLM_MODEL", None)
            or (getattr(settings, "AIHUB_MODEL", "gpt-oss")
                if getattr(settings, "LLM_PROVIDER", "ollama") == "aihub"
                else settings.OLLAMA_LLM_MODEL)
        ),
        "embedding_provider": getattr(settings, "EMBEDDING_PROVIDER", "ollama"),
        "embedding_model": getattr(settings, "EMBEDDING_MODEL", None) or settings.OLLAMA_EMBED_MODEL,
        "gemini_key_hash": hash(getattr(settings, "GEMINI_API_KEY", "") or ""),
        "aihub_key_hash": hash(getattr(settings, "AIHUB_API_KEY", "") or ""),
        "aihub_base": getattr(settings, "AIHUB_BASE_URL", ""),
    }


def _build_ollama_provider(default_model: Optional[str], embed_model: Optional[str]) -> OllamaProvider:
    client = OllamaClient(
        settings.OLLAMA_BASE_URL,
        keep_alive=settings.OLLAMA_KEEP_ALIVE,
        default_model=default_model,
        timeout=settings.OLLAMA_TIMEOUT,
    )
    return OllamaProvider(client, default_model=default_model, embed_model=embed_model)


def _build_gemini_provider(default_model: str, embed_model: str) -> GeminiProvider:
    api_key = getattr(settings, "GEMINI_API_KEY", None)
    if not api_key:
        raise RuntimeError(
            "LLM_PROVIDER=gemini 但 GEMINI_API_KEY 為空 — 請在 .env 設定或在 admin 頁面填入"
        )
    return GeminiProvider(api_key, default_model=default_model, embed_model=embed_model)


def _build_aihub_provider(default_model: Optional[str]) -> AiHubProvider:
    return AiHubProvider(
        api_key=getattr(settings, "AIHUB_API_KEY", None) or "",
        base_url=getattr(settings, "AIHUB_BASE_URL", ""),
        default_model=default_model or getattr(settings, "AIHUB_MODEL", "gpt-oss"),
        timeout=settings.OLLAMA_TIMEOUT,
        session_pool_size=getattr(settings, "AIHUB_SESSION_POOL", 4),
        session_max_uses=getattr(settings, "AIHUB_SESSION_MAX_USES", 200),
    )


def get_llm_provider(force_rebuild: bool = False) -> LLMProvider:
    global _llm_singleton, _cached_signature
    with _llm_lock:
        sig = _signature()
        if (
            _llm_singleton is None
            or force_rebuild
            or sig.get("llm_provider") != _cached_signature.get("llm_provider")
            or sig.get("llm_model") != _cached_signature.get("llm_model")
            or sig.get("gemini_key_hash") != _cached_signature.get("gemini_key_hash")
            or sig.get("aihub_key_hash") != _cached_signature.get("aihub_key_hash")
            or sig.get("aihub_base") != _cached_signature.get("aihub_base")
        ):
            provider_name = sig["llm_provider"]
            model = sig["llm_model"]
            if provider_name == "aihub":
                # AiHub 的 model 是 service/version，與 Ollama 的 tag 命名不同；
                # LLM_MODEL 沒指定就用 AIHUB_MODEL，不要退回 OLLAMA_LLM_MODEL。
                _llm_singleton = _build_aihub_provider(
                    default_model=(getattr(settings, "LLM_MODEL", None)
                                   or getattr(settings, "AIHUB_MODEL", "gpt-oss")))
            elif provider_name == "gemini":
                _llm_singleton = _build_gemini_provider(
                    default_model=model or "gemini-2.5-flash",
                    embed_model=sig["embedding_model"] or "text-embedding-004",
                )
            else:
                _llm_singleton = _build_ollama_provider(
                    default_model=model,
                    embed_model=sig["embedding_model"],
                )
            _cached_signature.update(sig)
            logger.info("LLM provider initialized: %s (model=%s)", provider_name, model)
        return _llm_singleton


def get_embedding_provider(force_rebuild: bool = False) -> LLMProvider:
    global _embed_singleton, _cached_signature
    with _embed_lock:
        sig = _signature()
        if (
            _embed_singleton is None
            or force_rebuild
            or sig.get("embedding_provider") != _cached_signature.get("embedding_provider")
            or sig.get("embedding_model") != _cached_signature.get("embedding_model")
            or sig.get("gemini_key_hash") != _cached_signature.get("gemini_key_hash")
        ):
            provider_name = sig["embedding_provider"]
            model = sig["embedding_model"]
            if provider_name == "gemini":
                _embed_singleton = _build_gemini_provider(
                    default_model=sig["llm_model"] or "gemini-2.5-flash",
                    embed_model=model or "text-embedding-004",
                )
            else:
                _embed_singleton = _build_ollama_provider(
                    default_model=sig["llm_model"],
                    embed_model=model,
                )
            _cached_signature.update(sig)
            logger.info("Embedding provider initialized: %s (model=%s)", provider_name, model)
        return _embed_singleton


def invalidate() -> None:
    """Call after admin changes provider settings so next call rebuilds."""
    global _llm_singleton, _embed_singleton
    with _llm_lock, _embed_lock:
        _llm_singleton = None
        _embed_singleton = None
