# ~/scripts/monitoring/test_data_layer.py
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from openclaw_tui import parse_model_status   # will fail until implemented

SAMPLE_STATUS = {
    "defaultModel": "google-gemini-cli/gemini-3-flash",
    "fallbacks": ["google-gemini-cli/gemini-3-pro-preview", "groq/llama-3.3-70b-versatile"],
    "aliases": {"gemini-flash": "google-gemini-cli/gemini-3-flash"},
    "auth": {
        "oauth": {
            "profiles": [
                {
                    "profileId": "google-gemini-cli:bob",
                    "provider": "google-gemini-cli",
                    "status": "ok",
                    "expiresAt": 9999999999000,
                    "remainingMs": 3600000,
                }
            ]
        },
        "providers": []
    },
    "allowed": ["google-gemini-cli/gemini-3-flash"]
}

def test_parse_model_status_extracts_default():
    result = parse_model_status(SAMPLE_STATUS)
    assert result["default"] == "google-gemini-cli/gemini-3-flash"

def test_parse_model_status_rotation_list():
    result = parse_model_status(SAMPLE_STATUS)
    assert result["rotation"][0] == {
        "model": "google-gemini-cli/gemini-3-flash",
        "label": "gemini-cli/gemini-3-flash",
        "status": "ACTIVE",
        "position": 0,
        "alias": "gemini-flash",
    }
    assert result["rotation"][1]["status"] == "#1"

def test_parse_model_status_oauth_profiles():
    result = parse_model_status(SAMPLE_STATUS)
    assert len(result["oauth_profiles"]) == 1
    assert result["oauth_profiles"][0]["remaining_ms"] == 3600000
