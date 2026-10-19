#!/usr/bin/env python3
"""Start Local Accountant on macOS, Windows or Linux. Needs uv: https://docs.astral.sh/uv/

    uv run start.py                                   # open http://127.0.0.1:8765
    uv run start.py --port 9000 --no-browser
    uv run start.py --read statement.pdf [--redact]   # test the statement reader on one PDF/CSV

`uv run` installs the right Python and the locked dependencies (uv.lock) by itself. The first start
also downloads Docling's document models once; after that everything runs offline. The server only
listens on 127.0.0.1 (this computer).
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP = ROOT / "app"


def in_project_env() -> bool:
    """True when running inside the project's uv environment (.venv)."""
    return Path(sys.prefix).resolve() == (ROOT / ".venv").resolve()


def reexec_with_uv() -> None:
    if not shutil.which("uv"):
        sys.exit("uv is required. Install it from https://docs.astral.sh/uv/getting-started/installation/\n"
                 "  macOS/Linux: curl -LsSf https://astral.sh/uv/install.sh | sh\n"
                 "  Windows:     powershell -c \"irm https://astral.sh/uv/install.ps1 | iex\"\n"
                 "then run: uv run start.py")
    sys.exit(subprocess.call(["uv", "run", "--frozen", "python", str(Path(__file__).resolve())] + sys.argv[1:],
                             cwd=str(ROOT)))


def serve(port: int, open_browser: bool) -> None:
    import uvicorn
    # technical details (errors, AI problems) go to this terminal, never to the browser
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    url = "http://127.0.0.1:%d" % port
    if open_browser:
        threading.Thread(target=lambda: (time.sleep(1.5), webbrowser.open(url)), daemon=True).start()
    print("\nLocal Accountant is running at %s  (Ctrl+C to stop)\n" % url)
    uvicorn.run("server:app", host="127.0.0.1", port=port, app_dir=str(APP), log_level="warning")


def main() -> None:
    ap = argparse.ArgumentParser(description="Local Accountant")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--read", metavar="FILE", help="show what the statement reader extracts from a PDF or CSV")
    ap.add_argument("--redact", action="store_true", help="with --read: mask names, merchants and amounts")
    a = ap.parse_args()
    if not in_project_env():
        return reexec_with_uv()

    sys.path.insert(0, str(APP))
    import pdf_import
    pdf_import.ensure_models()  # one-time download; afterwards Docling runs offline
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    if a.read:
        import read_pdf
        return sys.exit(read_pdf.run(str(Path(a.read).expanduser().resolve()), a.redact))
    serve(a.port, not a.no_browser)


if __name__ == "__main__":
    main()
