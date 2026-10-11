"""Infer a company's email naming pattern and rank candidate addresses.

Placeholders: {first} {last} {middle}, their initials {f} {l} {m}, and {n} for the number
companies add when two people share a name (ssingh1@, priya.sharma2@).
"""

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

from hunterly.finder.names import local_part, name_parts

# Likelihood of each pattern when there is no evidence for the domain (roughly how common
# each is across companies). Evidence from known addresses at the domain outweighs these.
BASE_PRIORS: dict[str, float] = {
    "{first}.{last}": 0.250,
    "{first}": 0.180,
    "{f}{last}": 0.120,
    "{first}{last}": 0.080,
    "{first}{l}": 0.040,
    "{f}.{last}": 0.040,
    "{first}_{last}": 0.030,
    "{last}": 0.020,
    "{last}.{first}": 0.020,
    "{last}{f}": 0.020,
    "{first}.{l}": 0.020,
    "{first}-{last}": 0.015,
    "{last}{first}": 0.010,
    "{last}.{f}": 0.010,
    "{f}{l}": 0.006,
    "{f}_{last}": 0.006,
    "{last}_{first}": 0.005,
    "{f}-{last}": 0.004,
    "{l}{first}": 0.003,
    "{first}{last}{n}": 0.006,
    "{f}{last}{n}": 0.010,
    "{first}.{last}{n}": 0.006,
    "{first}{l}{n}": 0.003,
    "{first}{n}": 0.003,
    "{f}{m}{last}": 0.010,
    "{first}.{m}.{last}": 0.006,
    "{first}{m}{last}": 0.004,
    "{first}.{middle}.{last}": 0.004,
    "{first}{middle}": 0.003,
}
NUMBERS = (1, 2, 3)
NUMBER_SHARE = {1: 0.6, 2: 0.3, 3: 0.1}


@dataclass
class Candidate:
    address: str
    pattern: str
    score: float


def priors(company_size: str | None = None) -> dict[str, float]:
    weights = dict(BASE_PRIORS)
    size = parse_size(company_size)
    if size is not None and size <= 50:
        weights["{first}"] *= 1.8
    elif size is not None and size >= 1000:
        weights["{first}.{last}"] *= 1.5
        weights["{first}"] *= 0.5
        for p in weights:  # big companies are where name clashes need numbers
            if "{n}" in p:
                weights[p] *= 1.5
    total = sum(weights.values())
    return {p: w / total for p, w in weights.items()}


def parse_size(size: str | None) -> int | None:
    """'11-50' -> 50, '1,001-5,000' -> 5000, '200' -> 200, anything else -> None."""
    if not size:
        return None
    try:
        parts = [int(p.replace(",", "")) for p in size.replace("+", "").split("-") if p.strip()]
    except ValueError:
        return None
    return max(parts) if parts else None


def render(pattern: str, first: str, last: str, middle: str = "", n: int | None = None) -> str | None:
    f, lst, mid = local_part(first), local_part(last), local_part(middle)
    if not f:
        return None
    if ("{last}" in pattern or "{l}" in pattern) and not lst:
        return None
    if ("{middle}" in pattern or "{m}" in pattern) and not mid:
        return None
    if "{n}" in pattern and n is None:
        return None
    return pattern.format(first=f, last=lst, middle=mid, f=f[:1], l=lst[:1], m=mid[:1],
                          n="" if n is None else n)


def infer_patterns(known: Iterable[tuple[str, str]], domain: str) -> Counter:
    """Count which patterns explain the known (full name, address) pairs at this domain."""
    counts: Counter = Counter()
    domain = domain.lower()
    for full_name, address in known:
        local, _, dom = address.strip().lower().partition("@")
        if dom != domain:
            continue
        first, middle, last = name_parts(full_name)
        m = re.match(r"^(.*?)(\d+)$", local)
        for pattern in BASE_PRIORS:
            if "{n}" in pattern:
                if m and render(pattern, first, last, middle, int(m.group(2))) == local:
                    counts[pattern] += 1
            elif render(pattern, first, last, middle) == local:
                counts[pattern] += 1
    return counts


def rank_candidates(
    first: str,
    last: str,
    domain: str,
    known: Iterable[tuple[str, str]] = (),
    company_size: str | None = None,
    limit: int = 30,
    middle: str = "",
) -> list[Candidate]:
    """Candidate addresses, most likely first. Evidence from known emails outweighs priors."""
    base = priors(company_size)
    counts = infer_patterns(known, domain)
    total = sum(counts.values())
    scores = {p: (counts[p] + prior) / (total + 1) for p, prior in base.items()}

    expanded: list[tuple[float, str, str]] = []
    for pattern, score in scores.items():
        if "{n}" in pattern:
            for n in NUMBERS:
                local = render(pattern, first, last, middle, n)
                if local:
                    expanded.append((score * NUMBER_SHARE[n], pattern.replace("{n}", str(n)), local))
        else:
            local = render(pattern, first, last, middle)
            if local:
                expanded.append((score, pattern, local))

    seen: set[str] = set()
    out: list[Candidate] = []
    for score, pattern, local in sorted(expanded, key=lambda t: t[0], reverse=True):
        if local in seen:
            continue
        seen.add(local)
        out.append(Candidate(f"{local}@{domain.lower()}", pattern, round(score, 4)))
    return out[:limit]
