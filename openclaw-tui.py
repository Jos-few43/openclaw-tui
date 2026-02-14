#!/usr/bin/env python3
"""OpenClaw TUI — live dashboard and provider manager."""
from __future__ import annotations

import json
import queue
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── Paths ──────────────────────────────────────────────────────────────
HOME = Path.home()
OPENCLAW_JSON   = HOME / ".openclaw/openclaw.json"
AUTH_PROFILES   = HOME / ".openclaw/agents/main/agent/auth-profiles.json"
LOG_DIR         = Path("/tmp/openclaw")

def log_path() -> Path:
    today = datetime.now().strftime("%Y-%m-%d")
    return LOG_DIR / f"openclaw-{today}.log"

# ── ASCII banner ────────────────────────────────────────────────────────
BANNER = r"""
  ██████╗ ██████╗ ███████╗███╗  ██╗ ██████╗██╗      █████╗ ██╗
 ██╔═══██╗██╔══██╗██╔════╝████╗ ██║██╔════╝██║     ██╔══██╗██║
 ██║   ██║██████╔╝█████╗  ██╔██╗██║██║     ██║     ███████║██║
 ╚██████╔╝██║     ███████╗██║ ╚███║╚██████╗███████╗██║  ██║██║
  ╚═════╝ ╚═╝     ╚══════╝╚═╝  ╚══╝ ╚═════╝╚══════╝╚═╝  ╚═╝╚═╝""".lstrip("\n")

if __name__ == "__main__":
    print("scaffold ok")
