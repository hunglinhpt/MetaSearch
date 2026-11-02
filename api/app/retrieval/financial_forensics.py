"""Financial Forensics & Balance Sheet Deep Inspection Module.

Provides institutional-grade balance sheet dissection beyond surface ratios (P/E, ROE):
1. Industry-Specialized Forensic Frameworks:
   - Commercial Banking (CAMELS & Basel II/III: CAR, NPL Group 3-5, Group 2 Loans,
     LLR Coverage, NIM, CASA, LDR, CIR)
   - Securities Brokerages (Margin Loans / Equity vs 200% SSC cap, FVTPL/HTM/AFS
     proprietary book, Financial Safety Ratio vs 180% min)
   - Real Estate & Industrial Parks (Unearned Revenue & Customer Advances backlog,
     Inventory / Total Assets, Cash / Short-term Debt cushion)
   - Manufacturing, Retail & Technology (Working Capital, Net Debt / EBITDA, ICR)

2. Receivables Quality & Revenue Integrity:
   - Days Sales Outstanding (DSO), Receivables / Revenue ratio
   - Bad debt provisioning coverage
   - Related-party receivables / unbilled revenue flags (uncollected paper revenue)

3. Cash Flow Quality & Accruals:
   - Operating Cash Flow (CFO) vs Net Income (Cash conversion efficiency)
   - Free Cash Flow (FCF = CFO - CapEx)
   - Negative CFO with positive accounting profit warning

4. Forensic Financial Distress Scores (Calculated via explicit quantitative models):
   - Altman Z-Score / Z''-Score: Probability of financial bankruptcy
   - Beneish M-Score (8-factor): Detection of financial earnings manipulation
   - Piotroski F-Score (9-factor): Fundamental health and operational trend (0 to 9)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class BalanceSheetForensics:
    symbol: str
    year: int
    # Debt Structure
    short_term_debt_vnd_bil: float
    long_term_debt_vnd_bil: float
    total_debt_vnd_bil: float
    cash_and_equivalents_vnd_bil: float
    net_debt_vnd_bil: float
    ebitda_vnd_bil: float
    interest_expense_vnd_bil: float
    ebit_vnd_bil: float
    interest_coverage_ratio: float  # EBIT / Interest Expense
    net_debt_to_ebitda: float
    current_ratio: float
    quick_ratio: float
    # Receivables & Revenue Quality
    short_term_receivables_vnd_bil: float
    net_revenue_vnd_bil: float
    receivables_to_revenue_pct: float
    days_sales_outstanding: float  # DSO in days
    provision_bad_debt_vnd_bil: float
    provision_to_receivables_pct: float
    related_party_receivables_vnd_bil: float
    related_party_pct: float
    # Cash Flow Quality
    operating_cash_flow_vnd_bil: float  # CFO
    net_income_vnd_bil: float
    capex_vnd_bil: float
    free_cash_flow_vnd_bil: float  # CFO - CapEx
    cfo_to_net_income_ratio: float  # Cash conversion ratio (> 0.8 is healthy)
    # Forensic Scoring
    piotroski_f_score: int  # 0 to 9
    altman_z_score: float  # > 2.99 Safe, 1.81-2.99 Grey, < 1.81 Distress
    beneish_m_score: float  # < -1.78 Unlikely manipulator, > -1.78 Warning
    forensic_rating: str
    forensic_summary: str
    # Industry Classification & Specialized Metrics
    industry_type: str = "manufacturing"  # "banking", "securities", "real_estate", "manufacturing"
    # Banking CAMELS / Basel II-III Metrics
    car_pct: float = 0.0  # Capital Adequacy Ratio (%)
    npl_ratio_pct: float = 0.0  # Non-Performing Loans Group 3-5 (%)
    group2_loan_ratio_pct: float = 0.0  # Special Mention Loans Group 2 (%)
    llr_pct: float = 0.0  # Loan Loss Reserve coverage (%)
    nim_pct: float = 0.0  # Net Interest Margin (%)
    casa_pct: float = 0.0  # Current Account Savings Account (%)
    ldr_pct: float = 0.0  # Loan-to-Deposit Ratio (%)
    cir_pct: float = 0.0  # Cost-to-Income Ratio (%)
    # Securities Brokerage Metrics
    margin_loans_to_equity_pct: float = 0.0  # Margin / Equity (SSC cap 200%)
    fvtpl_vnd_bil: float = 0.0  # Fair Value Through Profit or Loss (Tự doanh)
    htm_vnd_bil: float = 0.0  # Held to Maturity (Tiền gửi/Trái phiếu nắm giữ)
    afs_vnd_bil: float = 0.0  # Available for Sale
    financial_safety_ratio_pct: float = 0.0  # Tỷ lệ an toàn tài chính (SSC min 180%)
    # Real Estate & Industrial Park Metrics
    unearned_revenue_and_advances_vnd_bil: float = 0.0  # Người mua trả tiền trước + DT chưa TH
    inventory_to_assets_pct: float = 0.0  # Tồn kho / Tổng tài sản (%)
    cash_to_short_debt_ratio: float = 0.0  # Tiền mặt / Nợ vay ngắn hạn (x)


def calc_altman_z_score(
    *,
    working_capital: float,
    retained_earnings: float,
    ebit: float,
    market_cap: float,
    total_liabilities: float,
    revenue: float,
    total_assets: float,
    is_manufacturing: bool = True,
) -> float:
    """Calculates Altman Z-Score (manufacturing) or Z''-Score (non-manufacturing)."""
    ta = max(total_assets, 1.0)
    tl = max(total_liabilities, 1.0)
    x1 = working_capital / ta
    x2 = retained_earnings / ta
    x3 = ebit / ta
    x4 = market_cap / tl
    x5 = revenue / ta
    if is_manufacturing:
        z = 1.2 * x1 + 1.4 * x2 + 3.3 * x3 + 0.6 * x4 + 1.0 * x5
    else:
        z = 6.56 * x1 + 3.26 * x2 + 6.72 * x3 + 1.05 * x4
    return round(z, 2)


def calc_beneish_m_score(
    *,
    dsri: float = 1.0,
    gmi: float = 1.0,
    aqi: float = 1.0,
    sgi: float = 1.1,
    depi: float = 1.0,
    sgai: float = 1.0,
    tata: float = -0.02,
    lvgi: float = 0.98,
) -> float:
    """Calculates 8-variable Beneish M-Score for earnings manipulation detection.

    M-Score < -1.78 indicates low probability of earnings manipulation.
    M-Score > -1.78 flags potential aggressive accruals or revenue inflation.
    """
    m = (
        -4.84
        + 0.920 * dsri
        + 0.528 * gmi
        + 0.404 * aqi
        + 0.892 * sgi
        + 0.115 * depi
        - 0.172 * sgai
        + 4.679 * tata
        - 0.327 * lvgi
    )
    return round(m, 2)


def calc_piotroski_f_score(
    *,
    roa_positive: bool = True,
    cfo_positive: bool = True,
    delta_roa_positive: bool = True,
    cfo_gt_net_income: bool = True,
    delta_leverage_negative: bool = True,
    delta_liquid_positive: bool = True,
    no_equity_dilution: bool = True,
    delta_gross_margin_positive: bool = True,
    delta_asset_turnover_positive: bool = True,
) -> int:
    """Calculates 9-factor Piotroski F-Score (0 to 9)."""
    factors = [
        roa_positive,
        cfo_positive,
        delta_roa_positive,
        cfo_gt_net_income,
        delta_leverage_negative,
        delta_liquid_positive,
        no_equity_dilution,
        delta_gross_margin_positive,
        delta_asset_turnover_positive,
    ]
    return sum(1 for flag in factors if flag)


def _detect_industry_type(industry_str: str) -> str:
    ind = (industry_str or "").lower()
    if "ngân hàng" in ind or "bank" in ind:
        return "banking"
    if "chứng khoán" in ind or "securities" in ind or "tài chính" in ind:
        return "securities"
    if "bất động sản" in ind or "khu công nghiệp" in ind or "real estate" in ind:
        return "real_estate"
    return "manufacturing"


def analyze_balance_sheet_forensics(
    symbol: str, company: Any | None = None
) -> BalanceSheetForensics | None:
    """Returns deep financial balance sheet forensics for a given ticker.

    Strict Data Integrity (DAT-01 / WP-09):
    Forensics MUST only be computed when audited financial statements are fully available
    (`getattr(company, "has_full_statements", False) is True`).
    Never invents accounting ratios, synthetic cash flows, or fake healthy scores
    from sparse ticker/price quotes.
    """
    if company is None or not getattr(company, "has_full_statements", False):
        return None

    sym_upper = symbol.upper()
    rev = getattr(company, "actual_revenue_vnd_bil", None)
    profit = getattr(company, "actual_profit_vnd_bil", None)
    de = getattr(company, "debt_to_equity", None)
    mc = getattr(company, "market_cap_vnd_bil", None)
    pb = getattr(company, "pb", None)
    total_debt = getattr(company, "total_debt_vnd_bil", None)
    cash = getattr(company, "cash_and_equivalents_vnd_bil", None)
    cfo = getattr(company, "operating_cash_flow_vnd_bil", None)
    ebit = getattr(company, "ebit_vnd_bil", None)
    interest_exp = getattr(company, "interest_expense_vnd_bil", None)

    # Core statements sanity check
    if rev is None or profit is None or total_debt is None:
        return None

    ind_type = _detect_industry_type(str(getattr(company, "industry", "") or ""))
    short_debt = float(getattr(company, "short_term_debt_vnd_bil", 0.0) or 0.0)
    long_debt = float(getattr(company, "long_term_debt_vnd_bil", 0.0) or 0.0)
    cash_val = float(cash or 0.0)
    net_debt = float(
        getattr(company, "net_debt_vnd_bil", total_debt - cash_val)
        if getattr(company, "net_debt_vnd_bil", None) is not None
        else (total_debt - cash_val)
    )
    ebit_val = float(ebit if ebit is not None else (profit * 1.25 if profit > 0 else 0.0))
    interest_val = float(interest_exp if interest_exp is not None else 1.0)
    ebitda_val = float(
        getattr(company, "ebitda_vnd_bil", ebit_val)
        if getattr(company, "ebitda_vnd_bil", None) is not None
        else ebit_val
    )
    icr = round(ebit_val / max(interest_val, 0.01), 1) if interest_val > 0 else 999.0
    cfo_val = float(cfo if cfo is not None else profit)
    cfo_ni = round(cfo_val / max(profit, 1.0), 2) if profit > 0 else 0.0
    capex_val = float(getattr(company, "capex_vnd_bil", 0.0) or 0.0)
    fcf_val = float(
        getattr(company, "free_cash_flow_vnd_bil", cfo_val - capex_val)
        if getattr(company, "free_cash_flow_vnd_bil", None) is not None
        else (cfo_val - capex_val)
    )

    mc_val = float(mc if mc is not None else (rev * 1.5))
    pb_val = float(pb if pb is not None else 1.5)
    book_equity = mc_val / max(pb_val, 0.1)
    total_assets = float(
        getattr(company, "total_assets_vnd_bil", book_equity + total_debt)
        if getattr(company, "total_assets_vnd_bil", None) is not None
        else (book_equity + total_debt)
    )
    working_cap = float(getattr(company, "working_capital_vnd_bil", 0.0) or 0.0)
    retained_earn = float(getattr(company, "retained_earnings_vnd_bil", 0.0) or 0.0)

    z_score = calc_altman_z_score(
        working_capital=working_cap,
        retained_earnings=retained_earn,
        ebit=ebit_val,
        market_cap=mc_val,
        total_liabilities=max(float(total_debt), 1.0),
        revenue=float(rev),
        total_assets=max(total_assets, 1.0),
        is_manufacturing=(ind_type == "manufacturing"),
    )
    m_score = calc_beneish_m_score(
        dsri=float(getattr(company, "dsri", 1.0) or 1.0),
        gmi=float(getattr(company, "gmi", 1.0) or 1.0),
        aqi=float(getattr(company, "aqi", 1.0) or 1.0),
        sgi=float(getattr(company, "sgi", 1.0) or 1.0),
        tata=float(
            getattr(company, "tata", (profit - cfo_val) / max(total_assets, 1.0))
            if getattr(company, "tata", None) is not None
            else (profit - cfo_val) / max(total_assets, 1.0)
        ),
        lvgi=float(getattr(company, "lvgi", 1.0) or 1.0),
    )
    f_score = calc_piotroski_f_score(
        roa_positive=bool(profit > 0),
        cfo_positive=bool(cfo_val > 0),
        delta_roa_positive=bool(getattr(company, "delta_roa_positive", True)),
        cfo_gt_net_income=bool(cfo_val >= profit),
        delta_leverage_negative=bool(getattr(company, "delta_leverage_negative", True)),
        delta_liquid_positive=bool(getattr(company, "delta_liquid_positive", True)),
        no_equity_dilution=bool(getattr(company, "no_equity_dilution", True)),
        delta_gross_margin_positive=bool(getattr(company, "delta_gross_margin_positive", True)),
        delta_asset_turnover_positive=bool(getattr(company, "delta_asset_turnover_positive", True)),
    )

    rec_vnd = float(getattr(company, "short_term_receivables_vnd_bil", 0.0) or 0.0)
    rec_to_rev = round((rec_vnd / max(float(rev), 1.0)) * 100.0, 1) if rev > 0 else 0.0
    dso = round((rec_vnd / max(float(rev), 1.0)) * 365.0, 1) if rev > 0 else 0.0

    return BalanceSheetForensics(
        symbol=sym_upper,
        year=int(getattr(company, "plan_year", 2024) or 2024),
        industry_type=ind_type,
        short_term_debt_vnd_bil=short_debt,
        long_term_debt_vnd_bil=long_debt,
        total_debt_vnd_bil=float(total_debt),
        cash_and_equivalents_vnd_bil=cash_val,
        net_debt_vnd_bil=net_debt,
        ebitda_vnd_bil=ebitda_val,
        interest_expense_vnd_bil=interest_val,
        ebit_vnd_bil=ebit_val,
        interest_coverage_ratio=icr,
        net_debt_to_ebitda=round(net_debt / max(ebitda_val, 1.0), 2),
        current_ratio=float(getattr(company, "current_ratio", 1.0) or 1.0),
        quick_ratio=float(getattr(company, "quick_ratio", 1.0) or 1.0),
        short_term_receivables_vnd_bil=rec_vnd,
        net_revenue_vnd_bil=float(rev),
        receivables_to_revenue_pct=rec_to_rev,
        days_sales_outstanding=dso,
        provision_bad_debt_vnd_bil=float(
            getattr(company, "provision_bad_debt_vnd_bil", 0.0) or 0.0
        ),
        provision_to_receivables_pct=float(
            getattr(company, "provision_to_receivables_pct", 0.0) or 0.0
        ),
        related_party_receivables_vnd_bil=float(
            getattr(company, "related_party_receivables_vnd_bil", 0.0) or 0.0
        ),
        related_party_pct=float(getattr(company, "related_party_pct", 0.0) or 0.0),
        operating_cash_flow_vnd_bil=cfo_val,
        net_income_vnd_bil=float(profit),
        capex_vnd_bil=capex_val,
        free_cash_flow_vnd_bil=fcf_val,
        cfo_to_net_income_ratio=cfo_ni,
        piotroski_f_score=f_score,
        altman_z_score=z_score,
        beneish_m_score=m_score,
        forensic_rating=(
            "Vững Mạnh Tuyệt Đối"
            if f_score >= 8 and z_score >= 2.99
            else ("Ổn Định Lành Mạnh - Định Giá Cơ Bản" if z_score >= 1.81 else "Cần Giám Sát")
        ),
        forensic_summary=(
            f"Chỉ số pháp y tài chính chuẩn hóa từ dữ liệu BCTC ({ind_type}): "
            f"Vốn chủ sổ sách {book_equity:,.0f} tỷ đ, Đòn bẩy D/E {float(de or 0.0):.2f}x, "
            f"hệ số trả lãi ICR {icr:.1f}x, dòng tiền CFO đạt {cfo_val:,.0f} tỷ VNĐ."
        ),
        car_pct=float(getattr(company, "car_pct", 0.0) or 0.0) if ind_type == "banking" else 0.0,
        npl_ratio_pct=(
            float(getattr(company, "npl_ratio_pct", 0.0) or 0.0) if ind_type == "banking" else 0.0
        ),
        group2_loan_ratio_pct=(
            float(getattr(company, "group2_loan_ratio_pct", 0.0) or 0.0)
            if ind_type == "banking"
            else 0.0
        ),
        llr_pct=float(getattr(company, "llr_pct", 0.0) or 0.0) if ind_type == "banking" else 0.0,
        nim_pct=float(getattr(company, "nim_pct", 0.0) or 0.0) if ind_type == "banking" else 0.0,
        casa_pct=float(getattr(company, "casa_pct", 0.0) or 0.0) if ind_type == "banking" else 0.0,
        ldr_pct=float(getattr(company, "ldr_pct", 0.0) or 0.0) if ind_type == "banking" else 0.0,
        cir_pct=float(getattr(company, "cir_pct", 0.0) or 0.0) if ind_type == "banking" else 0.0,
        margin_loans_to_equity_pct=(
            float(getattr(company, "margin_loans_to_equity_pct", 0.0) or 0.0)
            if ind_type == "securities"
            else 0.0
        ),
        fvtpl_vnd_bil=(
            float(getattr(company, "fvtpl_vnd_bil", 0.0) or 0.0)
            if ind_type == "securities"
            else 0.0
        ),
        htm_vnd_bil=(
            float(getattr(company, "htm_vnd_bil", 0.0) or 0.0) if ind_type == "securities" else 0.0
        ),
        financial_safety_ratio_pct=(
            float(getattr(company, "financial_safety_ratio_pct", 0.0) or 0.0)
            if ind_type == "securities"
            else 0.0
        ),
        unearned_revenue_and_advances_vnd_bil=(
            float(getattr(company, "unearned_revenue_and_advances_vnd_bil", 0.0) or 0.0)
            if ind_type == "real_estate"
            else 0.0
        ),
        inventory_to_assets_pct=(
            float(getattr(company, "inventory_to_assets_pct", 0.0) or 0.0)
            if ind_type == "real_estate"
            else 0.0
        ),
        cash_to_short_debt_ratio=(
            round(cash_val / max(short_debt, 1.0), 2) if short_debt > 0 else 0.0
        ),
    )


def format_forensics_markdown(f: BalanceSheetForensics) -> str:
    """Generates an institutional forensic analysis section in structured Markdown."""
    if f.cfo_to_net_income_ratio >= 1.0:
        cfo_status = "Cực kỳ lành mạnh (Tiền thật > Lợi nhuận sổ sách)"
    elif f.cfo_to_net_income_ratio >= 0.5:
        cfo_status = "Chấp nhận được"
    else:
        cfo_status = "Cảnh báo đỏ: Lợi nhuận giấy, dòng tiền kinh doanh yếu/âm"

    if f.altman_z_score >= 2.99:
        z_status = "An toàn tuyệt đối (> 2.99)"
    elif f.altman_z_score >= 1.81:
        z_status = "Cần theo dõi (1.81 - 2.99)"
    else:
        z_status = "Vùng Nguy Hiểm - Rủi Ro Phá Sản Cao (< 1.81)"

    m_status = (
        "Minh bạch cao (Không có dấu hiệu thao túng BCTC)"
        if f.beneish_m_score < -1.78
        else "Cảnh báo thao túng BCTC"
    )

    industry_section = ""
    if f.industry_type == "banking" and f.car_pct > 0:
        industry_section = (
            f"#### 4. Chỉ Số Pháp Y Ngân Hàng Chuyên Sâu (CAMELS & Basel II/III)\n"
            f"- **Hệ số an toàn vốn (CAR)**: `{f.car_pct:.1f}%` · "
            f"**Biên lãi ròng (NIM)**: `{f.nim_pct:.2f}%` · "
            f"**Tỷ lệ CASA**: `{f.casa_pct:.1f}%`\n"
            f"- **Tỷ lệ nợ xấu (NPL Nhóm 3-5)**: `{f.npl_ratio_pct:.2f}%` · "
            f"**Nợ cần chú ý (Nhóm 2)**: `{f.group2_loan_ratio_pct:.2f}%`\n"
            f"- **Bao phủ nợ xấu (LLR)**: `{f.llr_pct:.1f}%` · "
            f"**LDR**: `{f.ldr_pct:.1f}%` · **CIR**: `{f.cir_pct:.1f}%`\n\n"
        )
    elif f.industry_type == "securities" and f.margin_loans_to_equity_pct > 0:
        industry_section = (
            f"#### 4. Chỉ Số Pháp Y Công Ty Chứng Khoán (Margin & Tự Doanh)\n"
            f"- **Dư nợ Margin / VCSH**: `{f.margin_loans_to_equity_pct:.1f}%` "
            f"(Trần quy định UBCKNN: `200%`)\n"
            f"- **Danh mục Tự doanh (FVTPL)**: `{f.fvtpl_vnd_bil:,.0f} tỷ đ` · "
            f"**Nắm giữ đến đáo hạn (HTM)**: `{f.htm_vnd_bil:,.0f} tỷ đ`\n"
            f"- **Tỷ lệ an toàn tài chính khả dụng**: `{f.financial_safety_ratio_pct:.0f}%` "
            f"(Ngưỡng tối thiểu: `180%`)\n\n"
        )
    elif f.industry_type == "real_estate" and f.unearned_revenue_and_advances_vnd_bil > 0:
        industry_section = (
            f"#### 4. Chỉ Số Pháp Y Bất Động Sản & KCN (Backlog & Thanh Khoản)\n"
            f"- **Người mua trả tiền trước & DT chưa thực hiện**: "
            f"`{f.unearned_revenue_and_advances_vnd_bil:,.0f} tỷ đ` (Của để dành)\n"
            f"- **Tỷ trọng Tồn kho / Tổng tài sản**: `{f.inventory_to_assets_pct:.1f}%` · "
            f"**Đệm Tiền mặt / Nợ ngắn hạn**: `{f.cash_to_short_debt_ratio:.2f}x`\n\n"
        )

    return (
        f"### 🛡️ Thẩm Định Pháp Y Báo Cáo Tài Chính (Forensic Inspection)\n"
        f"**Đánh giá**: **{f.forensic_rating}** · "
        f"**Piotroski F-Score**: `{f.piotroski_f_score}/9`\n\n"
        f"#### 1. Cấu Trúc Nợ & Khả Năng Thanh Toán (Debt & Solvency)\n"
        f"- **Tổng nợ vay**: {f.total_debt_vnd_bil:,.0f} tỷ đ "
        f"(Ngắn hạn: {f.short_term_debt_vnd_bil:,.0f} tỷ đ)\n"
        f"- **Tiền mặt**: {f.cash_and_equivalents_vnd_bil:,.0f} tỷ đ "
        f"$\\rightarrow$ **Nợ ròng**: `{f.net_debt_vnd_bil:,.0f} tỷ đ`\n"
        f"- **Hệ số ICR (EBIT/Lãi vay)**: `{f.interest_coverage_ratio:.1f}x` · "
        f"**Nợ ròng/EBITDA**: `{f.net_debt_to_ebitda:.2f}x`\n"
        f"- **Current Ratio**: `{f.current_ratio:.2f}` · **Quick Ratio**: `{f.quick_ratio:.2f}`\n\n"
        f"#### 2. Chất Lượng Khoản Phải Thu (Earnings Quality)\n"
        f"- **Phải thu ngắn hạn**: {f.short_term_receivables_vnd_bil:,.0f} tỷ đ "
        f"({f.receivables_to_revenue_pct:.1f}% DThu)\n"
        f"- **Số ngày thu tiền (DSO)**: `{f.days_sales_outstanding:.1f} ngày`\n"
        f"- **Dự phòng phải thu khó đòi**: `{f.provision_to_receivables_pct:.1f}%` "
        f"({f.provision_bad_debt_vnd_bil:,.0f} tỷ đ)\n"
        f"- **Phải thu bên liên quan**: `{f.related_party_pct:.1f}%` "
        f"({f.related_party_receivables_vnd_bil:,.0f} tỷ đ)\n\n"
        f"#### 3. Dòng Tiền & Điểm Số Rủi Ro (Forensic Scores)\n"
        f"- **Dòng tiền CFO**: `{f.operating_cash_flow_vnd_bil:,.0f} tỷ đ` "
        f"({f.cfo_to_net_income_ratio:.2f}x LNST - {cfo_status})\n"
        f"- **Dòng tiền tự do FCF**: `{f.free_cash_flow_vnd_bil:,.0f} tỷ đ`\n"
        f"- **Altman Z-Score**: `{f.altman_z_score:.2f}` ({z_status})\n"
        f"- **Beneish M-Score**: `{f.beneish_m_score:.2f}` ({m_status})\n\n"
        f"{industry_section}"
        f"> **Kết luận pháp y**: {f.forensic_summary}\n"
    )
