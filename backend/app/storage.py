from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List
from uuid import uuid4

# Import config constants
from backend.app.config import (
    CHARACTERS_PATH,
    CONVERSATION_PATH,
    CONTINUITY_PATH,
    DATA_DIR,
    CHAPTER_DIR,
    SNAPSHOT_DIR,
    STATIC_DIR,
    OUTLINE_PATH,
    OUTLINE_DRAFT_PATH,
    STORY_BRIEF_PATH,
    STORYLINE_PATH,
    INSTRUCTION_REGISTRY_PATH,
    DEFAULT_SEGMENT_TARGET_WORDS,
    FILE_LOCK,
    LLM_SETTINGS_PATH,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CHAPTER_DIR.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
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
            "story_tags": [],
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
        "story_tags": [],
        "global_constraints": [],
        "chapter_constraints": [],
        "style_preferences": [],
        "banned_patterns": [],
    }


def default_llm_settings() -> Dict[str, Any]:
    return {
        "base_url": "",
        "model": "",
        "api_key": "",
        "max_tokens": 8192,
    }


def normalize_llm_settings(data: Any) -> Dict[str, Any]:
    settings = dict(default_llm_settings())
    if isinstance(data, dict):
        settings.update(data)
    settings["base_url"] = str(settings.get("base_url", "") or "").strip().rstrip("/")
    settings["model"] = str(settings.get("model", "") or "").strip()
    settings["api_key"] = str(settings.get("api_key", "") or "").strip()
    try:
        max_tokens = int(settings.get("max_tokens") or 8192)
    except (TypeError, ValueError):
        max_tokens = 8192
    settings["max_tokens"] = max(512, min(max_tokens, 200000))
    return settings


def normalize_story_tags(value: Any) -> List[str]:
    if isinstance(value, str):
        raw_items = value.replace("，", ",").replace("、", ",").split(",")
    elif isinstance(value, list):
        raw_items = value
    else:
        raw_items = []
    tags: List[str] = []
    seen = set()
    for item in raw_items:
        tag = str(item or "").strip()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        tags.append(tag)
    return tags


def extract_story_tags_from_outline(outline: Dict[str, Any]) -> List[str]:
    writing_rules = safe_dict(outline.get("writing_rules"), default_outline()["writing_rules"])
    tags: List[str] = []
    for key in ("story_tags", "tags", "keywords"):
        tags.extend(normalize_story_tags(writing_rules.get(key)))
        tags.extend(normalize_story_tags(outline.get(key)))
    return normalize_story_tags(tags)


def normalize_instruction_registry(data: Any) -> Dict[str, Any]:
    registry = dict(default_instruction_registry())
    if isinstance(data, dict):
        registry.update(data)
    for key in ("story_tags", "global_constraints", "chapter_constraints", "style_preferences", "banned_patterns"):
        registry[key] = normalize_story_tags(registry.get(key))
    return registry


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
        # 因果驱动：上一章结尾留下的、应当推动下一章开场的具体后果/决定/行动，
        # 即使场景已切换，下一章也应从这个驱动力接起，而不是用通用时间过场开场。
        "next_chapter_driver": "",
    }


def default_continuity_contract() -> Dict[str, Any]:
    return {
        "must_start_from_previous_ending": False,
        "must_not_jump_time": False,
        "must_not_change_location_immediately": False,
        "opening_requirement": "",
        "allowed_transition_after_words": 0,
        # 叙事/因果接续要求：始终生效（即使 scene_continues=false）。
        # 描述下一章开头应承接的具体后果或动机，禁止用通用时间过场开场。
        "narrative_continuity_requirement": "",
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
    outline["writing_rules"]["story_tags"] = extract_story_tags_from_outline(outline)
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
        try:
            chapter_no = int(item.get("chapter_no") or item.get("no") or len(normalized_plan) + 1)
        except (TypeError, ValueError):
            chapter_no = len(normalized_plan) + 1
        # Accept aliases for goal
        goal = str(item.get("goal") or item.get("chapter_goal") or item.get("summary") or item.get("description") or "").strip()
        normalized_plan.append(
            {
                "chapter_no": chapter_no,
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
    tmp = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
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
        "instruction_registry": normalize_instruction_registry(load_json(INSTRUCTION_REGISTRY_PATH, default_instruction_registry())),
        "continuity": load_json(CONTINUITY_PATH, default_continuity()),
    }


def now_chapter_path(chapter_no: int) -> Path:
    return CHAPTER_DIR / f"chapter_{chapter_no:03d}.json"


def load_instruction_registry() -> Dict[str, Any]:
    return normalize_instruction_registry(load_json(INSTRUCTION_REGISTRY_PATH, default_instruction_registry()))


def save_instruction_registry(data: Dict[str, Any]) -> None:
    save_json(INSTRUCTION_REGISTRY_PATH, normalize_instruction_registry(data))


def load_llm_settings() -> Dict[str, Any]:
    return normalize_llm_settings(load_json(LLM_SETTINGS_PATH, default_llm_settings()))


def save_llm_settings(data: Dict[str, Any]) -> None:
    save_json(LLM_SETTINGS_PATH, normalize_llm_settings(data))


def load_continuity() -> Dict[str, Any]:
    return load_json(CONTINUITY_PATH, default_continuity())


def save_continuity(data: Dict[str, Any]) -> None:
    data["updated_at"] = utc_now()
    with FILE_LOCK:
        save_json(CONTINUITY_PATH, data)


# ---------------------------------------------------------------------------
# Snapshot helpers
# ---------------------------------------------------------------------------

_SNAPSHOT_DATA_FILES = [
    OUTLINE_PATH,
    OUTLINE_DRAFT_PATH,
    STORY_BRIEF_PATH,
    CHARACTERS_PATH,
    STORYLINE_PATH,
    CONVERSATION_PATH,
    CONTINUITY_PATH,
    INSTRUCTION_REGISTRY_PATH,
]


def create_snapshot(label: str) -> Path:
    """Copy all current data files into snapshots/<timestamp>_<label>/."""
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_label = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)
    snap_dir = SNAPSHOT_DIR / f"{ts}_{safe_label}"
    snap_dir.mkdir(parents=True, exist_ok=True)

    for src in _SNAPSHOT_DATA_FILES:
        if src.exists():
            shutil.copy2(src, snap_dir / src.name)

    snap_chapter_dir = snap_dir / "chapters"
    snap_chapter_dir.mkdir(parents=True, exist_ok=True)
    for src in sorted(CHAPTER_DIR.glob("chapter_*.json")):
        shutil.copy2(src, snap_chapter_dir / src.name)

    cleanup_old_snapshots(keep=10)
    return snap_dir


def restore_snapshot(snapshot_path: Path) -> None:
    """Restore all files from snapshot_path back into the data directory."""
    if not snapshot_path.exists():
        raise FileNotFoundError(f"Snapshot not found: {snapshot_path}")

    for src in snapshot_path.iterdir():
        if src.is_file() and src.suffix == ".json":
            shutil.copy2(src, DATA_DIR / src.name)

    snap_chapter_dir = snapshot_path / "chapters"
    if snap_chapter_dir.exists():
        for existing in CHAPTER_DIR.glob("chapter_*.json"):
            try:
                existing.unlink()
            except FileNotFoundError:
                pass
        for src in sorted(snap_chapter_dir.glob("chapter_*.json")):
            shutil.copy2(src, CHAPTER_DIR / src.name)

    shutil.rmtree(snapshot_path, ignore_errors=True)


def list_snapshots() -> List[Dict[str, Any]]:
    """Return snapshots sorted newest-first."""
    if not SNAPSHOT_DIR.exists():
        return []
    entries: List[Dict[str, Any]] = []
    for snap_dir in SNAPSHOT_DIR.iterdir():
        if not snap_dir.is_dir():
            continue
        name = snap_dir.name
        parts = name.split("_", 2)
        if len(parts) >= 3:
            try:
                created_at = datetime.strptime(f"{parts[0]}_{parts[1]}", "%Y%m%d_%H%M%S").isoformat()
            except ValueError:
                created_at = ""
            label = parts[2]
        else:
            label = name
            created_at = ""
        entries.append({"name": name, "path": str(snap_dir), "label": label, "created_at": created_at})
    entries.sort(key=lambda e: e["name"], reverse=True)
    return entries


def cleanup_old_snapshots(keep: int = 10) -> None:
    """Delete the oldest snapshots, retaining only the newest *keep* entries."""
    if not SNAPSHOT_DIR.exists():
        return
    snap_dirs = sorted(
        [d for d in SNAPSHOT_DIR.iterdir() if d.is_dir()],
        key=lambda d: d.name,
        reverse=True,
    )
    for old_dir in snap_dirs[keep:]:
        shutil.rmtree(old_dir, ignore_errors=True)
