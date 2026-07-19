"""Tests for classifying the customer's reply to the "save the code?" prompt."""

from __future__ import annotations

import pytest

from bcworker import decision


@pytest.mark.parametrize(
    "text",
    [
        "так",
        "Так!",
        "так, будь ласка",
        "збережи",
        "зберегти",
        "збережіть",
        "ага",
        "ок",
        "окей",
        "yes",
        "давай",
        "звісно",
        "+",
        "👍",
        "супер 👌",
    ],
)
def test_save(text: str) -> None:
    assert decision.classify(text) == decision.SAVE


@pytest.mark.parametrize(
    "text",
    [
        "ні",
        "Ні, не треба",
        "не зберігай",
        "не зберегти",
        "не варто",
        "no",
        "👎",
        "видали це",
    ],
)
def test_discard(text: str) -> None:
    assert decision.classify(text) == decision.DISCARD


@pytest.mark.parametrize("text", ["дякую", "коли буде готово?", "", "   ", "хм", "🤔"])
def test_unclear(text: str) -> None:
    assert decision.classify(text) is None


def test_discard_wins_on_mixed_signal() -> None:
    assert decision.classify("так, але ні") == decision.DISCARD
    assert decision.classify("збережи 👎") == decision.DISCARD


def test_boost_is_save_unless_explicit_no() -> None:
    assert decision.classify_boost("🎉") == decision.SAVE
    assert decision.classify_boost("❤️") == decision.SAVE
    assert decision.classify_boost("") == decision.SAVE
    assert decision.classify_boost("👎") == decision.DISCARD
    assert decision.classify_boost("ні") == decision.DISCARD
