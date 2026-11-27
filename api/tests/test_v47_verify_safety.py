"""docs/36 T1: claim-verification safety gates + gold-set scoring.

Live measurement before these gates (2026-11-26, 60-claim gold set): 16/30
FALSE claims were judged "supported" (false_support_rate 0.53). Each test below
pins one root cause found in that run."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.eval_verify import TARGET_FALSE_SUPPORT_RATE, score
from app.orchestration import claim_verify as cv

GOLD = Path(__file__).resolve().parent.parent / "evals" / "verify_golden.json"


def test_years_are_key_figures() -> None:
    nums, _ = cv.key_terms("Apple Inc. was founded in 1985")
    assert "1985" in nums
    a = cv.assess_chunk(
        "Apple Inc. was founded in 1985",
        "Apple was founded in 1976 by Steve Jobs, Steve Wozniak and Ronald Wayne.",
    )
    assert a.stance != "supports" and "1985" in a.missing_terms


def test_legal_suffix_is_not_an_entity() -> None:
    _, words = cv.key_terms("Apple Inc. was founded in 1976 by Steve Jobs")
    assert "inc" not in words and "apple" in words
    a = cv.assess_chunk(
        "Apple Inc. was founded in 1976 by Steve Jobs",
        "Apple was founded in 1976 by Steve Jobs and Steve Wozniak in California.",
    )
    assert a.stance == "supports"


def test_entities_must_share_one_sentence() -> None:
    # Two different sentences each hold half of the claim's entities.
    a = cv.assess_chunk(
        "Hà Nội là thủ đô của Thái Lan",
        "Hà Nội là thủ đô của Việt Nam. Bangkok là thủ đô của Thái Lan.",
    )
    assert a.stance != "supports"


def test_missing_entity_blocks_support() -> None:
    a = cv.assess_chunk(
        "The Federal Reserve is the central bank of Canada",
        "The Federal Reserve is the central bank of the United States.",
    )
    assert a.stance != "supports" and "canada" in a.missing_terms


def test_relation_words_must_be_present() -> None:
    a = cv.assess_chunk(
        "SpaceX was founded by Elon Musk in 2010",
        "A 2010 SpaceX office tour with Elon Musk shows the early days.",
    )
    assert a.stance != "supports"
    assert (
        cv.nonkey_coverage(
            "SpaceX was founded by Elon Musk", "SpaceX tour Elon Musk", ["spacex", "elon", "musk"]
        )
        == 0.0
    )


@pytest.mark.parametrize(
    "sentence",
    [
        "Goldfish may have a longer memory span than just three seconds.",
        "Do humans only use 10 percent of their brains?",
        "It is a myth. Humans only use 10 percent of their brains, people say.",
    ],
)
def test_hedges_questions_and_myths_never_support(sentence: str) -> None:
    claim = (
        "Goldfish have a memory span of only three seconds"
        if "Goldfish" in sentence
        else "Humans only use 10 percent of their brains"
    )
    assert cv.assess_chunk(claim, sentence).stance != "supports"


def test_neighbour_cue_blocks_support_but_cannot_refute() -> None:
    claim = "Thành phố Hồ Chí Minh trước đây có tên là Sài Gòn"
    content = (
        "Thông tin sai lệch lan truyền trên mạng về quy hoạch. "
        "Thành phố Hồ Chí Minh trước đây có tên là Sài Gòn."
    )
    a = cv.assess_chunk(claim, content)
    assert a.stance == "mentions"  # neighbour cue: no support, and no refutation either


def test_flip_refute_needs_same_relation() -> None:
    # Negation in a sentence about a different relation must not refute.
    a = cv.assess_chunk(
        "Thành phố Hồ Chí Minh trước đây có tên là Sài Gòn",
        "Sinh ra sau ngày Sài Gòn được đổi tên thành Thành phố Hồ Chí Minh, họ không biết tên cũ.",
    )
    assert a.stance != "refutes"


def test_ordered_coverage() -> None:
    assert cv.ordered_coverage("a capital of b", "a is the capital of b") == 1.0
    assert cv.ordered_coverage("hanoi capital thailand", "thailand capital hanoi") < 0.6
    assert cv.ordered_coverage("", "anything") == 1.0


def test_score_safety_first() -> None:
    gold = [
        {"id": "t", "truth": True, "kind": "fact"},
        {"id": "f", "truth": False, "kind": "myth"},
        {"id": "e", "truth": False, "kind": "myth"},
    ]
    s = score(gold, {"t": "supported", "f": "refuted", "e": None})
    assert s["false_support_rate"] == 0.0 and s["errors"] == 1 and s["passed"] is False
    s2 = score(gold[:2], {"t": "supported", "f": "supported"})
    assert s2["false_support_rate"] == 1.0 and s2["supported_precision"] == 0.5
    assert s2["passed"] is False and TARGET_FALSE_SUPPORT_RATE == 0.05


def test_gold_set_shape() -> None:
    data = json.loads(GOLD.read_text(encoding="utf-8"))
    claims = data["claims"]
    assert len(claims) == 60 and len({c["id"] for c in claims}) == 60
    assert sum(1 for c in claims if c["truth"]) == 30
    for c in claims:
        assert 8 <= len(c["claim"]) <= 500 and len(c["claim"].split()) >= 3


def test_refutation_cue_must_be_about_the_same_entities() -> None:
    claim = "Tiếng Việt sử dụng bảng chữ cái Latinh gọi là chữ Quốc ngữ"
    off_topic = "Bộ cho rằng các ý kiến về tiếng Việt và bảng chữ cái đều không có cơ sở."
    assert cv.assess_chunk(claim, off_topic).stance != "refutes"
    on_topic = "Myth: drinking bleach cures COVID-19 infection, doctors warn it is poison."
    assert cv.assess_chunk("Drinking bleach cures COVID-19 infection", on_topic).stance == "refutes"


def test_figures_must_be_anchored_to_the_same_relation() -> None:
    claim = "Việt Nam gia nhập Tổ chức Thương mại Thế giới WTO năm 1995"
    other_event = (
        "Việt Nam gia nhập Tổ chức Thương mại Thế giới WTO năm 2007, "
        "tổ chức này được thành lập từ năm 1995."
    )
    assert cv.assess_chunk(claim, other_event).stance != "supports"
    good = "Apple was founded in 1976 by Steve Jobs and Steve Wozniak in California."
    assert cv.figures_anchored("Apple was founded in 1976 by Steve Jobs", good, ["apple"])
    assert (
        cv.assess_chunk(
            "GDP Việt Nam năm 2024 tăng 7,09%",
            "Theo Tổng cục Thống kê, GDP Việt Nam năm 2024 tăng 7.09% so với năm trước.",
        ).stance
        == "supports"
    )


def test_intention_is_not_completion() -> None:
    claim = "Việt Nam gia nhập Tổ chức Thương mại Thế giới WTO năm 1995"
    applied = (
        "Ngày 4/1/1995, Tổ chức Thương mại Thế giới (WTO) chấp nhận đơn xin gia nhập của Việt Nam."
    )
    assert cv.assess_chunk(claim, applied).stance != "supports"
    assert (
        cv.assess_chunk(
            "Công ty dự kiến trả cổ tức năm 2026",
            "Công ty dự kiến trả cổ tức năm 2026 theo nghị quyết.",
        ).stance
        == "supports"
    )  # a hedged claim may be supported by the same hedge
