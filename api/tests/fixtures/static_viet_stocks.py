"""Static Vietnamese stock and forensics fixtures for unit tests.

These static datasets were moved out of production app runtime as part of WP-09
(Financial Data Integrity) to guarantee that the production engine only serves
real-time, verifiable market and financial data.
"""

from __future__ import annotations

from typing import Any


class MockCompanyFinancials:
    def __init__(self, **kwargs: Any) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)


# 13 sample companies for testing
VIET_STOCKS_FIXTURE: dict[str, dict[str, Any]] = {
    "FPT": {
        "symbol": "FPT",
        "name": "Công ty Cổ phần FPT",
        "exchange": "HSX",
        "industry": "Công nghệ thông tin & Viễn thông",
        "pe": 24.5,
        "pb": 5.8,
        "roe_pct": 28.5,
        "roa_pct": 11.2,
    },
    "HPG": {
        "symbol": "HPG",
        "name": "Tập đoàn Hòa Phát",
        "exchange": "HSX",
        "industry": "Thép & Vật liệu xây dựng",
        "pe": 12.8,
        "pb": 1.6,
        "roe_pct": 14.2,
        "roa_pct": 7.8,
    },
}

FORENSICS_FIXTURE: dict[str, dict[str, Any]] = {
    "FPT": {
        "symbol": "FPT",
        "year": 2024,
        "industry_type": "manufacturing",
        "piotroski_f_score": 8,
        "altman_z_score": 3.85,
        "beneish_m_score": -2.45,
    },
    "HPG": {
        "symbol": "HPG",
        "year": 2024,
        "industry_type": "manufacturing",
        "piotroski_f_score": 7,
        "altman_z_score": 2.45,
        "beneish_m_score": -2.25,
    },
}
