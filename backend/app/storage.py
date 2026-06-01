from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

# Import config constants
from backend.app.config import (
    CHARACTERS_PATH,
    CONVERSATION_PATH,
    CONTINUITY_PATH,
    DATA_DIR,
    CHAPTER_DIR,
    STATIC_DIR,
    OUTLINE_PATH,
    OUTLINE_DRAFT_PATH,
    STORY_BRIEF_PATH,
    STORYLINE_PATH,
    INSTRUCTION_REGISTRY_PATH,
    DEFAULT_SEGMENT_TARGET_WORDS,
    FILE_LOCK,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CHAPTER_DIR.mkdir(parents=True, exist_ok=True)
    STATIC_DIR.mkdir(parents=True, exist_ok=True)


def default_outline() -> Dict[str, Any]:
    return {
        "status": "draft",
        "title": "",
        "genre": "",
        "theme": "",
        "logline": "",
        "source_brief": "",
        "confirmed_at": "",
        "world_setting": {
            "time": "",
            "location": "",
        },
        "writing_rules": {
            "pov": "第三人称有限视角",
            "tone": "",
            "chapter_length_target": 1800,
        },
        "chapter_plan": [],
    }


def default_outline_draft() -> Dict[str, Any]:
    return default_outline()


def default_story_brief() -> Dict[str, Any]:
    return {
        "description": "",
        "created_at": "",
        "updated_at": "",
    }


def default_characters() -> Dict[str, Any]:
    return {
        "characters": []
    }


def default_storyline() -> Dict[str, Any]:
    return {
        "overall_summary": "故事尚未展开，等待根据已确认大纲进入正文写作。",
        "chapter_summaries": [],
        "open_threads": [],
        "resolved_threads": [],
    }


def default_character_state() -> Dict[str, Any]:
    return {
        "location": "",
        "mood": "",
        "injury": "",
        "known_information": [],
        "unknown_information": [],
    }


def default_conversation_memory() -> Dict[str, Any]:
    return {
        "dialogue_summary": {
            "user_preferences": [],
            "confirmed_constraints": [],
            "active_requests": [],
            "resolved_decisions": [],
            "freeform_summary": "",
        },
        "recent_turns": [],
        "next_turn_id": 1,
    }


def default_instruction_registry() -> Dict[str, Any]:
    return {
        "global_constraints": [],
        "chapter_constraints": [],
        "style_preferences": [],
        "banned_patterns": [],
    }


def default_ending_state() -> Dict[str, Any]:
    return {
        "location": "",
        "time": "",
        "characters_present": [],
        "last_action": "",
        "last_dialogue": "",
        "emotional_tone": "",
        "immediate_unresolved_question": "",
        "scene_continues": False,
        "recommended_next_opening": "",
        "scene_continues": False,
    }


def default_continuity_contract() -> Dict[str, Any]:
    return {
        "must_start_from_previous_ending": False,
        "must_not_jump_time": False,
        "must_not_change_location_immediately": False,
        "opening_requirement": "",
        "allowed_transition_after_words": 0,
    }


def default_continuity() -> Dict[str, Any]:
    return {
        "ending_state": default_ending_state(),
        "continuity_contract": default_continuity_contract(),
        "updated_at": "",
    }


def default_segment() -> Dict[str, Any]:
    return {
        "segment_no": 1,
        "status": "pending",
        "text": "",
        "summary": "",
        "ending_state": default_ending_state(),
        "quality_reports": [],
    }


def default_beat(beat_no: int = 1) -> Dict[str, Any]:
    return {
        "beat_no": beat_no,
        "purpose": "",
        "target_words": DEFAULT_SEGMENT_TARGET_WORDS,
        "must_start_from_previous_ending": True,
        "must_end_with": "",
    }


def default_chapter_segment_plan() -> Dict[str, Any]:
    return {
        "estimated_segments": 1,
        "chapter_goal": "",
        "beats": [default_beat(1)],
        "chapter_ending_target": "",
    }


def default_chapter_draft(chapter_no: int = 1) -> Dict[str, Any]:
    return {
        "chapter_no": chapter_no,
        "generation_mode": "single_segment",
        "status": "draft",
        "title": f"第{chapter_no}章",
        "target_words": 1800,
        "current_words": 0,
        "segments": [],
        "chapter_outline": "",
        "segment_plan": default_chapter_segment_plan(),
        "segment_continuity": default_continuity_contract(),
        "quality_reports": [],
        "chapter_text": "",
        "instruction": "",
        "tone": "",
        "length_target": 1800,
        "generated_at": "",
        "chapter_task_content": "",
        "needs_user_review": False,
    }


def safe_dict(value: Any, fallback: Dict[str, Any]) -> Dict[str, Any]:
    return value if isinstance(value, dict) else dict(fallback)


def safe_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def normalize_outline_shape(data: Any, story_brief: str = "") -> Dict[str, Any]:
    outline = dict(default_outline())
    if isinstance(data, dict):
        outline.update(data)
    outline["status"] = str(outline.get("status", "draft") or "draft")
    outline["source_brief"] = story_brief or str(outline.get("source_brief", "") or "")
    outline["confirmed_at"] = str(outline.get("confirmed_at", "") or "")
    outline["world_setting"] = safe_dict(outline.get("world_setting"), default_outline()["world_setting"])
    outline["writing_rules"] = safe_dict(outline.get("writing_rules"), default_outline()["writing_rules"])
    # Accept common aliases for character_seed
    raw_chars = safe_list(outline.get("character_seed"))
    if not raw_chars:
        for alias in ("characters", "character_list", "cast", "roles"):
            raw_chars = safe_list(outline.get(alias))
            if raw_chars:
                break
    outline["character_seed"] = raw_chars

    chapter_plan = safe_list(outline.get("chapter_plan"))
    normalized_plan: List[Dict[str, Any]] = []
    for item in chapter_plan:
        if not isinstance(item, dict):
            continue
        # Accept aliases for goal
        goal = str(item.get("goal") or item.get("chapter_goal") or item.get("summary") or item.get("description") or "").strip()
        normalized_plan.append(
            {
                "chapter_no": int(item.get("chapter_no") or item.get("no") or len(normalized_plan) + 1),
                "goal": goal,
                "must_include": safe_list(item.get("must_include")),
                "cannot_include": safe_list(item.get("cannot_include")),
            }
        )
    outline["chapter_plan"] = normalized_plan or default_outline()["chapter_plan"]
    if not outline.get("title"):
        outline["title"] = default_outline()["title"]
    return outline


def default_json_for_path(path: Path) -> Any:
    if path == OUTLINE_PATH:
        return default_outline()
    if path == OUTLINE_DRAFT_PATH:
        return default_outline_draft()
    if path == STORY_BRIEF_PATH:
        return default_story_brief()
    if path == CHARACTERS_PATH:
        return default_characters()
    if path == STORYLINE_PATH:
        return default_storyline()
    if path == CONVERSATION_PATH:
        return default_conversation_memory()
    if path == INSTRUCTION_REGISTRY_PATH:
        return default_instruction_registry()
    if path == CONTINUITY_PATH:
        return default_continuity()
    return {}


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def save_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    tmp.replace(path)


def ensure_default_files(force: bool = False) -> None:
    for path in [
        OUTLINE_PATH,
        OUTLINE_DRAFT_PATH,
        STORY_BRIEF_PATH,
        CHARACTERS_PATH,
        STORYLINE_PATH,
        CONVERSATION_PATH,
        INSTRUCTION_REGISTRY_PATH,
        CONTINUITY_PATH,
    ]:
        if force or not path.exists():
            save_json(path, default_json_for_path(path))
    if force:
        for chapter_file in CHAPTER_DIR.glob("chapter_*.json"):
            try:
                chapter_file.unlink()
            except FileNotFoundError:
                continue


def read_state() -> Dict[str, Any]:
    return {
        "outline": normalize_outline_shape(load_json(OUTLINE_PATH, default_outline())),
        "outline_draft": normalize_outline_shape(load_json(OUTLINE_DRAFT_PATH, default_outline_draft())),
        "story_brief": load_json(STORY_BRIEF_PATH, default_story_brief()),
        "characters": load_json(CHARACTERS_PATH, default_characters()),
        "storyline": load_json(STORYLINE_PATH, default_storyline()),
        "conversation_memory": load_json(CONVERSATION_PATH, default_conversation_memory()),
        "instruction_registry": load_json(INSTRUCTION_REGISTRY_PATH, default_instruction_registry()),
        "continuity": load_json(CONTINUITY_PATH, default_continuity()),
    }


def now_chapter_path(chapter_no: int) -> Path:
    return CHAPTER_DIR / f"chapter_{chapter_no:03d}.json"


def load_instruction_registry() -> Dict[str, Any]:
    return load_json(INSTRUCTION_REGISTRY_PATH, default_instruction_registry())


def save_instruction_registry(data: Dict[str, Any]) -> None:
    save_json(INSTRUCTION_REGISTRY_PATH, data)


def load_continuity() -> Dict[str, Any]:
    return load_json(CONTINUITY_PATH, default_continuity())


def save_continuity(data: Dict[str, Any]) -> None:
    data["updated_at"] = utc_now()
    save_json(CONTINUITY_PATH, data)
