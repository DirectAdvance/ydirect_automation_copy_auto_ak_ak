"""Keyword phrase normalization shared by copy-service upload paths."""
from __future__ import annotations

import re


_SPACE_RE = re.compile(r"\s+")


def _plain_word(token: str) -> str:
    return str(token or "").strip().lstrip("-!+").strip("\"[]()").lower().replace("ё", "е")


def _target_geo_words(geo_pairs: list[tuple[str, str]] | None) -> list[list[str]]:
    forms: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    for _old, new in geo_pairs or []:
        words = [_plain_word(w) for w in str(new or "").split()]
        words = [w for w in words if w]
        if not words:
            continue
        key = tuple(words)
        if key not in seen:
            seen.add(key)
            forms.append(words)
    forms.sort(key=len, reverse=True)
    return forms


def clean_keyword_phrase(phrase: str, geo_pairs: list[tuple[str, str]] | None = None) -> str:
    """Remove invalid/self-blocking inline minus fragments after geo rewrite."""
    tokens = _SPACE_RE.sub(" ", str(phrase or "").strip()).split(" ")
    if not tokens:
        return ""
    target_forms = _target_geo_words(geo_pairs)
    out: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i].strip()
        plain = _plain_word(token)
        if not plain:
            i += 1
            continue
        if token.startswith("-"):
            skipped_geo = False
            for form in target_forms:
                if plain != form[0]:
                    continue
                tail = tokens[i + 1:i + len(form)]
                if len(tail) == len(form) - 1 and [_plain_word(t) for t in tail] == form[1:]:
                    i += len(form)
                    skipped_geo = True
                    break
            if skipped_geo:
                continue
            if any(plain in form for form in target_forms):
                i += 1
                continue
        out.append(token)
        i += 1
    return " ".join(out).strip()
