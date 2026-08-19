import re
from dataclasses import dataclass

from nameparser import HumanName


@dataclass(frozen=True)
class NormalizedName:
    first_name: str
    last_name: str
    display_name: str


def normalize_name(raw: str) -> NormalizedName:
    cleaned = " ".join(raw.strip().split())
    if "," in cleaned:
        last_part, _, first_part = cleaned.partition(",")
        ordered = f"{first_part.strip()} {last_part.strip()}".strip()
    else:
        ordered = cleaned

    parsed = HumanName(ordered)
    first_name = parsed.first.strip()
    last_name = parsed.last.strip()
    display_name = " ".join(part for part in (first_name, last_name) if part)
    return NormalizedName(first_name=first_name, last_name=last_name, display_name=display_name)


_LEGAL_SUFFIXES = sorted(
    [
        "pvt ltd",
        "private limited",
        "ltd",
        "limited",
        "llc",
        "inc",
        "incorporated",
        "corp",
        "corporation",
        "co",
        "company",
    ],
    key=lambda suffix: -len(suffix.split()),
)


def normalize_company(raw: str) -> str:
    name = raw.strip().lower()
    name = re.sub(r"[.,]", "", name)
    name = re.sub(r"\s+", " ", name).strip()

    changed = True
    while changed:
        changed = False
        for suffix in _LEGAL_SUFFIXES:
            pattern = r"\s+" + re.escape(suffix) + r"$"
            stripped = re.sub(pattern, "", name)
            if stripped != name:
                name = stripped
                changed = True

    return name.strip()
