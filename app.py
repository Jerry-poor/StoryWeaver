from __future__ import annotations

import json
import os
import re
import threading
import traceback
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib import error, request
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
CHAPTER_DIR = DATA_DIR / "chapters"
STATIC_DIR = ROOT / "static"
ENV_PATH = ROOT / ".env"


def load_env_file(path: Path = ENV_PATH) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_env_file()

OUTLINE_PATH = DATA_DIR / "outline.json"
OUTLINE_DRAFT_PATH = DATA_DIR / "outline_draft.json"
STORY_BRIEF_PATH = DATA_DIR / "story_brief.json"
CHARACTERS_PATH = DATA_DIR / "characters.json"
STORYLINE_PATH = DATA_DIR / "storyline.json"
CONVERSATION_PATH = DATA_DIR / "conversation_memory.json"

DEEPSEEK_BASE_URL = os.getenv(
    "DEEPSEEK_BASE_URL",
    os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com/v1"),
).rstrip("/")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", os.getenv("OPENAI_API_KEY", ""))
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_MAX_TOKENS = int(os.getenv("DEEPSEEK_MAX_TOKENS", "8192"))
DEFAULT_PORT = int(os.getenv("PORT", "8787"))
MAX_RECENT_TURNS = 10

FILE_LOCK = threading.Lock()


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


def build_characters_from_outline(outline: Dict[str, Any]) -> Dict[str, Any]:
    seed_items = safe_list(outline.get("character_seed"))
    if not seed_items:
        return default_characters()

    characters: List[Dict[str, Any]] = []
    for index, item in enumerate(seed_items, start=1):
        if isinstance(item, dict):
            name = str(item.get("name") or item.get("character") or item.get("alias") or f"角色{index}").strip()
            role = str(item.get("role") or ("主角" if index == 1 else "重要角色")).strip()
            appearance = str(item.get("appearance") or "").strip()
            personality = [str(v).strip() for v in safe_list(item.get("personality")) if str(v).strip()]
            motivation = str(item.get("motivation") or "").strip()
            constraints = [str(v).strip() for v in safe_list(item.get("constraints")) if str(v).strip()]
            current_state = safe_dict(item.get("current_state"), default_character_state())
            current_state = {
                "location": str(current_state.get("location", "")).strip(),
                "mood": str(current_state.get("mood", "")).strip(),
                "injury": str(current_state.get("injury", "")).strip(),
                "known_information": [str(v).strip() for v in safe_list(current_state.get("known_information")) if str(v).strip()],
                "unknown_information": [str(v).strip() for v in safe_list(current_state.get("unknown_information")) if str(v).strip()],
            }
        else:
            name = str(item).strip() or f"角色{index}"
            role = "主角" if index == 1 else "重要角色"
            appearance = ""
            personality = []
            motivation = ""
            constraints = []
            current_state = default_character_state()

        characters.append(
            {
                "id": f"c{index:03d}",
                "name": name,
                "role": role,
                "appearance": appearance,
                "personality": personality,
                "motivation": motivation,
                "constraints": constraints,
                "current_state": current_state,
            }
        )

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
        for chapter_file in CHAPTER_DIR.glob("chapter_*.json"):
            try:
                chapter_file.unlink()
            except FileNotFoundError:
                continue


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
    for path in [OUTLINE_PATH, OUTLINE_DRAFT_PATH, STORY_BRIEF_PATH, CHARACTERS_PATH, STORYLINE_PATH, CONVERSATION_PATH]:
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
    }


def now_chapter_path(chapter_no: int) -> Path:
    return CHAPTER_DIR / f"chapter_{chapter_no:03d}.json"


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
    start = cleaned.find("{")
    if start != -1:
        cleaned = cleaned[start:]
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


def build_generation_context(state: Dict[str, Any], chapter_no: int, instruction: str, tone: str, length_target: int) -> Dict[str, Any]:
    outline = state["outline"]
    characters = state["characters"]
    storyline = state["storyline"]
    memory = state["conversation_memory"]
    chapter_plan = select_chapter_plan(outline, chapter_no)
    # Chapter turns are passed as actual messages for KV-cache; exclude them from context JSON
    recent_non_chapter = [t for t in memory.get("recent_turns", []) if t.get("type") != "chapter"]
    return {
        "chapter_task": {
            "chapter_no": chapter_no,
            "chapter_plan": chapter_plan,
            "extra_instruction": instruction,
            "tone": tone or safe_dict(outline.get("writing_rules"), default_outline()["writing_rules"]).get("tone", ""),
            "length_target": length_target or safe_dict(outline.get("writing_rules"), default_outline()["writing_rules"]).get("chapter_length_target", 1800),
        },
        "outline": outline,
        "characters": characters,
        "storyline_summary": storyline,
        "dialogue_summary": memory.get("dialogue_summary", {}),
        "recent_turns": recent_non_chapter[-MAX_RECENT_TURNS:],
        "priority_order": [
            "outline",
            "characters",
            "storyline_summary",
            "dialogue_summary",
            "recent_turns",
            "current_task",
        ],
    }


def build_chapter_messages_with_history(
    system_prompt: str,
    current_user_content: str,
    memory: Dict[str, Any],
) -> List[Dict[str, str]]:
    """Build messages replaying CONFIRMED chapter turns.
    user_prompt_text stores only the chapter_task (not full context) for KV-cache
    stability when outline/characters change between chapters.
    """
    messages: List[Dict[str, str]] = [{"role": "system", "content": system_prompt}]
    chapter_turns = [
        t for t in memory.get("recent_turns", [])
        if t.get("type") == "chapter" and t.get("confirmed", False)
    ]
    for turn in chapter_turns:
        task_text = turn.get("user_prompt_text")   # just chapter_task JSON
        assistant_text = turn.get("assistant_text") # full chapter prose
        if task_text and assistant_text:
            messages.append({"role": "user", "content": task_text})
            messages.append({"role": "assistant", "content": assistant_text})
    messages.append({"role": "user", "content": current_user_content})
    return messages


def generate_chapter_text(context: Dict[str, Any]) -> str:
    system_prompt = """
你是一个小说章节写作 agent。
必须严格参考输入中的大纲、角色表、故事线摘要和最近对话。
硬性优先级：大纲 > 角色表 > 故事线摘要 > 最近对话 > 当前临时指令。
要求：
1. 只输出本章正文，不要输出解释、标题说明、JSON 或分析过程。
2. 章节内容必须符合章节目标、必须包含项和不能包含项。
3. 角色行为必须符合角色表中的性格、动机和当前状态。
4. 不能与之前故事线冲突。
5. 如果发现前置故事线缺失或跳跃，请在生成本章时自动补齐必要的背景或前置逻辑，确保情节连贯。
6. 默认保持第三人称有限视角，文风稳定。
""".strip()
    user_prompt = json.dumps(context, ensure_ascii=False, indent=2)
    return deepseek_chat(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.85,
    )


def extract_chapter_updates(state: Dict[str, Any], chapter_no: int, chapter_text: str) -> Dict[str, Any]:
    prompt = {
        "task": "从小说章节正文中抽取用于更新故事线和角色状态的结构化信息。",
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
                    "appearance": "",
                    "motivation": "",
                    "current_state": {}
                }
            ],
            "storyline_summary": "",
        },
        "chapter_no": chapter_no,
        "outline": state["outline"],
        "characters": state["characters"],
        "previous_storyline": state["storyline"],
        "chapter_text": chapter_text,
        "requirements": [
            "输出严格 JSON",
            "character_updates 仅填写真正变化的角色",
            "current_state_changes 只放当前状态字段",
            "如果本章出现了已有角色表中没有的新重要角色，请在 new_characters 中补充",
            "如果没有变化或没有新角色，返回空数组或空对象",
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
        }


def merge_character_updates(characters: Dict[str, Any], updates: List[Dict[str, Any]], new_chars: List[Dict[str, Any]] = None) -> Dict[str, Any]:
    items = [item for item in characters.get("characters", []) if isinstance(item, dict)]
    index = {item.get("id"): item for item in items}
    
    if new_chars:
        for char in new_chars:
            if isinstance(char, dict) and char.get("id") and char.get("id") not in index:
                items.append(char)
                index[char["id"]] = char

    for update in updates:
        char_id = update.get("id")
        if char_id not in index:
            continue
        target = index[char_id]
        current_state = target.setdefault("current_state", {})
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
    # Filter out non-dict entries (e.g. leftover strings from bad data)
    history = [item for item in history if isinstance(item, dict) and str(item.get("chapter_no")) != str(chapter_no)]
    history.append(chapter_entry)
    history.sort(key=lambda item: int(item.get("chapter_no", 0)))
    storyline["chapter_summaries"] = history
    storyline["overall_summary"] = data.get("storyline_summary", storyline.get("overall_summary", ""))
    storyline["open_threads"] = merge_list_unique(
        list(storyline.get("open_threads", [])),
        list(data.get("open_threads", [])),
    )
    resolved = merge_list_unique(
        list(storyline.get("resolved_threads", [])),
        list(data.get("resolved_threads", [])),
    )
    storyline["resolved_threads"] = resolved
    return storyline


def chapter_title_from_updates(chapter_no: int, updates: Dict[str, Any]) -> str:
    title = str(updates.get("chapter_title", "")).strip()
    return title or f"第{chapter_no}章"


def chapter_word_count(text: str) -> int:
    return len(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]+", text))


def short_chat_response(messages: List[Dict[str, str]]) -> str:
    return deepseek_chat(messages, temperature=0.7)


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


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _is_stream_request(self) -> bool:
        query = parse_qs(urlparse(self.path).query)
        return query.get("stream", ["0"])[0] in {"1", "true", "yes"}

    def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _send_text(self, status: int, text: str, content_type: str = "text/html; charset=utf-8") -> None:
        raw = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _send_sse_headers(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

    def _send_sse_event(self, event: str, data: Any) -> None:
        payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        message = f"event: {event}\ndata: {payload}\n\n".encode("utf-8")
        self.wfile.write(message)
        self.wfile.flush()

    def _send_sse_comment(self, comment: str) -> None:
        self.wfile.write(f": {comment}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _read_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if not length:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw or "{}")

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        try:
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                index_path = STATIC_DIR / "index.html"
                self._send_text(HTTPStatus.OK, index_path.read_text(encoding="utf-8"))
                return
            if path == "/api/state":
                with FILE_LOCK:
                    self._send_json(HTTPStatus.OK, read_state())
                return
            if path == "/api/chapters":
                nos = []
                for f in sorted(CHAPTER_DIR.glob("chapter_*.json")):
                    try:
                        nos.append(int(f.stem.split("_")[1]))
                    except (ValueError, IndexError):
                        continue
                self._send_json(HTTPStatus.OK, {"chapters": nos})
                return
            if path.startswith("/api/chapter/"):
                chapter_no = int(path.rstrip("/").split("/")[-1])
                chapter_path = now_chapter_path(chapter_no)
                if not chapter_path.exists():
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "chapter not found"})
                    return
                with FILE_LOCK:
                    self._send_json(HTTPStatus.OK, load_json(chapter_path, {}))
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except Exception as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc), "trace": traceback.format_exc()})

    def do_POST(self) -> None:
        try:
            body = self._read_body()
            path = urlparse(self.path).path
            stream_request = self._is_stream_request()
            if path == "/api/generate-outline":
                if stream_request:
                    self.handle_generate_outline_stream(body)
                else:
                    self.handle_generate_outline(body)
                return
            if path == "/api/confirm-outline":
                self.handle_confirm_outline(body)
                return
            if path == "/api/save":
                name = body.get("name")
                text = body.get("json_text", "")
                mapping = {
                    "outline": OUTLINE_PATH,
                    "outline_draft": OUTLINE_DRAFT_PATH,
                    "story_brief": STORY_BRIEF_PATH,
                    "characters": CHARACTERS_PATH,
                    "storyline": STORYLINE_PATH,
                    "conversation_memory": CONVERSATION_PATH,
                }
                if name not in mapping:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "unknown file name"})
                    return
                data = json.loads(text)
                with FILE_LOCK:
                    save_json(mapping[name], data)
                self._send_json(HTTPStatus.OK, {"ok": True, "name": name})
                return
            if path == "/api/chat":
                if stream_request:
                    self.handle_chat_stream(body)
                else:
                    self.handle_chat(body)
                return
            if path == "/api/generate-chapter":
                if stream_request:
                    self.handle_generate_chapter_stream(body)
                else:
                    self.handle_generate_chapter(body)
                return
            if path == "/api/confirm-chapter":
                self.handle_confirm_chapter(body)
                return
            if path == "/api/reset":
                with FILE_LOCK:
                    ensure_default_files(force=True)
                self._send_json(HTTPStatus.OK, {"ok": True})
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except json.JSONDecodeError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": f"invalid json: {exc}"})
        except Exception as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc), "trace": traceback.format_exc()})

    def handle_generate_outline(self, body: Dict[str, Any]) -> None:
        story_brief = str(body.get("story_brief", "")).strip()
        if not story_brief:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "story_brief is required"})
            return
        with FILE_LOCK:
            state = read_state()
        conversation_context = {
            "dialogue_summary": state["conversation_memory"].get("dialogue_summary", {}),
            "recent_turns": state["conversation_memory"].get("recent_turns", [])[-MAX_RECENT_TURNS:],
        }
        draft_raw = generate_outline_from_brief(story_brief, conversation_context)
        draft = normalize_outline_draft(draft_raw, story_brief)
        record = {
            "story_brief": story_brief,
            "generated_at": utc_now(),
            "outline_draft": draft,
        }
        with FILE_LOCK:
            save_json(STORY_BRIEF_PATH, {
                "description": story_brief,
                "created_at": state["story_brief"].get("created_at") or utc_now(),
                "updated_at": utc_now(),
            })
            save_json(OUTLINE_DRAFT_PATH, draft)
            save_json(CHARACTERS_PATH, build_characters_from_outline(draft))
            save_json(STORYLINE_PATH, build_storyline_from_outline(draft))
            memory = load_json(CONVERSATION_PATH, default_conversation_memory())
            memory = append_turn(
                memory,
                f"生成大纲。{story_brief}",
                "已生成大纲草案，请确认后再进入章节写作。",
                turn_type="outline",
            )
            memory, removed_turns = compact_recent_turns(memory)
            if removed_turns:
                memory["dialogue_summary"] = summarize_old_turns(
                    removed_turns,
                    memory.get("dialogue_summary", default_conversation_memory()["dialogue_summary"]),
                )
            save_json(CONVERSATION_PATH, memory)
        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "outline_draft": draft,
                "record": record,
                "memory": memory,
            },
        )

    def handle_generate_outline_stream(self, body: Dict[str, Any]) -> None:
        story_brief = str(body.get("story_brief", "")).strip()
        if not story_brief:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "story_brief is required"})
            return
        with FILE_LOCK:
            state = read_state()
        conversation_context = {
            "dialogue_summary": state["conversation_memory"].get("dialogue_summary", {}),
            "recent_turns": state["conversation_memory"].get("recent_turns", [])[-MAX_RECENT_TURNS:],
        }
        messages = [
            {
                "role": "system",
                "content": (
                    "你是小说大纲设计师。请先用简短自然语言概述，再输出一个 ```json``` 代码块。"
                    "代码块里必须是可用于后续章节写作的结构化大纲 JSON。"
                    "要求：1. 只输出自然语言加 JSON 代码块，不要额外解释。"
                    "2. 大纲应包含 status, title, genre, theme, logline, world_setting, writing_rules, main_conflict, character_seed, chapter_plan。"
                    "3. chapter_plan 至少包含 6 章。4. status 必须是 draft。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "story_brief": story_brief,
                        "conversation_context": conversation_context,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            },
        ]
        self._send_sse_headers()
        self._send_sse_event("meta", {"kind": "outline", "story_brief": story_brief})
        parts: List[str] = []
        try:
            for chunk in deepseek_chat_stream(messages, temperature=0.35):
                parts.append(chunk)
                self._send_sse_event("chunk", {"text": chunk})
            full_text = "".join(parts)
            draft_raw = parse_outline_json_from_text(full_text)
            draft = normalize_outline_draft(draft_raw, story_brief)
            with FILE_LOCK:
                save_json(
                    STORY_BRIEF_PATH,
                    {
                        "description": story_brief,
                        "created_at": state["story_brief"].get("created_at") or utc_now(),
                        "updated_at": utc_now(),
                    },
                )
                save_json(OUTLINE_DRAFT_PATH, draft)
                save_json(CHARACTERS_PATH, build_characters_from_outline(draft))
                save_json(STORYLINE_PATH, build_storyline_from_outline(draft))
                memory = load_json(CONVERSATION_PATH, default_conversation_memory())
                memory = append_turn(
                    memory,
                    f"生成大纲。{story_brief}",
                    "已生成大纲草案，请确认后再进入章节写作。",
                    turn_type="outline",
                )
                memory, removed_turns = compact_recent_turns(memory)
            if removed_turns:
                memory["dialogue_summary"] = summarize_old_turns(
                    removed_turns,
                    memory.get("dialogue_summary", default_conversation_memory()["dialogue_summary"]),
                )
            with FILE_LOCK:
                save_json(CONVERSATION_PATH, memory)
            self._send_sse_event(
                "final",
                {
                    "ok": True,
                    "outline_draft": draft,
                    "memory": memory,
                    "full_text": full_text,
                },
            )
        except Exception as exc:
            self._send_sse_event("error", {"error": str(exc)})

    def handle_confirm_outline(self, body: Dict[str, Any]) -> None:
        outline_json = body.get("outline_json")
        try:
            if outline_json:
                draft = json.loads(outline_json)
            else:
                with FILE_LOCK:
                    draft = load_json(OUTLINE_DRAFT_PATH, default_outline_draft())
        except json.JSONDecodeError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": f"invalid outline json: {exc}"})
            return
        confirmed = normalize_outline_shape(draft, str(body.get("story_brief", "")).strip())
        confirmed["status"] = "confirmed"
        confirmed["confirmed_at"] = utc_now()
        with FILE_LOCK:
            save_json(OUTLINE_DRAFT_PATH, confirmed)
            save_json(OUTLINE_PATH, confirmed)
        reset_generated_story_state(confirmed)
        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "outline": confirmed,
            },
        )

    def handle_chat(self, body: Dict[str, Any]) -> None:
        """Chapter editing: rewrite a specific chapter based on user instruction.
        Does NOT write to recent_turns (chat is ephemeral)."""
        user_message = str(body.get("message", "")).strip()
        chapter_no = int(body.get("chapter_no") or 0)
        if not user_message:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "message is required"})
            return
        with FILE_LOCK:
            state = read_state()
        chapter_text = ""
        if chapter_no > 0:
            ch_path = now_chapter_path(chapter_no)
            if ch_path.exists():
                chapter_text = load_json(ch_path, {}).get("chapter_text", "")
        context = {
            "task": "章节修改指令",
            "chapter_no": chapter_no,
            "current_chapter_text": chapter_text,
            "outline": state["outline"],
            "characters": state["characters"],
            "storyline_summary": state["storyline"],
            "user_instruction": user_message,
        }
        messages = [
            {"role": "system", "content": (
                "你是小说章节编辑助手。根据用户指令修改指定章节正文。"
                "只输出修改后的完整正文，不要加任何解释。"
            )},
            {"role": "user", "content": json.dumps(context, ensure_ascii=False, indent=2)},
        ]
        result = short_chat_response(messages)
        # Save modified chapter text if chapter exists
        if chapter_no > 0 and chapter_text:
            ch_path = now_chapter_path(chapter_no)
            with FILE_LOCK:
                rec = load_json(ch_path, {})
                rec["chapter_text"] = result
                rec["last_edited_at"] = utc_now()
                rec["status"] = "draft"  # editing resets to draft
                save_json(ch_path, rec)
        self._send_json(HTTPStatus.OK, {"ok": True, "reply": result})

    def handle_chat_stream(self, body: Dict[str, Any]) -> None:
        """Chapter editing stream: ephemeral, no recent_turns write."""
        user_message = str(body.get("message", "")).strip()
        chapter_no = int(body.get("chapter_no") or 0)
        if not user_message:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "message is required"})
            return
        with FILE_LOCK:
            state = read_state()
        chapter_text = ""
        if chapter_no > 0:
            ch_path = now_chapter_path(chapter_no)
            if ch_path.exists():
                chapter_text = load_json(ch_path, {}).get("chapter_text", "")
        context = {
            "task": "章节修改指令",
            "chapter_no": chapter_no,
            "current_chapter_text": chapter_text,
            "outline": state["outline"],
            "characters": state["characters"],
            "storyline_summary": state["storyline"],
            "user_instruction": user_message,
        }
        messages = [
            {"role": "system", "content": (
                "你是小说章节编辑助手。根据用户指令修改指定章节正文。"
                "只输出修改后的完整正文，不要加任何解释。"
            )},
            {"role": "user", "content": json.dumps(context, ensure_ascii=False, indent=2)},
        ]
        self._send_sse_headers()
        self._send_sse_event("meta", {"kind": "chat_edit", "chapter_no": chapter_no})
        parts: List[str] = []
        try:
            for chunk in deepseek_chat_stream(messages, temperature=0.7):
                parts.append(chunk)
                self._send_sse_event("chunk", {"text": chunk})
            result = "".join(parts)
            if chapter_no > 0 and chapter_text:
                ch_path = now_chapter_path(chapter_no)
                with FILE_LOCK:
                    rec = load_json(ch_path, {})
                    rec["chapter_text"] = result
                    rec["last_edited_at"] = utc_now()
                    rec["status"] = "draft"
                    save_json(ch_path, rec)
            self._send_sse_event("final", {"ok": True, "reply": result, "chapter_no": chapter_no})
        except Exception as exc:
            self._send_sse_event("error", {"error": str(exc)})

    def handle_generate_chapter_stream(self, body: Dict[str, Any]) -> None:
        chapter_no = int(body.get("chapter_no") or 1)
        instruction = str(body.get("instruction", "")).strip()
        tone = str(body.get("tone", "")).strip()
        length_target = int(body.get("length_target") or 1800)
        chapter_title_hint = str(body.get("chapter_title", "")).strip()
        with FILE_LOCK:
            outline = normalize_outline_shape(load_json(OUTLINE_PATH, default_outline()))
        if not outline_is_confirmed(outline):
            self._send_json(HTTPStatus.BAD_REQUEST, {
                "error": "outline is not confirmed yet",
                "hint": "请先输入故事描述，生成大纲草案，并确认后再生成章节。",
            })
            return
        with FILE_LOCK:
            state = read_state()
        memory = state["conversation_memory"]
        context = build_generation_context(state, chapter_no, instruction, tone, length_target)
        current_user_content = json.dumps(context, ensure_ascii=False, indent=2)
        # Only the task portion is stored for multi-turn prefix replay (KV-cache stable)
        chapter_task_content = json.dumps(context.get("chapter_task", {}), ensure_ascii=False, indent=2)
        system_prompt = (
            "你是一个小说章节写作 agent。必须严格参考输入中的大纲、角色表、故事线摘要。"
            "硬性优先级：大纲 > 角色表 > 故事线摘要 > 当前章节任务。"
            f"目标字数约 {length_target} 字，请写完完整情节后自然收尾，不要中途截断。"
            "连续性要求（最高优先级）："
            "1. 必须从故事线中上一章的结尾场景/状态自然衔接，不得跳跃或重复。"
            "2. 必须推进或收束 storyline_summary 中的 open_threads 未解决线索。"
            "3. 角色状态必须与 characters 中的 current_state 一致，不得矛盾。"
            "输出要求："
            "4. 只输出本章正文，不要输出解释、标题说明、JSON 或分析过程。"
            "5. 如发现前置情节缺失，请在正文中自然补齐背景，确保读者无障碍阅读。"
            "6. 默认保持第三人称有限视角，文风稳定。"
        )
        messages = build_chapter_messages_with_history(system_prompt, current_user_content, memory)
        self._send_sse_headers()
        self._send_sse_event("meta", {"kind": "chapter", "chapter_no": chapter_no})
        parts: List[str] = []
        try:
            for chunk in deepseek_chat_stream(messages, temperature=0.85):
                parts.append(chunk)
                self._send_sse_event("chunk", {"text": chunk})
            chapter_text = "".join(parts)
            chapter_title = chapter_title_hint or f"第{chapter_no}章"
            # Save as DRAFT only — storyline/characters updated only after user confirms
            chapter_record = {
                "chapter_no": chapter_no,
                "status": "draft",
                "title": chapter_title,
                "generated_at": utc_now(),
                "instruction": instruction,
                "tone": tone,
                "length_target": length_target,
                "chapter_text": chapter_text,
                # Store task content for KV-cache replay after confirmation
                "chapter_task_content": chapter_task_content,
            }
            with FILE_LOCK:
                save_json(now_chapter_path(chapter_no), chapter_record)
            self._send_sse_event("final", {
                "ok": True,
                "chapter_no": chapter_no,
                "title": chapter_title,
                "chapter_text": chapter_text,
                "status": "draft",
            })
        except Exception as exc:
            import traceback; traceback.print_exc()
            self._send_sse_event("error", {"error": str(exc)})

    def handle_generate_chapter(self, body: Dict[str, Any]) -> None:
        chapter_no = int(body.get("chapter_no") or 1)
        instruction = str(body.get("instruction", "")).strip()
        tone = str(body.get("tone", "")).strip()
        length_target = int(body.get("length_target") or 1800)
        chapter_title_hint = str(body.get("chapter_title", "")).strip()
        with FILE_LOCK:
            outline = normalize_outline_shape(load_json(OUTLINE_PATH, default_outline()))
        if not outline_is_confirmed(outline):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {
                    "error": "outline is not confirmed yet",
                    "hint": "请先输入故事描述，生成大纲草案，并确认后再生成章节。",
                },
            )
            return
        with FILE_LOCK:
            state = read_state()
        memory = state["conversation_memory"]
        context = build_generation_context(state, chapter_no, instruction, tone, length_target)
        current_user_content = json.dumps(context, ensure_ascii=False, indent=2)
        system_prompt = (
            "你是一个小说章节写作 agent。必须严格参考输入中的大纲、角色表、故事线摘要和最近对话。"
            "硬性优先级：大纲 > 角色表 > 故事线摘要 > 最近对话 > 当前临时指令。"
            f"目标字数约 {length_target} 字，请写完完整情节后自然收尾，不要中途截断，也不要超出太多。"
            "要求："
            "1. 只输出本章正文，不要输出解释、标题说明、JSON 或分析过程。"
            "2. 角色行为必须符合角色表中的性格、动机和当前状态。"
            "3. 不能与之前故事线冲突。"
            "4. 如果发现前置故事线缺失或跳跃，请自动补齐必要的背景或前置逻辑，确保情节连贯。"
            "5. 默认保持第三人称有限视角，文风稳定。"
        )
        messages = build_chapter_messages_with_history(system_prompt, current_user_content, memory)
        chapter_text = deepseek_chat(messages, temperature=0.85)
        updates = extract_chapter_updates(state, chapter_no, chapter_text)
        chapter_title = chapter_title_hint or chapter_title_from_updates(chapter_no, updates)
        storyline = update_storyline(state["storyline"], chapter_no, chapter_title, updates)
        characters = merge_character_updates(
            state["characters"],
            list(updates.get("character_updates", [])),
            list(updates.get("new_characters", []))
        )
        wc = chapter_word_count(chapter_text)
        assistant_summary = f"已生成第{chapter_no}章《{chapter_title}》，并同步更新故事线与角色状态。"
        if wc:
            assistant_summary += f" 章节长度约 {wc} 个词块。"
        removed_turns: List[Dict[str, Any]] = []
        with FILE_LOCK:
            memory = load_json(CONVERSATION_PATH, default_conversation_memory())
            memory = append_turn(
                memory,
                f"生成第{chapter_no}章。{instruction}".strip(),
                assistant_summary,
                turn_type="chapter",
                chapter_no=chapter_no,
                extra_fields={
                    "user_prompt_text": current_user_content,
                    "assistant_text": chapter_text,
                },
            )
            memory, removed_turns = compact_recent_turns(memory)
        if removed_turns:
            memory["dialogue_summary"] = summarize_old_turns(
                removed_turns,
                memory.get("dialogue_summary", default_conversation_memory()["dialogue_summary"]),
            )
        chapter_task_content = json.dumps(context["chapter_task"], ensure_ascii=False, indent=2)
        chapter_record = {
            "chapter_no": chapter_no,
            "status": "draft",
            "title": chapter_title_hint or f"第{chapter_no}章",
            "generated_at": utc_now(),
            "instruction": instruction,
            "tone": tone,
            "length_target": length_target,
            "chapter_text": chapter_text,
            "chapter_task_content": chapter_task_content,
        }
        with FILE_LOCK:
            save_json(now_chapter_path(chapter_no), chapter_record)
        self._send_json(HTTPStatus.OK, {
            "ok": True,
            "chapter_no": chapter_no,
            "title": chapter_record["title"],
            "chapter_text": chapter_text,
            "status": "draft",
        })

    def handle_confirm_chapter(self, body: Dict[str, Any]) -> None:
        """Confirm a draft chapter: extract updates, persist storyline/characters,
        and add a CONFIRMED turn to recent_turns (with chapter_task as KV-cache prefix)."""
        chapter_no = int(body.get("chapter_no") or 1)
        with FILE_LOCK:
            chapter_path = now_chapter_path(chapter_no)
            if not chapter_path.exists():
                self._send_json(HTTPStatus.NOT_FOUND, {"error": f"chapter {chapter_no} not found"})
                return
            chapter_record = load_json(chapter_path, {})
            state = read_state()
        chapter_text = chapter_record.get("chapter_text", "")
        instruction = chapter_record.get("instruction", "")
        chapter_task_content = chapter_record.get("chapter_task_content", "")
        updates = extract_chapter_updates(state, chapter_no, chapter_text)
        chapter_title = chapter_title_from_updates(chapter_no, updates) or chapter_record.get("title", f"第{chapter_no}章")
        storyline = update_storyline(state["storyline"], chapter_no, chapter_title, updates)
        characters = merge_character_updates(
            state["characters"],
            list(updates.get("character_updates", [])),
            list(updates.get("new_characters", [])),
        )
        wc = chapter_word_count(chapter_text)
        summary = f"已确认第{chapter_no}章《{chapter_title}》。"
        if wc:
            summary += f" 约 {wc} 字。"
        removed_turns: List[Dict[str, Any]] = []
        with FILE_LOCK:
            memory = load_json(CONVERSATION_PATH, default_conversation_memory())
            # Remove any previous unconfirmed turn for same chapter_no
            memory["recent_turns"] = [
                t for t in memory.get("recent_turns", [])
                if not (t.get("type") == "chapter" and t.get("chapter_no") == chapter_no)
            ]
            memory = append_turn(
                memory,
                f"确认第{chapter_no}章。{instruction}".strip(),
                summary,
                turn_type="chapter",
                chapter_no=chapter_no,
                extra_fields={
                    "confirmed": True,
                    "user_prompt_text": chapter_task_content,  # task-only for KV prefix
                    "assistant_text": chapter_text,
                },
            )
            memory, removed_turns = compact_recent_turns(memory)
        if removed_turns:
            memory["dialogue_summary"] = summarize_old_turns(
                removed_turns,
                memory.get("dialogue_summary", default_conversation_memory()["dialogue_summary"]),
            )
        chapter_record.update({
            "title": chapter_title,
            "status": "confirmed",
            "confirmed_at": utc_now(),
            "chapter_summary": updates.get("chapter_summary", []),
            "new_events": updates.get("new_events", []),
            "open_threads": updates.get("open_threads", []),
            "resolved_threads": updates.get("resolved_threads", []),
            "character_updates": updates.get("character_updates", []),
        })
        with FILE_LOCK:
            save_json(CHARACTERS_PATH, characters)
            save_json(STORYLINE_PATH, storyline)
            save_json(CONVERSATION_PATH, memory)
            save_json(chapter_path, chapter_record)
        self._send_json(HTTPStatus.OK, {
            "ok": True,
            "chapter_no": chapter_no,
            "title": chapter_title,
            "updates": updates,
            "memory": memory,
        })


def main() -> None:
    ensure_dirs()
    ensure_default_files()
    server = ThreadingHTTPServer(("0.0.0.0", DEFAULT_PORT), Handler)
    print(f"Novel writer server running on http://localhost:{DEFAULT_PORT}")
    print(f"DeepSeek base URL: {DEEPSEEK_BASE_URL}")
    server.serve_forever()


if __name__ == "__main__":
    main()
