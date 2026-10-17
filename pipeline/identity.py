"""What identifies a hole: its record id, matching keys for its printed name ('BH No. 1' = 'BH-01') and its type
and number, report numbers and header texts, and how far its printed position and ground level may differ
between sheets."""

from __future__ import annotations

import re
import unicodedata
from typing import TypeVar

from schema.borehole import Borehole

from .extraction_models import XLog

HoleKey = tuple[str | None, str]  # (type, number), see hole_key
T = TypeVar("T")


def _name_key(name: str, separate_numbers: bool) -> str:
    s = name.upper().replace("|", " ")
    if separate_numbers:  # 'BH1-01' and 'BH10-1' stay apart
        s = re.sub(r"(?<=\d)[^A-Z0-9]+(?=\d)", "|", s)
    s = re.sub(r"[^A-Z0-9|]", "", s)
    return re.sub(r"NO(?=\d)", "", s).replace("|", "-")  # 'BH No. 1' → 'BH1'


def id_key(name: str, separate_numbers: bool = False) -> str:
    """Name part of the borehole ID: 'BH No. 01' → 'BH01'. Keeps leading zeros so IDs stay stable.
    `separate_numbers` keeps a '-' between number groups, for names that would otherwise collide."""
    return _name_key(name, separate_numbers) or name.upper()


def norm_name(name: str) -> str:
    """Key for matching sheets, tables and documents: ignores separators and leading zeros
    ('BH-01' = 'BH 1' = 'BH No. 1'), but keeps number groups apart ('BH1-01' = 'BH1-1' ≠ 'BH10-1')."""
    return re.sub(r"(?<!\d)0+(?=\d)", "", _name_key(name, separate_numbers=True)) or name.upper()


# Type words in hole names. Reports number boreholes and test pits separately ('1'..'6' and '1'..'20'),
# so the type is part of the matching key and the word itself is not.
_PIT_WORDS = re.compile(r"^\s*(?:test\s*pit|trial\s*pit|t\.?\s*p\.?|pit|calicata|cata|schurf)(?![a-z])\.?\s*"
                        r"(?:no\.?|nr\.?|n[º°]\.?)?\s*", re.I)
# Borehole words in the languages of the data ('Hull nr. 7' is Norwegian for hole no. 7).
_HOLE_WORDS = re.compile(r"^\s*(?:bore\s*hole|boring|bh|sondeo|drill\s*hole|borhull|borehull|hull|bohrung|bohrloch|"
                         r"kernbohrung|sondagem|furo|forage|sondage|sondaj)(?![a-z])\.?\s*(?:no\.?|nr\.?|n[º°]\.?)?\s*",
                         re.I)
# Probings are other investigations than boreholes with the same number ('CPT 1' is not 'BH 1').
_PROBE_WORDS = re.compile(r"^\s*(?:cptu?|dpsh|dpl|dph|dp|totalsondering|trykksondering|dreiesondering|sondering|"
                          r"rammsondierung|sondierung|sonda)(?![a-z])\.?\s*(?:no\.?|nr\.?)?\s*", re.I)
_HOLE_KIND_OF_PAGE = {"test_pit_log": "test_pit", "probe_log": "probe", "borehole_log": "borehole"}


def hole_key(name: str, kind: str | None) -> HoleKey:
    """(type, number) for matching logs, tables and lab rows: 'Test Pit No. 2' and 'T.P. 2' are the test pit
    '2', 'BH-2' is the borehole '2'. `kind` (from the page or the model) applies when the name has no type word."""
    for words, word_kind in ((_PIT_WORDS, "test_pit"), (_PROBE_WORDS, "probe"), (_HOLE_WORDS, "borehole")):
        rest = words.sub("", name, count=1)
        if rest != name and re.search(r"\d", rest):
            return word_kind, norm_name(rest)
    return (kind if kind in ("borehole", "test_pit", "probe") else None), norm_name(name)


def lookup_hole(index: dict[HoleKey, T], key: HoleKey) -> T | None:
    """Exact (type, number) match; without a known type only a number that is unique across types."""
    if key in index:
        return index[key]
    if key[0] is None:
        hits = [v for (kind, n), v in index.items() if n == key[1]]
        return hits[0] if len(hits) == 1 else None
    return index.get((None, key[1]))


# Sheets of one hole repeat the same header; beyond these differences a sheet shows another hole.
POSITION_TOLERANCE_M = 25.0
ELEVATION_TOLERANCE_M = 0.5


def same_id(a: str, b: str) -> bool:
    """Report or archive numbers as printed ('S.I. No. 12' = 'SI No 12'); one may add a number the other
    leaves out."""
    na = {n.lstrip("0") for n in re.findall(r"\d+", a)}
    nb = {n.lstrip("0") for n in re.findall(r"\d+", b)}
    if na and nb:
        return na <= nb or nb <= na
    return re.sub(r"\W+", "", a.lower()) == re.sub(r"\W+", "", b.lower())


def header_words(text: str) -> set[str]:
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return set(re.findall(r"[a-z0-9]{2,}", plain))


def same_header_text(a: str, b: str) -> bool:
    """Header texts read with different spelling, case or abbreviation still share most of their words."""
    wa, wb = header_words(a), header_words(b)
    return not wa or not wb or 2 * len(wa & wb) >= min(len(wa), len(wb))


def log_key(log: XLog, page_kind: str) -> HoleKey:
    return hole_key(log.name, log.investigation_type if log.investigation_type in ("borehole", "test_pit", "probe")
                    else _HOLE_KIND_OF_PAGE.get(page_kind))


def document_id(bh: Borehole) -> str:
    return bh.borehole_id.split(":", 1)[0]
