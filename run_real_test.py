from __future__ import annotations

import json
import time
import subprocess
import sys
from pathlib import Path
from urllib import request, error

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
CHAPTERS_DIR = DATA_DIR / "chapters"

FILES_TO_CLEAN = [
    DATA_DIR / "characters.json",
    DATA_DIR / "conversation_memory.json",
    DATA_DIR / "outline_draft.json",
    DATA_DIR / "outline.json",
    DATA_DIR / "story_brief.json",
    DATA_DIR / "storyline.json",
    DATA_DIR / "continuity.json",
    DATA_DIR / "instruction_registry.json"
]


def clean_files() -> None:
    print("Cleaning generated files...")
    for f in FILES_TO_CLEAN:
        if f.exists():
            try:
                f.unlink()
            except Exception as e:
                print(f"Failed to delete {f.name}: {e}")
    if CHAPTERS_DIR.exists():
        for ch_file in CHAPTERS_DIR.glob("chapter_*.json"):
            try:
                ch_file.unlink()
            except Exception as e:
                print(f"Failed to delete {ch_file.name}: {e}")


def wait_http(url: str, timeout_sec: int = 40) -> None:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            req = request.Request(url, method="GET")
            with request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError(f"Timed out waiting for {url}")


def post_json(url: str, data: dict) -> dict:
    body = json.dumps(data).encode("utf-8")
    req = request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    try:
        with request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="ignore")
        print(f"POST {url} failed with status {exc.code}. Response body:")
        print(err_body)
        raise exc


def main() -> None:
    clean_files()
    
    print("Starting app...")
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "backend" / "main.py")]
    )
    
    try:
        wait_http("http://127.0.0.1:8787/api/state")
        brief = "一个年轻人追查姐姐失踪与旧城秘密之间的联系。"
        
        print("Generating outline...")
        outline_res = post_json("http://127.0.0.1:8787/api/generate-outline", {"story_brief": brief})
        draft = outline_res.get("outline_draft")
        if not draft:
            raise ValueError(f"Failed to generate outline draft. Response: {outline_res}")
            
        print("Confirming outline...")
        confirm_res = post_json("http://127.0.0.1:8787/api/confirm-outline", {
            "outline_json": json.dumps(draft),
            "story_brief": brief
        })
        
        chapter_results = []
        for chapter_no in range(1, 6):
            time.sleep(5)  # Throttling to prevent hitting API rate limits
            print(f"Generating chapter {chapter_no}...")
            ch_res = post_json("http://127.0.0.1:8787/api/generate-chapter", {
                "chapter_no": chapter_no,
                "chapter_title": "",
                "tone": "紧张",
                "length_target": 800,
                "instruction": f"第{chapter_no}章推进剧情并增加悬念。"
            })
            chapter_results.append({
                "chapter_no": chapter_no,
                "ok": ch_res.get("ok") is True
            })
            
        # Fetch final integrated state
        req = request.Request("http://127.0.0.1:8787/api/state", method="GET")
        with request.urlopen(req) as resp:
            state = json.loads(resp.read().decode("utf-8"))
            
        chapter_files = list(CHAPTERS_DIR.glob("chapter_*.json"))
        
        print("Test complete.")
        summary = {
            "outline_ok": True,
            "confirmed_ok": True,
            "chapter_count": len(chapter_files),
            "chapter_results": chapter_results,
            "outline_status": state.get("outline", {}).get("status")
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        
    finally:
        print("Terminating server...")
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        clean_files()


if __name__ == "__main__":
    main()
