"""Official YouTube category labels from the reviewed shared registry.

Runtime lookup is local and defaults to English. Category labels (including
category 42, "Shorts") never determine Long/Short/stream membership.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from types import MappingProxyType



def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate YouTube category registry key: {key}")
        result[key] = value
    return result


def _validate_category_registry(data):
    if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise ValueError("Unsupported YouTube category registry schema_version")
    try:
        retrieved_at = datetime.fromisoformat(data["retrieved_at"].replace("Z", "+00:00"))
    except (KeyError, AttributeError, TypeError, ValueError) as error:
        raise ValueError("YouTube category registry requires a UTC retrieved_at timestamp") from error
    if retrieved_at.utcoffset() != timedelta(0):
        raise ValueError("YouTube category registry requires a UTC retrieved_at timestamp")
    source = data.get("source")
    if not isinstance(source, dict) or any(
        not isinstance(source.get(key), str) or not source[key].startswith("https://")
        for key in ("languages_url", "categories_url")
    ):
        raise ValueError("YouTube category registry requires source URLs")
    languages, categories = data.get("languages"), data.get("categories")
    if not isinstance(languages, dict) or not isinstance(categories, dict) or "en" not in languages:
        raise ValueError("YouTube category registry requires languages, categories and English fallback")
    if set(languages) != set(categories):
        raise ValueError("YouTube category registry language and category sets differ")
    normalized_codes = set()
    for code, language_name in languages.items():
        if not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", code):
            raise ValueError("Invalid YouTube category registry language code")
        if code.lower() in normalized_codes:
            raise ValueError("Duplicate normalized YouTube category registry language code")
        normalized_codes.add(code.lower())
        if not isinstance(language_name, str) or not language_name.strip():
            raise ValueError("YouTube category registry requires nonempty language names")
        labels = categories[code]
        if not isinstance(labels, dict) or not labels:
            raise ValueError("YouTube category registry requires category labels for every language")
        for category_id, label in labels.items():
            if not re.fullmatch(r"[1-9][0-9]*", category_id) or not isinstance(label, str) or not label.strip():
                raise ValueError("YouTube category registry requires numeric IDs and nonempty labels")
    return (
        MappingProxyType(languages),
        MappingProxyType({code: MappingProxyType(labels) for code, labels in categories.items()}),
    )


def youtube_video_category_name(category_id: object, language_code: str = "en") -> str:
    """Resolve a label only through the managed, freshness-checked registry."""
    from youtube_category_reference import managed_reference_store
    return managed_reference_store().category_name(category_id, language_code)
