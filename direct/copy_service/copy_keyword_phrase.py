"""Keyword phrase normalization shared by copy-service upload paths."""
from __future__ import annotations

import re


_SPACE_RE = re.compile(r"\s+")
DIRECT_KEYWORD_MAX_POSITIVE_WORDS = 7
_DANGLING_PREPOSITIONS = {
    "без", "в", "во", "для", "до", "за", "из", "к", "ко", "на", "над", "о", "об", "от", "по",
    "под", "при", "про", "с", "со", "у", "через",
}


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


def _positive_word_count(tokens: list[str]) -> int:
    return sum(1 for token in tokens if token and not token.startswith("-"))


def _is_dangling_positive_token(token: str) -> bool:
    plain = _plain_word(token)
    return bool(token.startswith("+") or plain in _DANGLING_PREPOSITIONS)


def _drop_target_geo_form_once(tokens: list[str], target_forms: list[list[str]]) -> tuple[list[str], bool]:
    for form in target_forms:
        if not form:
            continue
        for idx in range(0, len(tokens) - len(form) + 1):
            chunk = tokens[idx:idx + len(form)]
            if any(str(token).startswith("-") for token in chunk):
                continue
            if [_plain_word(token) for token in chunk] == form:
                return tokens[:idx] + tokens[idx + len(form):], True
    return tokens, False


def _limit_positive_words(
    tokens: list[str],
    target_forms: list[list[str]],
    max_positive_words: int | None,
) -> list[str]:
    if not max_positive_words or max_positive_words <= 0:
        return tokens
    limited = list(tokens)
    was_over_limit = _positive_word_count(limited) > max_positive_words
    while was_over_limit:
        limited, dropped = _drop_target_geo_form_once(limited, target_forms)
        if not dropped:
            break
    if _positive_word_count(limited) <= max_positive_words:
        return limited

    out: list[str] = []
    positives = 0
    for token in limited:
        if token.startswith("-"):
            out.append(token)
            continue
        if positives >= max_positive_words:
            continue
        out.append(token)
        positives += 1

    # Avoid ending keyword phrases with a dangling preposition like "+с" or "в".
    if out and _is_dangling_positive_token(out[-1]):
        next_positive = None
        for token in limited:
            if token in out or token.startswith("-"):
                continue
            next_positive = token
            break
        if next_positive and _positive_word_count(out) >= max_positive_words:
            if str(out[-1]).startswith("+"):
                for idx in range(len(out) - 2, -1, -1):
                    if not out[idx].startswith("-"):
                        out.pop(idx)
                        break
            else:
                out.pop()
            out.append(next_positive)
    return out


def clean_keyword_phrase(
    phrase: str,
    geo_pairs: list[tuple[str, str]] | None = None,
    *,
    max_positive_words: int | None = None,
) -> str:
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
    out = _limit_positive_words(out, target_forms, max_positive_words)
    return " ".join(out).strip()
