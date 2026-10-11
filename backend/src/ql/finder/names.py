"""Turn a display name (often messy on LinkedIn) into first and last name parts."""

import re
import unicodedata

_HONORIFICS = {"dr", "mr", "mrs", "ms", "miss", "prof", "er", "adv", "shri", "smt", "sir"}
_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "phd", "mba", "md", "cpa", "pmp", "cfa", "pe"}
_EMOJI = re.compile("[\U0001f000-\U0010ffff☀-➿]")


def ascii_fold(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def clean_full_name(raw: str) -> str:
    """Drop emoji, bracketed notes, credentials after a comma and text after separators."""
    text = _EMOJI.sub(" ", raw or "")
    text = re.sub(r"\(.*?\)|\[.*?\]", " ", text)
    text = re.split(r"[|•·–—]", text)[0]
    text = text.split(",")[0]
    return " ".join(text.split())


def name_parts(raw: str) -> tuple[str, str, str]:
    """Return (first, middle, last). 'Sandeep Kumar Singh' -> ('Sandeep', 'Kumar', 'Singh')."""
    tokens = _tokens(raw)
    if len(tokens) < 3:
        first, last = (tokens + ["", ""])[:2]
        return first, "", last
    return tokens[0], " ".join(tokens[1:-1]), tokens[-1]


def _tokens(raw: str) -> list[str]:
    tokens = clean_full_name(raw).split()
    while tokens and tokens[0].lower().strip(".") in _HONORIFICS:
        tokens.pop(0)
    while len(tokens) > 1 and tokens[-1].lower().strip(".") in _SUFFIXES:
        tokens.pop()
    return tokens


def split_name(raw: str) -> tuple[str, str]:
    """Return (first, last). Middle names are ignored; a single name has no last part."""
    tokens = clean_full_name(raw).split()
    while tokens and tokens[0].lower().strip(".") in _HONORIFICS:
        tokens.pop(0)
    while len(tokens) > 1 and tokens[-1].lower().strip(".") in _SUFFIXES:
        tokens.pop()
    if not tokens:
        return "", ""
    if len(tokens) == 1:
        return tokens[0], ""
    return tokens[0], tokens[-1]


def local_part(text: str) -> str:
    """Lowercase ASCII letters and digits only, as used in an email local part."""
    return re.sub(r"[^a-z0-9]", "", ascii_fold(text).lower())
