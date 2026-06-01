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
    with request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def test_auto_generate_stream(url: str, payload: dict) -> None:
    print("Initiating auto-generate chapters stream request...")
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "text/event-stream")
    
    with request.urlopen(req, timeout=300) as resp:
        print("Connected to stream. Parsing Server-Sent Events (SSE)...")
        buffer = ""
        while True:
            chunk = resp.read(1024)
            if not chunk:
                break
            buffer += chunk.decode("utf-8")
            while "\n\n" in buffer:
                event_block, buffer = buffer.split("\n\n", 1)
                event_type = "message"
                data_payload = ""
                for line in event_block.splitlines():
                    if line.startswith("event:"):
                        event_type = line[6:].strip()
                    elif line.startswith("data:"):
                        data_payload = line[5:].strip()
                
                print(f"SSE Event [type={event_type}]: {data_payload[:120]}...")
                if event_type == "error":
                    raise RuntimeError(f"Stream returned error event: {data_payload}")


def main() -> None:
    clean_files()
    
    print("Starting app...")
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "backend" / "main.py")]
    )
    
    try:
        wait_http("http://127.0.0.1:8787/api/state")
        brief = "一个追寻真相的侦探，在古老图书馆发现了一本预言自己死亡的神秘手稿。"
        
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
        
        print("Running batch auto-generate chapters E2E ESE test (2 chapters)...")
        test_auto_generate_stream(
            "http://127.0.0.1:8787/api/auto-generate-chapters?stream=1",
            {
                "start_chapter_no": 1,
                "count": 2,
                "instruction": "以悬疑色彩为主，每章约1000字左右",
                "auto_confirm": True
            }
        )
        
        print("E2E Batch Auto Generation completed successfully!")
        
        # Verify that chapters were created
        chapter_files = sorted(list(CHAPTERS_DIR.glob("chapter_*.json")))
        print(f"Verified chapter files created in {CHAPTERS_DIR.name}: {[f.name for f in chapter_files]}")
        assert len(chapter_files) == 2, f"Expected 2 chapters to be generated, found {len(chapter_files)}"
        
        # Verify their status is confirmed
        for f in chapter_files:
            with f.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
                print(f"Verified {f.name} status: {data.get('status')} (Title: {data.get('title')})")
                assert data.get("status") == "confirmed", f"Expected chapter status to be confirmed, got {data.get('status')}"
        
        print("All E2E ESE assertions passed successfully!")
        
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
