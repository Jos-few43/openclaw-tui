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

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.screen import Screen, ModalScreen
from textual.widgets import DataTable, Footer, Header, RichLog, Static, Input, Label, ListView, ListItem
from textual.containers import Container, Horizontal, Vertical, ScrollableContainer
from textual.reactive import reactive
from rich.text import Text
from rich.style import Style

APP_CSS = """
Screen {
    background: #0a0a0f;
}

#banner {
    color: $accent;
    height: auto;
    padding: 0 1;
}

#gateway-status {
    dock: right;
    width: 22;
    height: auto;
    padding: 0 1;
    color: $text-muted;
}

#main-grid {
    height: 1fr;
    layout: horizontal;
}

#model-panel {
    width: 62%;
    border: tall $panel;
    padding: 0 1;
}

#auth-panel {
    width: 38%;
    border: tall $panel;
    padding: 0 1;
    overflow-y: auto;
}

#log-panel {
    height: 9;
    border: tall $panel;
    padding: 0 1;
}

#log-header {
    dock: top;
    height: 1;
    color: $text-muted;
}

#model-table {
    height: 1fr;
}

.panel-title {
    color: $text;
    text-style: bold;
    padding-bottom: 1;
}

/* Overlays */
SwitchModelScreen, ClearCooldownScreen, RestartConfirmScreen, AddProviderWizard {
    align: center middle;
}

.modal-box {
    width: 44;
    border: double $accent;
    background: #12121a;
    padding: 1 2;
}

.modal-title {
    text-style: bold;
    color: $accent;
    text-align: center;
    padding-bottom: 1;
}

.modal-hint {
    color: $text-muted;
    text-align: center;
    padding-bottom: 1;
}

.modal-footer {
    color: $text-muted;
    text-align: center;
    padding-top: 1;
}

/* Provider screen */
#provider-list {
    width: 35%;
    border: tall $panel;
    padding: 0 1;
}

#provider-detail {
    width: 65%;
    border: tall $panel;
    padding: 0 1;
}
"""

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

def read_auth_profiles(raw: dict | None = None) -> list[dict]:
    """Read auth-profiles.json and return per-profile dicts with cooldown state."""
    if raw is None:
        try:
            raw = json.loads(AUTH_PROFILES.read_text())
        except Exception:
            return []

    profiles_raw = raw.get("profiles", {})
    usage = raw.get("usageStats", {})
    now_ms = datetime.now(timezone.utc).timestamp() * 1000

    result = []
    for pid, pdata in profiles_raw.items():
        stats = usage.get(pid, {})
        cooldown_until = stats.get("cooldownUntil", 0)
        in_cooldown = cooldown_until > now_ms
        cooldown_remaining_ms = max(0, cooldown_until - now_ms) if in_cooldown else 0

        result.append({
            "profile_id": pid,
            "provider": pdata.get("provider", pid.split(":")[0]),
            "auth_type": pdata.get("type", "unknown"),
            "email": pdata.get("email"),
            "api_key": pdata.get("apiKey", "")[:8] + "..." if pdata.get("apiKey") else None,
            "expires_ms": pdata.get("expires"),
            "in_cooldown": in_cooldown,
            "cooldown_remaining_ms": cooldown_remaining_ms,
            "error_count": stats.get("errorCount", 0),
            "last_used_ms": stats.get("lastUsed"),
        })
    return result


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


LOG_IMPORTANT_SUBSYSTEMS = {"model", "ratelimit", "fallback", "error", "gateway/reload"}

def _parse_log_line(raw_line: str) -> dict | None:
    """Parse a single JSONL log line into a display-ready dict."""
    try:
        obj = json.loads(raw_line)
        meta = obj.get("_meta", {})
        # subsystem is encoded in the "name" field as JSON string
        name_raw = meta.get("name", "{}")
        try:
            name_obj = json.loads(name_raw) if isinstance(name_raw, str) else name_raw
            subsystem = name_obj.get("subsystem", "unknown").removeprefix("gateway/")
        except Exception:
            subsystem = str(name_raw)

        msg_raw = obj.get("1", "")
        msg = msg_raw if isinstance(msg_raw, str) else json.dumps(msg_raw)
        level = meta.get("logLevelName", "INFO").upper()
        ts = obj.get("time", "")[:19].replace("T", " ")

        return {
            "time": ts,
            "subsystem": subsystem,
            "message": msg,
            "level": level,
            "important": subsystem in LOG_IMPORTANT_SUBSYSTEMS or level == "ERROR",
        }
    except Exception:
        return None

class LogTailer:
    """Background thread that tails the gateway JSONL log."""

    def __init__(self, q: queue.Queue):
        self._q = q
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            path = log_path()
            if not path.exists():
                self._stop.wait(2)
                continue
            try:
                with open(path, "r") as f:
                    f.seek(0, 2)  # seek to end
                    while not self._stop.is_set():
                        line = f.readline()
                        if line:
                            parsed = _parse_log_line(line.strip())
                            if parsed:
                                self._q.put(parsed)
                        else:
                            self._stop.wait(0.2)
            except Exception:
                self._stop.wait(1)


class DashboardScreen(Screen):
    BINDINGS = [
        Binding("ctrl+s", "switch_model", "Switch model"),
        Binding("ctrl+r", "restart_gateway", "Restart gateway"),
        Binding("ctrl+c", "clear_cooldown", "Clear cooldown", show=True),
        Binding("ctrl+v", "toggle_verbose", "Toggle verbose"),
        Binding("ctrl+p", "goto_providers", "Providers"),
        Binding("ctrl+q", "quit_app", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Static("DASHBOARD - placeholder", id="banner")

    def action_goto_providers(self): self.app.push_screen("providers")
    def action_quit_app(self): self.app.exit()
    def action_toggle_verbose(self): pass
    def action_switch_model(self): pass
    def action_restart_gateway(self): pass
    def action_clear_cooldown(self): pass


class ProviderScreen(Screen):
    BINDINGS = [
        Binding("ctrl+n", "new_provider", "New provider"),
        Binding("ctrl+d", "remove_provider", "Remove"),
        Binding("ctrl+q", "go_back", "Back"),
    ]

    def compose(self) -> ComposeResult:
        yield Static("PROVIDERS - placeholder", id="banner")

    def action_go_back(self): self.app.pop_screen()
    def action_new_provider(self): pass
    def action_remove_provider(self): pass


class OpenClawTUI(App):
    CSS = APP_CSS
    SCREENS = {"dashboard": DashboardScreen, "providers": ProviderScreen}

    def on_mount(self):
        self.push_screen("dashboard")


if __name__ == "__main__":
    OpenClawTUI().run()
