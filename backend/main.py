from __future__ import annotations

import sys
from pathlib import Path

# Add repository root to sys.path to support direct executions
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from http.server import ThreadingHTTPServer

# Modular imports
from backend.app.config import DEFAULT_PORT, DEEPSEEK_BASE_URL
from backend.app.storage import ensure_dirs, ensure_default_files
from backend.app.handlers import Handler


def main() -> None:
    # Validate workspace folders and load default JSON schemas
    ensure_dirs()
    ensure_default_files()
    
    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer(("0.0.0.0", DEFAULT_PORT), Handler)
    print(f"Novel writer server running on http://localhost:{DEFAULT_PORT}")
    print(f"DeepSeek base URL: {DEEPSEEK_BASE_URL}")
    server.serve_forever()


if __name__ == "__main__":
    main()
