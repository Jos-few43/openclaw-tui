# ~/scripts/monitoring/test_data_layer.py
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from openclaw_tui import parse_model_status, read_auth_profiles

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


SAMPLE_AUTH = {
    "profiles": {
        "groq:manual": {"type": "apiKey", "provider": "groq", "apiKey": "gsk_abc"},
        "google-gemini-cli:bob": {
            "type": "oauth", "provider": "google-gemini-cli",
            "expires": 9999999999000, "email": "bob@example.com"
        }
    },
    "usageStats": {
        "google-gemini-cli:bob": {
            "lastUsed": 1770900000000,
            "errorCount": 2,
            "cooldownUntil": 9999999999000,
            "failureCounts": {"rate_limit": 2}
        }
    }
}

def test_read_auth_profiles_finds_cooldown():
    result = read_auth_profiles(SAMPLE_AUTH)
    assert any(p["in_cooldown"] for p in result if p["profile_id"] == "google-gemini-cli:bob")

def test_read_auth_profiles_api_key_type():
    result = read_auth_profiles(SAMPLE_AUTH)
    groq = next(p for p in result if p["profile_id"] == "groq:manual")
    assert groq["auth_type"] == "apiKey"
    assert groq["in_cooldown"] is False
