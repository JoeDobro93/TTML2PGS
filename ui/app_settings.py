"""
ui/app_settings.py  # NEW FILE - create this in your ui/ folder

Manages persistent application settings, stored in a JSON file
alongside the main script (or in the user's app data directory).

Currently handles:
- Temp files directory mode ('match_source' or 'custom')
- Custom temp files directory path
"""

import json
import os

# Settings file lives next to main.py
_SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app_settings.json")
_SETTINGS_PATH = os.path.normpath(_SETTINGS_PATH)

_DEFAULTS = {
    "temp_dir_mode": "match_source",   # "match_source" | "custom"
    "temp_dir_custom": "",             # Absolute path when mode == "custom"
}


def load() -> dict:
    """Load settings from disk. Returns defaults if file doesn't exist or is corrupt."""
    settings = dict(_DEFAULTS)
    if os.path.exists(_SETTINGS_PATH):
        try:
            with open(_SETTINGS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Merge: only accept known keys so stale keys don't cause issues
            for key in _DEFAULTS:
                if key in data:
                    settings[key] = data[key]
        except Exception as e:
            print(f"[AppSettings] Failed to load settings: {e}. Using defaults.")
    return settings


def save(settings: dict):
    """Persist settings to disk."""
    try:
        # Only write recognised keys
        out = {k: settings.get(k, _DEFAULTS[k]) for k in _DEFAULTS}
        with open(_SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2)
        print(f"[AppSettings] Saved to {_SETTINGS_PATH}")
    except Exception as e:
        print(f"[AppSettings] Failed to save settings: {e}")


def resolve_temp_dir(settings: dict, source_path: str) -> str:
    """
    Return the directory that should be used for temp files for a given job.

    Args:
        settings:    The loaded app settings dict.
        source_path: The subtitle (or video) file path for the current job,
                     used when mode is 'match_source'.

    Returns:
        An absolute directory path string.
    """
    mode = settings.get("temp_dir_mode", "match_source")
    if mode == "custom":
        custom = settings.get("temp_dir_custom", "").strip()
        if custom and os.path.isdir(custom):
            return custom
        else:
            print(f"[AppSettings] Custom temp dir '{custom}' not valid; falling back to match_source.")

    # Default: subfolder next to the source file (original behaviour)
    return os.path.dirname(source_path)
