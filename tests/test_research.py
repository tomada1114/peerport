"""Tests for `peerport.mate.research` (word-threshold, digest, title, #24)."""

from __future__ import annotations

from peerport.mate.research import (
    WORD_THRESHOLD,
    digest_of,
    exceeds_threshold,
    title_from_request,
    word_count,
)


class TestWordCount:
    def test_counts_whitespace_separated_words(self) -> None:
        assert word_count("one two three") == 3

    def test_empty_string_is_zero(self) -> None:
        assert word_count("") == 0


class TestExceedsThreshold:
    def test_exactly_300_words_stays_inline(self) -> None:
        text = " ".join(["word"] * WORD_THRESHOLD)
        assert exceeds_threshold(text) is False

    def test_301_words_exceeds(self) -> None:
        text = " ".join(["word"] * (WORD_THRESHOLD + 1))
        assert exceeds_threshold(text) is True


class TestDigestOf:
    def test_takes_first_two_sentences(self) -> None:
        text = "First sentence. Second sentence. Third sentence should be dropped."
        digest = digest_of(text)
        assert digest == "First sentence. Second sentence."

    def test_fewer_than_two_sentences_returns_all(self) -> None:
        assert digest_of("Only one sentence.") == "Only one sentence."


class TestTitleFromRequest:
    def test_strips_trailing_punctuation_and_capitalizes(self) -> None:
        assert (
            title_from_request("look into tide patterns?") == "Look into tide patterns"
        )

    def test_empty_request_falls_back_to_default(self) -> None:
        assert title_from_request("   ") == "Research Notes"

    def test_long_request_is_truncated(self) -> None:
        long_text = "look into " + "x" * 200
        title = title_from_request(long_text)
        assert len(title) <= 80
