"""Stage B — canonical bilingual company-alias → ticker resolver (docs/30, docs/35 M1).

Three-stage resolver for wire entity tagging; this module implements Stage B:
a canonical company/brand alias dictionary built from two sources:

1. **Auto-generated from `vn_tickers.json`** — for every listed ticker the
   distinctive remainder of `short_name` / `company_name` (generic corporate
   prefixes/suffixes stripped) becomes an alias IFF the remainder is
   unambiguous (maps to exactly one symbol globally, ≥ 2 distinctive tokens
   or one ≥ 6-char token, never a generic word).
2. **Curated brand aliases** — brand names that never appear verbatim in the
   registry (`Vinamilk`, `Vietcombank`, `VinFast`, …) plus disambiguating
   overrides for multi-candidate families (`Thép Hòa Phát` / `Tập đoàn Hòa
   Phát` → HPG; bare `Hòa Phát` stays ambiguous across HTP/HPA/HPG).

docs/35 M1 precision gates (live sample 2026-11-23..26: "hải phòng" → HAC,
"truyền thống" → TTN, "bất động sản" → VNI, "dầu khí" → PSI were all being
tagged from ordinary prose):

- **Generic-phrase guard** (auto aliases): a remainder made only of industry
  words and place names ("Bất động sản", "Hải Phòng", "Truyền thông",
  "Cấp nước Hải Phòng") is a common noun phrase, never an alias.
- **Accent-exact match**: matching runs on diacritic-folded text (so English
  copy "Hoa Phat" still matches), but a span written WITH diacritics must equal
  an original spelling — "truyền thống" (tradition) never matches "Truyền
  thông" (media).
- **Proper-noun casing** (auto aliases): every distinctive syllable of the span
  must be capitalised — names are proper nouns, prose is lowercase.
- **Case-sensitive acronyms**: short all-caps curated aliases ("HSC") only
  match uppercase.

Every hit carries its source span, character offset and an ``alias_confidence``
so callers can apply their own threshold (``AliasMatch``).
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.core.logging import get_logger

logger = get_logger(__name__)

_TICKERS_PATH = Path(__file__).resolve().parent / "data" / "vn_tickers.json"

# Generic corporate words stripped before an alias becomes distinctive.
_GENERIC_WORDS: frozenset[str] = frozenset(
    {
        "công",
        "ty",
        "cổ",
        "phần",
        "ctcp",
        "cp",
        "tổng",
        "tập",
        "đoàn",
        "và",
        "việt",
        "nam",
        "vietnam",
        "vn",
        "của",
        "kinh",
        "doanh",
        "dịch",
        "vụ",
        "dịch vụ",
        "thương mại",
        "đầu",
        "tư",
        "đầu tư",
        "chứng",
        "khoán",
        "ngân",
        "hàng",
        "số",
        "một",
        "vietnamese",
        "holding",
        "company",
        "joint",
        "stock",
        "corporation",
        "group",
        "the",
        "and",
        "of",
    }
)

# docs/35 M1: industry / common-noun phrases. A registry remainder made ONLY of
# these and place names is ordinary prose ("bất động sản", "truyền thông").
_INDUSTRY_PHRASES: tuple[str, ...] = (
    "bất động sản",
    "địa ốc",
    "nhà đất",
    "nhà ở",
    "xây dựng",
    "xây lắp",
    "lắp máy",
    "công trình",
    "dầu khí",
    "xăng dầu",
    "hóa dầu",
    "lọc dầu",
    "khí",
    "điện",
    "điện lực",
    "nhiệt điện",
    "thủy điện",
    "năng lượng",
    "cấp nước",
    "thoát nước",
    "cấp thoát nước",
    "nước sạch",
    "môi trường",
    "đô thị",
    "truyền thông",
    "công nghệ",
    "viễn thông",
    "phát triển",
    "thương mại",
    "sản xuất",
    "xuất nhập khẩu",
    "xuất khẩu",
    "nhập khẩu",
    "vận tải",
    "cảng",
    "logistics",
    "bảo hiểm",
    "tái bảo hiểm",
    "tài chính",
    "ngân hàng",
    "chứng khoán",
    "ck",
    "quản lý quỹ",
    "hóa chất",
    "phân bón",
    "thực phẩm",
    "nông sản",
    "nông nghiệp",
    "thủy sản",
    "lâm sản",
    "dược",
    "dược phẩm",
    "y tế",
    "giáo dục",
    "du lịch",
    "khách sạn",
    "may",
    "dệt may",
    "nhựa",
    "thép",
    "kim khí",
    "cơ khí",
    "xi măng",
    "khoáng sản",
    "cao su",
    "mía đường",
    "bia",
    "rượu",
    "nước giải khát",
    "hàng không",
    "đường sắt",
    "giao thông",
    "hạ tầng",
    "khu công nghiệp",
    "công nghiệp",
    "kỹ thuật",
    "thiết bị",
    "vật liệu",
    "vật tư",
    "bao bì",
    "điện tử",
    "tin học",
    "phần mềm",
    "dịch vụ",
    "đầu tư",
    "kinh doanh",
    "tư vấn",
    "khai thác",
    "chế biến",
    "phân phối",
    "bán lẻ",
    "tổng hợp",
    "quốc tế",
    "quốc gia",
    "hàng hải",
    "đóng tàu",
    "nội thất",
    "đt",
    "tct",
    "xnk",
    "bđs",
)

# docs/35 M1: place names (provinces, major cities, regions, landmarks). Masked
# out of the generic guard: "Hải Phòng", "Thăng Long", "Thành phố Hồ Chí Minh".
_PLACE_PHRASES: tuple[str, ...] = (
    "thành phố hồ chí minh",
    "hồ chí minh",
    "thành phố",
    "tp.hcm",
    "tp hcm",
    "tphcm",
    "hcm",
    "sài gòn",
    "hà nội",
    "hải phòng",
    "đà nẵng",
    "cần thơ",
    "huế",
    "thừa thiên huế",
    "an giang",
    "bà rịa",
    "vũng tàu",
    "bắc giang",
    "bắc kạn",
    "bạc liêu",
    "bắc ninh",
    "bến tre",
    "bình định",
    "bình dương",
    "bình phước",
    "bình thuận",
    "cà mau",
    "cao bằng",
    "đắk lắk",
    "đắk nông",
    "điện biên",
    "đồng nai",
    "đồng tháp",
    "gia lai",
    "hà giang",
    "hà nam",
    "hà tĩnh",
    "hải dương",
    "hậu giang",
    "hòa bình",
    "hưng yên",
    "khánh hòa",
    "kiên giang",
    "kon tum",
    "lai châu",
    "lâm đồng",
    "lạng sơn",
    "lào cai",
    "long an",
    "nam định",
    "nghệ an",
    "ninh bình",
    "ninh thuận",
    "phú thọ",
    "phú yên",
    "quảng bình",
    "quảng nam",
    "quảng ngãi",
    "quảng ninh",
    "quảng trị",
    "sóc trăng",
    "sơn la",
    "tây ninh",
    "thái bình",
    "thái nguyên",
    "thanh hóa",
    "tiền giang",
    "trà vinh",
    "tuyên quang",
    "vĩnh long",
    "vĩnh phúc",
    "yên bái",
    "thăng long",
    "thủ đô",
    "thủ đức",
    "miền bắc",
    "miền trung",
    "miền nam",
    "tây bắc",
    "đông bắc",
    "tây nguyên",
    "nam bộ",
    "bắc bộ",
    "trung bộ",
    "đông nam bộ",
    "tây nam bộ",
    "cửu long",
    "mê kông",
    "mekong",
    "sông đà",
    "sông hồng",
    "việt nam",
    "hanoi",
    "saigon",
    "haiphong",
    "danang",
)

# Curated brand aliases (docs/29 ISSUE: bilingual brand names missing from the
# registry) — brand/subsidiary → canonical ticker. Bare-word entries that
# collide with common Vietnamese/English words live here deliberately with
# the multi-token disambiguating form instead (Stage C keeps bare-word rules).
_CURATED_ALIASES: tuple[tuple[str, str], ...] = (
    ("Vinamilk", "VNM"),
    ("Sữa Việt Nam", "VNM"),
    ("Thép Hòa Phát", "HPG"),
    ("Tập đoàn Hòa Phát", "HPG"),
    ("FPT Software", "FPT"),
    ("FPT Telecom", "FPT"),
    ("FPT Retail", "FRT"),
    ("Nhà thuốc Long Châu", "FRT"),
    ("Vietcombank", "VCB"),
    ("BIDV", "BID"),
    ("Techcombank", "TCB"),
    ("VPBank", "VPB"),
    # NOTE: "VinFast" -> VFS was removed (docs/35 M1): VinFast trades on NASDAQ;
    # VFS in the Vietnamese registry is Nhat Viet Securities (HNX).
    ("Vingroup", "VIC"),
    ("Vinhomes", "VHM"),
    ("Masan", "MSN"),
    ("SABECO", "SAB"),
    ("Sabeco", "SAB"),
    ("Bia Sài Gòn", "SAB"),
    ("Vietjet", "VJC"),
    ("Vietnam Airlines", "HVN"),
    ("Thaco", "THG"),
    ("Trường Hải", "THG"),
    ("Nosco", "NOS"),
    ("Thiên Long", "TLG"),
    ("Petrovietnam", "PVS"),
    ("Petro Viet Nam", "PVS"),
    ("Hoa Sen", "HSG"),
    ("Tôn Hoa Sen", "HSG"),
    ("Đầu tư Địa ốc Hoàng Quân", "HQC"),
    ("Địa ốc Hoàng Quân", "HQC"),
    ("Hoàng Quân Địa ốc", "HQC"),
    ("Hoàng Quân", "HQC"),  # unique to HQC in the registry (verified)
    ("Tư vấn Thương mại Dịch vụ Địa ốc Hoàng Quân", "HQC"),
    # docs/35 M1 (brand collisions seen live): CMC Technology Group trades as CMG
    # ("CTCP Tập đoàn Công nghệ CMC (MCK: CMG)"), distinct from listed CMC JSC.
    ("Công nghệ CMC", "CMG"),
    ("Tập đoàn Công nghệ CMC", "CMG"),
    ("CMC Corporation", "CMG"),
    ("Cen Land", "CRE"),
    ("CENLAND", "CRE"),
    # HCM (HOSE) is "HSC" securities; the city "TP.HCM" must never tag it.
    ("Chứng khoán HSC", "HCM"),
    ("HSC", "HCM"),
    # Brand forms the registry spells differently (live headlines 2026-11-26).
    ("SeABank", "SSB"),
    ("Novaland", "NVL"),
    ("TTC AgriS", "SBT"),
    ("AgriS", "SBT"),
    ("Bảo Minh", "BMI"),
    ("Chứng khoán Bảo Minh", "BMS"),
    ("Chứng khoán Nhất Việt", "VFS"),
    ("Đá Núi Nhỏ", "NNC"),
    ("Đá Hóa An", "DHA"),
    ("Xuân Mai Corp", "XMC"),
    ("Thaiholdings", "THD"),
)

_CURATED_CONFIDENCE = 0.97
_AUTO_CONFIDENCE = 0.9
_RARE_TOKEN_BONUS = 0.05  # alias contains a token used by <= 3 listed company names
_FOLDED_ONLY_PENALTY = 0.08  # source span written without diacritics (EN copy)
_RARE_DF = 3


@dataclass(frozen=True, slots=True)
class AliasMatch:
    """One alias hit in the source text (offsets are into the NFC text)."""

    symbol: str
    span: str
    start: int
    end: int
    confidence: float
    curated: bool


@dataclass(frozen=True, slots=True)
class _AliasEntry:
    symbol: str
    originals: frozenset[str]  # casefolded NFC spellings (with diacritics)
    curated: bool
    rare: bool
    case_sensitive: bool  # short all-caps acronym (e.g. "HSC")


def _strip_generic(tokens: list[str]) -> list[str]:
    """Drop generic corporate words from both ends and collapse whitespace."""
    while tokens and tokens[0].lower() in _GENERIC_WORDS:
        tokens = tokens[1:]
    while tokens and tokens[-1].lower() in _GENERIC_WORDS:
        tokens = tokens[:-1]
    return tokens


def _fold(text: str) -> str:
    """Case-fold + strip diacritics (NFD, drop combining marks) so EN /
    diacritic-stripped wire copy ("Hoa Phat", "Bia Sai Gon", "Sua Viet Nam")
    matches the Vietnamese originals (PASS-2 audit item 4 — bilingual means
    both orthographies)."""
    lowered = unicodedata.normalize("NFD", text).casefold()
    return "".join(ch for ch in lowered if not unicodedata.combining(ch))


def _fold_with_map(nfc: str) -> tuple[str, list[int]]:
    """Fold char-by-char, returning the folded string and, for every folded
    character, the index of the source character it came from."""
    out: list[str] = []
    idx: list[int] = []
    for i, ch in enumerate(nfc):
        folded = _fold(ch)
        out.append(folded)
        idx.extend([i] * len(folded))
    return "".join(out), idx


# Old vs new Vietnamese tone-mark placement are both correct ("Hoá"/"Hóa",
# "Thuý"/"Thúy"): normalise to one convention before the accent-exact check.
_TONE_VARIANTS = {
    "óa": "oá",
    "òa": "oà",
    "ỏa": "oả",
    "õa": "oã",
    "ọa": "oạ",
    "óe": "oé",
    "òe": "oè",
    "ỏe": "oẻ",
    "õe": "oẽ",
    "ọe": "oẹ",
    "úy": "uý",
    "ùy": "uỳ",
    "ủy": "uỷ",
    "ũy": "uỹ",
    "ụy": "uỵ",
}
_TONE_RE = re.compile("|".join(_TONE_VARIANTS))


def _norm_spelling(text: str) -> str:
    low = " ".join(unicodedata.normalize("NFC", text).casefold().split())
    return _TONE_RE.sub(lambda m: _TONE_VARIANTS[m.group(0)], low)


@lru_cache(maxsize=1)
def _generic_phrase_re() -> re.Pattern[str]:
    phrases = sorted(
        {_fold(p) for p in (*_INDUSTRY_PHRASES, *_PLACE_PHRASES)}, key=len, reverse=True
    )
    alt = "|".join(re.escape(p) for p in phrases)
    return re.compile(rf"(?<![\w]){'(?:' + alt + ')'}(?![\w])")


@lru_cache(maxsize=1)
def _generic_syllables() -> frozenset[str]:
    syl: set[str] = set()
    for p in (*_INDUSTRY_PHRASES, *_GENERIC_WORDS):
        syl.update(_fold(p).split())
    return frozenset(syl)


def is_generic_alias(alias: str) -> bool:
    """True when an alias is nothing but industry words / place names."""
    rest = _generic_phrase_re().sub(" ", _fold(alias))
    return not any(len(t.strip(".,-&()")) >= 2 for t in rest.split())


def _distinctive(tokens: list[str]) -> bool:
    """A remainder is distinctive enough for exact matching."""
    if len(tokens) >= 2:
        return all(len(t) >= 3 for t in tokens)
    return len(tokens) == 1 and len(tokens[0]) >= 6


def _build_index() -> tuple[dict[str, _AliasEntry], re.Pattern[str]]:
    """(folded alias → entry, compiled matcher over folded text). Build once."""
    auto: dict[str, tuple[str, set[str]] | None] = {}
    df: Counter[str] = Counter()

    try:
        data = json.loads(_TICKERS_PATH.read_text(encoding="utf-8"))
        tickers = data.get("tickers", {}) if isinstance(data, dict) else {}
        for meta in tickers.values():
            if not isinstance(meta, dict):
                continue
            toks: set[str] = set()
            for raw in (meta.get("short_name"), meta.get("company_name")):
                if raw:
                    toks |= {_fold(t) for t in str(raw).split()}
            df.update(toks)
        for symbol, meta in tickers.items():
            if not isinstance(meta, dict):
                continue
            for raw in (meta.get("short_name"), meta.get("company_name")):
                if not raw:
                    continue
                tokens = _strip_generic(str(raw).split())
                if not _distinctive(tokens):
                    continue
                original = " ".join(tokens)
                folded = _fold(original)
                if len(folded) < 5 or is_generic_alias(original):
                    continue  # short/bare word, or prose ("bất động sản", "hải phòng")
                if folded in auto and auto[folded] is not None:
                    prev = auto[folded]
                    if prev is not None and prev[0] != symbol:
                        auto[folded] = None  # ambiguous across symbols: never index
                    elif prev is not None:
                        prev[1].add(_norm_spelling(original))
                elif folded not in auto:
                    auto[folded] = (symbol, {_norm_spelling(original)})
    except Exception as exc:  # fail-open: wire tagging without the registry
        logger.warning("alias_index_registry_load_failed", error=type(exc).__name__)

    entries: dict[str, _AliasEntry] = {}
    for folded, val in auto.items():
        if val is None:
            continue
        symbol, originals = val
        rare = len(folded.split()) >= 2 and any(df[t] <= _RARE_DF for t in folded.split())
        entries[folded] = _AliasEntry(symbol, frozenset(originals), False, rare, False)

    for alias, symbol in _CURATED_ALIASES:
        # curated wins over auto (ambiguity resolved by the explicit entry)
        folded = _fold(alias)
        case_sensitive = alias.isascii() and alias.isupper() and len(alias) <= 5
        prev_entry = entries.get(folded)
        originals = {_norm_spelling(alias)}
        if prev_entry is not None and prev_entry.curated and prev_entry.symbol == symbol:
            originals |= set(prev_entry.originals)
        entries[folded] = _AliasEntry(symbol, frozenset(originals), True, True, case_sensitive)

    if entries:
        alternation = "|".join(re.escape(a) for a in sorted(entries, key=len, reverse=True))
        matcher = re.compile(rf"(?<![\w]){'(?:' + alternation + ')'}(?![\w])", re.UNICODE)
    else:
        matcher = re.compile(r"\Z\A")  # never matches
    return entries, matcher


@lru_cache(maxsize=1)
def _alias_index() -> tuple[dict[str, _AliasEntry], re.Pattern[str]]:
    return _build_index()


def _proper_noun_cased(span: str) -> bool:
    """Every distinctive (non-industry) syllable starts uppercase, or the span
    is all caps."""
    letters = [c for c in span if c.isalpha()]
    if letters and all(c.isupper() for c in letters):
        return True
    generic = _generic_syllables()
    for syl in span.split():
        core = syl.strip(".,-&()'\"")
        if not core or not core[0].isalpha():
            continue
        if _fold(core) in generic:
            continue
        if not core[0].isupper():
            return False
    return True


def _confidence(entry: _AliasEntry, span: str) -> float | None:
    """Validate one hit; ``None`` rejects it."""
    if entry.case_sensitive and span != span.upper():
        return None
    has_marks = not span.isascii()
    if has_marks:
        if _norm_spelling(span) not in entry.originals:
            return None  # "truyền thống" is not "Truyền thông"
        folded_only = False
    else:
        # ASCII span: exact when the original is ASCII too, else EN/unaccented copy.
        folded_only = all(not o.isascii() for o in entry.originals)
    if not entry.curated and not _proper_noun_cased(span):
        return None
    conf = _CURATED_CONFIDENCE if entry.curated else _AUTO_CONFIDENCE
    if not entry.curated and entry.rare:
        conf += _RARE_TOKEN_BONUS
    if folded_only:
        conf -= _FOLDED_ONLY_PENALTY
    return round(min(conf, 0.99), 3)


def resolve_alias_matches(text: str) -> list[AliasMatch]:
    """All validated alias hits in ``text`` (NFC offsets), first-occurrence order."""
    if not text or not text.strip():
        return []
    nfc = unicodedata.normalize("NFC", text)
    folded, idx_map = _fold_with_map(nfc)
    entries, matcher = _alias_index()
    out: list[AliasMatch] = []
    for m in matcher.finditer(folded):
        entry = entries.get(m.group(0))
        if entry is None or m.end() <= m.start():
            continue
        start = idx_map[m.start()]
        end = idx_map[m.end() - 1] + 1
        span = nfc[start:end]
        conf = _confidence(entry, span)
        if conf is None:
            continue
        out.append(AliasMatch(entry.symbol, span, start, end, conf, entry.curated))
    return out


def resolve_alias_tickers(text: str) -> list[str]:
    """Stage B: canonical tickers for corporate/brand aliases found in ``text``
    (deduped, first-occurrence order)."""
    out: list[str] = []
    for hit in resolve_alias_matches(text):
        if hit.symbol not in out:
            out.append(hit.symbol)
    return out


def reset_alias_index() -> None:
    """Test hook: force a rebuild after registry changes."""
    _alias_index.cache_clear()
    _generic_phrase_re.cache_clear()
    _generic_syllables.cache_clear()
