from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional
from urllib import error, request

# Import config constants
from backend.app.config import (
    DEEPSEEK_BASE_URL,
    DEEPSEEK_API_KEY,
    DEEPSEEK_MODEL,
    DEEPSEEK_MAX_TOKENS,
)


def extract_json_block(text: str) -> str:
    match = re.search(r"```json\s*(.*?)\s*```", text, re.S | re.I)
    if match:
        return match.group(1).strip()
    match = re.search(r"```(?:\w+)?\s*(.*?)\s*```", text, re.S)
    if match:
        return match.group(1).strip()
    return text.strip()


def parse_json_relaxed(text: str) -> Any:
    cleaned = extract_json_block(text)
    first_object = cleaned.find("{")
    first_array = cleaned.find("[")
    starts = [idx for idx in (first_object, first_array) if idx != -1]
    if starts:
        start = min(starts)
        cleaned = cleaned[start:]
        if cleaned.startswith("["):
            end = cleaned.rfind("]")
        else:
            end = cleaned.rfind("}")
        if end != -1:
            cleaned = cleaned[: end + 1]
    return json.loads(cleaned)


def parse_outline_json_from_text(text: str) -> Dict[str, Any]:
    match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.S | re.I)
    if match:
        data = json.loads(match.group(1))
        if isinstance(data, dict):
            return data
    return parse_json_relaxed(text)


def deepseek_chat(
    messages: List[Dict[str, str]],
    temperature: float = 0.7,
    max_tokens: int = DEEPSEEK_MAX_TOKENS,
) -> str:
    url = f"{DEEPSEEK_BASE_URL}/chat/completions"
    payload: Dict[str, Any] = {
        "model": DEEPSEEK_MODEL,
        "messages": messages,
        "temperature": temperature,
    }
    payload["max_tokens"] = max_tokens
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
    }
    if DEEPSEEK_API_KEY:
        headers["Authorization"] = f"Bearer {DEEPSEEK_API_KEY}"
    last_error: Optional[Exception] = None
    for attempt in range(5):
        req = request.Request(url, data=body, headers=headers, method="POST")
        try:
            with request.urlopen(req, timeout=180) as resp:
                raw = resp.read().decode("utf-8")
            data = json.loads(raw)
            return data["choices"][0]["message"]["content"]
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            last_error = RuntimeError(f"DeepSeek API HTTP {exc.code}: {detail}")
        except json.JSONDecodeError:
            last_error = RuntimeError(f"DeepSeek API returned non-JSON response on attempt {attempt + 1}")
        except Exception as exc:
            last_error = RuntimeError(f"DeepSeek API request failed on attempt {attempt + 1}: {exc}")
        if attempt < 4:
            import time as _time

            _time.sleep(min(10, 2**attempt))
    raise RuntimeError(str(last_error) if last_error else "DeepSeek API failed")


def deepseek_chat_stream(
    messages: List[Dict[str, str]],
    temperature: float = 0.7,
    max_tokens: int = DEEPSEEK_MAX_TOKENS,
) -> Any:
    url = f"{DEEPSEEK_BASE_URL}/chat/completions"
    payload: Dict[str, Any] = {
        "model": DEEPSEEK_MODEL,
        "messages": messages,
        "temperature": temperature,
        "stream": True,
    }
    payload["max_tokens"] = max_tokens
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    if DEEPSEEK_API_KEY:
        headers["Authorization"] = f"Bearer {DEEPSEEK_API_KEY}"
    def iterator():
        last_error: Optional[Exception] = None
        for attempt in range(5):
            req = request.Request(url, data=body, headers=headers, method="POST")
            try:
                resp = request.urlopen(req, timeout=180)
                with resp:
                    for raw_line in resp:
                        line = raw_line.decode("utf-8", errors="ignore").strip()
                        if not line or not line.startswith("data:"):
                            continue
                        data = line[len("data:"):].strip()
                        if data == "[DONE]":
                            return
                        try:
                            payload = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        choices = payload.get("choices") or []
                        if not choices:
                            continue
                        delta = choices[0].get("delta") or {}
                        content = delta.get("content")
                        if content:
                            yield content
                return
            except error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="ignore")
                last_error = RuntimeError(f"DeepSeek API HTTP {exc.code}: {detail}")
            except Exception as exc:
                last_error = RuntimeError(f"DeepSeek API request failed on attempt {attempt + 1}: {exc}")
            if attempt < 4:
                import time as _time

                _time.sleep(min(10, 2**attempt))
        raise RuntimeError(str(last_error) if last_error else "DeepSeek streaming API failed")

    return iterator()


def short_chat_response(messages: List[Dict[str, str]]) -> str:
    return deepseek_chat(messages, temperature=0.7)
