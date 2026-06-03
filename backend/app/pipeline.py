from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Tuple, Optional

# Modular imports
from backend.app.config import (
    FILE_LOCK,
    STORY_BRIEF_PATH,
    CHARACTERS_PATH,
    STORYLINE_PATH,
    CONVERSATION_PATH,
    INSTRUCTION_REGISTRY_PATH,
    CONTINUITY_PATH,
    MAX_RECENT_TURNS,
    CHAPTER_DIR,
    SINGLE_SEGMENT_THRESHOLD,
    DEFAULT_SEGMENT_TARGET_WORDS,
    DEFAULT_MAX_SEGMENT_TOKENS,
    DEFAULT_MAX_SEGMENTS,
    KV_WINDOW,
)
from backend.app.storage import (
    default_outline,
    default_characters,
    default_storyline,
    default_conversation_memory,
    default_instruction_registry,
    default_continuity,
    default_character_state,
    default_ending_state,
    default_continuity_contract,
    default_segment,
    default_beat,
    default_chapter_segment_plan,
    default_chapter_draft,
    safe_dict,
    safe_list,
    normalize_outline_shape,
    save_json,
    load_json,
    utc_now,
    load_instruction_registry,
    load_continuity,
    save_continuity,
)
from backend.app.llm import deepseek_chat, deepseek_chat_stream, parse_json_relaxed, short_chat_response
from backend.app.agents import merge_list_unique


def _resolve_field(item: Dict[str, Any], primary: str, aliases: Tuple[str, ...], default: Any = "") -> Any:
    """Resolve a field value from an item dict, trying primary key then aliases."""
    val = item.get(primary)
    if val is not None and val != "" and val != []:
        return val
    for alias in aliases:
        val = item.get(alias)
        if val is not None and val != "" and val != []:
            return val
    return default


def _coerce_str_list(value: Any) -> List[str]:
    """Coerce a value to a list of non-empty strings.
    
    Handles: list, comma/Chinese-comma-separated string, single string.
    """
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str) and value.strip():
        # Split on common delimiters: Chinese comma, comma, semicolon
        parts = re.split(r'[,，、；;]+', value)
        result = [p.strip() for p in parts if p.strip()]
        return result if len(result) > 1 else [value.strip()]
    return []


def normalize_character_record(item: Dict[str, Any], index: int) -> Dict[str, Any]:
    """Normalize a raw character dict into the canonical character schema.
    
    Handles common field name aliases (Chinese and English) and coerces
    types so that personality/constraints are always lists, appearance/motivation
    are always strings, and current_state always has the full set of keys.
    """
    if not isinstance(item, dict):
        name = str(item).strip() or f"角色{index}"
        return {
            "id": f"c{index:03d}",
            "name": name,
            "role": "主角" if index == 1 else "重要角色",
            "appearance": "",
            "personality": [],
            "motivation": "",
            "constraints": [],
            "current_state": default_character_state(),
        }

    name = str(
        _resolve_field(item, "name", ("character", "alias", "角色名", "姓名", "称呼"), f"角色{index}")
    ).strip()
    role = str(
        _resolve_field(item, "role", ("角色", "身份", "定位", "type"), "主角" if index == 1 else "重要角色")
    ).strip()
    appearance = str(
        _resolve_field(item, "appearance", ("外貌", "looks", "外貌描述", "描述", "description", "外观"), "")
    ).strip()
    personality = _coerce_str_list(
        _resolve_field(item, "personality", ("性格", "traits", "trait", "性格特点", "特点", "character_traits"), [])
    )
    motivation = str(
        _resolve_field(item, "motivation", ("动机", "goal", "objective", "目标", "目的"), "")
    ).strip()
    constraints = _coerce_str_list(
        _resolve_field(item, "constraints", ("约束", "限制", "规则", "rules"), [])
    )

    raw_state = _resolve_field(item, "current_state", ("state", "状态", "当前状态"), {})
    if not isinstance(raw_state, dict):
        raw_state = {}
    current_state = {
        "location": str(raw_state.get("location", raw_state.get("位置", ""))).strip(),
        "mood": str(raw_state.get("mood", raw_state.get("情绪", raw_state.get("心情", "")))).strip(),
        "injury": str(raw_state.get("injury", raw_state.get("伤势", ""))).strip(),
        "known_information": _coerce_str_list(raw_state.get("known_information", raw_state.get("已知信息", []))),
        "unknown_information": _coerce_str_list(raw_state.get("unknown_information", raw_state.get("未知信息", []))),
    }

    # Preserve the original id if present
    char_id = str(item.get("id", f"c{index:03d}")).strip()

    return {
        "id": char_id,
        "name": name,
        "role": role,
        "appearance": appearance,
        "personality": personality,
        "motivation": motivation,
        "constraints": constraints,
        "current_state": current_state,
    }


def build_characters_from_outline(outline: Dict[str, Any]) -> Dict[str, Any]:
    seed_items = safe_list(outline.get("character_seed"))
    if not seed_items:
        return default_characters()

    characters: List[Dict[str, Any]] = []
    for index, item in enumerate(seed_items, start=1):
        record = normalize_character_record(item, index)
        # Always assign sequential id for seed characters
        record["id"] = f"c{index:03d}"
        characters.append(record)

    return {"characters": characters}


def build_storyline_from_outline(outline: Dict[str, Any]) -> Dict[str, Any]:
    title = str(outline.get("title", "")).strip() or default_outline()["title"]
    genre = str(outline.get("genre", "")).strip()
    theme = str(outline.get("theme", "")).strip()
    logline = str(outline.get("logline", "")).strip()
    world_setting = safe_dict(outline.get("world_setting"), default_outline()["world_setting"])
    location = str(world_setting.get("location", "")).strip()
    time_setting = str(world_setting.get("time", "")).strip()
    overall_summary = "；".join([s for s in [
        f"《{title}》",
        f"题材：{genre}" if genre else "",
        f"主题：{theme}" if theme else "",
        f"设定：{time_setting} / {location}" if time_setting or location else "",
        logline,
    ] if s])
    if not overall_summary:
        overall_summary = "故事尚未展开，等待根据已确认大纲进入正文写作。"
        
    chapter_plans = safe_list(outline.get("chapter_plan"))
    chapter_summaries = []
    for plan in chapter_plans:
        if isinstance(plan, dict):
            chapter_summaries.append({
                "chapter_no": plan.get("chapter_no", len(chapter_summaries) + 1),
                "title": f"第{plan.get('chapter_no', '?')}章",
                "summary": [plan.get("goal", "")] if plan.get("goal") else [],
                "events": [],
                "open_threads": [],
                "resolved_threads": [],
            })

    return {
        "overall_summary": overall_summary,
        "chapter_summaries": chapter_summaries,
        "open_threads": [],
        "resolved_threads": [],
    }


def reset_generated_story_state(outline: Dict[str, Any]) -> None:
    story_brief = str(outline.get("source_brief", "")).strip()
    story_brief_record = {
        "description": story_brief,
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }
    storyline = build_storyline_from_outline(outline)
    characters = build_characters_from_outline(outline)
    conversation_memory = default_conversation_memory()
    with FILE_LOCK:
        save_json(STORY_BRIEF_PATH, story_brief_record)
        save_json(CHARACTERS_PATH, characters)
        save_json(STORYLINE_PATH, storyline)
        save_json(CONVERSATION_PATH, conversation_memory)
        save_json(INSTRUCTION_REGISTRY_PATH, default_instruction_registry())
        save_json(CONTINUITY_PATH, default_continuity())
        for chapter_file in CHAPTER_DIR.glob("chapter_*.json"):
            try:
                chapter_file.unlink()
            except FileNotFoundError:
                continue


def select_chapter_plan(outline: Dict[str, Any], chapter_no: int) -> Dict[str, Any]:
    chapter_plan = safe_list(outline.get("chapter_plan"))
    for item in chapter_plan:
        try:
            if not isinstance(item, dict):
                continue
            if int(item.get("chapter_no")) == chapter_no:
                return item
        except Exception:
            continue
    return {
        "chapter_no": chapter_no,
        "goal": "继续推进故事",
        "must_include": [],
        "cannot_include": [],
    }


def _extract_plot_points(instruction: str) -> List[str]:
    """Extract distinct plot points from user instruction for explicit tracking.

    Splits Chinese text at common delimiters (sentence-ending punctuation,
    semicolons, numbered items) so each atomic requirement is listed separately.
    This makes it harder for the LLM to silently skip any requirement.
    """
    if not instruction:
        return []
    parts = re.split(r'[。；;！!？?\n]+', instruction)
    points = [p.strip() for p in parts if p.strip() and len(p.strip()) > 2]
    return points if points else [instruction.strip()]


def build_generation_context(state: Dict[str, Any], chapter_no: int, instruction: str, tone: str, length_target: int) -> Dict[str, Any]:
    outline = state["outline"]
    characters = state["characters"]
    storyline = state["storyline"]
    memory = state["conversation_memory"]
    registry = state.get("instruction_registry") or load_instruction_registry()
    continuity = state.get("continuity") or load_continuity()
    chapter_plan = select_chapter_plan(outline, chapter_no)
    recent_non_chapter = [t for t in memory.get("recent_turns", []) if t.get("type") != "chapter"]
    arc_summaries = memory.get("chapter_arc_summaries", [])
    confirmed_story_facts = []
    for cs in safe_list(storyline.get("chapter_summaries", [])):
        if isinstance(cs, dict):
            confirmed_story_facts.extend(safe_list(cs.get("events", [])))
    confirmed_story_facts = confirmed_story_facts[-30:]
    return {
        "current_user_directives": {
            "extra_instruction": instruction,
            "instruction_plot_points": _extract_plot_points(instruction),
            "tone": tone or safe_dict(outline.get("writing_rules"), default_outline()["writing_rules"]).get("tone", ""),
            "length_target": length_target or safe_dict(outline.get("writing_rules"), default_outline()["writing_rules"]).get("chapter_length_target", 1800),
        },
        "active_instruction_constraints": registry,
        "continuity_contract": continuity.get("continuity_contract", default_continuity_contract()),
        "previous_ending_state": continuity.get("ending_state", default_ending_state()),
        "thread_priority": continuity.get("thread_priority", {"immediate_threads": [], "chapter_threads": [], "long_arc_threads": []}),
        "confirmed_story_facts": confirmed_story_facts,
        "chapter_task": {
            "chapter_no": chapter_no,
            "chapter_plan": chapter_plan,
        },
        "characters": characters,
        "outline": outline,
        "storyline_summary": storyline,
        "chapter_arc_summaries": arc_summaries,
        "dialogue_summary": memory.get("dialogue_summary", {}),
        "recent_turns": recent_non_chapter[-MAX_RECENT_TURNS:],
        "writing_optimization_goals": {
            "advance_open_threads": True,
            "resolve_immediate_threads": True,
            "limit_new_threads": True,
            "narrative_continuity_with_previous_chapter": True,
            "avoid_forced_suspense": True,
            "auto_fill_missing_backstory": True,
            "maintain_pov_consistency": True,
        },
        "priority_order": [
            "current_user_directives",
            "active_instruction_constraints",
            "continuity_contract",
            "confirmed_story_facts",
            "characters",
            "outline",
            "storyline_summary",
            "thread_priority",
            "chapter_arc_summaries",
            "dialogue_summary",
            "recent_turns",
            "chapter_task",
            "writing_optimization_goals",
        ],
    }


def build_chapter_messages_with_history(
    system_prompt: str,
    current_user_content: str,
    memory: Dict[str, Any],
) -> List[Dict[str, str]]:
    """Build messages replaying at most KV_WINDOW most recent CONFIRMED chapter turns."""
    messages: List[Dict[str, str]] = [{"role": "system", "content": system_prompt}]
    chapter_turns = [
        t for t in memory.get("recent_turns", [])
        if t.get("type") == "chapter" and t.get("confirmed", False)
    ]
    for turn in chapter_turns[-KV_WINDOW:]:
        task_text = turn.get("user_prompt_text")
        assistant_text = turn.get("assistant_text")
        if task_text and assistant_text:
            messages.append({"role": "user", "content": task_text})
            messages.append({"role": "assistant", "content": assistant_text})
    messages.append({"role": "user", "content": current_user_content})
    return messages


def generate_chapter_text(context: Dict[str, Any], memory: Optional[Dict[str, Any]] = None) -> str:
    length_target = context.get("current_user_directives", {}).get("length_target", 1800)
    system_prompt = build_chapter_system_prompt(length_target)
    user_prompt = json.dumps(context, ensure_ascii=False, indent=2)
    messages = build_chapter_messages_with_history(system_prompt, user_prompt, memory) if memory else [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    return deepseek_chat(
        messages,
        temperature=0.85,
    )


def extract_chapter_updates(state: Dict[str, Any], chapter_no: int, chapter_text: str) -> Dict[str, Any]:
    continuity = state.get("continuity") or load_continuity()
    prompt = {
        "task": "从小说章节正文中抽取用于更新故事线和角色状态的结构化信息，以及章节结尾状态和线索优先级。",
        "schema": {
            "chapter_title": "",
            "chapter_summary": [],
            "new_events": [],
            "open_threads": [],
            "resolved_threads": [],
            "character_updates": [
                {
                    "id": "c001",
                    "current_state_changes": {},
                    "notes": "",
                }
            ],
            "new_characters": [
                {
                    "id": "c999",
                    "name": "",
                    "role": "",
                    "appearance": "外貌描述",
                    "personality": ["性格特点"],
                    "motivation": "角色动机",
                    "constraints": [],
                    "current_state": {}
                }
            ],
            "storyline_summary": "",
            "ending_state": {
                "location": "",
                "time": "",
                "characters_present": [],
                "last_action": "",
                "last_dialogue": "",
                "emotional_tone": "",
                "immediate_unresolved_question": "",
                "scene_continues": False,
                "recommended_next_opening": "",
                "next_chapter_driver": "",
            },
            "thread_priority": {
                "immediate_threads": [],
                "chapter_threads": [],
                "long_arc_threads": [],
            },
        },
        "chapter_no": chapter_no,
        "outline": state["outline"],
        "characters": state["characters"],
        "previous_storyline": state["storyline"],
        "previous_ending_state": continuity.get("ending_state", default_ending_state()),
        "chapter_text": chapter_text,
        "requirements": [
            "输出严格 JSON",
            "character_updates 仅填写真正变化的角色",
            "current_state_changes 只放当前状态字段",
            "如果本章出现了已有角色表中没有的新重要角色，请在 new_characters 中补充",
            "如果没有变化或没有新角色，返回空数组或空对象",
            "ending_state 必须准确反映本章结尾的场景状态",
            "scene_continues 仅在本章结尾场景在物理时空上字面尚未结束（动作进行中、对话未完）时为 true；不要为了制造悬念而设为 true",
            "next_chapter_driver 必须填写：用一句话描述本章结尾留下的、应当推动下一章开场的具体后果/决定/行动/动机（例如'主角决定连夜赶往码头核实账本'）。即使下一章场景切换，开场也应承接这个驱动力，而不是用'天黑了/第二天'之类通用时间过场",
            "immediate_unresolved_question 只记录剧情自然产生的待解问题，不要为了悬疑刻意制造神秘钩子",
            "thread_priority 将 open_threads 按紧迫程度分为三类：immediate_threads（必须下一章处理）、chapter_threads（近几章内处理）、long_arc_threads（长线伏笔）",
        ],
    }
    messages = [
        {"role": "system", "content": "你是结构化抽取器，只输出严格 JSON。"},
        {"role": "user", "content": json.dumps(prompt, ensure_ascii=False, indent=2)},
    ]
    try:
        text = deepseek_chat(messages, temperature=0.2)
        data = parse_json_relaxed(text)
        if not isinstance(data, dict):
            raise ValueError("chapter update extraction did not return JSON object")
        if "ending_state" not in data or not isinstance(data.get("ending_state"), dict):
            data["ending_state"] = default_ending_state()
        if "thread_priority" not in data or not isinstance(data.get("thread_priority"), dict):
            data["thread_priority"] = {"immediate_threads": [], "chapter_threads": [], "long_arc_threads": []}
        return data
    except Exception:
        return {
            "chapter_title": f"第{chapter_no}章",
            "chapter_summary": [f"第{chapter_no}章完成。"],
            "new_events": [],
            "open_threads": [],
            "resolved_threads": [],
            "character_updates": [],
            "new_characters": [],
            "storyline_summary": state.get("storyline", {}).get("overall_summary", ""),
            "ending_state": default_ending_state(),
            "thread_priority": {"immediate_threads": [], "chapter_threads": [], "long_arc_threads": []},
        }


def merge_character_updates(characters: Dict[str, Any], updates: List[Dict[str, Any]], new_chars: List[Dict[str, Any]] = None) -> Dict[str, Any]:
    items = [item for item in characters.get("characters", []) if isinstance(item, dict)]
    index = {item.get("id"): item for item in items}
    
    if new_chars:
        next_index = len(items) + 1
        for char in new_chars:
            if isinstance(char, dict) and char.get("id") and char.get("id") not in index:
                # Normalize new character to ensure all fields are present
                normalized = normalize_character_record(char, next_index)
                # Preserve the original id from extraction
                normalized["id"] = char["id"]
                items.append(normalized)
                index[normalized["id"]] = normalized
                next_index += 1

    for update in updates:
        char_id = update.get("id")
        if char_id not in index:
            continue
        target = index[char_id]
        current_state = target.setdefault("current_state", default_character_state())
        changes = update.get("current_state_changes", {})
        if isinstance(changes, dict):
            for key, value in changes.items():
                current_state[key] = value
    characters["characters"] = items
    return characters


def update_storyline(storyline: Dict[str, Any], chapter_no: int, chapter_title: str, data: Dict[str, Any]) -> Dict[str, Any]:
    chapter_entry = {
        "chapter_no": chapter_no,
        "title": chapter_title or f"第{chapter_no}章",
        "summary": data.get("chapter_summary", []),
        "events": data.get("new_events", []),
        "open_threads": data.get("open_threads", []),
        "resolved_threads": data.get("resolved_threads", []),
    }
    history = list(storyline.get("chapter_summaries", []))
    history = [item for item in history if isinstance(item, dict) and str(item.get("chapter_no")) != str(chapter_no)]
    history.append(chapter_entry)
    history.sort(key=lambda item: int(item.get("chapter_no", 0)))
    storyline["chapter_summaries"] = history
    storyline["overall_summary"] = data.get("storyline_summary", storyline.get("overall_summary", ""))
    resolved = merge_list_unique(
        list(storyline.get("resolved_threads", [])),
        list(data.get("resolved_threads", [])),
    )
    resolved_set = set(resolved)
    storyline["open_threads"] = [
        thread for thread in merge_list_unique(
            list(storyline.get("open_threads", [])),
            list(data.get("open_threads", [])),
        )
        if thread not in resolved_set
    ]
    storyline["resolved_threads"] = resolved
    return storyline


def chapter_title_from_updates(chapter_no: int, updates: Dict[str, Any]) -> str:
    title = str(updates.get("chapter_title", "")).strip()
    return title or f"第{chapter_no}章"


def chapter_word_count(text: str) -> int:
    return len(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]+", text))


def check_instruction_compliance(chapter_text: str, context: Dict[str, Any]) -> Dict[str, Any]:
    user_directives = context.get("current_user_directives", {})
    registry = context.get("active_instruction_constraints", {})
    extra_instruction = str(user_directives.get("extra_instruction", "")).strip()
    instruction_plot_points = user_directives.get("instruction_plot_points", [])
    global_constraints = registry.get("global_constraints", [])
    chapter_constraints = registry.get("chapter_constraints", [])
    banned_patterns = registry.get("banned_patterns", [])
    if not extra_instruction and not global_constraints and not chapter_constraints and not banned_patterns:
        return {"compliant": True, "violations": []}
    prompt = {
        "task": "检查小说章节正文是否违反用户指令和长期约束，并检查用户要求的情节要点是否在正文中得到体现。",
        "chapter_text": chapter_text,
        "current_user_directives": user_directives,
        "instruction_plot_points": instruction_plot_points,
        "global_constraints": global_constraints,
        "chapter_constraints": chapter_constraints,
        "banned_patterns": banned_patterns,
        "output_schema": {
            "compliant": True,
            "violations": [
                {
                    "type": "user_directive | plot_point_missing | global_constraint | chapter_constraint | banned_pattern",
                    "rule": "被违反的具体规则或遗漏的情节要点",
                    "evidence": "章节中违反该规则的原文片段，或说明哪个情节要点未体现",
                    "severity": "hard | soft",
                }
            ],
        },
        "requirements": [
            "仔细检查章节正文是否违反了任何用户硬指令或长期约束",
            "如果 instruction_plot_points 非空，逐一检查每个情节要点是否在正文中得到了合理体现",
            "如果某个情节要点在正文中被完全省略或严重简化（仅一笔带过而非展开描写），这是 type=plot_point_missing、severity=hard 的违反",
            "如果用户指令说'不要写战斗'，章节中出现任何战斗描写都是 hard violation",
            "如果 banned_patterns 包含'男主冷笑'，章节中出现该模式就是 hard violation",
            "severity=hard 表示必须修订，soft 表示建议修订",
            "如果没有任何违反，compliant=true，violations 为空数组",
            "输出严格 JSON",
        ],
    }
    messages = [
        {"role": "system", "content": "你是合规检查器，只输出严格 JSON。"},
        {"role": "user", "content": json.dumps(prompt, ensure_ascii=False, indent=2)},
    ]
    try:
        text = deepseek_chat(messages, temperature=0.1)
        data = parse_json_relaxed(text)
        if not isinstance(data, dict):
            return {"compliant": True, "violations": []}
        data.setdefault("compliant", True)
        data.setdefault("violations", [])
        return data
    except Exception:
        return {"compliant": True, "violations": []}


TIME_JUMP_PATTERNS = re.compile(
    r"(三日后|数日后|翌日|第二天|几天后|与此同时|转眼|数周后|数月后|半年后|一年后|多年后|次日|隔天|过了?几天|过了?数日|数天之后|几周之后|几月之后)",
)

# 通用时间过场 / 环境定场开场，用于检测章节间生硬切割（即使 scene_continues=false 也应避免）。
GENERIC_OPENER_PATTERNS = re.compile(
    r"^[\s　]*(天黑了|天亮了|夜幕降临|夜色降临|夜深了|入夜|清晨的阳光|清晨时分|晨光|朝阳|第二天|翌日|次日|几天后|数日后|三日后|转眼|与此同时|时间一晃|时光荏苒|不知过了多久|许久之后|许久以后)",
)


def check_chapter_continuity(chapter_text: str, context: Dict[str, Any]) -> Dict[str, Any]:
    contract = context.get("continuity_contract", default_continuity_contract())
    ending_state = context.get("previous_ending_state", default_ending_state())
    narrative_req = str(contract.get("narrative_continuity_requirement", "")).strip()
    opening_words = min(300, len(chapter_text))
    opening_text = chapter_text[:opening_words]
    if not contract.get("must_start_from_previous_ending") and not contract.get("must_not_jump_time") and not contract.get("must_not_change_location_immediately"):
        # 场景未要求严格接续，但仍需叙事/因果连贯：检测生硬的通用过场开场。
        m = GENERIC_OPENER_PATTERNS.search(opening_text)
        if narrative_req and m:
            return {
                "continuous": False,
                "violations": [{
                    "type": "scene_break",
                    "description": "章节以通用时间过场/环境定场开场，未承接上一章的因果驱动力",
                    "evidence": m.group(0),
                    "severity": "hard",
                }],
                "revision_instruction": (
                    "删除开头的通用时间过场/环境定场句，改为直接承接上一章结尾的因果驱动力："
                    + narrative_req
                    + "。让本章从这个后果/决定/动机出发的具体行动或处境进入，不要用'天黑了/第二天'之类的句子切割。"
                ),
            }
        return {"continuous": True, "violations": [], "revision_instruction": ""}
    prompt = {
        "task": "检查新章节开头是否满足章节接续契约。",
        "opening_text": opening_text,
        "continuity_contract": contract,
        "previous_ending_state": ending_state,
        "output_schema": {
            "continuous": True,
            "violations": [
                {
                    "type": "time_jump | location_change | scene_break | character_switch | macro_summary",
                    "description": "具体违反描述",
                    "evidence": "开头中的原文片段",
                    "severity": "hard | soft",
                }
            ],
            "revision_instruction": "具体的修订指令，告诉作者如何修改开头以满足接续契约",
        },
        "requirements": [
            "如果 must_not_jump_time=true，开头不得出现时间跳转词（如：三日后、数日后、翌日、第二天、几天后、与此同时、转眼等）",
            "如果 must_not_change_location_immediately=true，开头场景必须与上一章结尾在同一地点",
            "如果 must_start_from_previous_ending=true，开头必须从上一章结尾的场景/动作/对话直接延续",
            "如果上一章 scene_continues=true，本章开头不得宏观总结、不得切换到无关人物",
            "如果以上契约均满足，continuous=true",
            "输出严格 JSON",
        ],
    }
    messages = [
        {"role": "system", "content": "你是连续性检查器，只输出严格 JSON。"},
        {"role": "user", "content": json.dumps(prompt, ensure_ascii=False, indent=2)},
    ]
    try:
        text = deepseek_chat(messages, temperature=0.1)
        data = parse_json_relaxed(text)
        if not isinstance(data, dict):
            return {"continuous": True, "violations": [], "revision_instruction": ""}
        data.setdefault("continuous", True)
        data.setdefault("violations", [])
        data.setdefault("revision_instruction", "")
        if contract.get("must_not_jump_time") and TIME_JUMP_PATTERNS.search(opening_text):
            hard_time_violation = False
            for v in data.get("violations", []):
                if isinstance(v, dict) and v.get("type") == "time_jump" and v.get("severity") == "hard":
                    hard_time_violation = True
                    break
            if not hard_time_violation:
                data["continuous"] = False
                data["violations"].append({
                    "type": "time_jump",
                    "description": "开头包含时间跳转词，违反 must_not_jump_time 契约",
                    "evidence": TIME_JUMP_PATTERNS.search(opening_text).group(0),
                    "severity": "hard",
                })
        return data
    except Exception:
        return {"continuous": True, "violations": [], "revision_instruction": ""}


def revise_chapter_with_feedback(
    chapter_text: str,
    context: Dict[str, Any],
    compliance_result: Dict[str, Any],
    continuity_result: Dict[str, Any],
) -> str:
    violations: List[str] = []
    for v in compliance_result.get("violations", []):
        if isinstance(v, dict) and v.get("severity") == "hard":
            violations.append(f"[指令违反] {v.get('rule', '')}: {v.get('evidence', '')}")
    for v in continuity_result.get("violations", []):
        if isinstance(v, dict) and v.get("severity") == "hard":
            violations.append(f"[接续违反] {v.get('description', '')}: {v.get('evidence', '')}")
    revision_instruction = continuity_result.get("revision_instruction", "")
    if not violations and not revision_instruction:
        return chapter_text
    feedback_parts = violations.copy()
    if revision_instruction:
        feedback_parts.append(f"[修订指令] {revision_instruction}")
    feedback = "\n".join(feedback_parts)
    prompt = {
        "task": "根据检查反馈修订小说章节。只修订有问题的部分，保持其余内容不变。",
        "original_chapter": chapter_text,
        "revision_feedback": feedback,
        "active_instruction_constraints": context.get("active_instruction_constraints", {}),
        "continuity_contract": context.get("continuity_contract", default_continuity_contract()),
        "previous_ending_state": context.get("previous_ending_state", default_ending_state()),
        "requirements": [
            "只输出修订后的完整章节正文，不要输出解释",
            "必须解决所有 hard violation",
            "如果反馈指出开头时间跳转，必须改为从上一章结尾场景直接接续",
            "如果反馈指出包含禁止模式，必须删除或替换该模式",
            "不得引入新的违反",
            "保持文风和视角一致",
        ],
    }
    messages = [
        {"role": "system", "content": "你是小说章节修订 agent。只输出修订后的完整正文，不要加任何解释。"},
        {"role": "user", "content": json.dumps(prompt, ensure_ascii=False, indent=2)},
    ]
    try:
        return deepseek_chat(messages, temperature=0.5)
    except Exception:
        return chapter_text


def run_chapter_quality_pipeline(chapter_text: str, context: Dict[str, Any]) -> tuple[str, List[Dict[str, Any]], bool]:
    quality_reports: List[Dict[str, Any]] = []
    needs_user_review = False
    current_text = chapter_text
    for attempt in range(3):
        compliance_result = check_instruction_compliance(current_text, context)
        continuity_result = check_chapter_continuity(current_text, context)
        has_hard_compliance = any(
            isinstance(v, dict) and v.get("severity") == "hard"
            for v in compliance_result.get("violations", [])
        )
        has_hard_continuity = any(
            isinstance(v, dict) and v.get("severity") == "hard"
            for v in continuity_result.get("violations", [])
        )
        if not has_hard_compliance and not has_hard_continuity and continuity_result.get("continuous", True):
            quality_reports.append({
                "attempt": attempt + 1,
                "compliance": compliance_result,
                "continuity": continuity_result,
                "result": "passed",
            })
            break
        quality_reports.append({
            "attempt": attempt + 1,
            "compliance": compliance_result,
            "continuity": continuity_result,
            "result": "revision_needed",
        })
        if attempt < 2:
            current_text = revise_chapter_with_feedback(current_text, context, compliance_result, continuity_result)
        else:
            needs_user_review = True
    return current_text, quality_reports, needs_user_review


def build_chapter_system_prompt(length_target: int) -> str:
    return (
        "你是一个小说章节写作 agent。\n"
        "【约束优先级】（从高到低，前者绝对高于后者，冲突时必须服从高优先级）：\n"
        "1. current_user_directives — 当前用户硬指令（extra_instruction、instruction_plot_points、tone、length_target）\n"
        "   → 用户指令中描述的具体情节、场景和角色行为必须在正文中完整体现，不得省略\n"
        "2. active_instruction_constraints — 长期约束注册表（global_constraints、chapter_constraints、banned_patterns、style_preferences）\n"
        "3. continuity_contract — 章节接续契约（必须从上一章结尾接起、不得跳时间/换地点等）\n"
        "4. confirmed_story_facts — 已确认的故事事实（之前章节已发生的事件）\n"
        "5. characters — 角色状态（性格、动机、当前位置和情绪）\n"
        "6. outline — 大纲和章节计划\n"
        "7. storyline_summary / dialogue_summary / recent_turns — 故事线摘要、对话摘要、最近对话\n"
        "8. writing_optimization_goals — 写作优化目标（推进 open_threads、自动补齐前置逻辑等）\n\n"
        "【关键规则】：\n"
        "- 用户硬指令和长期约束绝对优先于大纲。如果用户说'不要写战斗'，即使大纲有战斗目标也不得写战斗。\n"
        "- 用户指令中如果描述了具体的情节、场景或角色行为（如'主角发现xxx'、'在xxx发生yyy'），这些内容必须在本章中完整体现，不得省略、简化或用其他情节替代。\n"
        "- 如果 current_user_directives.instruction_plot_points 非空，每一个情节要点都必须在正文中得到展开和描写，不能仅一笔带过。\n"
        "- 如果 current_user_directives.extra_instruction 中包含多个情节要点，每一个要点都必须在正文中得到展开和描写。\n"
        "- banned_patterns 中的模式绝对禁止出现。例如'男主冷笑'被禁止，则全文不得出现。\n"
        "- continuity_contract 要求接续上一章时，开头必须从 previous_ending_state 的场景直接延续，不得跳时间、换地点、宏观总结或切换无关人物。\n"
        "- 自动补齐前置逻辑（writing_optimization_goals.auto_fill_missing_backstory）不得违反用户硬指令 and continuity_contract。\n"
        "- 推进 open_threads 是写作优化目标，不是硬约束，不得因此违反用户指令或接续契约。\n\n"
        "【叙事连贯性——必须遵守】：\n"
        "- 本章开头必须承接 continuity_contract.narrative_continuity_requirement 与 previous_ending_state.next_chapter_driver 所描述的因果驱动力：上一章留下的后果、决定或动机，是本章开场的直接出发点。\n"
        "- 即使场景已切换（scene_continues=false），也禁止用通用时间过场或环境定场作为开场，例如：'天黑了'、'夜幕降临'、'第二天'、'几天后'、'转眼之间'、'与此同时'、'清晨的阳光'等。开场应直接进入承接上一章后果的具体行动或处境。\n"
        "- 章节之间是同一条故事线的连续推进，不是各自独立的一天或独立小故事；时间推移应通过角色的行动与因果自然带出，而非靠过场句切割。\n"
        "- 确需较大时间跳跃时，必须由 outline/用户指令明确要求，且开场仍要立刻把读者带回上一章未了的因果链上。\n\n"
        "【关于悬念与节奏——必须遵守】：\n"
        "- 不要为了制造钩子而在章节结尾或中途强行插入悬疑：禁止凭空冒出的神秘人物、毫无铺垫的突发危机、与主线无关的反转、刻意吊读者胃口的省略。\n"
        "- 悬念/伏笔只有在大纲、已确认事实或当前情节自然需要时才使用，并且必须服务于主线推进，事后要能回收。\n"
        "- 章节结尾应是当前情节的一个自然落点（一个阶段性结果或新的明确动机），而不是一个人为的悬疑断点。\n\n"
        "【故事线收束——必须遵守】：\n"
        "- 优先推进并解决 thread_priority.immediate_threads 中的线索；每章应让至少一条已开线索得到实质进展或解决，而不是只顾开新线索。\n"
        "- 严格控制新开线索的数量：除非剧情必需或用户要求，避免在一章内抛出多条互不相关的新悬念。\n"
        "- 故事临近大纲规划的结局阶段时，应主动收束 open_threads，使主线逐步聚拢、走向明确结局，而非持续发散。\n\n"
        f"【字数目标】：约 {length_target} 字，请写完完整情节后自然收尾，不要中途截断。\n\n"
        "【输出要求】：\n"
        "- 只输出本章正文，不要输出解释、标题说明、JSON 或 analysis过程。\n"
        "- 默认保持第三人称有限视角，文风稳定。\n"
        "- 角色行为必须符合角色表中的性格、动机和当前状态。\n"
        "- 不能与已确认故事事实冲突。\n"
    )


def update_continuity_from_updates(updates: Dict[str, Any]) -> None:
    ending_state = updates.get("ending_state")
    if not ending_state or not isinstance(ending_state, dict):
        ending_state = default_ending_state()
    scene_continues = bool(ending_state.get("scene_continues", False))
    driver = str(ending_state.get("next_chapter_driver", "")).strip()
    # 叙事接续始终生效：即使场景切换，下一章开场也必须承接上一章留下的因果驱动力。
    narrative_req = driver or str(ending_state.get("immediate_unresolved_question", "")).strip()
    contract = {
        "must_start_from_previous_ending": scene_continues,
        "must_not_jump_time": scene_continues,
        "must_not_change_location_immediately": scene_continues,
        "opening_requirement": str(ending_state.get("recommended_next_opening", "")).strip(),
        "allowed_transition_after_words": 0 if scene_continues else 200,
        "narrative_continuity_requirement": narrative_req,
    }
    thread_priority = updates.get("thread_priority")
    if not isinstance(thread_priority, dict):
        thread_priority = {"immediate_threads": [], "chapter_threads": [], "long_arc_threads": []}
    continuity = {
        "ending_state": ending_state,
        "continuity_contract": contract,
        "thread_priority": thread_priority,
        "updated_at": utc_now(),
    }
    save_continuity(continuity)


def build_segment_continuity_from_ending_state(ending_state: Dict[str, Any]) -> Dict[str, Any]:
    if not ending_state or not isinstance(ending_state, dict):
        ending_state = default_ending_state()
    scene_continues = bool(ending_state.get("scene_continues", False))
    return {
        "must_start_from_previous_ending": True,
        "must_not_jump_time": scene_continues,
        "must_not_change_location_immediately": scene_continues,
        "opening_requirement": str(ending_state.get("recommended_next_opening", "")).strip(),
        "allowed_transition_after_words": 0 if scene_continues else 100,
        "previous_ending_state": ending_state,
    }


def generate_chapter_segment_plan(
    state: Dict[str, Any],
    chapter_no: int,
    target_words: int,
    instruction: str,
) -> Dict[str, Any]:
    outline = state["outline"]
    chapter_plan = select_chapter_plan(outline, chapter_no)
    continuity = state.get("continuity") or load_continuity()
    estimated_segments = max(1, (target_words + DEFAULT_SEGMENT_TARGET_WORDS - 1) // DEFAULT_SEGMENT_TARGET_WORDS)
    prompt = {
        "task": "为小说章节生成多片段写作计划。每个片段是章节内部的连续片段，不是独立小章节。",
        "chapter_no": chapter_no,
        "chapter_goal": chapter_plan.get("goal", ""),
        "must_include": chapter_plan.get("must_include", []),
        "cannot_include": chapter_plan.get("cannot_include", []),
        "target_words": target_words,
        "estimated_segments": estimated_segments,
        "previous_ending_state": continuity.get("ending_state", default_ending_state()),
        "continuity_contract": continuity.get("continuity_contract", default_continuity_contract()),
        "user_instruction": instruction,
        "outline_title": outline.get("title", ""),
        "storyline_summary": state.get("storyline", {}).get("overall_summary", ""),
        "output_schema": {
            "estimated_segments": estimated_segments,
            "chapter_goal": "本章整体目标",
            "beats": [
                {
                    "beat_no": 1,
                    "purpose": "本片段目的",
                    "target_words": DEFAULT_SEGMENT_TARGET_WORDS,
                    "must_start_from_previous_ending": True,
                    "must_end_with": "本片段应推进到的叙事状态（事件进展/情绪或关系变化/新的明确动机），不要刻意制造悬念或反转",
                }
            ],
            "chapter_ending_target": "本章结尾应达到的状态（当前情节的自然落点，而非人为悬疑断点）",
        },
        "requirements": [
            "每个 beat 是章节内部的连续片段，不是独立章节",
            "beats 之间必须自然衔接，不得每个 beat 重新开场",
            "最后一个 beat 必须为章节结尾做铺垫，并推动/解决至少一条已开线索",
            "不要在 beat 边界刻意制造悬念、神秘人物或突发反转，除非剧情/大纲自然需要",
            "must_end_with 描述本片段结束时应达到的叙事状态，不是字面结尾词",
            "target_words 总和应接近章节 target_words",
            "输出严格 JSON",
        ],
    }
    messages = [
        {"role": "system", "content": "你是小说写作计划器，只输出严格 JSON。"},
        {"role": "user", "content": json.dumps(prompt, ensure_ascii=False, indent=2)},
    ]
    try:
        text = deepseek_chat(messages, temperature=0.3)
        data = parse_json_relaxed(text)
        if not isinstance(data, dict) or "beats" not in data:
            raise ValueError("invalid segment plan")
        for i, beat in enumerate(data.get("beats", [])):
            if not isinstance(beat, dict):
                data["beats"][i] = default_beat(i + 1)
                continue
            beat.setdefault("beat_no", i + 1)
            beat.setdefault("purpose", "")
            beat.setdefault("target_words", DEFAULT_SEGMENT_TARGET_WORDS)
            beat.setdefault("must_start_from_previous_ending", True)
            beat.setdefault("must_end_with", "")
        data.setdefault("estimated_segments", len(data.get("beats", [])))
        data.setdefault("chapter_goal", chapter_plan.get("goal", ""))
        data.setdefault("chapter_ending_target", "")
        return data
    except Exception:
        beats = []
        words_remaining = target_words
        for i in range(estimated_segments):
            seg_words = min(DEFAULT_SEGMENT_TARGET_WORDS, words_remaining)
            if seg_words <= 0:
                break
            beat = default_beat(i + 1)
            beat["target_words"] = seg_words
            beat["must_start_from_previous_ending"] = (i > 0)
            if i == 0:
                beat["purpose"] = chapter_plan.get("goal", "推进故事")
            elif i == estimated_segments - 1:
                beat["purpose"] = "完成本章核心冲突并收尾"
            else:
                beat["purpose"] = "继续推进本章情节"
            beats.append(beat)
            words_remaining -= seg_words
        return {
            "estimated_segments": len(beats),
            "chapter_goal": chapter_plan.get("goal", ""),
            "beats": beats,
            "chapter_ending_target": "",
        }


def build_segment_system_prompt(
    segment_no: int,
    total_segments: int,
    beat: Dict[str, Any],
    segment_continuity: Dict[str, Any],
    length_target: int,
) -> str:
    is_first = segment_no == 1
    is_last = segment_no == total_segments
    parts = [
        "你是一个小说章节片段写作 agent。你正在写同一章节的第 {}/{} 个片段。".format(segment_no, total_segments),
        "",
        "【核心规则——必须严格遵守】：",
        "- 这是第 {} 个片段，不是完整章节。不得重新开场，不得总结前文，不得提前完成章节结局。".format(segment_no),
        "- 必须从上一片段的结尾状态无缝接续，不得跳时间、换地点、切换到无关人物。",
        "- 不得重复前文已写的内容。",
        "- 不得在片段结尾做总结或收束，除非这是最后一个片段。",
    ]
    if is_first:
        parts.extend([
            "",
            "【首片段特殊规则】：",
            "- 你是本章的第一个片段，需要建立本章场景和氛围。",
            "- 必须遵守全局 continuity_contract 的接续要求。",
        ])
    if is_last:
        parts.extend([
            "",
            "【末片段特殊规则】：",
            "- 你是本章的最后一个片段，需要为章节做自然收尾。",
            "- 章节结尾应达到 chapter_ending_target 描述的状态，是当前情节的自然落点（一个阶段性结果或新的明确动机），而不是人为的悬疑断点。",
            "- 本章核心冲突应有阶段性收束，并尽量推动或解决一条已开线索；不要为了制造钩子而强行插入悬念、神秘人物或突发反转。",
            "- 结尾应留下一个清晰的因果驱动力（next_chapter_driver），让下一章能自然承接，而不是靠时间过场切换。",
        ])
    parts.extend([
        "",
        "【当前片段任务】：",
        "- 片段目的：{}".format(beat.get("purpose", "推进本章情节")),
        "- 片段结束时应达到：{}".format(beat.get("must_end_with", "自然过渡到下一片段")),
    ])
    if segment_continuity.get("must_start_from_previous_ending"):
        parts.extend([
            "",
            "【接续契约】：",
            "- 必须从上一片段结尾场景直接延续",
        ])
        if segment_continuity.get("must_not_jump_time"):
            parts.append("- 不得跳转时间")
        if segment_continuity.get("must_not_change_location_immediately"):
            parts.append("- 不得立即切换地点")
        opening_req = segment_continuity.get("opening_requirement", "")
        if opening_req:
            parts.append("- 开头要求：{}".format(opening_req))
    parts.extend([
        "",
        "【约束优先级】（从高到低）：",
        "1. current_user_directives — 用户硬指令",
        "   → 用户指令中描述的具体情节必须完整体现，不得省略或简化",
        "   → 如果 instruction_plot_points 非空，每一个情节要点都必须在本片段或本章中得到展开",
        "2. active_instruction_constraints — 长期约束",
        "3. continuity_contract / segment_continuity — 接续契约",
        "4. confirmed_story_facts — 已确认故事事实",
        "5. characters — 角色状态",
        "6. outline — 大纲",
        "7. storyline_summary — 故事线摘要",
        "8. writing_optimization_goals — 写作优化",
        "",
        "【字数目标】：约 {} 字。写完当前 beat 后自然过渡，不要中途截断。".format(length_target),
        "",
        "【输出要求】：",
        "- 只输出本片段正文，不要输出解释、标题、JSON 或分析过程。",
        "- 保持第三人称有限视角，文风与前文一致。",
        "- 角色行为必须符合角色表中的性格、动机和当前状态。",
    ])
    return "\n".join(parts)


def generate_chapter_segment(
    context: Dict[str, Any],
    draft: Dict[str, Any],
    segment_no: int,
    beat: Dict[str, Any],
    max_segment_tokens: int = DEFAULT_MAX_SEGMENT_TOKENS,
) -> str:
    segment_continuity = draft.get("segment_continuity", default_continuity_contract())
    total_segments = max(segment_no, draft.get("segment_plan", {}).get("estimated_segments", 1))
    target_words = beat.get("target_words", DEFAULT_SEGMENT_TARGET_WORDS)
    system_prompt = build_segment_system_prompt(
        segment_no, total_segments, beat, segment_continuity, target_words,
    )
    segment_context = dict(context)
    segment_context["segment_info"] = {
        "segment_no": segment_no,
        "total_segments": total_segments,
        "beat": beat,
        "segment_continuity": segment_continuity,
    }
    if segment_no > 1:
        prev_segments = draft.get("segments", [])
        prev_texts = [s.get("text", "") for s in prev_segments if s.get("text")]
        if prev_texts:
            segment_context["previous_segments_text"] = "\n".join(prev_texts)
        prev_ending = default_ending_state()
        if prev_segments:
            last_seg = prev_segments[-1]
            prev_ending = last_seg.get("ending_state", default_ending_state())
        segment_context["previous_segment_ending_state"] = prev_ending
    user_prompt = json.dumps(segment_context, ensure_ascii=False, indent=2)
    return deepseek_chat(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.85,
        max_tokens=max_segment_tokens,
    )


def generate_chapter_segment_stream(
    context: Dict[str, Any],
    draft: Dict[str, Any],
    segment_no: int,
    beat: Dict[str, Any],
    max_segment_tokens: int = DEFAULT_MAX_SEGMENT_TOKENS,
) -> Any:
    segment_continuity = draft.get("segment_continuity", default_continuity_contract())
    total_segments = max(segment_no, draft.get("segment_plan", {}).get("estimated_segments", 1))
    target_words = beat.get("target_words", DEFAULT_SEGMENT_TARGET_WORDS)
    system_prompt = build_segment_system_prompt(
        segment_no, total_segments, beat, segment_continuity, target_words,
    )
    segment_context = dict(context)
    segment_context["segment_info"] = {
        "segment_no": segment_no,
        "total_segments": total_segments,
        "beat": beat,
        "segment_continuity": segment_continuity,
    }
    if segment_no > 1:
        prev_segments = draft.get("segments", [])
        prev_texts = [s.get("text", "") for s in prev_segments if s.get("text")]
        if prev_texts:
            segment_context["previous_segments_text"] = "\n".join(prev_texts)
        prev_ending = default_ending_state()
        if prev_segments:
            last_seg = prev_segments[-1]
            prev_ending = last_seg.get("ending_state", default_ending_state())
        segment_context["previous_segment_ending_state"] = prev_ending
    user_prompt = json.dumps(segment_context, ensure_ascii=False, indent=2)
    return deepseek_chat_stream(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.85,
        max_tokens=max_segment_tokens,
    )


def extract_segment_updates(
    state: Dict[str, Any],
    chapter_no: int,
    segment_text: str,
    segment_no: int,
    beat: Dict[str, Any],
) -> Dict[str, Any]:
    prompt = {
        "task": "从小说章节片段正文中抽取片段级别的结构化信息。这些信息只用于章节内部状态追踪，不会写入正式故事线。",
        "schema": {
            "segment_summary": "",
            "ending_state": {
                "location": "",
                "time": "",
                "characters_present": [],
                "last_action": "",
                "last_dialogue": "",
                "emotional_tone": "",
                "immediate_unresolved_question": "",
                "scene_continues": False,
                "recommended_next_opening": "",
            },
            "local_character_state_delta": [
                {
                    "id": "c001",
                    "changes": {},
                    "notes": "",
                }
            ],
            "local_open_threads": [],
            "warnings": [],
        },
        "chapter_no": chapter_no,
        "segment_no": segment_no,
        "beat_purpose": beat.get("purpose", ""),
        "segment_text": segment_text,
        "requirements": [
            "输出严格 JSON",
            "segment_summary 用 1-2 句话概括本片段内容",
            "ending_state 必须准确反映本片段结尾的场景状态",
            "scene_continues=true 表示本片段结尾场景尚未结束，下一片段必须直接接续",
            "local_character_state_delta 只记录本片段中有变化的角色状态",
            "local_open_threads 只记录本片段新产生的未解决线索",
            "warnings 记录任何潜在问题（如角色行为不一致、与前文矛盾等）",
        ],
    }
    messages = [
        {"role": "system", "content": "你是结构化抽取器，只输出严格 JSON。"},
        {"role": "user", "content": json.dumps(prompt, ensure_ascii=False, indent=2)},
    ]
    try:
        text = deepseek_chat(messages, temperature=0.2)
        data = parse_json_relaxed(text)
        if not isinstance(data, dict):
            raise ValueError("segment update extraction did not return JSON object")
        if "ending_state" not in data or not isinstance(data.get("ending_state"), dict):
            data["ending_state"] = default_ending_state()
        data.setdefault("segment_summary", "")
        data.setdefault("local_character_state_delta", [])
        data.setdefault("local_open_threads", [])
        data.setdefault("warnings", [])
        return data
    except Exception:
        return {
            "segment_summary": f"第{segment_no}片段完成。",
            "ending_state": default_ending_state(),
            "local_character_state_delta": [],
            "local_open_threads": [],
            "warnings": [],
        }


def check_segment_continuity(
    segment_text: str,
    segment_no: int,
    segment_continuity: Dict[str, Any],
    previous_segment_ending: Dict[str, Any],
) -> Dict[str, Any]:
    if segment_no == 1:
        return {"continuous": True, "violations": [], "revision_instruction": ""}
    if not segment_continuity.get("must_start_from_previous_ending") and not segment_continuity.get("must_not_jump_time"):
        return {"continuous": True, "violations": [], "revision_instruction": ""}
    opening_words = min(300, len(segment_text))
    opening_text = segment_text[:opening_words]
    prompt = {
        "task": "检查章节片段开头是否满足片段接续契约。这是同一章节内部的连续片段，不是新章节。",
        "segment_no": segment_no,
        "opening_text": opening_text,
        "segment_continuity": segment_continuity,
        "previous_segment_ending": previous_segment_ending,
        "output_schema": {
            "continuous": True,
            "violations": [
                {
                    "type": "new_scene_opening | time_jump | location_change | repetition | character_switch",
                    "description": "具体违反描述",
                    "evidence": "开头中的原文片段",
                    "severity": "hard | soft",
                }
            ],
            "revision_instruction": "",
        },
        "requirements": [
            "这是同一章节内部的片段，不是新章节",
            "如果片段像新章节一样重新开场（如'第X章'、新场景描写开头），这是 hard violation",
            "如果片段开头跳转时间（如'三日后'、'翌日'），这是 hard violation",
            "如果片段开头切换到无关地点或人物，这是 hard violation",
            "如果片段重复前文已写的内容，这是 hard violation",
            "如果以上均满足，continuous=true",
            "输出严格 JSON",
        ],
    }
    messages = [
        {"role": "system", "content": "你是片段连续性检查器，只输出严格 JSON。"},
        {"role": "user", "content": json.dumps(prompt, ensure_ascii=False, indent=2)},
    ]
    try:
        text = deepseek_chat(messages, temperature=0.1)
        data = parse_json_relaxed(text)
        if not isinstance(data, dict):
            return {"continuous": True, "violations": [], "revision_instruction": ""}
        data.setdefault("continuous", True)
        data.setdefault("violations", [])
        data.setdefault("revision_instruction", "")
        if segment_continuity.get("must_not_jump_time") and TIME_JUMP_PATTERNS.search(opening_text):
            has_time_violation = any(
                isinstance(v, dict) and v.get("type") == "time_jump" and v.get("severity") == "hard"
                for v in data.get("violations", [])
            )
            if not has_time_violation:
                data["continuous"] = False
                data["violations"].append({
                    "type": "time_jump",
                    "description": "片段开头包含时间跳转词",
                    "evidence": TIME_JUMP_PATTERNS.search(opening_text).group(0),
                    "severity": "hard",
                })
        return data
    except Exception:
        return {"continuous": True, "violations": [], "revision_instruction": ""}


def check_segment_beat_fulfillment(
    segment_text: str,
    beat: Dict[str, Any],
) -> Dict[str, Any]:
    if not beat.get("purpose") and not beat.get("must_end_with"):
        return {"fulfilled": True, "violations": [], "revision_instruction": ""}
    prompt = {
        "task": "检查章节片段是否完成了当前 beat 的叙事目标。",
        "segment_text": segment_text,
        "beat": beat,
        "output_schema": {
            "fulfilled": True,
            "violations": [
                {
                    "type": "purpose_not_addressed | wrong_ending | premature_chapter_close | no_progress",
                    "description": "具体违反描述",
                    "severity": "hard | soft",
                }
            ],
            "revision_instruction": "",
        },
        "requirements": [
            "检查片段是否推进了 beat.purpose 描述的叙事目标",
            "如果 beat.must_end_with 有要求，检查片段结尾是否接近该状态",
            "如果片段像完整章节一样收束了叙事（有总结、收尾），而不是继续推进，这是 hard violation（premature_chapter_close）",
            "如果片段完全没有推进 beat 目标，这是 soft violation",
            "如果以上均满足，fulfilled=true",
            "输出严格 JSON",
        ],
    }
    messages = [
        {"role": "system", "content": "你是片段叙事目标检查器，只输出严格 JSON。"},
        {"role": "user", "content": json.dumps(prompt, ensure_ascii=False, indent=2)},
    ]
    try:
        text = deepseek_chat(messages, temperature=0.1)
        data = parse_json_relaxed(text)
        if not isinstance(data, dict):
            return {"fulfilled": True, "violations": [], "revision_instruction": ""}
        data.setdefault("fulfilled", True)
        data.setdefault("violations", [])
        data.setdefault("revision_instruction", "")
        return data
    except Exception:
        return {"fulfilled": True, "violations": [], "revision_instruction": ""}


def run_segment_quality_pipeline(
    segment_text: str,
    context: Dict[str, Any],
    segment_no: int,
    beat: Dict[str, Any],
    segment_continuity: Dict[str, Any],
    previous_segment_ending: Dict[str, Any],
) -> tuple[str, List[Dict[str, Any]], bool]:
    quality_reports: List[Dict[str, Any]] = []
    needs_revision = False
    current_text = segment_text
    for attempt in range(2):
        compliance_result = check_instruction_compliance(current_text, context)
        continuity_result = check_segment_continuity(current_text, segment_no, segment_continuity, previous_segment_ending)
        beat_result = check_segment_beat_fulfillment(current_text, beat)
        has_hard = any(
            isinstance(v, dict) and v.get("severity") == "hard"
            for v in compliance_result.get("violations", []) + continuity_result.get("violations", []) + beat_result.get("violations", [])
        )
        if not has_hard and continuity_result.get("continuous", True) and beat_result.get("fulfilled", True):
            quality_reports.append({
                "attempt": attempt + 1,
                "compliance": compliance_result,
                "continuity": continuity_result,
                "beat_fulfillment": beat_result,
                "result": "passed",
            })
            break
        quality_reports.append({
            "attempt": attempt + 1,
            "compliance": compliance_result,
            "continuity": continuity_result,
            "beat_fulfillment": beat_result,
            "result": "revision_needed",
        })
        if attempt < 1:
            current_text = revise_chapter_with_feedback(current_text, context, compliance_result, continuity_result)
        else:
            needs_revision = True
    return current_text, quality_reports, needs_revision


def assemble_full_chapter(draft: Dict[str, Any]) -> str:
    segments = draft.get("segments", [])
    texts = [s.get("text", "") for s in segments if s.get("text")]
    return "\n".join(texts)


def chapter_draft_complete(draft: Dict[str, Any], user_force_end: bool = False) -> bool:
    if user_force_end:
        return True
    plan = draft.get("segment_plan", {})
    beats = plan.get("beats", [])
    segments = draft.get("segments", [])
    if not beats:
        return bool(segments)
    if len(segments) < len(beats):
        return False
    for seg in segments:
        if seg.get("status") != "done":
            return False
    return True


def start_chapter_generation(
    state: Dict[str, Any],
    chapter_no: int,
    instruction: str,
    tone: str,
    target_words: int,
    max_segment_tokens: int = DEFAULT_MAX_SEGMENT_TOKENS,
) -> Dict[str, Any]:
    generation_mode = "multi_segment" if target_words > SINGLE_SEGMENT_THRESHOLD else "single_segment"
    draft = default_chapter_draft(chapter_no)
    draft["generation_mode"] = generation_mode
    draft["target_words"] = target_words
    draft["instruction"] = instruction
    draft["tone"] = tone
    draft["length_target"] = target_words
    draft["generated_at"] = utc_now()
    context = build_generation_context(state, chapter_no, instruction, tone, target_words)
    chapter_task_content = json.dumps(context.get("chapter_task", {}), ensure_ascii=False, indent=2)
    draft["chapter_task_content"] = chapter_task_content
    if generation_mode == "single_segment":
        raw_text = generate_chapter_text(context, state.get("conversation_memory", default_conversation_memory()))
        revised_text, quality_reports, needs_user_review = run_chapter_quality_pipeline(raw_text, context)
        draft["chapter_text"] = revised_text
        draft["quality_reports"] = quality_reports
        draft["needs_user_review"] = needs_user_review
        draft["current_words"] = chapter_word_count(revised_text)
        if revised_text != raw_text:
            draft["raw_text_before_revision"] = raw_text
        segment = default_segment()
        segment["segment_no"] = 1
        segment["status"] = "done"
        segment["text"] = revised_text
        segment["quality_reports"] = quality_reports
        draft["segments"] = [segment]
    else:
        plan = generate_chapter_segment_plan(state, chapter_no, target_words, instruction)
        draft["segment_plan"] = plan
        continuity = state.get("continuity") or load_continuity()
        draft["segment_continuity"] = continuity.get("continuity_contract", default_continuity_contract())
        first_beat = plan.get("beats", [default_beat(1)])[0]
        raw_text = generate_chapter_segment(context, draft, 1, first_beat, max_segment_tokens)
        prev_ending = continuity.get("ending_state", default_ending_state())
        revised_text, quality_reports, needs_revision = run_segment_quality_pipeline(
            raw_text, context, 1, first_beat,
            draft["segment_continuity"], prev_ending,
        )
        segment_updates = extract_segment_updates(state, chapter_no, revised_text, 1, first_beat)
        segment = default_segment()
        segment["segment_no"] = 1
        segment["status"] = "done"
        segment["text"] = revised_text
        segment["summary"] = segment_updates.get("segment_summary", "")
        segment["ending_state"] = segment_updates.get("ending_state", default_ending_state())
        segment["quality_reports"] = quality_reports
        draft["segments"] = [segment]
        draft["segment_continuity"] = build_segment_continuity_from_ending_state(
            segment_updates.get("ending_state", default_ending_state()),
        )
        draft["chapter_text"] = assemble_full_chapter(draft)
        draft["current_words"] = chapter_word_count(draft["chapter_text"])
        draft["quality_reports"] = quality_reports
        if revised_text != raw_text:
            segment["raw_text_before_revision"] = raw_text
    return draft


def continue_chapter_generation(
    state: Dict[str, Any],
    draft: Dict[str, Any],
    max_segment_tokens: int = DEFAULT_MAX_SEGMENT_TOKENS,
) -> Dict[str, Any]:
    if draft.get("generation_mode") != "multi_segment":
        return draft
    plan = draft.get("segment_plan", {})
    beats = plan.get("beats", [])
    segments = draft.get("segments", [])
    next_segment_no = len(segments) + 1
    next_beat = None
    for beat in beats:
        if beat.get("beat_no", 0) == next_segment_no:
            next_beat = beat
            break
    if next_beat is None:
        if beats and next_segment_no <= len(beats):
            next_beat = beats[next_segment_no - 1]
        else:
            next_beat = default_beat(next_segment_no)
            next_beat["purpose"] = "继续推进本章情节"
    chapter_no = draft.get("chapter_no", 1)
    instruction = draft.get("instruction", "")
    tone = draft.get("tone", "")
    target_words = draft.get("target_words", 1800)
    context = build_generation_context(state, chapter_no, instruction, tone, target_words)
    segment_continuity = draft.get("segment_continuity", default_continuity_contract())
    prev_ending = default_ending_state()
    if segments:
        prev_ending = segments[-1].get("ending_state", default_ending_state())
    raw_text = generate_chapter_segment(context, draft, next_segment_no, next_beat, max_segment_tokens)
    revised_text, quality_reports, needs_revision = run_segment_quality_pipeline(
        raw_text, context, next_segment_no, next_beat,
        segment_continuity, prev_ending,
    )
    segment_updates = extract_segment_updates(state, chapter_no, revised_text, next_segment_no, next_beat)
    segment = default_segment()
    segment["segment_no"] = next_segment_no
    segment["status"] = "done"
    segment["text"] = revised_text
    segment["summary"] = segment_updates.get("segment_summary", "")
    segment["ending_state"] = segment_updates.get("ending_state", default_ending_state())
    segment["quality_reports"] = quality_reports
    if revised_text != raw_text:
        segment["raw_text_before_revision"] = raw_text
    draft["segments"].append(segment)
    draft["segment_continuity"] = build_segment_continuity_from_ending_state(
        segment_updates.get("ending_state", default_ending_state()),
    )
    draft["chapter_text"] = assemble_full_chapter(draft)
    draft["current_words"] = chapter_word_count(draft["chapter_text"])
    if needs_revision:
        draft["needs_user_review"] = True
    return draft


def outline_is_confirmed(outline: Dict[str, Any]) -> bool:
    if not isinstance(outline, dict):
        return False
    return str(outline.get("status", "")).lower() == "confirmed"


def generate_outline_from_brief(story_brief: str, conversation_context: Dict[str, Any]) -> Dict[str, Any]:
    system_prompt = """
你是小说大纲设计师。
根据用户提供的故事描述，生成可用于后续章节写作的结构化大纲 JSON。
要求：
1. 只输出严格 JSON，不要输出解释。
2. 大纲应包含：status, title, genre, theme, logline, world_setting, writing_rules, main_conflict, character_seed, chapter_plan。
3. character_seed 必须列出所有主要角色（至少 2 个），每个角色包含 name, role, personality(数组), motivation, appearance。
4. chapter_plan 至少包含 6 章，且每章必须包含 chapter_no, goal(本章具体剧情目标,不能为空), must_include, cannot_include。
5. status 必须是 "draft"。
6. 不要直接写章节正文。
""".strip()
    user_prompt = {
        "story_brief": story_brief,
        "conversation_context": conversation_context,
        "output_schema_hint": {
            "status": "draft",
            "title": "",
            "genre": "",
            "theme": "",
            "logline": "",
            "world_setting": {
                "time": "",
                "location": "",
            },
            "writing_rules": {
                "pov": "第三人称有限视角",
                "tone": "",
                "chapter_length_target": 1800,
            },
            "main_conflict": "",
            "character_seed": [
                {
                    "name": "角色名",
                    "role": "主角/配角",
                    "personality": ["性格特点1", "性格特点2"],
                    "motivation": "角色动机",
                    "appearance": "外貌描述",
                }
            ],
            "chapter_plan": [
                {
                    "chapter_no": 1,
                    "goal": "本章具体剧情目标（必填）",
                    "must_include": ["必须出现的元素"],
                    "cannot_include": [],
                }
            ],
        },
        "requirements": [
            "先理解故事描述，再生成适合长篇写作的章节规划",
            "character_seed 必须包含故事中提到的所有主要角色，不能为空",
            "每章的 goal 必须写具体的剧情目标，不能留空",
            "保持章节目标清晰递进",
            "预留冲突升级和伏笔回收空间",
        ],
    }
    text = deepseek_chat(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_prompt, ensure_ascii=False, indent=2)},
        ],
        temperature=0.35,
    )
    data = parse_json_relaxed(text)
    if not isinstance(data, dict):
        raise ValueError("outline generation did not return JSON object")
    return data


def normalize_outline_draft(draft: Dict[str, Any], story_brief: str) -> Dict[str, Any]:
    outline = normalize_outline_shape(draft, story_brief)
    outline["status"] = "draft"
    outline["source_brief"] = story_brief
    outline["confirmed_at"] = ""
    return outline


def _merge_instruction(user_instruction: str, planned_instruction: str) -> str:
    """Merge user's original instruction with planner's instruction.

    User instruction always takes precedence and is preserved in full.
    Planner's instruction is appended as supplementary guidance.
    """
    if not user_instruction:
        return planned_instruction
    if not planned_instruction:
        return user_instruction
    # If planner's instruction is a subset of user's, just use user's
    if planned_instruction in user_instruction:
        return user_instruction
    return f"【用户要求（必须遵循）】{user_instruction}\n【补充指引】{planned_instruction}"


def plan_auto_chapters(
    state: Dict[str, Any],
    start_chapter_no: int,
    count: int,
    instruction: str,
) -> List[Dict[str, Any]]:
    outline = state.get("outline", {})
    characters = state.get("characters", {})
    storyline = state.get("storyline", {})
    continuity = state.get("continuity") or load_continuity()
    chapter_plan = safe_list(outline.get("chapter_plan", []))
    existing_nos = []
    for cp in chapter_plan:
        try:
            existing_nos.append(int(cp.get("chapter_no", 0)))
        except (ValueError, TypeError):
            pass
    max_existing = max(existing_nos) if existing_nos else 0
    writing_rules = safe_dict(outline.get("writing_rules"), default_outline()["writing_rules"])
    default_length = writing_rules.get("chapter_length_target", 1800)
    system_prompt = """你是小说创作规划师。根据已有大纲、故事进展和用户指令，为接下来的章节制定详细的写作计划。

要求：
1. 只输出严格 JSON，不要输出解释。
2. 输出一个数组，每个元素包含：chapter_no, title_hint, goal, target_words, tone, instruction。
3. goal 必须具体描述本章的情节发展，不能为空。如果用户指令（user_extra_instruction）中包含具体的情节描述、场景要求或角色行为，goal 必须完整覆盖这些内容，不得省略或概括。
4. target_words 根据情节复杂度合理分配，通常在 1500-4000 之间。
5. tone 建议本章的语气风格。
6. instruction 必须原样保留用户指令（user_extra_instruction）中的所有具体要求、情节描述和约束，并在此基础上补充本章的具体创作指引。绝对不能省略、概括或减少用户指令中的任何内容。
7. 章节之间要有连贯性和递进感。
8. 【最高优先级】用户指令中的情节描述和要求，优先级高于大纲的 chapter_plan。如果用户指令与大纲冲突，必须以用户指令为准。""".strip()

    recent_summaries = []
    for cs in safe_list(storyline.get("chapter_summaries", []))[-5:]:
        if isinstance(cs, dict):
            events = safe_list(cs.get("events", []))
            if events:
                recent_summaries.append(f"第{cs.get('chapter_no','?')}章: {'; '.join(str(e) for e in events[:3])}")

    user_prompt = json.dumps({
        "story_title": outline.get("title", "未命名"),
        "genre": outline.get("genre", ""),
        "theme": outline.get("theme", ""),
        "logline": outline.get("logline", ""),
        "main_conflict": outline.get("main_conflict", ""),
        "start_chapter_no": start_chapter_no,
        "count": count,
        "default_chapter_length": default_length,
        "outline_chapter_plan": [cp for cp in chapter_plan if cp.get("chapter_no", 0) >= start_chapter_no],
        "recent_story_progress": recent_summaries,
        "ending_state": continuity.get("ending_state", default_ending_state()),
        "main_characters": [c for c in safe_list(characters.get("characters", []))[:8]],
        "user_extra_instruction": instruction,
        "critical_rule": (
            "user_extra_instruction 中的所有情节描述、场景要求和角色行为必须完整体现在 goal 和 instruction 中。"
            "不得以任何理由省略或简化用户指令中的具体内容。"
            "如果用户指令描述了多个事件，每个事件都必须在某一章的 goal 中体现。"
        ),
    }, ensure_ascii=False, indent=2)

    raw = deepseek_chat(
        [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
        temperature=0.7,
    )
    parsed = parse_json_relaxed(raw)
    if isinstance(parsed, dict):
        for key in ("chapters", "plan", "items", "data"):
            if key in parsed and isinstance(parsed[key], list):
                parsed = parsed[key]
                break
    if not isinstance(parsed, list):
        parsed = []
    result = []
    for i, item in enumerate(parsed):
        if not isinstance(item, dict):
            continue
        no = item.get("chapter_no", start_chapter_no + i)
        try:
            no = int(no)
        except (ValueError, TypeError):
            no = start_chapter_no + i
        outline_plan = select_chapter_plan(outline, no)
        result.append({
            "chapter_no": no,
            "title_hint": str(item.get("title_hint", "")).strip(),
            "goal": str(item.get("goal", outline_plan.get("goal", ""))).strip(),
            "target_words": safe_parse_int(item.get("target_words"), default_length),
            "tone": str(item.get("tone", writing_rules.get("tone", ""))).strip(),
            "instruction": _merge_instruction(instruction, str(item.get("instruction", "")).strip()),
            "outline_plan": outline_plan,
        })
    while len(result) < count:
        idx = len(result)
        no = start_chapter_no + idx
        outline_plan = select_chapter_plan(outline, no)
        result.append({
            "chapter_no": no,
            "title_hint": "",
            "goal": outline_plan.get("goal", "继续推进故事"),
            "target_words": default_length,
            "tone": writing_rules.get("tone", ""),
            "instruction": instruction,
            "outline_plan": outline_plan,
        })
    return result[:count]


def safe_parse_int(value: Any, fallback: int) -> int:
    if value is None:
        return fallback
    try:
        return int(value)
    except (ValueError, TypeError):
        # Extract digits from the string representation
        digits = "".join(c for c in str(value) if c.isdigit())
        return int(digits) if digits else fallback
