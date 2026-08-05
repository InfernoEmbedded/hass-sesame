"""Unit tests for validating Home Assistant translation files in sesame_ble."""

import glob
import json
import os
import re
from pathlib import Path
import pytest

TRANSLATIONS_DIR = Path(__file__).parent.parent / "custom_components" / "sesame_ble" / "translations"
STRINGS_FILE = Path(__file__).parent.parent / "custom_components" / "sesame_ble" / "strings.json"


def get_nested_key_paths(d: dict, prefix: str = "") -> dict[str, str]:
    """Recursively extract key path -> string value mapping from nested translation dict."""
    result = {}
    for k, v in d.items():
        full_key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            result.update(get_nested_key_paths(v, full_key))
        else:
            result[full_key] = str(v) if v is not None else ""
    return result


def extract_placeholders(text: str) -> set[str]:
    """Extract format placeholders like {name}, {model}, {mac} from a translation string."""
    return set(re.findall(r"\{([a-zA-Z0-9_]+)\}", text))


def test_strings_json_exists_and_valid() -> None:
    """Verify strings.json exists and is valid JSON."""
    assert STRINGS_FILE.is_file(), "strings.json file must exist"
    with open(STRINGS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert "config" in data, "strings.json must contain top-level 'config' key"


def test_translation_files_exist_and_valid() -> None:
    """Verify translation directory exists and all translation files are valid JSON."""
    assert TRANSLATIONS_DIR.is_dir(), "translations directory must exist"
    json_files = list(TRANSLATIONS_DIR.glob("*.json"))
    assert len(json_files) > 0, "translations directory must contain at least one .json file"

    for filepath in json_files:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert isinstance(data, dict), f"{filepath.name} must contain a top-level JSON object"


def test_translations_keys_match_strings_json() -> None:
    """Verify that every translation file has identical key structure to strings.json."""
    with open(STRINGS_FILE, "r", encoding="utf-8") as f:
        strings_data = json.load(f)
    
    base_keys = set(get_nested_key_paths(strings_data).keys())

    json_files = list(TRANSLATIONS_DIR.glob("*.json"))
    for filepath in json_files:
        with open(filepath, "r", encoding="utf-8") as f:
            t_data = json.load(f)
        
        t_keys = set(get_nested_key_paths(t_data).keys())

        missing_keys = base_keys - t_keys
        extra_keys = t_keys - base_keys

        assert not missing_keys, f"{filepath.name} is missing translation keys: {sorted(missing_keys)}"
        assert not extra_keys, f"{filepath.name} contains extra/orphan translation keys: {sorted(extra_keys)}"


def test_translations_no_empty_values() -> None:
    """Verify that no translation key has empty or None values."""
    json_files = list(TRANSLATIONS_DIR.glob("*.json"))
    for filepath in json_files:
        with open(filepath, "r", encoding="utf-8") as f:
            t_data = json.load(f)
        
        key_value_map = get_nested_key_paths(t_data)
        empty_keys = [k for k, v in key_value_map.items() if not v.strip()]

        assert not empty_keys, f"{filepath.name} contains empty translation values for keys: {empty_keys}"


def test_translations_placeholders_match_base() -> None:
    """Verify that format placeholders ({model}, {mac}, etc.) in strings.json exist in all translations."""
    with open(STRINGS_FILE, "r", encoding="utf-8") as f:
        strings_data = json.load(f)
    
    base_key_value_map = get_nested_key_paths(strings_data)
    base_placeholders = {
        key: extract_placeholders(val)
        for key, val in base_key_value_map.items()
        if extract_placeholders(val)
    }

    json_files = list(TRANSLATIONS_DIR.glob("*.json"))
    for filepath in json_files:
        with open(filepath, "r", encoding="utf-8") as f:
            t_data = json.load(f)
        
        t_key_value_map = get_nested_key_paths(t_data)

        for key, expected_placeholders in base_placeholders.items():
            t_val = t_key_value_map.get(key, "")
            actual_placeholders = extract_placeholders(t_val)
            missing = expected_placeholders - actual_placeholders

            assert not missing, (
                f"{filepath.name} key '{key}' is missing required placeholders {missing} "
                f"(expected: {expected_placeholders}, found: {actual_placeholders})"
            )
