from __future__ import annotations

import json
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

# Import modular settings and paths
from backend.app.config import (
    FILE_LOCK,
    STATIC_DIR,
    CHAPTER_DIR,
    OUTLINE_PATH,
    OUTLINE_DRAFT_PATH,
    STORY_BRIEF_PATH,
    CHARACTERS_PATH,
    STORYLINE_PATH,
    CONVERSATION_PATH,
    MAX_RECENT_TURNS,
)
from backend.app.storage import (
    read_state,
    now_chapter_path,
    load_json,
    save_json,
    ensure_default_files,
    default_conversation_memory,
    default_outline,
    default_outline_draft,
    default_chapter_draft,
    default_segment,
    default_continuity_contract,
    default_ending_state,
    load_instruction_registry,
    save_instruction_registry,
    normalize_outline_shape,
    utc_now,
    create_snapshot,
    restore_snapshot,
    list_snapshots,
)
from backend.app.llm import (
    deepseek_chat,
    deepseek_chat_stream,
    parse_outline_json_from_text,
    short_chat_response,
)
from backend.app.agents import (
    parse_user_constraints_from_message,
    append_turn,
    compact_recent_turns,
    summarize_old_turns,
    compact_chapter_turns,
    drain_arc_compression,
)
from backend.app.pipeline import (
    generate_outline_from_brief,
    normalize_outline_draft,
    build_characters_from_outline,
    build_storyline_from_outline,
    reset_generated_story_state,
    outline_is_confirmed,
    build_generation_context,
    build_chapter_system_prompt,
    build_chapter_messages_with_history,
    run_chapter_quality_pipeline,
    chapter_word_count,
    extract_chapter_updates,
    chapter_title_from_updates,
    update_storyline,
    merge_character_updates,
    update_continuity_from_updates,
    start_chapter_generation,
    continue_chapter_generation,
    assemble_full_chapter,
    chapter_draft_complete,
    generate_chapter_segment_stream,
    plan_auto_chapters,
)


class ClientDisconnectedError(ConnectionError):
    pass


def drain_generation_memory(state: Dict[str, Any]) -> Dict[str, Any]:
    memory = state.get("conversation_memory") or default_conversation_memory()
    memory = drain_arc_compression(memory)
    state["conversation_memory"] = memory
    with FILE_LOCK:
        save_json(CONVERSATION_PATH, memory)
    return memory


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def handle(self) -> None:
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError):
            return

    def _is_stream_request(self) -> bool:
        query = parse_qs(urlparse(self.path).query)
        return query.get("stream", ["0"])[0] in {"1", "true", "yes"}

    def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise ClientDisconnectedError() from exc

    def _send_text(self, status: int, text: str, content_type: str = "text/html; charset=utf-8") -> None:
        raw = text.encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise ClientDisconnectedError() from exc

    def _send_sse_headers(self) -> None:
        try:
            # SSE requests are long-lived but finite in this app; close the
            # socket when the handler returns so stream readers can finish.
            self.close_connection = True
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise ClientDisconnectedError() from exc

    def _send_sse_event(self, event: str, data: Any) -> None:
        payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        message = f"event: {event}\ndata: {payload}\n\n".encode("utf-8")
        try:
            self.wfile.write(message)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise ClientDisconnectedError() from exc

    def _send_sse_comment(self, comment: str) -> None:
        try:
            self.wfile.write(f": {comment}\n\n".encode("utf-8"))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise ClientDisconnectedError() from exc

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
            if path == "/api/snapshots":
                self._send_json(HTTPStatus.OK, {"snapshots": list_snapshots()})
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except ClientDisconnectedError:
            return
        except Exception as exc:
            try:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc), "trace": traceback.format_exc()})
            except ClientDisconnectedError:
                return

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
            if path == "/api/start-chapter":
                if stream_request:
                    self.handle_start_chapter_stream(body)
                else:
                    self.handle_start_chapter(body)
                return
            if path == "/api/continue-chapter":
                if stream_request:
                    self.handle_continue_chapter_stream(body)
                else:
                    self.handle_continue_chapter(body)
                return
            if path == "/api/finish-chapter":
                self.handle_finish_chapter(body)
                return
            if path == "/api/auto-generate-chapters":
                if stream_request:
                    self.handle_auto_generate_chapters_stream(body)
                else:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "only stream mode is supported for auto-generate-chapters"})
                return
            if path == "/api/undo":
                snapshots = list_snapshots()
                if not snapshots:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "没有可撤回的操作"})
                    return
                latest = snapshots[0]
                with FILE_LOCK:
                    restore_snapshot(Path(latest["path"]))
                    state = read_state()
                self._send_json(HTTPStatus.OK, {"ok": True, "restored": latest["name"], "state": state})
                return
            if path == "/api/reset":
                with FILE_LOCK:
                    ensure_default_files(force=True)
                self._send_json(HTTPStatus.OK, {"ok": True})
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except ClientDisconnectedError:
            return
        except json.JSONDecodeError as exc:
            try:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": f"invalid json: {exc}"})
            except ClientDisconnectedError:
                return
        except Exception as exc:
            try:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc), "trace": traceback.format_exc()})
            except ClientDisconnectedError:
                return

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
        create_snapshot("confirm_outline")
        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "outline": confirmed,
            },
        )

    def handle_chat(self, body: Dict[str, Any]) -> None:
        user_message = str(body.get("message", "")).strip()
        chapter_no = int(body.get("chapter_no") or 0)
        if not user_message:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "message is required"})
            return
        with FILE_LOCK:
            state = read_state()
        registry = state.get("instruction_registry") or load_instruction_registry()
        registry = parse_user_constraints_from_message(user_message, registry)
        with FILE_LOCK:
            save_instruction_registry(registry)
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
            "active_instruction_constraints": registry,
            "user_instruction": user_message,
        }
        messages = [
            {"role": "system", "content": (
                "你是小说章节编辑助手。根据用户指令修改指定章节正文。"
                "必须遵守 active_instruction_constraints 中的长期约束，特别是 banned_patterns 和 global_constraints。"
                "只输出修改后的完整正文，不要加任何解释。"
            )},
            {"role": "user", "content": json.dumps(context, ensure_ascii=False, indent=2)},
        ]
        result = short_chat_response(messages)
        updates = {}
        if chapter_no > 0:
            ch_path = now_chapter_path(chapter_no)
            updates = extract_chapter_updates(state, chapter_no, result)
            chapter_title = chapter_title_from_updates(chapter_no, updates)
            storyline = update_storyline(state["storyline"], chapter_no, chapter_title, updates)
            characters = merge_character_updates(
                state["characters"],
                list(updates.get("character_updates", [])),
                list(updates.get("new_characters", [])),
            )
            with FILE_LOCK:
                rec = load_json(ch_path, default_chapter_draft(chapter_no))
                rec["chapter_text"] = result
                rec["title"] = chapter_title or rec.get("title", f"第{chapter_no}章")
                rec["last_edited_at"] = utc_now()
                rec["status"] = "draft"
                rec["chapter_summary"] = updates.get("chapter_summary", [])
                rec["new_events"] = updates.get("new_events", [])
                rec["open_threads"] = updates.get("open_threads", [])
                rec["resolved_threads"] = updates.get("resolved_threads", [])
                rec["character_updates"] = updates.get("character_updates", [])
                save_json(ch_path, rec)
                save_json(CHARACTERS_PATH, characters)
                save_json(STORYLINE_PATH, storyline)
                memory = load_json(CONVERSATION_PATH, default_conversation_memory())
                memory = append_turn(
                    memory,
                    f"修改第{chapter_no}章。{user_message}",
                    f"已根据指令修改第{chapter_no}章《{chapter_title}》。",
                    turn_type="chapter",
                    chapter_no=chapter_no,
                )
                memory, removed_turns = compact_recent_turns(memory)
                if removed_turns:
                    memory["dialogue_summary"] = summarize_old_turns(
                        removed_turns,
                        memory.get("dialogue_summary", default_conversation_memory()["dialogue_summary"]),
                    )
                save_json(CONVERSATION_PATH, memory)
            update_continuity_from_updates(updates)
        self._send_json(HTTPStatus.OK, {"ok": True, "reply": result, "updates": updates})

    def handle_chat_stream(self, body: Dict[str, Any]) -> None:
        user_message = str(body.get("message", "")).strip()
        chapter_no = int(body.get("chapter_no") or 0)
        if not user_message:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "message is required"})
            return
        with FILE_LOCK:
            state = read_state()
        registry = state.get("instruction_registry") or load_instruction_registry()
        registry = parse_user_constraints_from_message(user_message, registry)
        with FILE_LOCK:
            save_instruction_registry(registry)
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
            "active_instruction_constraints": registry,
            "user_instruction": user_message,
        }
        messages = [
            {"role": "system", "content": (
                "你是小说章节编辑助手。根据用户指令修改指定章节正文。"
                "必须遵守 active_instruction_constraints 中的长期约束，特别是 banned_patterns 和 global_constraints。"
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
            updates = {}
            if chapter_no > 0:
                ch_path = now_chapter_path(chapter_no)
                updates = extract_chapter_updates(state, chapter_no, result)
                chapter_title = chapter_title_from_updates(chapter_no, updates)
                storyline = update_storyline(state["storyline"], chapter_no, chapter_title, updates)
                characters = merge_character_updates(
                    state["characters"],
                    list(updates.get("character_updates", [])),
                    list(updates.get("new_characters", [])),
                )
                with FILE_LOCK:
                    rec = load_json(ch_path, default_chapter_draft(chapter_no))
                    rec["chapter_text"] = result
                    rec["title"] = chapter_title or rec.get("title", f"第{chapter_no}章")
                    rec["last_edited_at"] = utc_now()
                    rec["status"] = "draft"
                    rec["chapter_summary"] = updates.get("chapter_summary", [])
                    rec["new_events"] = updates.get("new_events", [])
                    rec["open_threads"] = updates.get("open_threads", [])
                    rec["resolved_threads"] = updates.get("resolved_threads", [])
                    rec["character_updates"] = updates.get("character_updates", [])
                    save_json(ch_path, rec)
                    save_json(CHARACTERS_PATH, characters)
                    save_json(STORYLINE_PATH, storyline)
                    memory = load_json(CONVERSATION_PATH, default_conversation_memory())
                    memory = append_turn(
                        memory,
                        f"修改第{chapter_no}章。{user_message}",
                        f"已根据指令修改第{chapter_no}章《{chapter_title}》。",
                        turn_type="chapter",
                        chapter_no=chapter_no,
                    )
                    memory, removed_turns = compact_recent_turns(memory)
                    if removed_turns:
                        memory["dialogue_summary"] = summarize_old_turns(
                            removed_turns,
                            memory.get("dialogue_summary", default_conversation_memory()["dialogue_summary"]),
                        )
                    save_json(CONVERSATION_PATH, memory)
                update_continuity_from_updates(updates)
            self._send_sse_event("final", {"ok": True, "reply": result, "chapter_no": chapter_no, "updates": updates})
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
        memory = drain_generation_memory(state)
        context = build_generation_context(state, chapter_no, instruction, tone, length_target)
        current_user_content = json.dumps(context, ensure_ascii=False, indent=2)
        chapter_task_content = json.dumps(context.get("chapter_task", {}), ensure_ascii=False, indent=2)
        system_prompt = build_chapter_system_prompt(length_target)
        messages = build_chapter_messages_with_history(system_prompt, current_user_content, memory)
        self._send_sse_headers()
        self._send_sse_event("meta", {"kind": "chapter", "chapter_no": chapter_no})
        parts: List[str] = []
        try:
            for chunk in deepseek_chat_stream(messages, temperature=0.85):
                parts.append(chunk)
                self._send_sse_event("chunk", {"text": chunk})
            raw_text = "".join(parts)
            revised_text, quality_reports, needs_user_review = run_chapter_quality_pipeline(raw_text, context)
            chapter_text = revised_text
            updates = extract_chapter_updates(state, chapter_no, chapter_text)
            chapter_title = chapter_title_hint or chapter_title_from_updates(chapter_no, updates)
            storyline = update_storyline(state["storyline"], chapter_no, chapter_title, updates)
            characters = merge_character_updates(
                state["characters"],
                list(updates.get("character_updates", [])),
                list(updates.get("new_characters", [])),
            )
            wc = chapter_word_count(chapter_text)
            chapter_record = {
                "chapter_no": chapter_no,
                "generation_mode": "single_segment",
                "status": "draft",
                "title": chapter_title,
                "target_words": length_target,
                "current_words": wc,
                "generated_at": utc_now(),
                "instruction": instruction,
                "tone": tone,
                "length_target": length_target,
                "chapter_text": chapter_text,
                "chapter_task_content": chapter_task_content,
                "quality_reports": quality_reports,
                "needs_user_review": needs_user_review,
                "segments": [
                    {
                        "segment_no": 1,
                        "status": "done",
                        "text": chapter_text,
                        "quality_reports": quality_reports,
                    }
                ],
            }
            if revised_text != raw_text:
                chapter_record["raw_text_before_revision"] = raw_text
            with FILE_LOCK:
                save_json(CHARACTERS_PATH, characters)
                save_json(STORYLINE_PATH, storyline)
                save_json(now_chapter_path(chapter_no), chapter_record)
            assistant_summary = f"已生成第{chapter_no}章《{chapter_title}》，并同步更新故事线与角色状态。"
            if wc:
                assistant_summary += f" 章节长度约 {wc} 个词块。"
            if needs_user_review:
                assistant_summary += " [注意：质量检查未完全通过，请人工审核。]"
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
            with FILE_LOCK:
                save_json(CONVERSATION_PATH, memory)
            create_snapshot(f"generate_ch{chapter_no}")
            self._send_sse_event(
                "final",
                {
                    "ok": True,
                    "chapter_no": chapter_no,
                    "title": chapter_title,
                    "chapter_text": chapter_text,
                    "status": "draft",
                    "updates": updates,
                    "quality_reports": quality_reports,
                    "needs_user_review": needs_user_review,
                    "memory": memory,
                },
            )
        except Exception as exc:
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
        memory = drain_generation_memory(state)
        context = build_generation_context(state, chapter_no, instruction, tone, length_target)
        current_user_content = json.dumps(context, ensure_ascii=False, indent=2)
        system_prompt = build_chapter_system_prompt(length_target)
        messages = build_chapter_messages_with_history(system_prompt, current_user_content, memory)
        raw_text = deepseek_chat(messages, temperature=0.85)
        revised_text, quality_reports, needs_user_review = run_chapter_quality_pipeline(raw_text, context)
        chapter_text = revised_text
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
        if needs_user_review:
            assistant_summary += " [注意：质量检查未完全通过，请人工审核。]"
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
            "generation_mode": "single_segment",
            "status": "draft",
            "title": chapter_title,
            "target_words": length_target,
            "current_words": wc,
            "generated_at": utc_now(),
            "instruction": instruction,
            "tone": tone,
            "length_target": length_target,
            "chapter_text": chapter_text,
            "chapter_task_content": chapter_task_content,
            "quality_reports": quality_reports,
            "needs_user_review": needs_user_review,
            "segments": [
                {
                    "segment_no": 1,
                    "status": "done",
                    "text": chapter_text,
                    "quality_reports": quality_reports,
                }
            ],
        }
        if revised_text != raw_text:
            chapter_record["raw_text_before_revision"] = raw_text
        with FILE_LOCK:
            save_json(CHARACTERS_PATH, characters)
            save_json(STORYLINE_PATH, storyline)
            save_json(CONVERSATION_PATH, memory)
            save_json(now_chapter_path(chapter_no), chapter_record)
        create_snapshot(f"generate_ch{chapter_no}")
        self._send_json(HTTPStatus.OK, {
            "ok": True,
            "chapter_no": chapter_no,
            "title": chapter_record["title"],
            "chapter_text": chapter_text,
            "status": "draft",
            "updates": updates,
            "quality_reports": quality_reports,
            "needs_user_review": needs_user_review,
            "memory": memory,
        })

    def handle_confirm_chapter(self, body: Dict[str, Any]) -> None:
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
        wc = chapter_word_count(chapter_text)
        summary = f"已确认第{chapter_no}章《{chapter_title}》。"
        if wc:
            summary += f" 约 {wc} 字。"
        with FILE_LOCK:
            state = read_state()
            storyline = update_storyline(state["storyline"], chapter_no, chapter_title, updates)
            characters = merge_character_updates(
                state["characters"],
                list(updates.get("character_updates", [])),
                list(updates.get("new_characters", [])),
            )
            memory = load_json(CONVERSATION_PATH, default_conversation_memory())
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
                    "user_prompt_text": chapter_task_content,
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
            save_json(CHARACTERS_PATH, characters)
            save_json(STORYLINE_PATH, storyline)
            memory = compact_chapter_turns(memory)
            save_json(CONVERSATION_PATH, memory)
            save_json(chapter_path, chapter_record)
        update_continuity_from_updates(updates)
        create_snapshot(f"confirm_ch{chapter_no}")
        self._send_json(HTTPStatus.OK, {
            "ok": True,
            "chapter_no": chapter_no,
            "title": chapter_title,
            "updates": updates,
            "memory": memory,
        })

    def handle_start_chapter(self, body: Dict[str, Any]) -> None:
        chapter_no = int(body.get("chapter_no") or 1)
        instruction = str(body.get("instruction", "")).strip()
        tone = str(body.get("tone", "")).strip()
        target_words = int(body.get("target_words") or body.get("length_target") or 1800)
        with FILE_LOCK:
            state = read_state()
            if not outline_is_confirmed(state.get("outline", {})):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "outline must be confirmed first"})
                return
        drain_generation_memory(state)
        draft = start_chapter_generation(state, chapter_no, instruction, tone, target_words)
        with FILE_LOCK:
            save_json(now_chapter_path(chapter_no), draft)
        is_complete = chapter_draft_complete(draft)
        self._send_json(HTTPStatus.OK, {
            "ok": True,
            "chapter_no": chapter_no,
            "generation_mode": draft.get("generation_mode"),
            "chapter_text": draft.get("chapter_text", ""),
            "current_words": draft.get("current_words", 0),
            "target_words": draft.get("target_words", 0),
            "segments_count": len(draft.get("segments", [])),
            "estimated_segments": draft.get("segment_plan", {}).get("estimated_segments", 1),
            "is_complete": is_complete,
            "needs_user_review": draft.get("needs_user_review", False),
            "quality_reports": draft.get("quality_reports", []),
        })

    def handle_start_chapter_stream(self, body: Dict[str, Any]) -> None:
        chapter_no = int(body.get("chapter_no") or 1)
        instruction = str(body.get("instruction", "")).strip()
        tone = str(body.get("tone", "")).strip()
        target_words = int(body.get("target_words") or body.get("length_target") or 1800)
        with FILE_LOCK:
            state = read_state()
            if not outline_is_confirmed(state.get("outline", {})):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "outline must be confirmed first"})
                return
        drain_generation_memory(state)
        generation_mode = "multi_segment" if target_words > 4000 else "single_segment"
        self._send_sse_headers()
        self._send_sse_event("meta", {
            "chapter_no": chapter_no,
            "generation_mode": generation_mode,
            "target_words": target_words,
        })
        draft = default_chapter_draft(chapter_no)
        draft["generation_mode"] = generation_mode
        draft["target_words"] = target_words
        draft["instruction"] = instruction
        draft["tone"] = tone
        draft["length_target"] = target_words
        draft["generated_at"] = utc_now()
        context = build_generation_context(state, chapter_no, instruction, tone, target_words)
        draft["chapter_task_content"] = json.dumps(context.get("chapter_task", {}), ensure_ascii=False, indent=2)
        if generation_mode == "single_segment":
            memory = state["conversation_memory"]
            current_user_content = json.dumps(context, ensure_ascii=False, indent=2)
            system_prompt = build_chapter_system_prompt(target_words)
            messages = build_chapter_messages_with_history(system_prompt, current_user_content, memory)
            full_text = ""
            try:
                for chunk in deepseek_chat_stream(messages, temperature=0.85):
                    if chunk:
                        full_text += chunk
                        self._send_sse_event("chunk", {"text": chunk})
            except Exception as exc:
                self._send_sse_event("error", {"message": str(exc)})
                return
            revised_text, quality_reports, needs_user_review = run_chapter_quality_pipeline(full_text, context)
            draft["chapter_text"] = revised_text
            draft["quality_reports"] = quality_reports
            draft["needs_user_review"] = needs_user_review
            draft["current_words"] = chapter_word_count(revised_text)
            segment = default_segment()
            segment["segment_no"] = 1
            segment["status"] = "done"
            segment["text"] = revised_text
            segment["quality_reports"] = quality_reports
            draft["segments"] = [segment]
        else:
            from backend.app.pipeline import (
                generate_chapter_segment_plan,
                generate_chapter_segment,
                extract_segment_updates,
                run_segment_quality_pipeline,
                build_segment_continuity_from_ending_state,
            )
            from backend.app.storage import default_beat, default_ending_state, load_continuity
            plan = generate_chapter_segment_plan(state, chapter_no, target_words, instruction)
            draft["segment_plan"] = plan
            continuity = state.get("continuity") or load_continuity()
            draft["segment_continuity"] = continuity.get("continuity_contract", default_continuity_contract())
            first_beat = plan.get("beats", [default_beat(1)])[0]
            self._send_sse_event("segment_start", {
                "segment_no": 1,
                "total_segments": plan.get("estimated_segments", 1),
                "beat_purpose": first_beat.get("purpose", ""),
            })
            full_text = ""
            try:
                for chunk in generate_chapter_segment_stream(context, draft, 1, first_beat):
                    if chunk:
                        full_text += chunk
                        self._send_sse_event("chunk", {"text": chunk, "segment_no": 1})
            except Exception as exc:
                self._send_sse_event("error", {"message": str(exc)})
                return
            prev_ending = continuity.get("ending_state", default_ending_state())
            revised_text, quality_reports, needs_revision = run_segment_quality_pipeline(
                full_text, context, 1, first_beat,
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
        with FILE_LOCK:
            save_json(now_chapter_path(chapter_no), draft)
        is_complete = chapter_draft_complete(draft)
        self._send_sse_event("done", {
            "chapter_no": chapter_no,
            "generation_mode": draft.get("generation_mode"),
            "current_words": draft.get("current_words", 0),
            "segments_count": len(draft.get("segments", [])),
            "estimated_segments": draft.get("segment_plan", {}).get("estimated_segments", 1),
            "is_complete": is_complete,
            "needs_user_review": draft.get("needs_user_review", False),
        })

    def handle_continue_chapter(self, body: Dict[str, Any]) -> None:
        chapter_no = int(body.get("chapter_no") or 1)
        with FILE_LOCK:
            state = read_state()
            chapter_path = now_chapter_path(chapter_no)
            if not chapter_path.exists():
                self._send_json(HTTPStatus.NOT_FOUND, {"error": f"chapter {chapter_no} draft not found"})
                return
            draft = load_json(chapter_path, {})
        if draft.get("generation_mode") != "multi_segment":
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "only multi_segment chapters can be continued"})
            return
        if chapter_draft_complete(draft):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "chapter draft is already complete, use finish-chapter"})
            return
        draft = continue_chapter_generation(state, draft)
        with FILE_LOCK:
            save_json(now_chapter_path(chapter_no), draft)
        is_complete = chapter_draft_complete(draft)
        self._send_json(HTTPStatus.OK, {
            "ok": True,
            "chapter_no": chapter_no,
            "chapter_text": draft.get("chapter_text", ""),
            "current_words": draft.get("current_words", 0),
            "segments_count": len(draft.get("segments", [])),
            "estimated_segments": draft.get("segment_plan", {}).get("estimated_segments", 1),
            "is_complete": is_complete,
            "needs_user_review": draft.get("needs_user_review", False),
            "quality_reports": draft.get("segments", [])[-1].get("quality_reports", []) if draft.get("segments") else [],
        })

    def handle_continue_chapter_stream(self, body: Dict[str, Any]) -> None:
        chapter_no = int(body.get("chapter_no") or 1)
        with FILE_LOCK:
            state = read_state()
            chapter_path = now_chapter_path(chapter_no)
            if not chapter_path.exists():
                self._send_json(HTTPStatus.NOT_FOUND, {"error": f"chapter {chapter_no} draft not found"})
                return
            draft = load_json(chapter_path, {})
        if draft.get("generation_mode") != "multi_segment":
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "only multi_segment chapters can be continued"})
            return
        if chapter_draft_complete(draft):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "chapter draft is already complete, use finish-chapter"})
            return
        from backend.app.pipeline import (
            generate_chapter_segment,
            extract_segment_updates,
            run_segment_quality_pipeline,
            build_segment_continuity_from_ending_state,
        )
        from backend.app.storage import default_beat, default_ending_state
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
        instruction = draft.get("instruction", "")
        tone = draft.get("tone", "")
        target_words = draft.get("target_words", 1800)
        context = build_generation_context(state, chapter_no, instruction, tone, target_words)
        self._send_sse_headers()
        self._send_sse_event("segment_start", {
            "segment_no": next_segment_no,
            "total_segments": plan.get("estimated_segments", next_segment_no),
            "beat_purpose": next_beat.get("purpose", ""),
        })
        full_text = ""
        try:
            for chunk in generate_chapter_segment_stream(context, draft, next_segment_no, next_beat):
                if chunk:
                    full_text += chunk
                    self._send_sse_event("chunk", {"text": chunk, "segment_no": next_segment_no})
        except Exception as exc:
            self._send_sse_event("error", {"message": str(exc)})
            return
        segment_continuity = draft.get("segment_continuity", default_continuity_contract())
        prev_ending = default_ending_state()
        if segments:
            prev_ending = segments[-1].get("ending_state", default_ending_state())
        revised_text, quality_reports, needs_revision = run_segment_quality_pipeline(
            full_text, context, next_segment_no, next_beat,
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
        draft["segments"].append(segment)
        draft["segment_continuity"] = build_segment_continuity_from_ending_state(
            segment_updates.get("ending_state", default_ending_state()),
        )
        draft["chapter_text"] = assemble_full_chapter(draft)
        draft["current_words"] = chapter_word_count(draft["chapter_text"])
        if needs_revision:
            draft["needs_user_review"] = True
        with FILE_LOCK:
            save_json(now_chapter_path(chapter_no), draft)
        is_complete = chapter_draft_complete(draft)
        self._send_sse_event("done", {
            "chapter_no": chapter_no,
            "current_words": draft.get("current_words", 0),
            "segments_count": len(draft.get("segments", [])),
            "estimated_segments": plan.get("estimated_segments", next_segment_no),
            "is_complete": is_complete,
            "needs_user_review": draft.get("needs_user_review", False),
        })

    def handle_finish_chapter(self, body: Dict[str, Any]) -> None:
        chapter_no = int(body.get("chapter_no") or 1)
        force_end = bool(body.get("force_end", False))
        with FILE_LOCK:
            chapter_path = now_chapter_path(chapter_no)
            if not chapter_path.exists():
                self._send_json(HTTPStatus.NOT_FOUND, {"error": f"chapter {chapter_no} draft not found"})
                return
            draft = load_json(chapter_path, {})
        if not chapter_draft_complete(draft, user_force_end=force_end):
            self._send_json(HTTPStatus.BAD_REQUEST, {
                "error": "chapter draft is not complete yet. Use continue-chapter to add more segments, or set force_end=true",
            })
            return
        draft["chapter_text"] = assemble_full_chapter(draft)
        draft["current_words"] = chapter_word_count(draft["chapter_text"])
        draft["status"] = "draft_complete"
        with FILE_LOCK:
            save_json(now_chapter_path(chapter_no), draft)
        self._send_json(HTTPStatus.OK, {
            "ok": True,
            "chapter_no": chapter_no,
            "chapter_text": draft.get("chapter_text", ""),
            "current_words": draft.get("current_words", 0),
            "segments_count": len(draft.get("segments", [])),
            "status": "draft_complete",
        })

    def handle_auto_generate_chapters_stream(self, body: Dict[str, Any]) -> None:
        start_chapter_no = int(body.get("start_chapter_no") or 1)
        count = int(body.get("count") or 3)
        instruction = str(body.get("instruction", "")).strip()
        auto_confirm = bool(body.get("auto_confirm", True))
        if count < 1:
            count = 1
        if count > 20:
            count = 20
        with FILE_LOCK:
            state = read_state()
            if not outline_is_confirmed(state.get("outline", {})):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "outline must be confirmed first"})
                return
        self._send_sse_headers()
        self._send_sse_event("phase", {"phase": "planning", "message": "正在规划章节..."})
        try:
            plan = plan_auto_chapters(state, start_chapter_no, count, instruction)
        except Exception as exc:
            self._send_sse_event("error", {"error": f"规划失败: {str(exc)}"})
            return
        self._send_sse_event("plan", {"chapters": plan, "total": len(plan)})
        from backend.app.pipeline import (
            generate_chapter_segment_plan,
            extract_segment_updates,
            run_segment_quality_pipeline,
            run_chapter_quality_pipeline,
            build_segment_continuity_from_ending_state,
        )
        from backend.app.storage import default_beat, default_ending_state, load_continuity
        for idx, chapter_info in enumerate(plan):
            chapter_no = chapter_info["chapter_no"]
            target_words = chapter_info["target_words"]
            chapter_tone = chapter_info["tone"]
            chapter_instruction = chapter_info["instruction"]
            title_hint = chapter_info["title_hint"]
            self._send_sse_event("chapter_start", {
                "chapter_no": chapter_no,
                "index": idx + 1,
                "total": len(plan),
                "title_hint": title_hint,
                "goal": chapter_info["goal"],
                "target_words": target_words,
            })
            with FILE_LOCK:
                state = read_state()
            drain_generation_memory(state)
            generation_mode = "multi_segment" if target_words > 4000 else "single_segment"
            draft = default_chapter_draft(chapter_no)
            draft["generation_mode"] = generation_mode
            draft["target_words"] = target_words
            draft["instruction"] = chapter_instruction
            draft["tone"] = chapter_tone
            draft["length_target"] = target_words
            draft["generated_at"] = utc_now()
            context = build_generation_context(state, chapter_no, chapter_instruction, chapter_tone, target_words)
            chapter_task_content = json.dumps(context.get("chapter_task", {}), ensure_ascii=False, indent=2)
            draft["chapter_task_content"] = chapter_task_content
            if generation_mode == "single_segment":
                memory = state["conversation_memory"]
                current_user_content = json.dumps(context, ensure_ascii=False, indent=2)
                system_prompt = build_chapter_system_prompt(target_words)
                messages = build_chapter_messages_with_history(system_prompt, current_user_content, memory)
                full_text = ""
                try:
                    for chunk in deepseek_chat_stream(messages, temperature=0.85):
                        if chunk:
                            full_text += chunk
                            self._send_sse_event("chunk", {"text": chunk, "chapter_no": chapter_no})
                except Exception as exc:
                    self._send_sse_event("chapter_error", {"chapter_no": chapter_no, "error": str(exc)})
                    continue
                revised_text, quality_reports, needs_user_review = run_chapter_quality_pipeline(full_text, context)
                draft["chapter_text"] = revised_text
                draft["quality_reports"] = quality_reports
                draft["needs_user_review"] = needs_user_review
                draft["current_words"] = chapter_word_count(revised_text)
                draft["title"] = title_hint or f"第{chapter_no}章"
                segment = default_segment()
                segment["segment_no"] = 1
                segment["status"] = "done"
                segment["text"] = revised_text
                segment["quality_reports"] = quality_reports
                draft["segments"] = [segment]
            else:
                seg_plan = generate_chapter_segment_plan(state, chapter_no, target_words, chapter_instruction)
                draft["segment_plan"] = seg_plan
                continuity = state.get("continuity") or load_continuity()
                draft["segment_continuity"] = continuity.get("continuity_contract", default_continuity_contract())
                estimated_segments = seg_plan.get("estimated_segments", 2)
                segments_done = 0
                while True:
                    next_seg_no = segments_done + 1
                    next_beat = None
                    for beat in seg_plan.get("beats", []):
                        if beat.get("beat_no", 0) == next_seg_no:
                            next_beat = beat
                            break
                    if next_beat is None:
                        if seg_plan.get("beats") and next_seg_no <= len(seg_plan["beats"]):
                            next_beat = seg_plan["beats"][next_seg_no - 1]
                        else:
                            next_beat = default_beat(next_seg_no)
                            next_beat["purpose"] = "继续推进本章情节"
                    self._send_sse_event("segment_start", {
                        "chapter_no": chapter_no,
                        "segment_no": next_seg_no,
                        "total_segments": estimated_segments,
                        "beat_purpose": next_beat.get("purpose", ""),
                    })
                    seg_text = ""
                    try:
                        for chunk in generate_chapter_segment_stream(context, draft, next_seg_no, next_beat):
                            if chunk:
                                seg_text += chunk
                                self._send_sse_event("chunk", {"text": chunk, "chapter_no": chapter_no, "segment_no": next_seg_no})
                    except Exception as exc:
                        self._send_sse_event("chapter_error", {"chapter_no": chapter_no, "error": str(exc)})
                        break
                    if not seg_text:
                        break
                    segment_continuity = draft.get("segment_continuity", default_continuity_contract())
                    prev_ending = default_ending_state()
                    if draft.get("segments"):
                        prev_ending = draft["segments"][-1].get("ending_state", default_ending_state())
                    revised_seg, seg_quality, needs_revision = run_segment_quality_pipeline(
                        seg_text, context, next_seg_no, next_beat,
                        segment_continuity, prev_ending,
                    )
                    seg_updates = extract_segment_updates(state, chapter_no, revised_seg, next_seg_no, next_beat)
                    seg = default_segment()
                    seg["segment_no"] = next_seg_no
                    seg["status"] = "done"
                    seg["text"] = revised_seg
                    seg["summary"] = seg_updates.get("segment_summary", "")
                    seg["ending_state"] = seg_updates.get("ending_state", default_ending_state())
                    seg["quality_reports"] = seg_quality
                    draft["segments"].append(seg)
                    draft["segment_continuity"] = build_segment_continuity_from_ending_state(
                        seg_updates.get("ending_state", default_ending_state()),
                    )
                    draft["chapter_text"] = assemble_full_chapter(draft)
                    draft["current_words"] = chapter_word_count(draft["chapter_text"])
                    segments_done = next_seg_no
                    if chapter_draft_complete(draft):
                        break
                    if next_seg_no >= estimated_segments:
                        break
                    with FILE_LOCK:
                        state = read_state()
                    context = build_generation_context(state, chapter_no, chapter_instruction, chapter_tone, target_words)
                draft["title"] = title_hint or f"第{chapter_no}章"
            with FILE_LOCK:
                save_json(now_chapter_path(chapter_no), draft)
            chapter_text = draft.get("chapter_text", "")
            wc = draft.get("current_words", 0)
            self._send_sse_event("chapter_done", {
                "chapter_no": chapter_no,
                "title": draft.get("title", f"第{chapter_no}章"),
                "chapter_text": chapter_text,
                "current_words": wc,
                "status": "draft",
                "needs_user_review": draft.get("needs_user_review", False),
                "index": idx + 1,
                "total": len(plan),
            })
            if auto_confirm and chapter_text:
                with FILE_LOCK:
                    state = read_state()
                    chapter_path = now_chapter_path(chapter_no)
                    chapter_record = load_json(chapter_path, {})
                updates = extract_chapter_updates(state, chapter_no, chapter_text)
                ch_title = chapter_title_from_updates(chapter_no, updates) or draft.get("title", f"第{chapter_no}章")
                with FILE_LOCK:
                    state = read_state()
                    storyline = update_storyline(state["storyline"], chapter_no, ch_title, updates)
                    characters = merge_character_updates(
                        state["characters"],
                        list(updates.get("character_updates", [])),
                        list(updates.get("new_characters", [])),
                    )
                    memory = load_json(CONVERSATION_PATH, default_conversation_memory())
                    memory["recent_turns"] = [
                        t for t in memory.get("recent_turns", [])
                        if not (t.get("type") == "chapter" and t.get("chapter_no") == chapter_no)
                    ]
                    summary = f"已确认第{chapter_no}章《{ch_title}》。"
                    if wc:
                        summary += f" 约 {wc} 字。"
                    memory = append_turn(
                        memory,
                        f"确认第{chapter_no}章。{chapter_instruction}".strip(),
                        summary,
                        turn_type="chapter",
                        chapter_no=chapter_no,
                        extra_fields={
                            "confirmed": True,
                            "user_prompt_text": chapter_task_content,
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
                        "title": ch_title,
                        "status": "confirmed",
                        "confirmed_at": utc_now(),
                        "chapter_summary": updates.get("chapter_summary", []),
                        "new_events": updates.get("new_events", []),
                        "open_threads": updates.get("open_threads", []),
                        "resolved_threads": updates.get("resolved_threads", []),
                        "character_updates": updates.get("character_updates", []),
                    })
                    save_json(CHARACTERS_PATH, characters)
                    save_json(STORYLINE_PATH, storyline)
                    memory = compact_chapter_turns(memory)
                    save_json(CONVERSATION_PATH, memory)
                    save_json(chapter_path, chapter_record)
                update_continuity_from_updates(updates)
                create_snapshot(f"autogen_ch{chapter_no}")
                self._send_sse_event("chapter_confirmed", {
                    "chapter_no": chapter_no,
                    "title": ch_title,
                    "current_words": wc,
                    "index": idx + 1,
                    "total": len(plan),
                })
        self._send_sse_event("all_done", {
            "total_chapters": len(plan),
            "auto_confirmed": auto_confirm,
        })
