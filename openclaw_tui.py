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

def _shorten(model_id: str) -> str:
    """gemini-cli/gemini-3-flash instead of google-gemini-cli/gemini-3-flash"""
    parts = model_id.split("/")
    if len(parts) == 2:
        provider = parts[0].removeprefix("google-")
        return f"{provider}/{parts[1]}"
    return model_id

def parse_model_status(raw: dict) -> dict:
    """Parse the JSON from `openclaw models status --json` into UI-ready structs."""
    default = raw.get("defaultModel", "")
    fallbacks = raw.get("fallbacks", [])
    aliases_inv = {v: k for k, v in raw.get("aliases", {}).items()}

    rotation = []
    for i, model in enumerate([default] + fallbacks):
        if i == 0:
            status = "ACTIVE"
        else:
            status = f"#{i}"
        rotation.append({
            "model": model,
            "label": _shorten(model),
            "status": status,
            "position": i,
            "alias": aliases_inv.get(model),
        })

    oauth_profiles = []
    for p in raw.get("auth", {}).get("oauth", {}).get("profiles", []):
        oauth_profiles.append({
            "profile_id": p.get("profileId", ""),
            "provider": p.get("provider", ""),
            "status": p.get("status", ""),
            "expires_at": p.get("expiresAt"),
            "remaining_ms": p.get("remainingMs", 0),
        })

    return {
        "default": default,
        "rotation": rotation,
        "oauth_profiles": oauth_profiles,
        "aliases": raw.get("aliases", {}),
        "raw": raw,
    }

def fetch_model_status() -> dict | None:
    """Run `openclaw models status --json` and return parsed result."""
    try:
        result = subprocess.run(
            ["openclaw", "models", "status", "--json"],
            capture_output=True, text=True, timeout=8
        )
        # Strip any non-JSON preamble lines (openclaw prints env info to stdout)
        lines = result.stdout.strip().splitlines()
        json_start = next((i for i, l in enumerate(lines) if l.startswith("{")), None)
        if json_start is None:
            return None
        raw = json.loads("\n".join(lines[json_start:]))
        return parse_model_status(raw)
    except Exception:
        return None


if __name__ == "__main__":
    print("scaffold ok")
