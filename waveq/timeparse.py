from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import re

_TIME_UNITS = {
    "fs": Fraction(1, 10**15),
    "ps": Fraction(1, 10**12),
    "ns": Fraction(1, 10**9),
    "us": Fraction(1, 10**6),
    "ms": Fraction(1, 10**3),
    "s": Fraction(1, 1),
}

@dataclass(frozen=True)
class Timescale:
    number: int
    unit: str

    @property
    def tick_seconds(self) -> Fraction:
        if self.unit not in _TIME_UNITS:
            raise ValueError(f"unsupported timescale unit: {self.unit}")
        return Fraction(self.number, 1) * _TIME_UNITS[self.unit]

    def format_tick(self, tick: int) -> str:
        # VCD ticks are integer multiples of the timescale. Prefer native units.
        native = tick * self.number
        return f"{native}{self.unit}"

_TIME_RE = re.compile(r"^\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))(?:\s*([a-zA-Z]+))?\s*$")


def fraction_from_decimal(text: str) -> Fraction:
    text = text.strip()
    sign = -1 if text.startswith("-") else 1
    if text[0] in "+-":
        text = text[1:]
    if "." in text:
        whole, frac = text.split(".", 1)
        whole_i = int(whole or "0")
        denom = 10 ** len(frac)
        num = whole_i * denom + int(frac or "0")
        return sign * Fraction(num, denom)
    return sign * Fraction(int(text), 1)


def parse_time_to_ticks(value: str | int, timescale: Timescale, *, allow_nearest: bool = False) -> int:
    """Parse a user time into an exact VCD tick.

    Plain integers are interpreted as raw VCD ticks. Unit-bearing values are
    converted through Fraction arithmetic and rejected unless exactly
    representable, unless allow_nearest=True.
    """
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        raise ValueError("time must be a string or integer tick")
    s = value.strip()
    if re.fullmatch(r"[+-]?\d+", s):
        return int(s)
    m = _TIME_RE.match(s)
    if not m:
        raise ValueError(f"invalid time literal: {value!r}")
    magnitude = fraction_from_decimal(m.group(1))
    unit = (m.group(2) or "").lower()
    if unit not in _TIME_UNITS:
        raise ValueError(f"time literal needs one of {sorted(_TIME_UNITS)} or a raw integer tick: {value!r}")
    ticks = magnitude * _TIME_UNITS[unit] / timescale.tick_seconds
    if ticks.denominator == 1:
        return ticks.numerator
    if allow_nearest:
        # round half away from zero using rational arithmetic
        if ticks >= 0:
            return int(ticks + Fraction(1, 2))
        return int(ticks - Fraction(1, 2))
    raise ValueError(
        f"{value!r} is not representable at timescale {timescale.number}{timescale.unit}; "
        f"it equals {ticks} ticks"
    )


def parse_radius_to_ticks(value: str | int, timescale: Timescale) -> int:
    ticks = parse_time_to_ticks(value, timescale)
    if ticks < 0:
        raise ValueError("radius must be non-negative")
    return ticks
