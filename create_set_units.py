"""Direct API units-limit helpers for create_set."""
from __future__ import annotations

import re
from typing import Any


UNITS_RE = re.compile(r"(?i)(\b152\b.*(unit|балл|v501|api)|(unit|балл).*\b152\b|units?\s*:\s*0|"
                      r"лимит\w*\s+балл|балл\w*\s+директ|исчерпан\w*\s+(суточн|лимит|балл)|"
                      r"недостаточн\w*\s+балл|not\s+enough\s+units)")


def is_units_exhausted(message: Any) -> bool:
    """True when an error text means Direct daily units limit is exhausted."""
    return bool(message) and bool(UNITS_RE.search(str(message)))


def units_in_result(row: dict[str, Any]) -> bool:
    """Return True when result row or nested campaigns contain error 152/units text."""
    if not isinstance(row, dict):
        return False
    if is_units_exhausted(row.get("error")):
        return True
    return any(isinstance(child, dict) and is_units_exhausted(child.get("error"))
               for child in (row.get("campaigns") or []))


AUTH_ERROR_RE = re.compile(
    r"(?i)(\b53\b.*(авториз|auth|ошибка)"
    r"|(авториз|ошибка\s+авториз).*\b53\b"
    r"|error_code.*\b53\b"
    r"|Ошибка\s+авторизации"
    r"|не\s+авторизован)",
)


def is_auth_error(message: Any) -> bool:
    """True when an error text means Direct returned auth error code 53."""
    return bool(message) and bool(AUTH_ERROR_RE.search(str(message)))


def auth_error_in_result(row: dict[str, Any]) -> bool:
    """True when result row or nested campaigns contain auth error 53 text."""
    if not isinstance(row, dict):
        return False
    if is_auth_error(row.get("error")):
        return True
    return any(isinstance(child, dict) and is_auth_error(child.get("error"))
               for child in (row.get("campaigns") or []))


def units_failed_names(results: list[dict[str, Any]]) -> set[str]:
    return {(row.get("name") or "") for row in results or []
            if (not row.get("ok")) and units_in_result(row)}


def count_created(results: list[dict[str, Any]]) -> int:
    return sum(1 for row in results or [] if row.get("ok") and not row.get("skipped"))


def count_skipped_existing(results: list[dict[str, Any]]) -> int:
    return sum(1 for row in results or [] if row.get("skipped"))


def count_failed(results: list[dict[str, Any]]) -> int:
    return sum(1 for row in results or []
               if (not row.get("ok")) and not units_in_result(row) and not row.get("defer"))
