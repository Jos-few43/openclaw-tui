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
#provider-grid {
    height: 1fr;
    layout: horizontal;
}

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


def make_bar(pct: float, width: int = 18) -> str:
    """Block-character progress bar: ▕████░░░░▏"""
    pct = max(0.0, min(1.0, pct))
    filled = round(pct * width)
    return f"▕{'█' * filled}{'░' * (width - filled)}▏"

def bar_color(pct: float) -> str:
    if pct > 0.5: return "green"
    if pct > 0.2: return "yellow"
    return "red"

CTX_MAP = {
    "google-gemini-cli": "1024k",
    "google-antigravity": "1024k",
    "groq": "128k",
    "qwen-portal": "125k",
    "opencode": "256k",
    "ollama": "125k",
}

def ctx_label(model_id: str, raw_ctx: int | None = None) -> str:
    if raw_ctx:
        if raw_ctx >= 1_000_000: return f"{raw_ctx // 1000}k"
        if raw_ctx >= 1_000: return f"{raw_ctx // 1000}k"
        return str(raw_ctx)
    provider = model_id.split("/")[0]
    # per-model overrides
    if "deepseek" in model_id: return "16k"
    if "gemma2-9b" in model_id: return "8k"
    if "sonnet" in model_id or "claude" in model_id: return "195k"
    return CTX_MAP.get(provider, "???")


class ModelTableWidget(Static):
    rotation: reactive[list] = reactive([])

    def render(self):
        from rich.table import Table
        from rich.text import Text

        t = Table(
            show_header=True, header_style="bold dim",
            box=None, padding=(0, 1), expand=True
        )
        t.add_column("NAME", style="", no_wrap=True, min_width=28)
        t.add_column("CTX", justify="right", width=6)
        t.add_column("STATUS", justify="left", width=10)

        for entry in self.rotation:
            model = entry["model"]
            label = entry["label"]
            status = entry["status"]
            is_active = status == "ACTIVE"
            provider = model.split("/")[0]
            is_local = provider == "ollama"

            name_text = Text()
            if is_active:
                name_text.append("►", style="bold cyan")
                name_text.append(f" {label}", style="bold cyan")
            else:
                name_text.append("  " + label, style="dim white" if not is_local else "green")

            if status == "ACTIVE":
                status_text = Text("[ACTIVE]", style="bold cyan")
            elif is_local:
                status_text = Text("[LOCAL] ", style="bold green")
            elif "COOLDOWN" in status:
                status_text = Text("COOLDOWN", style="bold red")
            else:
                status_text = Text(status.ljust(7), style="dim white")

            t.add_row(name_text, ctx_label(model), status_text)

        return t


class AuthPanelWidget(Static):
    profiles: reactive[list] = reactive([])

    def render(self):
        from rich.console import Group
        from rich.text import Text

        now_ms = datetime.now(timezone.utc).timestamp() * 1000
        lines = [Text(" AUTH ACCOUNTS", style="bold")]

        for p in self.profiles:
            lines.append(Text(""))
            pid = p["profile_id"]
            short = pid.split(":")[-1][:22]
            provider_short = p["provider"].removeprefix("google-")[:14]
            label = f"{provider_short}:{short}"

            if p["in_cooldown"]:
                rem_s = int(p["cooldown_remaining_ms"] / 1000)
                rem_fmt = f"{rem_s // 60}m {rem_s % 60:02d}s"
                lines.append(Text(f"  {label}", style="bold red"))
                lines.append(Text(f"  COOLDOWN  {rem_fmt} remaining", style="red"))
                bar = make_bar(1.0)
                lines.append(Text(f"  {bar}  100%", style="red"))
            elif p["auth_type"] == "apiKey":
                lines.append(Text(f"  {label}", style="yellow"))
                key_hint = p.get("api_key") or "key set"
                lines.append(Text(f"  API KEY  {key_hint}", style="dim yellow"))
                lines.append(Text(f"  {make_bar(0)}  no exp", style="dim"))
            else:
                exp_ms = p.get("expires_ms")
                if exp_ms:
                    rem_ms = max(0, exp_ms - now_ms)
                    # OAuth tokens typically last 1hr = 3_600_000ms
                    pct = min(1.0, rem_ms / 3_600_000)
                    rem_s = int(rem_ms / 1000)
                    if rem_s <= 0:
                        rem_fmt = "EXPIRED"
                        pct = 0.0
                    else:
                        rem_fmt = f"{rem_s // 60}m {rem_s % 60:02d}s"
                    color = bar_color(pct)
                    pct_int = int(pct * 100)
                    lines.append(Text(f"  {label}", style=color))
                    lines.append(Text(f"  expires  {rem_fmt}", style="dim"))
                    lines.append(Text(
                        f"  {make_bar(pct)}  {pct_int}%",
                        style=color
                    ))
                else:
                    lines.append(Text(f"  {label}", style="cyan"))
                    lines.append(Text("  OAuth  no expiry data", style="dim"))

        from rich.console import Group as RGroup
        return RGroup(*lines)


class BannerWidget(Static):
    def render(self):
        from rich.text import Text
        t = Text(BANNER, style="bold cyan")
        return t


class GatewayStatusWidget(Static):
    status: reactive[dict] = reactive({})

    def render(self):
        s = self.status
        pid = s.get("pid", "---")
        sessions = s.get("sessions", "?")
        agents = s.get("agents", "?")
        version = s.get("version", "")
        gw = s.get("gateway", "---")
        lines = [
            f" [dim]{version}[/]",
            f" gateway: [{'green' if gw == 'RUN' else 'red'}]{gw}[/]",
            f" pid: [dim]{pid}[/]",
            f" sessions: [dim]{sessions}[/]",
            f" agents: [dim]{agents}[/]",
        ]
        return "\n".join(lines)


def fetch_gateway_status() -> dict:
    """Parse `openclaw health` output."""
    try:
        r = subprocess.run(["openclaw", "health"], capture_output=True, text=True, timeout=8)
        out = r.stdout
        result = {"gateway": "RUN"}
        for line in out.splitlines():
            if "PID" in line or "pid" in line:
                import re
                m = re.search(r"(\d{4,})", line)
                if m:
                    result["pid"] = m.group(1)
            elif "Session store" in line:
                import re
                m = re.search(r"\((\d+) entries\)", line)
                if m:
                    result["sessions"] = m.group(1)
            elif line.strip().startswith("Agents:"):
                agents = line.split(":", 1)[1].strip()
                result["agents"] = len(agents.split(","))
        ver_r = subprocess.run(["openclaw", "--version"], capture_output=True, text=True, timeout=4)
        result["version"] = ver_r.stdout.strip()[:20]
        return result
    except Exception:
        return {"gateway": "ERR", "pid": "?", "sessions": "?", "agents": "?", "version": "?"}


def _load_providers() -> list[dict]:
    """Read provider list from openclaw.json."""
    try:
        cfg = json.loads(OPENCLAW_JSON.read_text())
        providers_cfg = cfg.get("models", {}).get("providers", {})
        auth_profiles = json.loads(AUTH_PROFILES.read_text())
        result = []
        for pid, pdata in providers_cfg.items():
            oauth_count = sum(
                1 for p in auth_profiles.get("profiles", {}).values()
                if p.get("provider") == pid and p.get("type") == "oauth"
            )
            api_key = pdata.get("apiKey", "")
            if oauth_count:
                auth_tag = f"OAUTH {oauth_count}"
            elif api_key and api_key != "from-auth-profiles":
                auth_tag = "API KEY"
            elif pdata.get("baseUrl", "").startswith("http://127") or pdata.get("baseUrl", "").startswith("http://localhost"):
                auth_tag = "LOCAL  "
            else:
                auth_tag = "CUSTOM "
            result.append({
                "id": pid,
                "base_url": pdata.get("baseUrl", ""),
                "api_key": api_key,
                "models": pdata.get("models", []),
                "auth_tag": auth_tag,
            })
        return result
    except Exception:
        return []


class SwitchModelScreen(ModalScreen):
    BINDINGS = [
        Binding("escape", "dismiss", "Cancel"),
        Binding("enter", "select", "Select"),
    ]

    def __init__(self, rotation: list[dict], **kwargs):
        super().__init__(**kwargs)
        self._rotation = rotation
        self._cursor = 0
        self._filter = ""

    def compose(self) -> ComposeResult:
        with Container(classes="modal-box"):
            yield Static(" SWITCH MODEL", classes="modal-title")
            yield Static(
                " Up/Down navigate   /: filter   Enter: select",
                classes="modal-hint"
            )
            yield Static(id="model-list-body")
            yield Static(" Esc: cancel", classes="modal-footer")

    def on_mount(self):
        self._render_list()

    def _filtered(self) -> list[dict]:
        if not self._filter:
            return self._rotation
        return [e for e in self._rotation if self._filter.lower() in e["label"].lower()]

    def _render_list(self):
        from rich.text import Text
        entries = self._filtered()
        lines = []
        for i, e in enumerate(entries):
            status = e["status"]
            is_active = status == "ACTIVE"
            is_local = e["model"].startswith("ollama/")

            if status == "ACTIVE":
                tag = "[ACTIVE]"
                tag_style = "bold cyan"
            elif is_local:
                tag = "[LOCAL] "
                tag_style = "bold green"
            else:
                tag = f" {status:<7}"
                tag_style = "dim white"

            row = Text()
            if i == self._cursor:
                row.append("►", style="bold cyan")
                row.append(f" {tag} ", style=tag_style)
                row.append(e["label"], style="bold cyan on #1a2a3a")
            else:
                row.append("  ")
                row.append(f" {tag} ", style=tag_style)
                row.append(e["label"], style="dim white" if not is_local else "green")

            lines.append(row)

        from rich.console import Group
        self.query_one("#model-list-body", Static).update(Group(*lines))

    def on_key(self, event):
        entries = self._filtered()
        if event.key == "up":
            self._cursor = max(0, self._cursor - 1)
            self._render_list()
        elif event.key == "down":
            self._cursor = min(len(entries) - 1, self._cursor + 1)
            self._render_list()
        elif event.key == "slash":
            self._filter = ""
            self._render_list()
        elif len(event.key) == 1 and event.key.isprintable() and event.key not in "\n\r":
            # Typing filter
            self._filter += event.key
            self._cursor = 0
            self._render_list()
        elif event.key == "backspace" and self._filter:
            self._filter = self._filter[:-1]
            self._cursor = 0
            self._render_list()

    def action_select(self):
        entries = self._filtered()
        if entries:
            self.dismiss(entries[self._cursor]["model"])

    def action_dismiss_modal(self):
        self.dismiss(None)


class RestartConfirmScreen(ModalScreen):
    BINDINGS = [Binding("escape", "dismiss", "Cancel")]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._cursor = 0

    def compose(self) -> ComposeResult:
        with Container(classes="modal-box"):
            yield Static(" RESTART GATEWAY", classes="modal-title")
            yield Static(
                "\n  Restart the openclaw-gateway service?\n"
                "  Active sessions will be interrupted.\n",
                classes="modal-hint"
            )
            yield Static(id="confirm-body")
            yield Static(" Esc: cancel", classes="modal-footer")

    def on_mount(self): self._render()

    def _render(self):
        from rich.text import Text
        opts = ["Confirm restart", "Cancel"]
        lines = []
        for i, opt in enumerate(opts):
            t = Text()
            if i == self._cursor:
                t.append("►", style="bold cyan")
                t.append(f"  {opt}", style="bold cyan on #1a2a3a")
            else:
                t.append(f"   {opt}", style="dim white")
            lines.append(t)
        from rich.console import Group
        self.query_one("#confirm-body", Static).update(Group(*lines))

    def on_key(self, event):
        if event.key in ("up", "down"):
            self._cursor = 1 - self._cursor
            self._render()
        elif event.key == "enter":
            self.dismiss(self._cursor == 0)


class ClearCooldownScreen(ModalScreen):
    BINDINGS = [Binding("escape", "dismiss", "Cancel")]

    def __init__(self, profiles: list[dict], **kwargs):
        super().__init__(**kwargs)
        self._profiles = [p for p in profiles if p["in_cooldown"]]
        self._cursor = 0

    def compose(self) -> ComposeResult:
        with Container(classes="modal-box"):
            yield Static(" CLEAR COOLDOWN", classes="modal-title")
            yield Static(" Select account to clear", classes="modal-hint")
            yield Static(id="cooldown-list")
            yield Static(" Esc: cancel", classes="modal-footer")

    def on_mount(self):
        if not self._profiles:
            self.query_one("#cooldown-list").update("  No accounts in cooldown")
        else:
            self._render()

    def _render(self):
        from rich.text import Text, Group
        lines = []
        for i, p in enumerate(self._profiles):
            rem_s = int(p["cooldown_remaining_ms"] / 1000)
            rem = f"{rem_s // 60}m {rem_s % 60:02d}s"
            t = Text()
            if i == self._cursor:
                t.append("►", style="bold cyan")
                t.append(f"  {p['profile_id']:<30} {rem}", style="bold red on #1a2a3a")
            else:
                t.append(f"   {p['profile_id']:<30} {rem}", style="red")
            lines.append(t)
        from rich.console import Group as G
        self.query_one("#cooldown-list", Static).update(G(*lines))

    def on_key(self, event):
        if event.key == "up":
            self._cursor = max(0, self._cursor - 1); self._render()
        elif event.key == "down":
            self._cursor = min(len(self._profiles) - 1, self._cursor + 1); self._render()
        elif event.key == "enter" and self._profiles:
            self.dismiss(self._profiles[self._cursor]["profile_id"])


def _clear_cooldown_in_file(profile_id: str):
    """Remove cooldownUntil for a profile in auth-profiles.json."""
    try:
        data = json.loads(AUTH_PROFILES.read_text())
        stats = data.setdefault("usageStats", {})
        if profile_id in stats:
            stats[profile_id].pop("cooldownUntil", None)
            stats[profile_id]["errorCount"] = 0
        AUTH_PROFILES.write_text(json.dumps(data, indent=2))
    except Exception:
        pass


class DataLayer:
    def __init__(self):
        self._log_queue: queue.Queue = queue.Queue(maxsize=500)
        self._tailer = LogTailer(self._log_queue)
        self._status: dict = {}
        self._profiles: list = []

    def start(self):
        self._tailer.start()

    def stop(self):
        self._tailer.stop()

    def refresh(self):
        """Fetch fresh data from openclaw CLI + auth-profiles.json."""
        status = fetch_model_status()
        if status:
            self._status = status
        self._profiles = read_auth_profiles()

    @property
    def rotation(self) -> list:
        return self._status.get("rotation", [])

    @property
    def profiles(self) -> list:
        return self._profiles

    @property
    def default_model(self) -> str:
        return self._status.get("default", "")

    def drain_logs(self) -> list[dict]:
        events = []
        while not self._log_queue.empty():
            try:
                events.append(self._log_queue.get_nowait())
            except queue.Empty:
                break
        return events


class DashboardScreen(Screen):
    BINDINGS = [
        Binding("ctrl+s", "switch_model", "Switch model"),
        Binding("ctrl+r", "restart_gateway", "Restart gateway"),
        Binding("ctrl+c", "clear_cooldown", "Clear cooldown", show=True),
        Binding("ctrl+v", "toggle_verbose", "Toggle verbose"),
        Binding("ctrl+p", "goto_providers", "Providers"),
        Binding("ctrl+q", "quit_app", "Quit"),
    ]

    verbose: reactive[bool] = reactive(False)

    def __init__(self, data: "DataLayer", **kwargs):
        super().__init__(**kwargs)
        self._data = data

    def compose(self) -> ComposeResult:
        with Horizontal(id="header-row"):
            yield BannerWidget(id="banner")
            yield GatewayStatusWidget(id="gateway-status")
        with Horizontal(id="main-grid"):
            with Vertical(id="model-panel"):
                yield Static(" MODEL ROTATION", classes="panel-title")
                yield ModelTableWidget(id="model-table")
            with ScrollableContainer(id="auth-panel"):
                yield AuthPanelWidget(id="auth-widget")
        yield Static(" LOG  [FILTERED]", id="log-header")
        yield RichLog(id="log-panel", highlight=True, markup=True, max_lines=200)
        yield Static(
            " ^S:switch  ^R:restart  ^C:clear-cooldown  ^V:verbose  ^P:providers  ^Q:quit",
            id="footer-bar"
        )

    def on_mount(self):
        self.set_interval(2.0, self._refresh_data)
        self.set_interval(0.5, self._drain_logs)
        self.set_interval(30.0, self._refresh_gateway)
        # Initial load
        self._data.refresh()
        self._refresh_gateway()
        self._update_widgets()

    def _refresh_data(self):
        self._data.refresh()
        self._update_widgets()

    def _update_widgets(self):
        self.query_one("#model-table", ModelTableWidget).rotation = self._data.rotation
        self.query_one("#auth-widget", AuthPanelWidget).profiles = self._data.profiles

    def _refresh_gateway(self):
        status = fetch_gateway_status()
        self.query_one("#gateway-status", GatewayStatusWidget).status = status

    def _drain_logs(self):
        log = self.query_one("#log-panel", RichLog)
        header = self.query_one("#log-header", Static)
        mode = "[VERBOSE]" if self.verbose else "[FILTERED]"
        header.update(f" LOG  {mode}")
        for event in self._data.drain_logs():
            if not self.verbose and not event["important"]:
                continue
            level = event["level"]
            subsys = event["subsystem"]
            msg = event["message"]
            ts = event["time"][11:19]  # HH:MM:SS

            if level == "ERROR" or "error" in subsys:
                style = "red"
            elif subsys in ("ratelimit", "fallback"):
                style = "yellow"
            elif subsys == "model":
                style = "cyan"
            else:
                style = "dim white"

            log.write(
                f"[dim]{ts}[/]  [{style}]{subsys:<12}[/] {msg[:80]}"
            )

    def action_toggle_verbose(self):
        self.verbose = not self.verbose

    def action_goto_providers(self): self.app.push_screen(ProviderScreen(self._data))
    def action_quit_app(self): self.app.exit()
    def action_switch_model(self):
        def on_select(model: str | None):
            if model:
                subprocess.run(["openclaw", "models", "set", model],
                               capture_output=True, timeout=8)
                self._data.refresh()
                self._update_widgets()
        self.app.push_screen(SwitchModelScreen(self._data.rotation), on_select)
    def action_restart_gateway(self):
        def on_confirm(confirmed: bool | None):
            if confirmed:
                subprocess.run(
                    ["systemctl", "--user", "restart", "openclaw-gateway.service"],
                    capture_output=True, timeout=15
                )
        self.app.push_screen(RestartConfirmScreen(), on_confirm)

    def action_clear_cooldown(self):
        def on_select(profile_id: str | None):
            if profile_id:
                _clear_cooldown_in_file(profile_id)
                self._data.refresh()
                self._update_widgets()
        self.app.push_screen(ClearCooldownScreen(self._data.profiles), on_select)


class ProviderScreen(Screen):
    BINDINGS = [
        Binding("ctrl+n", "new_provider", "New provider"),
        Binding("ctrl+d", "remove_provider", "Remove"),
        Binding("ctrl+x", "toggle_provider", "Enable/Disable"),
        Binding("ctrl+q", "go_back", "Back"),
    ]

    def __init__(self, data: "DataLayer", **kwargs):
        super().__init__(**kwargs)
        self._data = data
        self._providers: list[dict] = []
        self._cursor = 0

    def compose(self) -> ComposeResult:
        yield Static(" PROVIDER MANAGER           ^N:new  ^D:remove  ^X:toggle  ^Q:back",
                     id="provider-header")
        with Horizontal(id="provider-grid"):
            with Vertical(id="provider-list"):
                yield Static(" PROVIDERS", classes="panel-title")
                yield Static(id="provider-list-body")
            with ScrollableContainer(id="provider-detail"):
                yield Static(id="provider-detail-body")

    def on_mount(self):
        self._providers = _load_providers()
        self._render_list()
        self._render_detail()

    def _render_list(self):
        from rich.text import Text, Group
        lines = []
        for i, p in enumerate(self._providers):
            tag = p["auth_tag"]
            if "OAUTH" in tag: tag_style = "cyan"
            elif "API" in tag: tag_style = "yellow"
            elif "LOCAL" in tag: tag_style = "green"
            else: tag_style = "dim white"

            t = Text()
            if i == self._cursor:
                t.append("►", style="bold cyan")
                t.append(f" {p['id']:<22}", style="bold cyan on #1a2a3a")
                t.append(f" [{tag}]", style=f"bold {tag_style} on #1a2a3a")
            else:
                t.append(f"  {p['id']:<22}", style="dim white")
                t.append(f" [{tag}]", style=tag_style)
            lines.append(t)

        lines.append(Text(""))
        add_t = Text()
        add_t.append("  + Add new provider", style="dim cyan")
        lines.append(add_t)
        from rich.console import Group as G
        self.query_one("#provider-list-body", Static).update(G(*lines))

    def _render_detail(self):
        if not self._providers:
            self.query_one("#provider-detail-body", Static).update("  No providers configured")
            return
        p = self._providers[self._cursor]
        from rich.text import Text, Group
        lines = [
            Text(f"\n  DETAILS  {p['id']}", style="bold"),
            Text(""),
            Text(f"  provider    {p['id']}"),
            Text(f"  base url    {p['base_url']}", style="dim"),
        ]
        if p["api_key"] and p["api_key"] != "from-auth-profiles":
            masked = p["api_key"][:8] + "..." if len(p["api_key"]) > 8 else p["api_key"]
            lines.append(Text(f"  api key     {masked}", style="yellow"))
        if p["models"]:
            lines.append(Text(""))
            lines.append(Text("  models in rotation", style="bold"))
            for m in p["models"]:
                mid = m.get("id", "?")
                ctx = m.get("contextWindow", 0)
                ctx_str = f"{ctx // 1000}k" if ctx >= 1000 else str(ctx)
                lines.append(Text(f"    {mid:<30} {ctx_str}", style="dim"))
        lines.append(Text(""))
        lines.append(Text("  Enter:edit  ^D:remove  ^X:disable", style="dim"))
        from rich.console import Group as G
        self.query_one("#provider-detail-body", Static).update(G(*lines))

    def on_key(self, event):
        if event.key == "up":
            self._cursor = max(0, self._cursor - 1)
            self._render_list(); self._render_detail()
        elif event.key == "down":
            self._cursor = min(len(self._providers) - 1, self._cursor + 1)
            self._render_list(); self._render_detail()

    def action_go_back(self): self.app.pop_screen()
    def action_new_provider(self): self.app.push_screen(AddProviderWizard(), self._on_provider_added)
    def action_remove_provider(self): self._remove_current()
    def action_toggle_provider(self): pass  # Task 13

    def _on_provider_added(self, result):
        if result:
            self._providers = _load_providers()
            self._render_list()
            self._render_detail()

    def _remove_current(self):
        if not self._providers: return
        pid = self._providers[self._cursor]["id"]
        try:
            cfg = json.loads(OPENCLAW_JSON.read_text())
            cfg.get("models", {}).get("providers", {}).pop(pid, None)
            OPENCLAW_JSON.write_text(json.dumps(cfg, indent=2))
        except Exception: pass
        self._providers = _load_providers()
        self._cursor = max(0, min(self._cursor, len(self._providers) - 1))
        self._render_list()
        self._render_detail()


class AddProviderWizard(ModalScreen):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._step = 1
        self._type: str | None = None
        self._type_cursor = 0
        self._form: dict = {}
        self._confirm_cursor = 0
        self._waiting_oauth = False

    def compose(self) -> ComposeResult:
        with Container(classes="modal-box", id="wizard-box"):
            yield Static(id="wizard-title", classes="modal-title")
            yield Static(id="wizard-body")
            yield Container(id="wizard-inputs")
            yield Static(id="wizard-footer", classes="modal-footer")

    def on_mount(self): self._render()

    def _render(self):
        title_w = self.query_one("#wizard-title", Static)
        body_w = self.query_one("#wizard-body", Static)
        footer_w = self.query_one("#wizard-footer", Static)
        inputs_c = self.query_one("#wizard-inputs", Container)

        # Clear inputs
        for child in list(inputs_c.children):
            child.remove()

        if self._step == 1:
            title_w.update(f" ADD PROVIDER  (1/3)")
            footer_w.update(" Up/Down  Enter:select  Esc:cancel")
            self._render_type_select(body_w)
            inputs_c.display = False

        elif self._step == 2 and self._type == "apikey":
            title_w.update(f" ADD PROVIDER  (2/3) — API Key")
            body_w.update("")
            footer_w.update(" Tab:next  Shift+Tab:prev  Enter:confirm  Esc:cancel")
            inputs_c.display = True
            inputs_c.mount(Label("  Provider name"))
            inputs_c.mount(Input(placeholder="groq", id="inp-name"))
            inputs_c.mount(Label("  Base URL"))
            inputs_c.mount(Input(placeholder="https://api.groq.com/openai/v1", id="inp-url"))
            inputs_c.mount(Label("  API Key"))
            inputs_c.mount(Input(placeholder="sk-...", password=True, id="inp-key"))

        elif self._step == 2 and self._type == "oauth":
            title_w.update(" ADD PROVIDER  (2/3) — OAuth")
            from rich.text import Text, Group
            lines = [
                Text("\n  Launching browser auth..."),
                Text("  (Paste URL in browser if it does not open)\n"),
                Text("  Run manually:"),
                Text("  openclaw models auth login\n", style="cyan"),
                Text(f"  {make_bar(0, 36)}", style="cyan"),
                Text("  Waiting for browser callback...", style="dim"),
            ]
            from rich.console import Group as G
            body_w.update(G(*lines))
            inputs_c.display = False
            footer_w.update(" Esc:cancel")
            # Trigger oauth flow in thread
            if not self._waiting_oauth:
                self._waiting_oauth = True
                threading.Thread(target=self._run_oauth, daemon=True).start()

        elif self._step == 2 and self._type == "custom":
            title_w.update(" ADD PROVIDER  (2/3) — Custom")
            body_w.update("")
            footer_w.update(" Tab:next  Enter:next field  Esc:cancel")
            inputs_c.display = True
            inputs_c.mount(Label("  Provider ID"))
            inputs_c.mount(Input(placeholder="my-provider", id="inp-name"))
            inputs_c.mount(Label("  Base URL"))
            inputs_c.mount(Input(placeholder="http://localhost:8080/v1", id="inp-url"))
            inputs_c.mount(Label("  API Key (optional)"))
            inputs_c.mount(Input(placeholder="sk-...", password=True, id="inp-key"))

        elif self._step == 3:
            title_w.update(" ADD PROVIDER  (3/3) — Confirm")
            f = self._form
            from rich.text import Text, Group
            lines = [
                Text(f"\n  Provider   {f.get('name', '?')}"),
                Text(f"  Base URL   {f.get('url', '?')}", style="dim"),
            ]
            if f.get("key"):
                lines.append(Text(f"  API Key    {f['key'][:6]}****", style="yellow"))
            lines.append(Text(""))
            body_w.update(Group(*lines))
            self._render_confirm_choices(inputs_c)
            inputs_c.display = True
            footer_w.update(" Up/Down  Enter:confirm  Esc:cancel")

    def _render_type_select(self, widget):
        opts = [
            ("API Key", "Groq, OpenCode, custom endpoints"),
            ("OAuth",   "Google, Qwen browser-based login"),
            ("Custom",  "New OpenAI-compatible endpoint"),
        ]
        from rich.text import Text, Group
        lines = [Text("  Select provider type\n")]
        for i, (name, desc) in enumerate(opts):
            t = Text()
            if i == self._type_cursor:
                t.append("► ", style="bold cyan")
                t.append(f"{name:<12}", style="bold cyan on #1a2a3a")
                t.append(f"  {desc}", style="dim cyan on #1a2a3a")
            else:
                t.append(f"   {name:<12}", style="dim white")
                t.append(f"  {desc}", style="dim")
            lines.append(t)
        widget.update(Group(*lines))

    def _render_confirm_choices(self, container):
        for child in list(container.children):
            child.remove()
        opts = ["Add and include in rotation", "Add without adding to rotation", "Cancel"]
        from rich.text import Text, Group
        lines = []
        for i, opt in enumerate(opts):
            t = Text()
            if i == self._confirm_cursor:
                t.append("► ", style="bold cyan")
                t.append(opt, style="bold cyan on #1a2a3a")
            else:
                t.append(f"   {opt}", style="dim white")
            lines.append(t)
        container.mount(Static(Group(*lines)))

    def _run_oauth(self):
        subprocess.run(["openclaw", "models", "auth", "login"],
                       capture_output=True, timeout=120)
        self.app.call_from_thread(self.dismiss, {"oauth": True})

    def on_key(self, event):
        if self._step == 1:
            if event.key == "up": self._type_cursor = max(0, self._type_cursor - 1); self._render()
            elif event.key == "down": self._type_cursor = min(2, self._type_cursor + 1); self._render()
            elif event.key == "enter":
                self._type = ["apikey", "oauth", "custom"][self._type_cursor]
                self._step = 2; self._render()
        elif self._step == 2 and self._type in ("apikey", "custom"):
            if event.key == "enter":
                try:
                    self._form["name"] = self.query_one("#inp-name", Input).value
                    self._form["url"]  = self.query_one("#inp-url", Input).value
                    self._form["key"]  = self.query_one("#inp-key", Input).value
                    if self._form["name"] and self._form["url"]:
                        self._step = 3; self._render()
                except Exception: pass
        elif self._step == 3:
            if event.key == "up": self._confirm_cursor = max(0, self._confirm_cursor - 1); self._render()
            elif event.key == "down": self._confirm_cursor = min(2, self._confirm_cursor + 1); self._render()
            elif event.key == "enter": self._confirm()

    def _confirm(self):
        if self._confirm_cursor == 2:
            self.dismiss(None); return
        f = self._form
        add_to_rotation = (self._confirm_cursor == 0)
        _write_new_provider(f["name"], f["url"], f.get("key", ""), add_to_rotation)
        self.dismiss({"added": f["name"]})

    def action_cancel(self):
        self.dismiss(None)


def _write_new_provider(name: str, base_url: str, api_key: str, add_to_rotation: bool):
    """Write a new provider entry to openclaw.json."""
    try:
        cfg = json.loads(OPENCLAW_JSON.read_text())
        providers = cfg.setdefault("models", {}).setdefault("providers", {})
        providers[name] = {
            "baseUrl": base_url,
            "apiKey": api_key or "from-auth-profiles",
            "api": "openai-completions",
            "models": []
        }
        if add_to_rotation:
            fallbacks = cfg.setdefault("agents", {}).setdefault("defaults", {}).setdefault(
                "model", {}).setdefault("fallbacks", [])
            # Add a placeholder model entry
            fallbacks.append(f"{name}/default")
        OPENCLAW_JSON.write_text(json.dumps(cfg, indent=2))
    except Exception:
        pass


class OpenClawTUI(App):
    CSS = APP_CSS

    def on_mount(self):
        self._data = DataLayer()
        self._data.start()
        self.push_screen(DashboardScreen(self._data))

    def on_unmount(self):
        self._data.stop()


if __name__ == "__main__":
    OpenClawTUI().run()
