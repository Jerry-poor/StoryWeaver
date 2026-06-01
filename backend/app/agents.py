from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

# Imports from modular packages
from backend.app.config import MAX_RECENT_TURNS
from backend.app.storage import default_conversation_memory, utc_now
from backend.app.llm import deepseek_chat, parse_json_relaxed


def parse_user_constraints_from_message(user_message: str, registry: Dict[str, Any]) -> Dict[str, Any]:
    prompt = {
        "task": "从用户消息中提取长期写作约束，分为四类：global_constraints（全局约束，如'不要写战斗'）、chapter_constraints（章节级约束，如'每章不超过2000字'）、style_preferences（风格偏好，如'多用对话'）、banned_patterns（禁止出现的模式，如'男主冷笑'）。",
        "user_message": user_message,
        "current_registry": registry,
        "output_schema": {
            "global_constraints": [],
            "chapter_constraints": [],
            "style_preferences": [],
            "banned_patterns": [],
        },
        "requirements": [
            "只提取明确的、长期的写作约束，不提取一次性指令",
            "如果用户说'以后不要'、'永远不要'、'不要再'等，归入 banned_patterns 或 global_constraints",
            "如果用户说'每次'、'总是'、'保持'等，归入 style_preferences 或 chapter_constraints",
            "输出严格 JSON，不要加解释",
        ],
    }
    messages = [
        {"role": "system", "content": "你是约束提取器，只输出严格 JSON。"},
        {"role": "user", "content": json.dumps(prompt, ensure_ascii=False, indent=2)},
    ]
    try:
        text = deepseek_chat(messages, temperature=0.1)
        data = parse_json_relaxed(text)
        if not isinstance(data, dict):
            return registry
        for key in ("global_constraints", "chapter_constraints", "style_preferences", "banned_patterns"):
            if key in data and isinstance(data[key], list):
                registry[key] = merge_list_unique(registry.get(key, []), [str(v).strip() for v in data[key] if str(v).strip()])
        return registry
    except Exception:
        return registry


def summarize_old_turns(old_turns: List[Dict[str, Any]], current_summary: Dict[str, Any]) -> Dict[str, Any]:
    if not old_turns:
        return current_summary
    prompt = {
        "task": "将被压缩的旧对话整理为结构化记忆，供小说写作继续使用。",
        "summary_schema": {
            "user_preferences": [],
            "confirmed_constraints": [],
            "active_requests": [],
            "resolved_decisions": [],
            "freeform_summary": "",
        },
        "current_summary": current_summary,
        "old_turns": old_turns,
        "requirements": [
            "提炼用户偏好、确认过的约束、仍然有效的请求和已经完成的决策",
            "不要保留闲聊和重复信息",
            "如果字段没有新增内容可以保持为空数组或空字符串",
            "输出严格 JSON，不要加解释",
        ],
    }
    messages = [
        {
            "role": "system",
            "content": "你是一个记忆压缩器，只输出严格 JSON。",
        },
        {
            "role": "user",
            "content": json.dumps(prompt, ensure_ascii=False, indent=2),
        },
    ]
    try:
        text = deepseek_chat(messages, temperature=0.2)
        data = parse_json_relaxed(text)
        if not isinstance(data, dict):
            raise ValueError("summary is not a JSON object")
        return merge_dialogue_summary(current_summary, data)
    except Exception:
        fallback = dict(current_summary)
        fallback["freeform_summary"] = (
            (fallback.get("freeform_summary") or "")
            + "\n"
            + " | ".join(
                [
                    f"{turn.get('role', 'unknown')}: {str(turn.get('content', ''))[:120]}"
                    for turn in old_turns
                ]
            )
        ).strip(" \n|")
        return fallback


def merge_list_unique(base: List[str], extra: List[str]) -> List[str]:
    seen = set()
    merged: List[str] = []
    for item in base + extra:
        if item is None:
            continue
        value = str(item).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        merged.append(value)
    return merged


def merge_dialogue_summary(current: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    merged = {
        "user_preferences": merge_list_unique(
            list(current.get("user_preferences", [])),
            list(patch.get("user_preferences", [])),
        ),
        "confirmed_constraints": merge_list_unique(
            list(current.get("confirmed_constraints", [])),
            list(patch.get("confirmed_constraints", [])),
        ),
        "active_requests": merge_list_unique(
            list(current.get("active_requests", [])),
            list(patch.get("active_requests", [])),
        ),
        "resolved_decisions": merge_list_unique(
            list(current.get("resolved_decisions", [])),
            list(patch.get("resolved_decisions", [])),
        ),
        "freeform_summary": "\n".join(
            [
                s.strip()
                for s in [
                    str(current.get("freeform_summary", "")).strip(),
                    str(patch.get("freeform_summary", "")).strip(),
                ]
                if s
            ]
        ),
    }
    return merged


def compact_recent_turns(memory: Dict[str, Any]) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
    turns = list(memory.get("recent_turns", []))
    # chapter turns are kept permanently for KV-cache replay; only compact chat/outline turns
    chapter_turns = [t for t in turns if t.get("type") == "chapter"]
    other_turns = [t for t in turns if t.get("type") != "chapter"]
    memory.setdefault("next_turn_id", 1)
    if len(other_turns) <= MAX_RECENT_TURNS:
        return memory, []
    removed = other_turns[:-MAX_RECENT_TURNS]
    retained_other = other_turns[-MAX_RECENT_TURNS:]
    all_retained = sorted(chapter_turns + retained_other, key=lambda t: int(t.get("turn_id", 0)))
    memory["recent_turns"] = all_retained
    return memory, removed


def append_turn(
    memory: Dict[str, Any],
    user_content: str,
    assistant_content: str,
    turn_type: str = "chat",
    chapter_no: Optional[int] = None,
    extra_fields: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    next_turn_id = int(memory.get("next_turn_id", 1))
    turn: Dict[str, Any] = {
        "turn_id": next_turn_id,
        "type": turn_type,
        "timestamp": utc_now(),
        "user": user_content,
        "assistant": assistant_content,
    }
    if chapter_no is not None:
        turn["chapter_no"] = chapter_no
    if extra_fields:
        turn.update(extra_fields)
    memory.setdefault("recent_turns", []).append(turn)
    memory["next_turn_id"] = next_turn_id + 1
    return memory
