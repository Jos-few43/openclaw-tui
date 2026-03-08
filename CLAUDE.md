# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Terminal UI dashboard for managing AI provider configurations, model switching, OAuth authentication, and system monitoring. Built with Textual framework.

## Tech Stack

| Component | Technology |
|---|---|
| Language | Python 3 |
| TUI | Textual |
| Styling | Rich |

## Project Structure

```
openclaw-tui/
├── openclaw_tui.py     # Main dashboard app (~4,300+ lines)
└── test_data_layer.py  # Data parsing tests
```

## Key Features

- Model status parsing and rotation management
- OAuth profile display and management
- Provider configuration wizard (3-step overlay)
- Multi-modal screens (model selection, auth management, logging)
- Supports: Gemini, Groq, Kimi, Qwen, Ollama local models

## Key Commands

```bash
python openclaw_tui.py    # Launch dashboard
# Ctrl+S: Switch model | Ctrl+R: Restart provider | Ctrl+C: Clear cooldown
```

## Cross-Repo Relationships

- **openclaw-workspace** — Interface for provider management
- **opencode-local-litellm** — Local model routing bridge

## Things to Avoid

- Don't hardcode `/home/yish` — use `$HOME` or `/var/home/yish`
