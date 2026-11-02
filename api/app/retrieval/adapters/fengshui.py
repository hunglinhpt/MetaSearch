"""Feng Shui & Eastern Metaphysics adapter — geomancy, Can Chi, Bat Trach & culture.

Computes exact algorithmic calculations:
- Thập Thiên Can, Thập Nhị Địa Chi & Lục Thập Hoa Giáp (Can Chi)
- Ngũ Hành Nạp Âm & Tương Sinh / Tương Khắc
- Bát Trạch Cung Phi (Đông Tứ Mệnh vs Tây Tứ Mệnh) across all centuries
- Bát Du Niên: 4 Cát Tinh (Sinh Khí, Diên Niên, Thiên Y, Phục Vị) & 4 Hung Tinh
- Hướng nhà, hướng bàn làm việc, màu sắc bản mệnh & Tam Hợp / Tứ Hành Xung
Category: ``fengshui``.
"""

from __future__ import annotations

import re
from typing import Any

from app.retrieval.models import RawResult

# 10 Thien Can
THIEN_CAN = ["Giáp", "Ất", "Bính", "Đinh", "Mậu", "Kỷ", "Canh", "Tân", "Nhâm", "Quý"]
# 12 Dia Chi
DIA_CHI = ["Tý", "Sửu", "Dần", "Mão", "Thìn", "Tỵ", "Ngọ", "Mùi", "Thân", "Dậu", "Tuất", "Hợi"]

# Bat Trach Cung Phi names
CUNG_PHI = ["Khảm", "Khôn", "Chấn", "Tốn", "Càn", "Đoài", "Cấn", "Ly"]

# Ngu Hanh nap am 60 Hoa Giap (index = (year - 4) % 60)
LUC_THAP_HOA_GIAP: dict[int, tuple[str, str, str]] = {
    0: ("Giáp Tý", "Hải Trung Kim", "Kim"),
    1: ("Ất Sửu", "Hải Trung Kim", "Kim"),
    2: ("Bính Dần", "Lư Trung Hỏa", "Hỏa"),
    3: ("Đinh Mão", "Lư Trung Hỏa", "Hỏa"),
    4: ("Mậu Thìn", "Đại Lâm Mộc", "Mộc"),
    5: ("Kỷ Tỵ", "Đại Lâm Mộc", "Mộc"),
    6: ("Canh Ngọ", "Lộ Bàng Thổ", "Thổ"),
    7: ("Tân Mùi", "Lộ Bàng Thổ", "Thổ"),
    8: ("Nhâm Thân", "Kiếm Phong Kim", "Kim"),
    9: ("Quý Dậu", "Kiếm Phong Kim", "Kim"),
    10: ("Giáp Tuất", "Sơn Đầu Hỏa", "Hỏa"),
    11: ("Ất Hợi", "Sơn Đầu Hỏa", "Hỏa"),
    12: ("Bính Tý", "Giản Hạ Thủy", "Thủy"),
    13: ("Đinh Sửu", "Giản Hạ Thủy", "Thủy"),
    14: ("Mậu Dần", "Thành Đầu Thổ", "Thổ"),
    15: ("Kỷ Mão", "Thành Đầu Thổ", "Thổ"),
    16: ("Canh Thìn", "Bạch Lạp Kim", "Kim"),
    17: ("Tân Tỵ", "Bạch Lạp Kim", "Kim"),
    18: ("Nhâm Ngọ", "Dương Liễu Mộc", "Mộc"),
    19: ("Quý Mùi", "Dương Liễu Mộc", "Mộc"),
    20: ("Giáp Thân", "Tuyền Trung Thủy", "Thủy"),
    21: ("Ất Dậu", "Tuyền Trung Thủy", "Thủy"),
    22: ("Bính Tuất", "Ốc Thượng Thổ", "Thổ"),
    23: ("Đinh Hợi", "Ốc Thượng Thổ", "Thổ"),
    24: ("Mậu Tý", "Tích Lịch Hỏa", "Hỏa"),
    25: ("Kỷ Sửu", "Tích Lịch Hỏa", "Hỏa"),
    26: ("Canh Dần", "Tùng Bách Mộc", "Mộc"),
    27: ("Tân Mão", "Tùng Bách Mộc", "Mộc"),
    28: ("Nhâm Thìn", "Trường Lưu Thủy", "Thủy"),
    29: ("Quý Tỵ", "Trường Lưu Thủy", "Thủy"),
    30: ("Giáp Ngọ", "Sa Trung Kim", "Kim"),
    31: ("Ất Mùi", "Sa Trung Kim", "Kim"),
    32: ("Bính Thân", "Sơn Hạ Hỏa", "Hỏa"),
    33: ("Đinh Dậu", "Sơn Hạ Hỏa", "Hỏa"),
    34: ("Mậu Tuất", "Bình Địa Mộc", "Mộc"),
    35: ("Kỷ Hợi", "Bình Địa Mộc", "Mộc"),
    36: ("Canh Tý", "Bích Thượng Thổ", "Thổ"),
    37: ("Tân Sửu", "Bích Thượng Thổ", "Thổ"),
    38: ("Nhâm Dần", "Kim Bạch Kim", "Kim"),
    39: ("Quý Mão", "Kim Bạch Kim", "Kim"),
    40: ("Giáp Thìn", "Phúc Đăng Hỏa", "Hỏa"),
    41: ("Ất Tỵ", "Phúc Đăng Hỏa", "Hỏa"),
    42: ("Bính Ngọ", "Thiên Hà Thủy", "Thủy"),
    43: ("Đinh Mùi", "Thiên Hà Thủy", "Thủy"),
    44: ("Mậu Thân", "Đại Dịch Thổ", "Thổ"),
    45: ("Kỷ Dậu", "Đại Dịch Thổ", "Thổ"),
    46: ("Canh Tuất", "Thoa Xuyến Kim", "Kim"),
    47: ("Tân Hợi", "Thoa Xuyến Kim", "Kim"),
    48: ("Nhâm Tý", "Tang Đố Mộc", "Mộc"),
    49: ("Quý Sửu", "Tang Đố Mộc", "Mộc"),
    50: ("Giáp Dần", "Đại Khê Thủy", "Thủy"),
    51: ("Ất Mão", "Đại Khê Thủy", "Thủy"),
    52: ("Bính Thìn", "Sa Trung Thổ", "Thổ"),
    53: ("Đinh Tỵ", "Sa Trung Thổ", "Thổ"),
    54: ("Mậu Ngọ", "Thiên Thượng Hỏa", "Hỏa"),
    55: ("Kỷ Mùi", "Thiên Thượng Hỏa", "Hỏa"),
    56: ("Canh Thân", "Thạch Lựu Mộc", "Mộc"),
    57: ("Tân Dậu", "Thạch Lựu Mộc", "Mộc"),
    58: ("Nhâm Tuất", "Đại Hải Thủy", "Thủy"),
    59: ("Quý Hợi", "Đại Hải Thủy", "Thủy"),
}

# Bat Trach huong hop theo cung phi
BAT_TRACH_INFO: dict[str, dict[str, str]] = {
    "Khảm": {
        "nhom": "Đông Tứ Mệnh",
        "sinh_khi": "Đông Nam",
        "thien_y": "Đông",
        "dien_nien": "Nam",
        "phuc_vi": "Bắc",
        "hung": "Tây Nam (Tuyệt Mệnh), Đông Bắc (Ngũ Quỷ), Tây Bắc (Lục Sát), Tây (Họa Hại)",
    },
    "Ly": {
        "nhom": "Đông Tứ Mệnh",
        "sinh_khi": "Đông",
        "thien_y": "Đông Nam",
        "dien_nien": "Bắc",
        "phuc_vi": "Nam",
        "hung": "Tây Bắc (Tuyệt Mệnh), Tây (Ngũ Quỷ), Tây Nam (Lục Sát), Đông Bắc (Họa Hại)",
    },
    "Chấn": {
        "nhom": "Đông Tứ Mệnh",
        "sinh_khi": "Nam",
        "thien_y": "Bắc",
        "dien_nien": "Đông Nam",
        "phuc_vi": "Đông",
        "hung": "Tây (Tuyệt Mệnh), Tây Bắc (Ngũ Quỷ), Đông Bắc (Lục Sát), Tây Nam (Họa Hại)",
    },
    "Tốn": {
        "nhom": "Đông Tứ Mệnh",
        "sinh_khi": "Bắc",
        "thien_y": "Nam",
        "dien_nien": "Đông",
        "phuc_vi": "Đông Nam",
        "hung": "Đông Bắc (Tuyệt Mệnh), Tây Nam (Ngũ Quỷ), Tây (Lục Sát), Tây Bắc (Họa Hại)",
    },
    "Càn": {
        "nhom": "Tây Tứ Mệnh",
        "sinh_khi": "Tây",
        "thien_y": "Đông Bắc",
        "dien_nien": "Tây Nam",
        "phuc_vi": "Tây Bắc",
        "hung": "Nam (Tuyệt Mệnh), Đông (Ngũ Quỷ), Bắc (Lục Sát), Đông Nam (Họa Hại)",
    },
    "Khôn": {
        "nhom": "Tây Tứ Mệnh",
        "sinh_khi": "Đông Bắc",
        "thien_y": "Tây",
        "dien_nien": "Tây Bắc",
        "phuc_vi": "Tây Nam",
        "hung": "Bắc (Tuyệt Mệnh), Đông Nam (Ngũ Quỷ), Nam (Lục Sát), Đông (Họa Hại)",
    },
    "Cấn": {
        "nhom": "Tây Tứ Mệnh",
        "sinh_khi": "Tây Nam",
        "thien_y": "Tây Bắc",
        "dien_nien": "Tây",
        "phuc_vi": "Đông Bắc",
        "hung": "Đông Nam (Tuyệt Mệnh), Bắc (Ngũ Quỷ), Đông (Lục Sát), Nam (Họa Hại)",
    },
    "Đoài": {
        "nhom": "Tây Tứ Mệnh",
        "sinh_khi": "Tây Bắc",
        "thien_y": "Tây Nam",
        "dien_nien": "Đông Bắc",
        "phuc_vi": "Tây",
        "hung": "Đông (Tuyệt Mệnh), Nam (Ngũ Quỷ), Đông Nam (Lục Sát), Bắc (Họa Hại)",
    },
}

NGU_HANH_COLORS: dict[str, dict[str, str]] = {
    "Kim": {
        "tuong_sinh": "Vàng, Nâu đất (Thổ sinh Kim)",
        "tuong_hop": "Trắng, Xám, Ghi, Ánh kim",
        "tuong_khac": "Đỏ, Hồng, Cam, Tím (Hỏa khắc Kim)",
    },
    "Mộc": {
        "tuong_sinh": "Đen, Xanh nước biển (Thủy sinh Mộc)",
        "tuong_hop": "Xanh lá cây, Xanh nõn chuối",
        "tuong_khac": "Trắng, Xám, Ghi (Kim khắc Mộc)",
    },
    "Thủy": {
        "tuong_sinh": "Trắng, Xám, Ghi (Kim sinh Thủy)",
        "tuong_hop": "Đen, Xanh nước biển",
        "tuong_khac": "Vàng, Nâu đất (Thổ khắc Thủy)",
    },
    "Hỏa": {
        "tuong_sinh": "Xanh lá cây (Mộc sinh Hỏa)",
        "tuong_hop": "Đỏ, Hồng, Cam, Tím",
        "tuong_khac": "Đen, Xanh nước biển (Thủy khắc Hỏa)",
    },
    "Thổ": {
        "tuong_sinh": "Đỏ, Hồng, Cam, Tím (Hỏa sinh Thổ)",
        "tuong_hop": "Vàng, Nâu đất",
        "tuong_khac": "Xanh lá cây (Mộc khắc Thổ)",
    },
}

# Tam Hop & Tu Hanh Xung by Dia Chi
DIA_CHI_HARMONY: dict[str, dict[str, str]] = {
    "Tý": {"tam_hop": "Thân - Tý - Thìn (Tam hợp Thủy cục)", "tu_xung": "Tý - Ngọ - Mão - Dậu"},
    "Thân": {"tam_hop": "Thân - Tý - Thìn (Tam hợp Thủy cục)", "tu_xung": "Dần - Thân - Tỵ - Hợi"},
    "Thìn": {
        "tam_hop": "Thân - Tý - Thìn (Tam hợp Thủy cục)",
        "tu_xung": "Thìn - Tuất - Sửu - Mùi",
    },
    "Dần": {"tam_hop": "Dần - Ngọ - Tuất (Tam hợp Hỏa cục)", "tu_xung": "Dần - Thân - Tỵ - Hợi"},
    "Ngọ": {"tam_hop": "Dần - Ngọ - Tuất (Tam hợp Hỏa cục)", "tu_xung": "Tý - Ngọ - Mão - Dậu"},
    "Tuất": {"tam_hop": "Dần - Ngọ - Tuất (Tam hợp Hỏa cục)", "tu_xung": "Thìn - Tuất - Sửu - Mùi"},
    "Tỵ": {"tam_hop": "Tỵ - Dậu - Sửu (Tam hợp Kim cục)", "tu_xung": "Dần - Thân - Tỵ - Hợi"},
    "Dậu": {"tam_hop": "Tỵ - Dậu - Sửu (Tam hợp Kim cục)", "tu_xung": "Tý - Ngọ - Mão - Dậu"},
    "Sửu": {"tam_hop": "Tỵ - Dậu - Sửu (Tam hợp Kim cục)", "tu_xung": "Thìn - Tuất - Sửu - Mùi"},
    "Hợi": {"tam_hop": "Hợi - Mão - Mùi (Tam hợp Mộc cục)", "tu_xung": "Dần - Thân - Tỵ - Hợi"},
    "Mão": {"tam_hop": "Hợi - Mão - Mùi (Tam hợp Mộc cục)", "tu_xung": "Tý - Ngọ - Mão - Dậu"},
    "Mùi": {"tam_hop": "Hợi - Mão - Mùi (Tam hợp Mộc cục)", "tu_xung": "Thìn - Tuất - Sửu - Mùi"},
}

# Reverse lookup from Can Chi name to representative birth year (e.g. "Canh Ngọ" -> 1990)
_CAN_CHI_TO_YEAR: dict[str, int] = {
    v[0].lower(): (1984 + idx if (1984 + idx) <= 2013 else 1924 + idx)
    for idx, v in LUC_THAP_HOA_GIAP.items()
}


def _digital_root(year: int) -> int:
    """Computes the single-digit digital root (1..9) of any positive year across all centuries."""
    d_sum = sum(int(ch) for ch in str(abs(year)) if ch.isdigit())
    while d_sum > 9:
        d_sum = (d_sum // 10) + (d_sum % 10)
    return d_sum or 9


def _calc_cung_phi_nam(year: int) -> str:
    root = _digital_root(year)
    val = 11 - root
    while val > 9:
        val -= 9
    if val <= 0:
        val += 9
    mapping = {
        1: "Khảm",
        2: "Khôn",
        3: "Chấn",
        4: "Tốn",
        5: "Khôn",
        6: "Càn",
        7: "Đoài",
        8: "Cấn",
        9: "Ly",
    }
    return mapping.get(val, "Khảm")


def _calc_cung_phi_nu(year: int) -> str:
    root = _digital_root(year)
    val = root + 4
    while val > 9:
        val -= 9
    mapping = {
        1: "Khảm",
        2: "Khôn",
        3: "Chấn",
        4: "Tốn",
        5: "Cấn",
        6: "Càn",
        7: "Đoài",
        8: "Cấn",
        9: "Ly",
    }
    return mapping.get(val, "Khôn")


class FengShuiAdapter:
    """Specialized Eastern Geomancy and Feng Shui calculation adapter."""

    id = "fengshui"

    async def search(
        self,
        query: str,
        *,
        lang: str | None = None,
        categories: list[str] | None = None,
        freshness_days: int | None = None,
        max_results: int = 5,
        timeout_s: float = 3.0,
        **kwargs: Any,
    ) -> list[RawResult]:
        q = (query or "").strip()
        if not q:
            return []

        q_lower = q.lower()
        # 1. Find 4-digit years in query (supporting 1800 - 2099)
        years = [int(m) for m in re.findall(r"\b(18\d{2}|19\d{2}|20\d{2})\b", q)]

        # 2. Also match Can Chi names (e.g. "Canh Ngọ", "Giáp Tý", "Giáp Thìn") if no year given
        if not years:
            for cc_name, rep_year in _CAN_CHI_TO_YEAR.items():
                if cc_name in q_lower and rep_year not in years:
                    years.append(rep_year)

        results: list[RawResult] = []

        if years:
            for y in years[:2]:
                idx = (y - 4) % 60
                can_chi, nap_am, hanh = LUC_THAP_HOA_GIAP[idx]
                dia_chi_part = can_chi.split()[-1] if " " in can_chi else ""
                harmony = DIA_CHI_HARMONY.get(dia_chi_part, {})
                tam_hop = harmony.get("tam_hop", "")
                tu_xung = harmony.get("tu_xung", "")
                cung_nam = _calc_cung_phi_nam(y)
                cung_nu = _calc_cung_phi_nu(y)
                bt_nam = BAT_TRACH_INFO.get(cung_nam, {})
                bt_nu = BAT_TRACH_INFO.get(cung_nu, {})
                colors = NGU_HANH_COLORS.get(hanh, {})

                sk = bt_nam.get("sinh_khi")
                ty = bt_nam.get("thien_y")
                dn = bt_nam.get("dien_nien")
                pv = bt_nam.get("phuc_vi")
                sk_nu = bt_nu.get("sinh_khi")
                ty_nu = bt_nu.get("thien_y")
                c_sinh = colors.get("tuong_sinh")
                c_hop = colors.get("tuong_hop")
                c_khac = colors.get("tuong_khac")
                slug_can_chi = can_chi.lower().replace(" ", "-")

                snippet = (
                    f"Năm {y} — Can Chi: {can_chi} | Mệnh: {nap_am} ({hanh}). "
                    f"Tam hợp: {tam_hop} | Tứ hành xung: {tu_xung}. "
                    f"Nam: Cung {cung_nam} ({bt_nam.get('nhom')}), "
                    f"Hướng tốt: Sinh Khí ({sk}), Thiên Y ({ty}), "
                    f"Diên Niên ({dn}), Phục Vị ({pv}). "
                    f"Nữ: Cung {cung_nu} ({bt_nu.get('nhom')}), "
                    f"Hướng tốt: Sinh Khí ({sk_nu}), Thiên Y ({ty_nu}). "
                    f"Tương sinh: {c_sinh}; Bản mệnh: {c_hop}; Kỵ: {c_khac}."
                )

                results.append(
                    RawResult(
                        url=f"https://vi.wikipedia.org/wiki/L%E1%BB%A5c_th%E1%BA%ADp_hoa_gi%C3%A1p#{slug_can_chi}",
                        title=f"Tra Cứu Bát Trạch & Can Chi Phong Thủy Tuổi {can_chi} ({y})",
                        snippet=snippet,
                        adapter="fengshui",
                        source_engines=["fengshui"],
                        rank=len(results) + 1,
                        published_at=None,
                        source_name="Thuật toán Can Chi & Bát Trạch",
                        source_url=None,
                        as_of=None,
                        data_kind="computed",
                    )
                )

        # General Feng Shui principles snippet if broader topic
        is_office_or_house = any(
            w in q_lower
            for w in [
                "bàn làm việc",
                "nhà",
                "phòng khách",
                "cửa chính",
                "tài lộc",
                "kinh doanh",
                "bếp",
                "giường ngủ",
            ]
        )
        if is_office_or_house or not results:
            results.append(
                RawResult(
                    url="https://vi.wikipedia.org/wiki/Phong_th%E1%BB%A7y",
                    title="Nguyên Lý Phong Thủy Loan Đầu & Bát Trạch Ứng Dụng Khoa Học",
                    snippet=(
                        "Phong thủy chính phái đề cao: 'Tàng phong tụ khí' và cân bằng âm dương. "
                        "Bàn làm việc: Tựa sơn hướng thủy (lưng tựa tường, trước mặt thoáng đãng, "
                        "tránh quay lưng ra cửa sổ). Nhà ở: Cửa chính nạp khí thanh tịnh, "
                        "phòng khách tụ khí, bếp tọa hung hướng cát để thiêu đốt hung khí, "
                        "tránh xà ngang đè đỉnh và các góc nhọn chiếu vào vị trí ngồi/nằm."
                    ),
                    adapter="fengshui",
                    source_engines=["fengshui"],
                    rank=len(results) + 1,
                    published_at=None,
                    source_name="Nguyên Lý Phong Thủy Học",
                    source_url=None,
                    as_of=None,
                    data_kind="computed",
                )
            )

        return results[:max_results]
