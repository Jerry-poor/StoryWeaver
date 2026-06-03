from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

# Imports from modular packages
from backend.app.config import MAX_RECENT_TURNS, KV_WINDOW, ARC_SIZE, CHAT_WINDOW, COMPRESS_THRESHOLD
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


def _safe_turn_id(t: dict) -> int:
    """Return turn_id as int, falling back to 0 for missing/non-numeric values."""
    try:
        return int(t.get("turn_id", 0))
    except (ValueError, TypeError):
        return 0


def compact_recent_turns(memory: Dict[str, Any]) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
    turns = list(memory.get("recent_turns", []))
    chapter_turns = [t for t in turns if t.get("type") == "chapter"]
    other_turns   = [t for t in turns if t.get("type") != "chapter"]
    memory.setdefault("next_turn_id", 1)
    if len(other_turns) <= COMPRESS_THRESHOLD:
        return memory, []
    removed = other_turns[:-CHAT_WINDOW]
    retained_other = other_turns[-CHAT_WINDOW:]
    all_retained = sorted(chapter_turns + retained_other, key=_safe_turn_id)
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


def compact_chapter_turns(memory: dict) -> dict:
    """将超出 KV_WINDOW 的旧 chapter turns 移入 archived_turns 并加入待压缩队列。

    不调用 LLM，纯内存操作，可在写操作结束后同步调用。
    """
    turns = list(memory.get("recent_turns", []))
    chapter_turns = sorted(
        [t for t in turns if t.get("type") == "chapter" and t.get("confirmed", False)],
        key=_safe_turn_id
    )
    other_turns = [t for t in turns if not (t.get("type") == "chapter" and t.get("confirmed", False))]

    if len(chapter_turns) <= KV_WINDOW:
        return memory

    excess = chapter_turns[:-KV_WINDOW]  # 最旧的需要移出的
    retained_chapters = chapter_turns[-KV_WINDOW:]

    # 移入冷存档（追加，去重）
    archived = memory.setdefault("archived_turns", [])
    existing_ids = {t.get("turn_id") for t in archived}
    for t in excess:
        if t.get("turn_id") not in existing_ids:
            archived.append(t)

    # 加入 arc 压缩队列（每 ARC_SIZE 条一批）
    pending = memory.setdefault("pending_arc_compression", [])
    # 只把还没有进入 pending 或 arc_summaries 的 excess 章节加入队列
    already_queued_chapters = set()
    for batch in pending:
        for t in batch:
            already_queued_chapters.add(t.get("turn_id"))
    arc_summaries = memory.get("chapter_arc_summaries", [])
    already_summarized = set()
    for arc in arc_summaries:
        for no in range(arc.get("chapter_range", [0, 0])[0],
                        arc.get("chapter_range", [0, 0])[1] + 1):
            already_summarized.add(no)

    new_excess = [t for t in excess
                  if t.get("turn_id") not in already_queued_chapters
                  and t.get("chapter_no") not in already_summarized]

    pending_turns: List[Dict[str, Any]] = []
    for batch in pending:
        if isinstance(batch, list):
            pending_turns.extend([t for t in batch if isinstance(t, dict)])
    pending_turns.extend(new_excess)

    rebuilt_pending = []
    for i in range(0, len(pending_turns), ARC_SIZE):
        batch = pending_turns[i:i + ARC_SIZE]
        if batch:
            rebuilt_pending.append(batch)
    memory["pending_arc_compression"] = rebuilt_pending

    # 更新 recent_turns：保留非 confirmed chapter turns + 热窗口 chapter turns
    memory["recent_turns"] = sorted(
        other_turns + retained_chapters,
        key=_safe_turn_id
    )
    return memory


def generate_arc_summary(chapter_turns_batch: list) -> dict:
    """将一批 chapter turns 用 LLM 压缩为 arc_summary。"""
    if not chapter_turns_batch:
        return {}

    chapter_nos = [t.get("chapter_no", 0) for t in chapter_turns_batch]
    chapter_range = [min(chapter_nos), max(chapter_nos)]
    arc_no = (chapter_range[0] - 1) // ARC_SIZE + 1

    # 只传摘要字段，不传完整正文（节省 tokens）
    batch_summary = [
        {
            "chapter_no": t.get("chapter_no"),
            "assistant_summary": t.get("assistant", "")[:300],
        }
        for t in chapter_turns_batch
    ]

    prompt = {
        "task": "将以下已完成章节整理为弧线摘要，供后续章节生成参考。",
        "chapters": batch_summary,
        "output_schema": {
            "arc_no": arc_no,
            "chapter_range": chapter_range,
            "key_events": ["重要事件列表，每条一句话"],
            "character_state_snapshot": {"角色ID": "当前状态描述"},
            "open_threads_inherited": ["仍未解决的悬念"],
            "arc_summary_text": "这一弧线的整体叙事进展，2-3句话",
        },
        "requirements": [
            "key_events 最多 8 条",
            "character_state_snapshot 只记录有变化的角色",
            "open_threads_inherited 只列真正未解决的悬念",
            "输出严格 JSON，不要加解释",
        ],
    }
    messages = [
        {"role": "system", "content": "你是小说弧线摘要器，只输出严格 JSON。"},
        {"role": "user", "content": json.dumps(prompt, ensure_ascii=False, indent=2)},
    ]
    try:
        text = deepseek_chat(messages, temperature=0.2)
        data = parse_json_relaxed(text)
        if not isinstance(data, dict):
            raise ValueError("arc summary is not a dict")
        # 确保必要字段存在
        data.setdefault("arc_no", arc_no)
        data.setdefault("chapter_range", chapter_range)
        data.setdefault("key_events", [])
        data.setdefault("character_state_snapshot", {})
        data.setdefault("open_threads_inherited", [])
        data.setdefault("arc_summary_text", "")
        return data
    except Exception:
        # 降级：用拼接摘要
        return {
            "arc_no": arc_no,
            "chapter_range": chapter_range,
            "key_events": [],
            "character_state_snapshot": {},
            "open_threads_inherited": [],
            "arc_summary_text": " | ".join(
                f"第{t.get('chapter_no')}章: {t.get('assistant', '')[:100]}"
                for t in chapter_turns_batch
            ),
        }


def drain_arc_compression(memory: dict) -> dict:
    """同步处理 pending_arc_compression 队列，调用 LLM 生成 arc summaries。

    在章节生成请求开始时调用，清空队列后再构建 context。
    """
    pending = memory.get("pending_arc_compression", [])
    if not pending:
        return memory

    arc_summaries = memory.setdefault("chapter_arc_summaries", [])
    existing_arc_nos = {a.get("arc_no") for a in arc_summaries}

    remaining_pending = []
    for batch in pending:
        if not isinstance(batch, list) or len(batch) < ARC_SIZE:
            remaining_pending.append(batch)
            continue
        arc = generate_arc_summary(batch)
        arc_no = arc.get("arc_no")
        if arc_no not in existing_arc_nos:
            arc_summaries.append(arc)
            existing_arc_nos.add(arc_no)

    # 排序
    memory["chapter_arc_summaries"] = sorted(arc_summaries, key=lambda a: a.get("arc_no", 0))
    memory["pending_arc_compression"] = remaining_pending
    return memory
