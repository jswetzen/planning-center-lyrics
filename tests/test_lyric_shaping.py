"""
Tests for pco_client's lyric shaping: label detection, stanza splitting,
de-duplication, and line wrapping.

Every threshold and vocabulary entry here was derived from a real corpus of
19 songs / 511 lines across five Sundays on this account, so the cases below
are mostly transcriptions of things that actually appeared -- including the
three the first version missed and the one it correctly refused.
"""

import pytest

from pco_client import (
    DEFAULT_WRAP_CHARS,
    dedupe_stanzas,
    looks_like_section_label,
    split_stanzas,
    wrap_line,
    wrap_lines,
)


# --------------------------------------------------------------------------
# Label detection -- vocabulary observed in the corpus
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "label",
    [
        # Every distinct form counted in the corpus.
        "VERSE 1:", "VERSE 2:", "VERSE 3:", "VERSE 4", "VERSE:", "Verse 1:", "Verse 2:",
        "VERS 1:", "VERS 2:", "VERS 3:", "VERS 1", "VERS 2",
        "CHORUS:", "Chorus:", "CHORUS 1:", "CHORUS 2:", "CHORUS 3:",
        "REFRÄNG:", "BRIDGE:", "Bridge:", "STICK:", "BRYGGA:", "TAG:", "VAMP:",
        "INTRO:", "Intro:", "INTRO", "OUTRO:", "ENDING:", "INTERLUDE:",
        "INSTRUMENTAL:", "PRE-CHORUS:",
    ],
)
def test_recognizes_every_label_form_in_the_corpus(label):
    assert looks_like_section_label(label)


@pytest.mark.parametrize("label", ["MELLANSPEL:", "INSTRUMENTALT:", "INTRO/INSTRUMENTAL:"])
def test_recognizes_the_three_forms_the_first_version_missed(label):
    """Found by running the matcher over five Sundays of real lyrics: two
    Swedish vocabulary gaps and one compound the regex couldn't parse."""
    assert looks_like_section_label(label)


@pytest.mark.parametrize(
    "line",
    [
        # The corpus case that proves the conservatism is worth keeping: a
        # backing-vocal line a looser matcher would silently delete.
        "(Allt som du har sagt)",
        "Bridge over troubled water",
        "Tack för Din trofasthet mot mig",
        "Chorus of angels singing out",
        "Verse one of many things I sing",
        "I will sing",
        "",
        "   ",
    ],
)
def test_never_mistakes_a_lyric_for_a_label(line):
    assert not looks_like_section_label(line)


@pytest.mark.parametrize("label", ["Chorus x2", "Bridge (2x)", "[Chorus]", "(Bridge)", "Verse 1 & 2"])
def test_recognizes_decorated_labels(label):
    assert looks_like_section_label(label)


def test_length_cap_protects_long_lines_starting_with_a_label_word():
    assert not looks_like_section_label("Bridge over troubled water and on")


# --------------------------------------------------------------------------
# Stanza splitting
# --------------------------------------------------------------------------


def test_blank_line_separates_stanzas():
    assert split_stanzas("a\nb\n\nc\nd") == ["a\nb", "c\nd"]


def test_leading_label_is_dropped():
    assert split_stanzas("VERSE 1:\nsing this") == ["sing this"]


def test_midblock_label_splits_the_stanza_and_disappears():
    """The gap the corpus exposed: 3 of 86 labels sat mid-block with no blank
    line before them. Treating them as text both left the marker on screen
    and merged two sections into a single slide."""
    assert split_stanzas("line A\nINTRO\nline B") == ["line A", "line B"]


def test_several_midblock_labels():
    assert split_stanzas("a\nVERSE 1:\nb\nCHORUS:\nc") == ["a", "b", "c"]


def test_stanza_that_is_only_a_label_disappears():
    assert split_stanzas("CHORUS:\n\nreal words") == ["real words"]


def test_label_stripping_can_be_disabled():
    assert split_stanzas("VERSE 1:\nsing this", strip_labels=False) == ["VERSE 1:\nsing this"]


def test_disabled_stripping_does_not_split_midblock_either():
    assert split_stanzas("a\nINTRO\nb", strip_labels=False) == ["a\nINTRO\nb"]


def test_runs_of_blank_lines_do_not_make_empty_stanzas():
    assert split_stanzas("a\n\n\n\nb") == ["a", "b"]


def test_windows_line_endings():
    assert split_stanzas("a\r\nb\r\n\r\nc") == ["a\nb", "c"]


def test_whitespace_is_stripped_from_both_ends():
    assert split_stanzas("\n\n  a  \n  b\n\n") == ["a\nb"]


def test_empty_input():
    assert split_stanzas("") == []
    assert split_stanzas("  \n\n ") == []


# --------------------------------------------------------------------------
# De-duplication
# --------------------------------------------------------------------------


def test_exact_repeat_is_dropped():
    assert dedupe_stanzas(["chorus", "verse", "chorus"]) == ["chorus", "verse"]


def test_first_occurrence_is_the_one_kept():
    assert dedupe_stanzas(["A", "B", "A"]) == ["A", "B"]


def test_repeats_differing_only_in_case_punctuation_or_spacing_are_dropped():
    assert dedupe_stanzas(["Sjung nu ut!", "sjung  nu ut"]) == ["Sjung nu ut!"]


def test_near_duplicates_are_kept():
    """A chorus with a word changed is a different chorus. Guessing at
    near-matches risks dropping a verse that merely rhymes with another."""
    assert len(dedupe_stanzas(["sjung nu ut all ära", "sjung nu ut all makt"])) == 2


def test_dedupe_of_empty_and_single():
    assert dedupe_stanzas([]) == []
    assert dedupe_stanzas(["only"]) == ["only"]


# --------------------------------------------------------------------------
# Line wrapping
# --------------------------------------------------------------------------


def test_short_lines_are_untouched():
    assert wrap_line("short enough") == ["short enough"]


def test_wraps_at_a_sentence_boundary_when_balanced():
    """The real corpus line, and the break lands where a singer breathes."""
    line = "Änglar sjung - er ut, he - e - lig. Hela skapel - sen sjunger he - e - lig."
    assert wrap_line(line) == [
        "Änglar sjung - er ut, he - e - lig.",
        "Hela skapel - sen sjunger he - e - lig.",
    ]


def test_balance_constraint_beats_nearest_punctuation():
    """Regression for the first attempt: preferring the comma nearest the
    middle split a 75-char line 21/53 -- worse than breaking at a space."""
    line = "Änglar sjung - er ut, he - e - lig. Hela skapel - sen sjunger he - e - lig."
    parts = wrap_line(line)
    shortest, longest = min(len(p) for p in parts), max(len(p) for p in parts)
    assert longest / shortest < 1.5


def test_every_piece_is_within_the_limit_where_possible():
    line = " ".join(["word"] * 40)
    assert all(len(p) <= DEFAULT_WRAP_CHARS for p in wrap_line(line))


def test_an_unbreakable_run_is_left_long_rather_than_mangled():
    """No spaces to break on: a butchered word is worse than a small line."""
    assert wrap_line("x" * 120) == ["x" * 120]


def test_wrapping_never_loses_or_reorders_words():
    line = "Du som är förlåten och du som blivit fri, sjung nu ut all ära till Guds lamm."
    assert " ".join(wrap_line(line)).split() == line.split()


def test_wrap_lines_applies_per_line_and_keeps_line_count_stable_for_short_text():
    text = "one\ntwo\nthree"
    assert wrap_lines(text) == text


def test_wrap_lines_expands_only_the_long_lines():
    text = "short\n" + "word " * 30
    out = wrap_lines(text).split("\n")
    assert out[0] == "short"
    assert len(out) > 2


def test_custom_limit_is_honoured():
    assert len(wrap_line("aaa bbb ccc ddd", limit=8)) > 1


def test_a_bare_section_word_on_its_own_line_is_treated_as_a_label():
    """Documented consequence of the vocabulary being matched without a
    number or colon: a lyric line consisting of exactly the word "chorus"
    would be stripped. Vanishingly rare in real lyrics, and the alternative
    (requiring punctuation) would miss the bare "INTRO" the corpus contains.
    """
    assert looks_like_section_label("chorus")
    assert looks_like_section_label("INTRO")
    # Two words is already enough to be safe.
    assert not looks_like_section_label("chorus of angels")
