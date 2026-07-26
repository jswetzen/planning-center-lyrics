"""
Tests for pptx_export.py.

Two things carry most of the risk. Stanza splitting decides how many slides
there are and what lands on each -- get it wrong and someone finds out
mid-service. Section-label stripping decides whether the word "Refräng" gets
projected at a congregation, so it has to be aggressive enough to catch real
labels and timid enough to never eat a lyric.
"""

import io

import pytest
from pptx import Presentation

import pptx_export
from pptx_export import (
    MAX_FONT_PT,
    MIN_FONT_PT,
    build_deck,
    build_deck_bytes,
    choose_font_size,
    footer_text,
    looks_like_section_label,
    split_stanzas,
    suggest_filename,
)
from pco_client import SongLyrics


def _song(title="Song A", lyrics="line one\nline two", ccli="1234"):
    return SongLyrics(title, lyrics, "chords here", ccli, item_id="i1")


def _slide_texts(prs):
    """Every slide's body text, footers excluded."""
    out = []
    for slide in prs.slides:
        boxes = [sh for sh in slide.shapes if sh.has_text_frame]
        out.append(boxes[0].text_frame.text if boxes else "")
    return out


# --------------------------------------------------------------------------
# split_stanzas
# --------------------------------------------------------------------------


def test_blank_line_separates_slides():
    assert split_stanzas("a\nb\n\nc\nd") == ["a\nb", "c\nd"]


def test_runs_of_blank_lines_do_not_make_empty_slides():
    """A double blank line between stanzas is common in hand-entered lyrics
    and must not project a black slide mid-song."""
    assert split_stanzas("a\n\n\n\nb") == ["a", "b"]


def test_windows_and_mac_line_endings():
    assert split_stanzas("a\r\nb\r\n\r\nc") == ["a\nb", "c"]


def test_leading_and_trailing_whitespace_is_ignored():
    assert split_stanzas("\n\n  a  \nb\n\n\n") == ["a\nb"]


def test_empty_lyrics_produce_no_stanzas():
    assert split_stanzas("") == []
    assert split_stanzas("   \n\n  ") == []


def test_lines_of_only_whitespace_inside_a_stanza_are_dropped():
    assert split_stanzas("a\n   \nb") == ["a", "b"]


# --------------------------------------------------------------------------
# Section labels
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "label",
    ["Verse", "Verse 1", "verse 2", "[Chorus]", "Chorus:", "Bridge", "Pre-Chorus",
     "Tag", "Intro", "Outro", "Refräng", "Refrang:", "Vers 2", "Brygga", "(Bridge)",
     "Chorus x2", "Bridge (x2)"],
)
def test_recognizes_section_labels(label):
    assert looks_like_section_label(label)


@pytest.mark.parametrize(
    "line",
    [
        "Bridge over troubled water",   # starts with a label word, but is a lyric
        "Tack för Din trofasthet mot mig",
        "I will sing",
        "",
        "Verse one of many things I sing",
        "Chorus of angels singing out",
    ],
)
def test_does_not_mistake_lyrics_for_labels(line):
    assert not looks_like_section_label(line)


def test_label_is_stripped_from_the_top_of_a_stanza():
    assert split_stanzas("Verse 1\nsing this\nand this") == ["sing this\nand this"]


def test_stanza_that_is_only_a_label_disappears():
    assert split_stanzas("Chorus\n\nreal words") == ["real words"]


def test_label_stripping_can_be_turned_off():
    assert split_stanzas("Verse 1\nsing this", strip_labels=False) == ["Verse 1\nsing this"]


def test_a_label_further_down_a_stanza_is_left_alone():
    """Only the first line is a plausible label; mangling the middle of a
    stanza would be worse than leaving a stray word in."""
    assert split_stanzas("sing this\nBridge") == ["sing this\nBridge"]


# --------------------------------------------------------------------------
# Font sizing
# --------------------------------------------------------------------------


def test_short_stanza_gets_the_largest_size():
    assert choose_font_size("Hey") == MAX_FONT_PT


def test_more_lines_never_increases_the_font_size():
    sizes = [choose_font_size("\n".join(["a line of lyrics"] * n)) for n in range(1, 15)]
    assert sizes == sorted(sizes, reverse=True)


def test_a_very_long_line_shrinks_the_font():
    assert choose_font_size("x" * 200) < choose_font_size("x" * 20)


def test_font_size_stays_within_bounds():
    for stanza in ["a", "\n".join(["long line of words here"] * 50), "y" * 1000]:
        assert MIN_FONT_PT <= choose_font_size(stanza) <= MAX_FONT_PT


def test_empty_stanza_does_not_divide_by_zero():
    assert choose_font_size("") == MAX_FONT_PT


# --------------------------------------------------------------------------
# Deck building
# --------------------------------------------------------------------------


def test_one_slide_per_stanza_in_plan_order():
    songs = [_song("A", "a1\n\na2"), _song("B", "b1")]
    assert _slide_texts(build_deck(songs)) == ["a1", "a2", "b1"]


def test_deck_is_widescreen():
    """python-pptx defaults to 4:3, which would letterbox on every projector
    installed this century."""
    prs = build_deck([_song()])
    assert round(prs.slide_width / prs.slide_height, 2) == round(16 / 9, 2)


def test_ccli_number_appears_on_every_slide():
    """Reporting what was projected is the licence-holder's obligation, and a
    number that only lives in Planning Center won't be transcribed later."""
    prs = build_deck([_song("A", "one\n\ntwo", ccli="7207484")])
    for slide in prs.slides:
        assert any("CCLI 7207484" in sh.text_frame.text for sh in slide.shapes if sh.has_text_frame)


def test_footer_omits_ccli_when_the_song_has_none():
    assert footer_text(_song("A", ccli=None)) == "A"


def test_song_without_lyrics_still_gets_a_placeholder_slide():
    """The gap should be obvious in the deck, not discovered mid-service."""
    texts = _slide_texts(build_deck([SongLyrics("Silent One", "", "", None, item_id="i9")]))
    assert texts == ["[Silent One]"]


def test_chords_are_never_exported():
    """A chord chart on the projector is the band's sheet on the wall."""
    song = SongLyrics("A", "just the words", "G  C  D\nchords", "1", item_id="i1")
    assert "chords" not in " ".join(_slide_texts(build_deck([song])))


def test_themes_differ_in_background():
    dark = build_deck([_song()], theme="dark")
    light = build_deck([_song()], theme="light")
    assert dark.slides[0].background.fill.fore_color.rgb != light.slides[0].background.fill.fore_color.rgb


def test_unknown_theme_falls_back_to_the_default():
    assert (
        build_deck([_song()], theme="chartreuse").slides[0].background.fill.fore_color.rgb
        == build_deck([_song()]).slides[0].background.fill.fore_color.rgb
    )


def test_empty_plan_produces_an_empty_but_valid_deck():
    prs = Presentation(io.BytesIO(build_deck_bytes([])))
    assert len(prs.slides) == 0


def test_bytes_round_trip_as_a_real_pptx():
    """The bytes the route hands the browser must actually open."""
    data = build_deck_bytes([_song("A", "one\n\ntwo")])
    assert data[:2] == b"PK"  # OOXML is a zip
    assert len(Presentation(io.BytesIO(data)).slides) == 2


# --------------------------------------------------------------------------
# Filenames
# --------------------------------------------------------------------------


def test_filename_matches_the_notion_export_naming():
    assert suggest_filename("Lovsång Brokyrkan", "2026-07-26") == "Lovsång Brokyrkan - 2026-07-26.pptx"


def test_filename_without_a_date():
    assert suggest_filename("Lovsång Brokyrkan", None) == "Lovsång Brokyrkan.pptx"


def test_filename_strips_characters_windows_and_macos_reject():
    assert "/" not in suggest_filename("A/B:C", "2026-01-01")
    assert suggest_filename('a<b>c:d"e/f\\g|h?i*j', None) == "a-b-c-d-e-f-g-h-i-j.pptx"


def test_filename_never_ends_up_empty():
    assert suggest_filename("///", None) == "lyrics.pptx"
