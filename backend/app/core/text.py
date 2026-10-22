"""Plain-text sanitising for user-supplied free text (announcements, profile fields).

Stored values are PLAIN TEXT: markup is removed (not escaped), control and bidi-override characters are dropped, and the
result is NFC normalised. Clients must still render it as text (never as HTML)."""

from __future__ import annotations

import re
import unicodedata

# <script>/<style> blocks including their content, HTML comments, then any remaining tag-like markup. A bare "<" or ">"
# (e.g. "a < b") is kept: only well-formed tags starting with a letter, "/", "!" or "?" are removed.
_BLOCKS = re.compile(r"<\s*(script|style)\b[^>]*>.*?<\s*/\s*\1\s*>", re.I | re.S)
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_TAG = re.compile(r"</?[A-Za-z!?][^<>]*>")
# C0/C1 controls (TAB/LF/CR handled separately), DEL, lone surrogates, non-characters, bidi overrides/isolates, zero-width
_BAD = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ud800-\udfff￾￿‪-‮⁦-⁩​-‏⁠﻿]")


def clean_text(value: str, *, multiline: bool = False) -> str:
    """Single-line (default): newlines/tabs become spaces and runs of whitespace collapse. Multi-line keeps \n (CRLF -> LF),
    turns each tab into a space and limits blank lines to one."""
    v = unicodedata.normalize("NFC", value)
    for _ in range(3):  # nested/obfuscated markup such as <<b>script> needs more than one pass
        stripped = _TAG.sub("", _COMMENT.sub("", _BLOCKS.sub("", v)))
        if stripped == v:
            break
        v = stripped
    v = v.replace("\r\n", "\n").replace("\r", "\n")
    if multiline:
        v = _BAD.sub("", v.replace("\t", " "))
        v = re.sub(r"[ ]{2,}", " ", v)
        v = re.sub(r"[ ]*\n[ ]*", "\n", v)
        v = re.sub(r"\n{3,}", "\n\n", v)
    else:
        v = _BAD.sub("", re.sub(r"[\t\n]+", " ", v))
        v = re.sub(r"\s{2,}", " ", v)
    return v.strip()
