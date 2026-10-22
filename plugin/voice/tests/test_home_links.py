"""Links that open inside an app on a TV: which values are links, the app that claims one, and its channel."""

from __future__ import annotations

import pytest

from jarvis_voice.home.links import KICK, Link, app_for_host, parse_link


def test_kick_links_get_https_the_bare_host_and_a_lowercase_channel() -> None:
    assert parse_link("kick.com/xQc") == Link("https://kick.com/xqc", KICK, "xqc", False)
    assert parse_link("https://www.kick.com/XQC?ref=1") == Link("https://kick.com/xqc?ref=1", KICK, "xqc", False)
    assert parse_link("http://kick.com/xqc") == Link("https://kick.com/xqc", KICK, "xqc", False)
    assert parse_link("  https://kick.com/xqc/#chat ") == Link("https://kick.com/xqc#chat", KICK, "xqc", False)
    assert app_for_host("WWW.Kick.com") is KICK and app_for_host("kick.example") is None


def test_longer_kick_paths_keep_their_case_and_name_no_channel() -> None:
    assert parse_link("https://kick.com/xqc/Some/Path") == Link("https://kick.com/xqc/Some/Path", KICK, None, False)


def test_kick_paths_the_app_does_not_claim_stay_plain_links() -> None:
    assert parse_link("https://kick.com/go-live") == Link("https://kick.com/go-live", None, None, False)
    assert parse_link("kick.com/tv_help/x") == Link("https://kick.com/tv_help/x", None, None, False)
    assert parse_link("https://www.kick.com/Go-Live") == Link("https://kick.com/Go-Live", None, None, False)


@pytest.mark.parametrize("text", ["kick.com", "Kick.com", "https://kick.com/", "https://www.kick.com"])
def test_the_bare_kick_host_means_the_app_itself(text: str) -> None:
    assert parse_link(text) == Link("https://kick.com/", KICK, None, True)


def test_other_links_pass_through_and_bare_ones_get_https() -> None:
    netflix = "https://www.netflix.com/title/80234304"
    assert parse_link(netflix) == Link(netflix, None, None, False)
    assert parse_link("nflx://www.netflix.com/title/1") == Link("nflx://www.netflix.com/title/1", None, None, False)
    assert parse_link("youtube.com/watch?v=abc") == Link("https://youtube.com/watch?v=abc", None, None, False)
    # Kick's host under another scheme is not Kick's link.
    assert parse_link("ftp://kick.com/xqc") == Link("ftp://kick.com/xqc", None, None, False)


@pytest.mark.parametrize(
    "text", ["Kick", "kick", "Netflix", "Disney+", "YouTube Kids", "com.kick.mobile", "AC/DC", "", "   "]
)
def test_app_names_and_bundle_ids_are_not_links(text: str) -> None:
    assert parse_link(text) is None


@pytest.mark.parametrize("path", ["a" * 26, "xq%20c", "x.q"])
def test_a_channel_is_named_only_when_it_looks_like_one(path: str) -> None:
    # The link still goes to Kick as it is; only no odd name is spoken.
    assert parse_link(f"https://kick.com/{path}") == Link(f"https://kick.com/{path}", KICK, None, False)
    assert parse_link("kick.com/" + "a" * 25) == Link("https://kick.com/" + "a" * 25, KICK, "a" * 25, False)
