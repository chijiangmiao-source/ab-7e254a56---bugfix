"""Version parsing / comparison for candidate images.

Versions are dotted numeric tuples such as ``1``, ``1.2`` or ``1.2.10``.
A candidate must be *strictly greater* than the currently active version.
"""
from __future__ import annotations

import re

_NUM = re.compile(r"^\d+(\.\d+)*$")


def parse_version(text: str) -> tuple[int, ...]:
    if not isinstance(text, str) or not _NUM.match(text.strip()):
        raise ValueError(f"invalid version: {text!r} (expected e.g. 1.2.3)")
    return tuple(int(part) for part in text.strip().split("."))


def is_higher(candidate: str, current: str) -> bool:
    return parse_version(candidate) > parse_version(current)
