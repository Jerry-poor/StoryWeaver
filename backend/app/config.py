from __future__ import annotations

import os
import threading
from pathlib import Path

# Paths resolved relative to repository root
BACKEND_APP_DIR = Path(__file__).resolve().parent
BACKEND_DIR = BACKEND_APP_DIR.parent
REPO_ROOT = BACKEND_DIR.parent

ENV_PATH = REPO_ROOT / ".env"


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


# Initialize environment variables
load_env_file()

DATA_DIR = REPO_ROOT / "data"
CHAPTER_DIR = DATA_DIR / "chapters"
SNAPSHOT_DIR = DATA_DIR / "snapshots"
STATIC_DIR = REPO_ROOT / "frontend"

OUTLINE_PATH = DATA_DIR / "outline.json"
OUTLINE_DRAFT_PATH = DATA_DIR / "outline_draft.json"
STORY_BRIEF_PATH = DATA_DIR / "story_brief.json"
CHARACTERS_PATH = DATA_DIR / "characters.json"
STORYLINE_PATH = DATA_DIR / "storyline.json"
CONVERSATION_PATH = DATA_DIR / "conversation_memory.json"
INSTRUCTION_REGISTRY_PATH = DATA_DIR / "instruction_registry.json"
CONTINUITY_PATH = DATA_DIR / "continuity.json"
LLM_SETTINGS_PATH = DATA_DIR / "llm_settings.json"

SINGLE_SEGMENT_THRESHOLD = 4000
DEFAULT_SEGMENT_TARGET_WORDS = 3000
DEFAULT_MAX_SEGMENT_TOKENS = 7500
DEFAULT_MAX_SEGMENTS = 8

DEEPSEEK_BASE_URL = os.getenv(
    "DEEPSEEK_BASE_URL",
    os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com/v1"),
).rstrip("/")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", os.getenv("OPENAI_API_KEY", ""))
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_MAX_TOKENS = int(os.getenv("DEEPSEEK_MAX_TOKENS", "8192"))
DEFAULT_PORT = int(os.getenv("PORT", "8787"))
MAX_RECENT_TURNS = 10
KV_WINDOW = int(os.getenv("KV_WINDOW", "5"))          # 注入 KV-cache 的最近完整章数
ARC_SIZE = int(os.getenv("ARC_SIZE", "5"))             # 每 N 章合并为一条 arc_summary
CHAT_WINDOW = int(os.getenv("CHAT_WINDOW", "6"))       # 热 chat turns 保留数
COMPRESS_THRESHOLD = int(os.getenv("COMPRESS_THRESHOLD", "9"))  # 超过这个数触发 chat 压缩

FILE_LOCK = threading.Lock()
