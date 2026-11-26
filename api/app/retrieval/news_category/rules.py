"""News-category rules with reasons (docs/35 M2).

Live sample 2026-11-23..26 showed ``regulatory`` assigned to "TP.HCM thu hồi
khu đất" (land reclamation), "Iran … cảnh báo nguy cơ thủy lôi" (a geopolitical
warning) and "Quốc hội chốt trần phạt hành chính" (a law, no sanctioned party),
and ``listing_status`` to a US IPO. ``regulatory`` is the most expensive class
to get wrong (consumers surface it first), so it now needs BOTH an enforcement
term AND a sanctioned/supervised subject (a tagged ticker or a named regulator /
exchange); a bare "cảnh báo" needs exchange context. ``listing_status`` needs a
Vietnamese exchange subject; foreign listings are ``general_finance`` with scope
``global``.
"""

from __future__ import annotations

import re

# Enforcement vocabulary (each counts only with a subject — see has_subject()).
ENFORCEMENT_TERMS: tuple[str, ...] = (
    "xử phạt",
    "bị phạt",
    "phạt tiền",
    "phạt và truy thu",
    "truy thu",
    "cưỡng chế",
    "đình chỉ",
    "tạm đình chỉ",
    "hạn chế giao dịch",
    "diện kiểm soát",
    "diện cảnh báo",
    "diện hạn chế",
    "cắt margin",
    "khởi tố",
    "bắt tạm giam",
    "tạm giam",
    "truy tố",
    "vi phạm",
    "sai phạm",
    "sai sót",
    "kết luận thanh tra",
    "yêu cầu giải trình",
    "thu hồi giấy phép",
    "thu hồi giấy chứng nhận",
    "hủy bỏ tư cách",
    "huỷ bỏ tư cách",
    "tước quyền",
    "enforcement",
    "sanction",
    "fined",
    "revocation",
)
# "cảnh báo"/"nhắc nhở" are regulatory only next to an exchange/regulator.
WARNING_TERMS: tuple[str, ...] = ("cảnh báo", "nhắc nhở", "chấn chỉnh")

REGULATOR_SUBJECTS: tuple[str, ...] = (
    "ubcknn",
    "ủy ban chứng khoán",
    "uỷ ban chứng khoán",
    "sở giao dịch chứng khoán",
    "hose",
    "hsx",
    "hnx",
    "upcom",
    "vsdc",
    "thanh tra",
    "cục thuế",
    "tổng cục thuế",
    "cơ quan điều tra",
    "bộ công an",
    "viện kiểm sát",
    "ngân hàng nhà nước",
    "sec ",
    "finra",
)
EXCHANGE_CONTEXT: tuple[str, ...] = (
    "hose",
    "hsx",
    "hnx",
    "upcom",
    "ubcknn",
    "ủy ban chứng khoán",
    "uỷ ban chứng khoán",
    "sở giao dịch",
    "diện cảnh báo",
    "diện kiểm soát",
)

VN_LISTING_TERMS: tuple[str, ...] = (
    "hose",
    "hsx",
    "hnx",
    "upcom",
    "niêm yết",
    "hủy niêm yết",
    "huỷ niêm yết",
    "chuyển sàn",
    "giao dịch đầu tiên",
    "tạm ngừng giao dịch",
    "giao dịch trở lại",
)

# Foreign-market markers (word-bounded): listings/sell-offs abroad -> scope "global".
_FOREIGN_RE = re.compile(
    r"(?i)(?<![\wÀ-ỹ])(?:mỹ|phố wall|wall street|nasdaq|nyse|new york|hồng kông|hong kong|"
    r"trung quốc|nhật bản|hàn quốc|châu âu|london|tokyo|thượng hải|shanghai|s&p ?500|"
    r"dow jones|nikkei|iran|israel|nga|ukraine|ấn độ|anthropic|openai)(?![\wÀ-ỹ])"
)

# Divesting/selling a SUBSIDIARY is a corporate action, not an insider trade.
_SUBSIDIARY_DEAL_RE = re.compile(
    r"(?i)(?:thoái\s+(?:hết\s+)?vốn|bán|chuyển\s+nhượng|giải\s+thể|sáp\s+nhập|thành\s+lập|lập)"
    r"[^.;:]{0,40}?công\s+ty\s+con"
)

INSIDER_ACTORS: tuple[str, ...] = (
    "quỹ ",
    "capital",
    "fund",
    "cổ đông",
    "chủ tịch",
    "tổng giám đốc",
    "phó tổng giám đốc",
    "giám đốc",
    "thành viên hđqt",
    "hđqt",
    "ban kiểm soát",
    "kế toán trưởng",
    "người nội bộ",
    "người có liên quan",
    "cổ đông lớn",
    "lãnh đạo",
    "con trai",
    "con gái",
    "vợ ",
    "chồng ",
    "em trai",
    "anh trai",
    "insider",
)

# Matched on the TITLE only: headlines state their topic; body copy mentions
# "chính sách"/"lãi suất" in passing (ETF/gold/business stories).
POLICY_TERMS: tuple[str, ...] = (
    "lãi suất điều hành",
    "chính sách",
    "sbv",
    "fed",
    "quốc hội",
    "nghị định",
    "nghị quyết của chính phủ",
    "thông tư",
    "luật ",
    "dự thảo",
    "chính phủ",
    "thủ tướng",
    "ban hành",
    "quy định mới",
    "đề xuất quy định",
    "áp thuế",
    "thuế quan",
    "thuế đối ứng",
    "mục 301",
    "ưu đãi đầu tư",
    "trung tâm tài chính",
    "ngân hàng nhà nước",
)
# Legal instruments outrank issuer vocabulary when no ticker is tagged
# ("Chính phủ ban hành nghị định về phát hành trái phiếu" is policy).
STRONG_POLICY_TERMS: tuple[str, ...] = (
    "quốc hội",
    "nghị định",
    "thông tư",
    "luật ",
    "dự thảo",
    "chính phủ",
    "thủ tướng",
    "ban hành",
    "ngân hàng nhà nước",
)
# Market-flow vocabulary (title): breadth/flows/index sessions, not any "thị trường".
MARKET_FLOW_TERMS: tuple[str, ...] = (
    "dòng tiền",
    "khối ngoại",
    "thanh khoản",
    "vn-index",
    "vnindex",
    "vn30",
    "hnx-index",
    "upcom-index",
    "mua ròng",
    "bán ròng",
    "thỏa thuận",
    "thoả thuận",
    "nhịp đập thị trường",
    "giờ giao dịch",
    "phiên sáng",
    "phiên chiều",
    "đỏ lửa",
    "chứng chỉ quỹ",
    "vietstock daily",
)
# Issuer-action vocabulary that is unambiguous on its own.
CORP_ACTION_TERMS: tuple[str, ...] = (
    "cổ tức",
    "thưởng cổ phiếu",
    "cổ phiếu thưởng",
    "chốt quyền",
    "gdkhq",
    "đhđcđ",
    "chia thưởng",
    "cổ phiếu quỹ",
)
# Issuance / capital / bond actions: need a ticker or a company subject.
_ISSUE_RE = re.compile(
    r"(?i)(?:phát\s+hành|chào\s+bán)[^.;]{0,40}?(?:cổ\s+phiếu|\bcp\b|cổ\s+phần|trái\s+phiếu|"
    r"chứng\s+quyền)|tăng\s+vốn|tất\s+toán[^.;]{0,30}?trái\s+phiếu|mua\s+lại[^.;]{0,20}?trái\s+phiếu"
)
_COMPANY_SUBJECT_RE = re.compile(
    r"(?i)(?<![\wÀ-ỹ])(?:công\s+ty|ctcp|doanh\s+nghiệp|tập\s+đoàn|chứng\s+khoán\s+[A-ZĐ])"
)
_LISTING_PHRASES: tuple[str, ...] = (
    "hủy niêm yết",
    "huỷ niêm yết",
    "hủy bỏ niêm yết",
    "huỷ bỏ niêm yết",
    "chấm dứt niêm yết",
    "ra khỏi diện",
    "niêm yết bổ sung",
    "chấp thuận niêm yết",
    "niêm yết cổ phiếu",
    "lễ niêm yết",
    "đăng ký giao dịch",
    "chào sàn",
    "giao dịch đầu tiên",
    "chuyển sàn",
    "tạm ngừng giao dịch",
    "giao dịch trở lại",
    "delisting",
    "trading halt",
)


def is_listing_phrase(low: str) -> str | None:
    return first_term(low, _LISTING_PHRASES)


def issuer_action(text: str, *, has_syms: bool) -> str | None:
    """Issuance/capital/bond action, only with a ticker or a company subject."""
    m = _ISSUE_RE.search(text)
    if not m:
        return None
    if has_syms or _COMPANY_SUBJECT_RE.search(text):
        return m.group(0)
    return None


def first_term(low: str, terms: tuple[str, ...]) -> str | None:
    for t in terms:
        if t in low:
            return t
    return None


def is_foreign(low: str) -> bool:
    return bool(_FOREIGN_RE.search(low))


def subsidiary_deal(low: str) -> str | None:
    m = _SUBSIDIARY_DEAL_RE.search(low)
    return m.group(0) if m else None
