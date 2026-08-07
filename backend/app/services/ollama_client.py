import logging
import json
import re
from typing import Any, Dict, List, Optional

import httpx

from ..core.config import settings

logger = logging.getLogger(__name__)


def _strip_reasoning_blocks(text: str) -> str:
    """Remove common reasoning/thinking blocks produced by some models.

    This keeps normal-model answers intact while hiding internal thoughts such as
    <think>...</think> or <analysis>...</analysis> blocks that would otherwise render
    as noise or be filtered out by the frontend Markdown renderer.
    """
    if not text:
        return text

    patterns = [
        r"<think>[\s\S]*?</think>",
        r"<reasoning>[\s\S]*?</reasoning>",
        r"<analysis>[\s\S]*?</analysis>",
        r"<scratchpad>[\s\S]*?</scratchpad>",
    ]
    cleaned = text
    for pat in patterns:
        cleaned = re.sub(pat, "", cleaned, flags=re.IGNORECASE)

    # Best-effort: drop stray XML-like tags that might remain
    cleaned = re.sub(r"</?\w+[^>]*>", "", cleaned)
    return cleaned.strip()


def _extract_from_json_blocks(text: str) -> Optional[str]:
    """Try to parse a JSON answer from code blocks or inline JSON and return final text.

    Looks for keys commonly used to denote the final answer.
    """
    if not text:
        return None

    # Try fenced code block first
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, flags=re.IGNORECASE)
    candidates: List[str] = []
    if fence:
        candidates.append(fence.group(1))

    # Also try to find the first JSON object in the text
    brace = re.search(r"\{[\s\S]*\}", text)
    if brace:
        candidates.append(brace.group(0))

    keys = ["final", "final_answer", "answer", "output", "response", "result"]
    for payload in candidates:
        try:
            data = json.loads(payload)
            for key in keys:
                if key in data and isinstance(data[key], str) and data[key].strip():
                    return data[key].strip()
        except Exception:
            # Not valid JSON – continue trying
            continue
    return None


def _extract_final_answer(text: str) -> str:
    """Heuristic extraction of the final answer from reasoning-style outputs.

    Order of operations:
      1) If a JSON block with a final/answer field exists, return it
      2) If <final>/<answer>/<output>/<response> tags exist, return inner text
      3) If markers like "Final answer:" exist, take trailing content
      4) Strip reasoning blocks and return remaining text
    """
    if not text:
        return text

    # 1) JSON block
    json_text = _extract_from_json_blocks(text)
    if json_text:
        return json_text

    # 2) Final-like tags
    tag_names = ["final", "final_answer", "answer", "output", "response"]
    for name in tag_names:
        m = re.search(fr"<{name}>([\s\S]*?)</{name}>", text, flags=re.IGNORECASE)
        if m and m.group(1).strip():
            return m.group(1).strip()

    # 3) Marker phrases (last occurrence wins)
    markers = [
        r"final\s*answer\s*[:：]\s*(.*)$",
        r"\banswer\s*[:：]\s*(.*)$",
        r"最終答案\s*[:：]\s*(.*)$",
        r"最终答案\s*[:：]\s*(.*)$",
        r"結論\s*[:：]\s*(.*)$",
        r"结论\s*[:：]\s*(.*)$",
        r"答案\s*[:：]\s*(.*)$",
    ]
    for pat in markers:
        m = re.search(pat, text, flags=re.IGNORECASE | re.DOTALL)
        if m and m.group(1).strip():
            return m.group(1).strip()

    # 4) Strip reasoning blocks
    cleaned = _strip_reasoning_blocks(text)
    if cleaned:
        return cleaned

    # 5) Last resort: remove all tags from original text and return inner text
    text_no_tags = re.sub(r"</?\w+[^>]*>", "", text).strip()
    return text_no_tags or text


def _best_effort_final(text: str) -> Optional[str]:
    """Derive a concise final answer from arbitrary text when no explicit marker exists."""
    if not text:
        return None
    cleaned = _strip_control_tokens(_squelch_repetition(_strip_reasoning_blocks(text))).strip()
    if not cleaned:
        return None
    # Prefer first few bullet lines
    bullets = re.findall(r"^(?:[-*•]\s+.+)$", cleaned, flags=re.MULTILINE)
    if bullets:
        return "\n".join(bullets[:5]).strip()
    # Otherwise pick a non-empty paragraph
    paras = [p.strip() for p in re.split(r"\n\s*\n+", cleaned) if p.strip()]
    for p in paras:
        if 10 <= len(p) <= 800:
            return p
    # Fallback to first 200 chars
    return cleaned[:200]


def _squelch_repetition(text: str) -> str:
    """Reduce degenerate repetition patterns without changing normal content.

    Heuristics:
      - Collapse any single character repeated >= 6 times to 6
      - Collapse short ASCII sequences (1–4 letters) repeated >= 6 times to 4
      - Collapse punctuation runs to max 3
    """
    if not text:
        return text

    out = text
    # Single-char repeats (handles CJK too)
    out = re.sub(r"(.)\1{6,}", lambda m: m.group(1) * 6, out)
    # Short ASCII token repeats glued together e.g., NonNonNon...
    out = re.sub(r"([A-Za-z]{1,4})(?:\1){6,}", lambda m: m.group(1) * 4, out)
    # Punctuation floods
    out = re.sub(r"([。．．。！，!?,，、\.\-–—])\1{3,}", lambda m: m.group(1) * 3, out)
    return out


def _strip_control_tokens(text: str) -> str:
    """Remove common chat template/control tokens that can leak into outputs.

    Examples: <|im_start|>, <|im_end|>, <s>, </s>, <<SYS>>, </SYS>, <|assistant|>, <|user|>, etc.
    Also removes zero-width and other non-printing characters.
    """
    if not text:
        return text

    cleaned = text
    # Truncate at stop tokens that signal end of model output
    for stop in ["<|endoftext|>", "<|im_end|>", "</s>"]:
        if stop in cleaned:
            cleaned = cleaned[:cleaned.index(stop)]

    # Zero-width and non-printing characters
    cleaned = re.sub(r"[\u200B\u200C\u200D\u2060\ufeff]", "", cleaned)

    # Common tokens to strip
    tokens = [
        r"<\|im_start\|>", r"<\|im_end\|>", r"<\|assistant\|>", r"<\|user\|>",
        r"<<SYS>>", r"</SYS>", r"<s>", r"</s>", r"<\|endoftext\|>",
    ]
    for t in tokens:
        cleaned = re.sub(t, "", cleaned, flags=re.IGNORECASE)

    # Remove leading role markers like "assistant:" / "user:" at start of lines
    cleaned = re.sub(r"^(\s*)(assistant|user|system)\s*:\s*", r"\1", cleaned, flags=re.IGNORECASE | re.MULTILINE)
    return cleaned.strip()


class OllamaClient:
    """Thin wrapper around the Ollama HTTP API."""

    def __init__(
        self,
        base_url: str,
        *,
        keep_alive: str = "5m",
        default_model: Optional[str] = None,
        timeout: int = 120,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.keep_alive = keep_alive
        self.default_model = default_model
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            headers={"Content-Type": "application/json"},
        )

    def _default_options(self) -> Dict[str, Any]:
        opts: Dict[str, Any] = {}
        # Respect configured defaults if provided
        try:
            if settings.OLLAMA_NUM_PREDICT is not None:
                opts["num_predict"] = settings.OLLAMA_NUM_PREDICT
        except Exception:
            pass
        try:
            if settings.OLLAMA_NUM_CTX is not None:
                opts["num_ctx"] = settings.OLLAMA_NUM_CTX
                logger.info(f"Ollama default options: num_ctx set to {settings.OLLAMA_NUM_CTX}")
        except Exception:
            pass
        # Sampling controls (optional)
        try:
            if settings.OLLAMA_TEMPERATURE is not None:
                opts["temperature"] = settings.OLLAMA_TEMPERATURE
        except Exception:
            pass
        try:
            if settings.OLLAMA_TOP_P is not None:
                opts["top_p"] = settings.OLLAMA_TOP_P
        except Exception:
            pass
        try:
            if settings.OLLAMA_TOP_K is not None:
                opts["top_k"] = settings.OLLAMA_TOP_K
        except Exception:
            pass
        # 固定隨機種子。
        #
        # OLLAMA_TEMPERATURE 預設是 None（從未設定），於是 Ollama 用模型自己的
        # 預設值，生成完全不可重現。實測同一份 golden set 連跑三次得到
        # 0.600 / 0.600 / 0.667 —— 也就是 ±1 題的差異是雜訊而非訊號。
        # 這讓「改動前後比較」在生成層失去意義：先前有兩次 0.033 的變化被當成
        # 真實效果解讀，實際上落在雜訊範圍內。
        #
        # 設 seed 才能讓相同輸入得到相同輸出（temperature=0 單獨不保證，
        # Ollama 仍會依 seed 取樣）。預設給值以確保可重現；要恢復多樣性
        # 把 OLLAMA_SEED 設成 None。
        try:
            if getattr(settings, "OLLAMA_SEED", None) is not None:
                opts["seed"] = settings.OLLAMA_SEED
        except Exception:
            pass
        try:
            if settings.OLLAMA_REPEAT_PENALTY is not None:
                opts["repeat_penalty"] = settings.OLLAMA_REPEAT_PENALTY
        except Exception:
            pass
        try:
            if settings.OLLAMA_MIROSTAT is not None:
                opts["mirostat"] = settings.OLLAMA_MIROSTAT
        except Exception:
            pass
        try:
            if settings.OLLAMA_MIROSTAT_TAU is not None:
                opts["mirostat_tau"] = settings.OLLAMA_MIROSTAT_TAU
        except Exception:
            pass
        try:
            if settings.OLLAMA_MIROSTAT_ETA is not None:
                opts["mirostat_eta"] = settings.OLLAMA_MIROSTAT_ETA
        except Exception:
            pass
        # Stop sequences from env
        try:
            if settings.OLLAMA_STOP:
                stops = [s.strip() for s in str(settings.OLLAMA_STOP).split(",") if s.strip()]
                if stops:
                    opts["stop"] = stops
        except Exception:
            pass
        return opts

    def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            response = self._client.post(path, json=payload)
            response.raise_for_status()
            return response.json()
        except httpx.RequestError as exc:
            logger.error("Ollama request error: %s", exc)
            raise RuntimeError("無法連線至 Ollama 服務，請確認服務已啟動") from exc
        except httpx.HTTPStatusError as exc:
            detail: Any = exc.response.text
            logger.error("Ollama API error (%s): %s", exc.response.status_code, detail)
            raise RuntimeError(f"Ollama API 錯誤：{detail}") from exc

    def generate(
        self,
        prompt: str,
        *,
        model: Optional[str] = None,
        images: Optional[List[str]] = None,
        system: Optional[str] = None,
        format: Optional[Any] = None,
        options: Optional[Dict[str, Any]] = None,
    ) -> str:
        payload: Dict[str, Any] = {
            "model": model or self.default_model,
            "prompt": prompt,
            "stream": False,
            "keep_alive": self.keep_alive,
        }
        if images:
            payload["images"] = images
        if system:
            payload["system"] = system
        if format:
            payload["format"] = format
        default_opts = self._default_options()
        if options:
            merged = {**default_opts, **options}
            if merged:
                payload["options"] = merged
        elif default_opts:
            payload["options"] = default_opts

        # Log request summary (INFO: meta only; DEBUG: prompt preview)
        try:
            logger.info("Ollama.generate request model=%s has_images=%s", payload.get("model"), bool(images))
            if logger.isEnabledFor(logging.DEBUG):
                preview = (prompt or "").replace("\n", " ")
                if len(preview) > 200:
                    preview = preview[:200] + "…"
                logger.debug("Ollama.generate prompt preview=%s", preview)
        except Exception:
            pass

        data = self._post("/api/generate", payload)
        raw = data.get("response", "")
        # Only post-process when the caller didn't request a structured format
        if format:
            return raw

        final = _strip_control_tokens(_squelch_repetition(_extract_final_answer(raw)))
        if final:
            return final

        # If 'response' is empty, some models (e.g., reasoning/vision) put text
        # into top-level thinking/reasoning fields. Try to use those first.
        for key in ("message", "thinking", "reasoning"):
            candidate = None
            if key == "message" and isinstance(data.get("message"), dict):
                candidate = data["message"].get("thinking") or data["message"].get("reasoning")
            elif key in data:
                candidate = data.get(key)
            if isinstance(candidate, str) and candidate.strip():
                cand = _strip_control_tokens(_squelch_repetition(candidate.strip()))
                # Try to extract a final answer from the candidate first
                cand_final = _strip_control_tokens(_squelch_repetition(_extract_final_answer(cand)))
                if cand_final:
                    return cand_final
                # Otherwise derive a concise final-only answer from the text
                best = _best_effort_final(cand)
                if best:
                    return best

        # Show raw response (if any) inside code block to avoid being stripped
        if raw:
            return f"```\n{_strip_control_tokens(_squelch_repetition(raw))}\n```"

        # As a last resort, dump the whole JSON so user sees something
        try:
            dump = json.dumps(data, ensure_ascii=False, indent=2)
            if dump and dump.strip():
                logger.info("Ollama.generate fallback content empty -> returning final-only placeholder")
                return "暫無足夠資訊"
        except Exception:
            pass
        return str(data) or "（模型未輸出內容）"

    def generate_stream(
        self,
        prompt: str,
        *,
        model: Optional[str] = None,
        images: Optional[List[str]] = None,
        system: Optional[str] = None,
        options: Optional[Dict[str, Any]] = None,
    ):
        """Stream tokens from /api/generate as they arrive.

        Yields dicts like {"type": "content", "text": "..."} continuously until done.
        """
        payload: Dict[str, Any] = {
            "model": model or self.default_model,
            "prompt": prompt,
            "stream": True,
            "keep_alive": self.keep_alive,
        }
        if images:
            payload["images"] = images
        if system:
            payload["system"] = system

        default_opts = self._default_options()
        if options:
            merged = {**default_opts, **options}
            if merged:
                payload["options"] = merged
        elif default_opts:
            payload["options"] = default_opts

        with self._client.stream("POST", "/api/generate", json=payload) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line:
                    continue
                try:
                    data = httpx.Response(200, content=line).json()
                except Exception:
                    try:
                        data = json.loads(line)
                    except Exception:
                        continue
                if isinstance(data, dict):
                    if data.get("response"):
                        yield {"type": "content", "text": str(data.get("response"))}
                    thinking = data.get("thinking")
                    if isinstance(thinking, str) and thinking:
                        yield {"type": "thinking", "text": thinking}
                    if data.get("done"):
                        break

    def chat(
        self,
        messages: List[Dict[str, Any]],
        *,
        model: Optional[str] = None,
        format: Optional[Any] = None,
        options: Optional[Dict[str, Any]] = None,
        think: bool = False,
    ) -> str:
        payload: Dict[str, Any] = {
            "model": model or self.default_model,
            "messages": messages,
            "stream": False,
            "keep_alive": self.keep_alive,
        }
        # Always send `think` explicitly. Omitting it makes Ollama fall back to the
        # model's default, and thinking models (e.g. gemma4) default to thinking ON,
        # which makes every RAG/chat call slow enough to hit OLLAMA_TIMEOUT.
        payload["think"] = think
        if format:
            payload["format"] = format
        default_opts = self._default_options()
        if options:
            merged = {**default_opts, **options}
            if merged:
                payload["options"] = merged
        elif default_opts:
            payload["options"] = default_opts

        # Log request summary
        try:
            # Summarize messages for logging without leaking images/base64
            msg_summaries = []
            for m in messages:
                role = m.get("role")
                content = m.get("content") or ""
                preview = content.replace("\n", " ")
                if len(preview) > 160:
                    preview = preview[:160] + "…"
                images_info = ""
                if "images" in m and isinstance(m["images"], list):
                    images_info = f" [images:{len(m['images'])}]"
                msg_summaries.append(f"{role}: {preview}{images_info}")
            logger.info("Ollama.chat request model=%s msgs=%d", payload.get("model"), len(messages))
            logger.debug("Ollama.chat prompt previews: %s", " | ".join(msg_summaries))
        except Exception:
            pass

        data = self._post("/api/chat", payload)
        message = data.get("message") or {}
        content = message.get("content", "")
        raw_content = content

        # Fallbacks if content is unexpectedly empty
        if not content:
            # Some servers may also include a top-level "response"
            content = data.get("response", "")
        if not content:
            # Or a list of messages with the last being the assistant output
            msgs = data.get("messages")
            if isinstance(msgs, list) and msgs:
                last = msgs[-1]
                if isinstance(last, dict):
                    content = last.get("content", "")

        # Response meta logging
        try:
            done_reason = data.get("done_reason") or data.get("doneReason")
            logger.info("Ollama.chat response model=%s content_len=%d done_reason=%s", payload.get("model"), len(content or ""), done_reason)
            if logger.isEnabledFor(logging.DEBUG):
                preview = (content or "").replace("\n", " ")
                if len(preview) > 200:
                    preview = preview[:200] + "…"
                logger.debug("Ollama.chat response preview=%s", preview)
        except Exception:
            pass

        # If a structured response was requested, do not alter the payload
        if format:
            return content

        # First pass extraction
        final = _strip_control_tokens(_squelch_repetition(_extract_final_answer(content)))
        if final:
            return final

        # If model produced empty content but exposed a reasoning/thinking field,
        # perform a single retry with a strict instruction to output a final answer.
        has_reasoning_field = False
        if isinstance(message, dict):
            has_reasoning_field = any(
                isinstance(message.get(k), str) and message.get(k) for k in ("reasoning", "thinking", "thoughts", "analysis", "chain_of_thought", "cot")
            )
        if not has_reasoning_field and isinstance(data, dict):
            msg_obj = data.get("message") or {}
            if isinstance(msg_obj, dict):
                has_reasoning_field = any(
                    isinstance(msg_obj.get(k), str) and msg_obj.get(k) for k in ("reasoning", "thinking", "thoughts", "analysis", "chain_of_thought", "cot")
                )

        if (not final) and (not content or has_reasoning_field):
            retry_messages = list(messages) + [{
                "role": "user",
                "content": (
                    "請直接輸出最終答案，至少一句完整敘述；"
                    "禁止輸出任何控制標記或思考過程（如 <think>、<|im_start|> 等）。"
                    "若無足夠資訊，請回答：暫無足夠資訊。"
                )
            }]

            retry_payload = {
                "model": model or self.default_model,
                "messages": retry_messages,
                "stream": False,
                "keep_alive": self.keep_alive,
            }
            default_opts = self._default_options()
            if options:
                merged = {**default_opts, **options}
                if merged:
                    retry_payload["options"] = merged
            elif default_opts:
                retry_payload["options"] = default_opts

            logger.info("Ollama.chat auto-retry triggered (content empty or reasoning-only)")
            retry_data = self._post("/api/chat", retry_payload)
            rmsg = retry_data.get("message") or {}
            rcontent = rmsg.get("content", "")
            final_retry = _strip_control_tokens(_squelch_repetition(_extract_final_answer(rcontent)))
            try:
                logger.info("Ollama.chat auto-retry response content_len=%d", len(rcontent or ""))
                if logger.isEnabledFor(logging.DEBUG):
                    preview = (rcontent or "").replace("\n", " ")
                    if len(preview) > 200:
                        preview = preview[:200] + "…"
                    logger.debug("Ollama.chat auto-retry preview=%s", preview)
            except Exception:
                pass
            if final_retry:
                return final_retry

        # As an additional safeguard for multi-modal: if any message contains images,
        # fall back to a single-prompt generate() call that flattens history like RAG.
        try:
            any_images = False
            last_images: List[str] = []
            for m in messages:
                imgs = m.get("images") if isinstance(m, dict) else None
                if isinstance(imgs, list) and imgs:
                    any_images = True
                    last_images = imgs  # keep last user-image set
            if (not final) and (not content) and any_images:
                logger.info("Ollama.chat fallback to generate() with flattened prompt for multimodal")
                # Build flattened prompt from history
                parts: List[str] = []
                for m in messages[-6:]:
                    if not isinstance(m, dict):
                        continue
                    role = (m.get("role") or "").lower()
                    text = m.get("content") or ""
                    if role == "system":
                        parts.append(f"[系統]\n{text}")
                    elif role == "user":
                        parts.append(f"Q: {text}")
                    elif role == "assistant":
                        parts.append(f"A: {text}")
                parts.append("請直接輸出最終答案（條列/簡潔），不得輸出任何思考或控制標記。")
                flat_prompt = "\n\n".join(parts)

                gen_text = self.generate(
                    flat_prompt,
                    model=model or self.default_model,
                    images=last_images or None,
                    options=options,
                )
                gen_final = _strip_control_tokens(_squelch_repetition(_extract_final_answer(gen_text)))
                if gen_final:
                    return gen_final
        except Exception as _e:
            logger.warning("generate() fallback failed: %s", _e)

        # Try common reasoning fields if content is empty
        for key in ("reasoning", "thinking", "thoughts", "analysis", "chain_of_thought", "cot"):
            val = None
            if isinstance(message, dict):
                val = message.get(key)
            if not val and isinstance(data, dict):
                val = data.get(key)
            if isinstance(val, str) and val.strip():
                cleaned_reason = _strip_control_tokens(_squelch_repetition(val.strip()))
                if cleaned_reason:
                    best = _best_effort_final(cleaned_reason)
                    if best:
                        return best

        # Show raw content (possibly with <think> tags) inside code block to avoid stripping
        if raw_content:
            return f"```\n{_strip_control_tokens(_squelch_repetition(raw_content))}\n```"

        # Last resort: return the full JSON for visibility
        try:
            dump = json.dumps(data, ensure_ascii=False, indent=2)
            if dump and dump.strip():
                logger.info("Ollama.chat fallback no usable content -> final-only placeholder")
                return "暫無足夠資訊"
        except Exception:
            pass
        return "暫無足夠資訊"

    def chat_stream(
        self,
        messages: List[Dict[str, Any]],
        *,
        model: Optional[str] = None,
        options: Optional[Dict[str, Any]] = None,
        think: bool = False,
    ):
        """Stream tokens from /api/chat.

        Yields dicts: {"type": "content"|"thinking", "text": "..."}
        """
        payload: Dict[str, Any] = {
            "model": model or self.default_model,
            "messages": messages,
            "stream": True,
            "keep_alive": self.keep_alive,
        }
        # Always send `think` explicitly. Omitting it makes Ollama fall back to the
        # model's default, and thinking models (e.g. gemma4) default to thinking ON,
        # which makes every RAG/chat call slow enough to hit OLLAMA_TIMEOUT.
        payload["think"] = think
        default_opts = self._default_options()
        if options:
            merged = {**default_opts, **options}
            if merged:
                payload["options"] = merged
        elif default_opts:
            payload["options"] = default_opts

        with self._client.stream("POST", "/api/chat", json=payload) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                logger.debug("Ollama stream received line: %s", line)
                if not line:
                    continue
                try:
                    data = httpx.Response(200, content=line).json()
                except Exception:
                    try:
                        data = json.loads(line)
                    except Exception:
                        continue
                if not isinstance(data, dict):
                    continue
                msg = data.get("message") or {}
                # Incremental delta content
                delta = msg.get("content")
                if isinstance(delta, str) and delta:
                    yield {"type": "content", "text": delta}
                # Some models surface "thinking" per chunk
                t = msg.get("thinking") or data.get("thinking")
                if isinstance(t, str) and t:
                    yield {"type": "thinking", "text": t}
                if data.get("done"):
                    break

    def embed(
        self,
        inputs: List[str],
        *,
        model: Optional[str] = None,
        truncate: bool = True,
        dimensions: Optional[int] = None,
    ) -> List[List[float]]:
        # Audit H1：防護式截斷上限改為可設定（settings.EMBEDDING_MAX_CHARS，預設 2000）。
        # 舊版硬編 450 → 1800 字 chunk 只有前 25% 進向量、後段內容永遠檢索不到。
        # 部署的 qwen3-embedding 上下文遠大於 2000 字；truncate=True 仍作為伺服器端保險。
        # 若改用 512-token 的 bge-large，請在 .env 設 EMBEDDING_MAX_CHARS=450。
        from ..core.config import settings as _settings
        max_chars = getattr(_settings, "EMBEDDING_MAX_CHARS", 2000) or 2000
        safe_inputs = [
            (s or "")[:max_chars] if isinstance(s, str) else str(s or "")[:max_chars]
            for s in inputs
        ]

        payload: Dict[str, Any] = {
            "model": model or self.default_model,
            "input": safe_inputs,
            "keep_alive": self.keep_alive,
            "truncate": truncate,
        }
        if dimensions:
            payload["dimensions"] = dimensions

        data = self._post("/api/embed", payload)
        embeddings = data.get("embeddings")
        if not embeddings:
            raise RuntimeError("Ollama 未返回任何 embedding 結果")
        return embeddings

    def list_models(self) -> List[Dict[str, Any]]:
        try:
            resp = self._client.get("/api/tags")
            resp.raise_for_status()
            data = resp.json()
            return data.get("models", [])
        except httpx.RequestError as exc:
            logger.warning("無法取得 Ollama 模型列表：%s", exc)
            return []
        except httpx.HTTPStatusError as exc:
            logger.warning("Ollama 模型列表 API 失敗：%s", exc)
            return []

    def version(self) -> Optional[str]:
        try:
            resp = self._client.get("/api/version")
            resp.raise_for_status()
            data = resp.json()
            return data.get("version")
        except httpx.HTTPError as exc:
            logger.warning("無法取得 Ollama 版本資訊：%s", exc)
            return None


_client: Optional[OllamaClient] = None


def get_client() -> OllamaClient:
    global _client
    if _client is None:
        _client = OllamaClient(
            settings.OLLAMA_BASE_URL,
            keep_alive=settings.OLLAMA_KEEP_ALIVE,
            default_model=settings.OLLAMA_LLM_MODEL,
            timeout=settings.OLLAMA_TIMEOUT,
        )
    return _client
