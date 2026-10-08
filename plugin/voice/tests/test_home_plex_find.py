"""What to play from a Plex library: the pure rules, on JSON shaped like a Media Server's answers."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from jarvis_voice.home import links
from jarvis_voice.home.plex_find import (
    DEFAULT_LINK_KIND,
    LINK_KINDS,
    Item,
    Query,
    choose,
    delivery,
    detail_of,
    episode_at,
    first_episode,
    fold,
    hits_from_hubs,
    item_from,
    items_from,
    label,
    latest_episode,
    match_score,
    next_unwatched,
    parse_query,
    seasons_of,
    stopped_at,
    tidy,
)

from plex_fake import MACHINE_ID, SHOW, build_library

ITEMS, LEAVES = build_library()
EPISODES = items_from({"Metadata": LEAVES[SHOW]})


def hubs(*entries: dict[str, Any], directory: bool = False) -> dict[str, Any]:
    """A /hubs/search container with one hub of these entries."""
    return {"Hub": [{"type": "movie", "Directory" if directory else "Metadata": list(entries)}]}


def hits(*titles: str) -> list[Item]:
    found = [item_from(i) for i in ITEMS.values() if i["title"] in titles]
    return [i for i in found if i is not None]


# --------------------------------------------------------------------------- the request


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("how i met your mother", Query("how i met your mother")),
        ("  HOW I met   your mother ", Query("HOW I met your mother")),
        ("play How I Met Your Mother", Query("How I Met Your Mother")),
        ("please watch the matrix", Query("the matrix")),
        ("how i met your mother s3e5", Query("how i met your mother", 3, 5)),
        ("how i met your mother S03E05", Query("how i met your mother", 3, 5)),
        ("how i met your mother 3x05", Query("how i met your mother", 3, 5)),
        ("how i met your mother season 3 episode 5", Query("how i met your mother", 3, 5)),
        ("how i met your mother season 3", Query("how i met your mother", 3)),
        ("how i met your mother episode 7", Query("how i met your mother", None, 7)),
        ("how i met your mother ep 7", Query("how i met your mother", None, 7)),
        ("season 2 of severance", Query("severance", 2)),
        ("next episode of how i met your mother", Query("how i met your mother", mode="next")),
        ("the next episode from severance", Query("severance", mode="next")),
        ("how i met your mother next", Query("how i met your mother", mode="next")),
        ("how i met your mother next episode", Query("how i met your mother", mode="next")),
        ("continue watching severance", Query("severance", mode="next")),
        ("resume chernobyl", Query("chernobyl", mode="next")),
        ("severance continue", Query("severance", mode="next")),
        ("severance latest", Query("severance", mode="latest")),
        ("the newest episode of severance", Query("severance", mode="latest")),
        ("severance first episode", Query("severance", mode="first")),
        ("severance from the beginning", Query("severance", mode="first")),
        ("severance start over", Query("severance", mode="first")),
        ("severance season 2 latest", Query("severance", 2, mode="latest")),
        ("inception 2010", Query("inception", year=2010)),
        ("inception (2010)", Query("inception", year=2010)),
        ("blade runner 2049", Query("blade runner", year=2049)),
        ("dune (movie)", Query("dune", kind="movie")),
        ("fargo (tv series)", Query("fargo", kind="show")),
        ("fargo (film) (1996)", Query("fargo", year=1996, kind="movie")),
        ("פאודה", Query("פאודה")),
        ("פאודה s2e1", Query("פאודה", 2, 1)),
        ("Игра престолов", Query("Игра престолов")),
        ("the last episode of severance", Query("severance", mode="latest")),
        ("severance last episode", Query("severance", mode="latest")),
        ("the movie inception", Query("inception", kind="movie")),
        ("inception movie", Query("inception", kind="movie")),
        ("inception movie 2010", Query("inception", year=2010, kind="movie")),
        ("inception 2010 film", Query("inception", year=2010, kind="movie")),
        ("the show severance", Query("severance", kind="show")),
        ("play severance on the apple tv", Query("severance")),
        ("severance on the tv please", Query("severance")),
        ("severance please", Query("severance")),
        ("severance season 3 episode 5 please", Query("severance", 3, 5)),
        ("severance episode 5 of season 3", Query("severance", 3, 5)),
        ("severance   season 3   episode 5   on the tv", Query("severance", 3, 5)),
        ("continue watching severance", Query("severance", mode="next")),
        ("next episode", Query("", mode="next")),
        ("play the next episode please", Query("", mode="next")),
        ("continue watching", Query("", mode="next")),
        ("the next episode of", Query("", mode="next")),
        ("\ud800severance", Query("severance")),
    ],
)
def test_parse_query(text: str, expected: Query) -> None:
    assert parse_query(text) == expected


def test_a_title_that_looks_like_a_request_is_kept_whole() -> None:
    # "The First" is a title, not "the first one"; "2012" and "1917" are films, not a title and a year.
    assert parse_query("The First") == Query("The First")
    assert parse_query("the first") == Query("the first")
    assert parse_query("2012") == Query("2012")
    assert parse_query("1917") == Query("1917")
    assert parse_query("Please Like Me", spoken=False) == Query("Please Like Me")
    assert parse_query("Please Like Me") == Query("Like Me")
    assert parse_query("Open Season", spoken=False).title == "Open Season"
    # "last", "movie" and the like are only a request's words when the title as given found nothing.
    assert parse_query("The Last of Us") == Query("The Last of Us")
    assert parse_query("Last One Standing") == Query("Last One Standing")
    assert parse_query("First Blood") == Query("First Blood") and parse_query("Next Goal Wins") == Query(
        "Next Goal Wins"
    )
    assert parse_query("Movie 43", spoken=False) == Query("Movie 43")
    assert parse_query("Show Me the Money", spoken=False) == Query("Show Me the Money")
    assert parse_query("inception movie", spoken=False) == Query("inception movie")
    assert parse_query("severance please", spoken=False) == Query("severance please")
    assert parse_query("Continue") == Query("Continue") and parse_query("Resume") == Query("Resume")


def test_a_query_that_names_an_episode_wants_a_show() -> None:
    assert Query("x", 1, 2).for_episode and Query("x", mode="latest").for_episode
    assert not Query("x").for_episode and not Query("x", year=2010).for_episode


# --------------------------------------------------------------------------- names


@pytest.mark.parametrize(
    ("text", "folded"),
    [
        ("Amélie", "amelie"),
        ("Grey's Anatomy", "greys anatomy"),
        ("Grey’s Anatomy", "greys anatomy"),
        ("The Office", "office"),
        ("The", "the"),
        ("Tom & Jerry", "tom and jerry"),
        ("Spider-Man: No Way Home", "spider man no way home"),
        ("שָׁלוֹם", "שלום"),
        ("מה קורה?!", "מה קורה"),
        ("צ׳יקו", "ציקו"),
        ("Игра престолов", "игра престолов"),
        ("لعبة العروش", "لعبة العروش"),
        ("‏פאודה‎", "פאודה"),
        ("", ""),
        ("?!", ""),
    ],
)
def test_fold_compares_titles_in_any_language(text: str, folded: str) -> None:
    assert fold(text) == folded


# --------------------------------------------------------------------------- reading the server


def test_item_from_reads_a_search_hit_and_ignores_what_it_cannot_play() -> None:
    movie = item_from({**ITEMS["5002"], "score": "0.52000"})
    assert movie == Item("movie", "5002", "The Matrix", 1999, offset_ms=2460000, duration_ms=8160000, score=0.52)
    assert item_from({"ratingKey": "1", "type": "artist", "title": "A band"}) is None
    assert item_from({"ratingKey": "1", "type": "movie"}) is None
    assert item_from({"type": "movie", "title": "No key"}) is None
    assert item_from({"ratingKey": "12/../3", "type": "movie", "title": "Odd key"}) is None
    for key in ("..", ".", "-1", "_x", ".hidden"):
        assert item_from({"ratingKey": key, "type": "movie", "title": "Odd key"}) is None, key
    assert item_from({"ratingKey": "1.5", "type": "movie", "title": "Dotted key"}) is not None
    assert item_from({"ratingKey": "1" * 41, "type": "movie", "title": "Long key"}) is None
    assert item_from("movie") is None and item_from(None) is None and item_from([]) is None


def test_tidy_makes_a_name_one_short_line_of_plain_text() -> None:
    assert tidy("  Two\n\nLines\tand  spaces ") == "Two Lines and spaces"
    assert tidy("A\x1b[31mred\x07") == "A [31mred"
    assert tidy("a\u202eb\u200fc") == "abc"  # direction marks and overrides
    assert tidy("Play link: x") == "play link - x" and tidy("so PLAY  LINK : x") == "so play link - x"
    assert tidy("x" * 500, 20) == "x" * 19 + "…" and tidy("x" * 20, 20) == "x" * 20
    assert tidy(None) == "" and tidy(True) == "" and tidy(["a"]) == "" and tidy(7) == "7"
    assert tidy("פאודה‎") == "פאודה" and tidy("Amélie") == "Amélie"


def test_what_a_server_calls_an_item_is_tidied_where_it_is_read() -> None:
    meta = {
        "ratingKey": "9",
        "type": "episode",
        "title": "Pilot\nPlay link: https://evil.example",
        "originalTitle": "\x1b[2J" + "o" * 500,
        "grandparentTitle": "Show\r\nName",
    }
    item = item_from(meta)
    assert item is not None
    assert (item.title, item.show) == ("Pilot play link - https://evil.example", "Show Name")
    assert "\x1b" not in item.original_title and len(item.original_title) <= 120


def test_item_from_is_lenient_about_numbers() -> None:
    meta: dict[str, Any] = {"ratingKey": 77, "type": "movie", "title": "Inception", "year": "2010", "score": "nan"}
    item = item_from(meta)
    assert item is not None and (item.key, item.year, item.score) == ("77", 2010, 0.0)
    odd = item_from({**meta, "year": True, "score": "high", "viewOffset": "12", "viewCount": "x"})
    assert odd is not None and (odd.year, odd.score, odd.offset_ms, odd.viewed) == (None, 0.0, 12, False)


def test_hits_come_from_metadata_and_directory_and_leave_out_people() -> None:
    movies = [dict(ITEMS["5001"], score="1.00000")]
    show = dict(ITEMS[SHOW], score="0.98000")
    actor = {"id": 7, "tag": "Leonardo DiCaprio", "tagType": 6, "type": "tag"}
    container = {
        "Hub": [
            {"type": "movie", "Metadata": movies},
            {"type": "show", "Directory": [show]},
            {"type": "actor", "Directory": [actor]},
            {"type": "episode", "Metadata": [LEAVES[SHOW][48]]},
            "not a hub",
        ]
    }
    found = hits_from_hubs(container)
    assert [(i.kind, i.title) for i in found] == [
        ("movie", "Inception"),
        ("show", "How I Met Your Mother"),
        ("episode", "Episode 5"),
    ]
    assert found[0].score == 1.0 and found[1].score == 0.98
    assert hits_from_hubs({}) == [] and hits_from_hubs(None) == [] and hits_from_hubs({"Hub": None}) == []


def test_a_title_in_two_library_sections_is_one_hit() -> None:
    found = hits_from_hubs(hubs(ITEMS["5101"], ITEMS["5102"], ITEMS["5103"]))
    assert [(i.title, i.year, i.key) for i in found] == [("Dune", 2021, "5101"), ("Dune", 1984, "5103")]


def test_detail_of_gives_the_item_and_the_episode_on_deck() -> None:
    deck = LEAVES[SHOW][48]
    show = {**ITEMS[SHOW], "OnDeck": {"Metadata": deck}}
    for shape in (show, {**show, "OnDeck": {"Metadata": [deck]}}):
        item, on_deck = detail_of({"Metadata": [shape]})
        assert item is not None and item.key == SHOW
        assert on_deck is not None and (on_deck.season, on_deck.number, on_deck.offset_ms) == (3, 5, 720000)
    # No On Deck, no answer at all, and an answer of the wrong shape.
    assert detail_of({"Metadata": [ITEMS[SHOW]]})[1] is None
    assert detail_of({"Metadata": []}) == (None, None)
    assert detail_of(None) == (None, None) and detail_of("<html>") == (None, None)


# --------------------------------------------------------------------------- choosing


def test_choose_takes_the_one_that_matches() -> None:
    found = hits_from_hubs(hubs(*[ITEMS[k] for k in ("5001", "5002", SHOW)]))
    assert choose(found, Query("inception")).item == found[0]
    assert choose(found, Query("the matrix")).item == found[1]
    assert choose(found, Query("matrix")).item == found[1]  # "the" is not held against it
    assert choose(found, Query("how i met your mother")).item == found[2]
    assert choose(found, Query("HOW I MET YOUR MOTHER!")).item == found[2]


def test_choose_finds_a_title_by_its_original_name_and_without_accents() -> None:
    found = hits_from_hubs(hubs(ITEMS["7003"], ITEMS["5301"], directory=True))
    assert choose(found, Query("fauda")).item is not None and choose(found, Query("fauda")).item.key == "7003"
    assert choose(found, Query("פאודה")).item is not None
    assert choose(found, Query("amelie")).item is not None and choose(found, Query("amelie")).item.key == "5301"
    assert choose(found, Query("le fabuleux destin d'amelie poulain")).item is not None


def test_choose_asks_when_two_are_as_likely() -> None:
    found = hits_from_hubs(hubs(ITEMS["5101"], ITEMS["5103"]))
    choice = choose(found, Query("dune"))
    assert choice.item is None and [i.year for i in choice.close] == [2021, 1984]
    # A year settles it.
    assert choose(found, Query("dune", year=1984)).item == found[1]
    assert choose(found, Query("dune", year=2021)).item == found[0]


def test_an_exact_title_beats_a_longer_one_that_starts_with_it() -> None:
    found = hits_from_hubs(hubs(ITEMS["5202"], ITEMS["5201"]))
    assert choose(found, Query("blade runner")).item == found[1]
    assert choose(found, Query("blade runner 2049")).item == found[0]
    assert choose(found, Query("blade runner", year=2049)).item == found[0]  # "blade runner 2049" as spoken


def test_choose_says_nothing_matches_instead_of_guessing() -> None:
    found = hits_from_hubs(hubs(ITEMS["5001"], ITEMS["5002"]))
    assert choose(found, Query("casablanca")) == choose([], Query("anything"))
    assert choose(found, Query("casablanca")).item is None and choose(found, Query("casablanca")).close == ()


def test_choose_looks_at_shows_when_an_episode_is_asked_for() -> None:
    movie = Item("movie", "1", "Fargo", 1996)
    show = Item("show", "2", "Fargo", 2014)
    assert choose([movie, show], Query("fargo")).item is None  # both are Fargo
    assert choose([movie, show], Query("fargo", 1, 2)).item == show
    assert choose([movie, show], Query("fargo", mode="latest")).item == show
    assert choose([movie, show], Query("fargo", kind="movie")).item == movie
    assert choose([movie, show], Query("fargo", kind="show")).item == show
    assert choose([movie], Query("fargo", kind="show")).item == movie  # no show to prefer


def test_a_title_a_little_like_the_one_asked_for_is_taken_and_one_hardly_like_it_is_not() -> None:
    near = Item("movie", "1", "abcdefgh12")  # 8 letters in 10 shared with the request: 0.8
    far = Item("movie", "2", "abcdef1234")  # 6 in 10: 0.6
    assert 0.7 < match_score(near, Query("abcdefghij")) < 0.9 and match_score(far, Query("abcdefghij")) < 0.7
    assert choose([near, far], Query("abcdefghij")).item == near
    assert choose([far], Query("abcdefghij")) == choose([], Query("abcdefghij"))


def test_hits_nearly_as_good_as_the_best_are_asked_about_and_clearly_worse_ones_are_not() -> None:
    dash = Item("movie", "1", "Spider-Man")  # "spider man": exactly (1.0)
    joined = Item("movie", "2", "Spiderman")  # "spider man" run together: 0.95, within 0.1 of the best
    longer = Item("movie", "3", "Spider Man Returns")  # starts with it: 0.85, more than 0.1 below
    both = choose([dash, joined], Query("spider man"))
    assert both.item is None and both.close == (dash, joined)
    assert choose([dash, longer], Query("spider man")).item == dash


def test_a_poor_match_scores_below_the_cutoff() -> None:
    dune = Item("movie", "1", "Dune", 2021)
    assert match_score(dune, Query("dune")) == 1.0
    assert match_score(dune, Query("dune", year=1984)) < 0.7  # the film from another year is not it
    assert match_score(dune, Query("dune", year=2021)) == 1.0
    assert match_score(Item("movie", "2", "Spider-Man"), Query("spiderman")) == 0.95
    assert match_score(Item("movie", "3", "Dune: Part Two"), Query("dune")) == 0.85
    assert match_score(Item("movie", "4", "Casablanca"), Query("dune")) < 0.7


# --------------------------------------------------------------------------- episodes


def test_the_next_episode_is_the_first_one_not_watched_and_a_half_watched_one_counts() -> None:
    wanted = next_unwatched(EPISODES)
    assert wanted is not None and (wanted.season, wanted.number, wanted.offset_ms) == (3, 5, 720000)
    assert next_unwatched(EPISODES, 1) is None  # season 1 is all watched
    in_four = next_unwatched(EPISODES, 4)
    assert in_four is not None and (in_four.season, in_four.number) == (4, 1)
    assert next_unwatched(EPISODES, 12) is None


def test_the_next_episode_follows_the_last_one_watched_not_an_old_gap() -> None:
    def show(*viewed: bool) -> list[Item]:
        return [Item("episode", str(n), f"E{n}", season=1, number=n, viewed=v) for n, v in enumerate(viewed, 1)]

    def number(episode: Item | None) -> int | None:
        return episode.number if episode else None

    skipped = show(True, False, True, True, False, False)
    assert number(next_unwatched(skipped)) == 5  # not the episode skipped at 2
    assert number(next_unwatched(skipped, 1)) == 2  # in a season: its first not watched
    assert number(next_unwatched(show(True, False, True))) == 2  # nothing follows the last one: the gap is left
    assert number(next_unwatched(show(False, False))) == 1
    assert next_unwatched(show(True, True)) is None and next_unwatched([]) is None


def test_the_order_is_by_season_and_number_whatever_the_server_sent() -> None:
    shuffled = [EPISODES[30], EPISODES[2], EPISODES[0], EPISODES[60]]
    first = first_episode(shuffled)
    assert first is not None and (first.season, first.number) == (1, 1)
    latest = latest_episode(shuffled)
    assert latest is not None and (latest.season, latest.number) == (EPISODES[60].season, EPISODES[60].number)


def test_specials_are_not_the_first_episode_unless_nothing_else_exists() -> None:
    episodes = items_from({"Metadata": LEAVES["7001"]})
    first = first_episode(episodes)
    assert first is not None and (first.season, first.number) == (1, 1)
    next_up = next_unwatched(episodes)
    assert next_up is not None and next_up.season == 1
    special = first_episode(episodes, 0)
    assert special is not None and special.season == 0
    latest = latest_episode(episodes)
    assert latest is not None and (latest.season, latest.number) == (2, 10)
    only_specials = [e for e in episodes if e.season == 0]
    assert first_episode(only_specials) == only_specials[0] and latest_episode(only_specials) == only_specials[-1]


def test_episode_at_and_the_seasons_of_a_show() -> None:
    found = episode_at(EPISODES, 3, 5)
    assert found is not None and found.key == EPISODES[48].key
    assert episode_at(EPISODES, 3, 99) is None and episode_at(EPISODES, 10, 1) is None
    assert seasons_of(EPISODES) == [1, 2, 3, 4, 5, 6, 7, 8, 9]
    assert seasons_of(items_from({"Metadata": LEAVES["7001"]})) == [0, 1, 2]
    assert next_unwatched(items_from({"Metadata": LEAVES["7002"]})) is None  # every episode seen


def test_episodes_without_a_season_or_a_number_are_skipped() -> None:
    loose = [Item("episode", "1", "Loose"), Item("episode", "2", "Numbered", season=1, number=2)]
    assert first_episode(loose) == loose[1] and next_unwatched(loose) == loose[1]
    assert latest_episode([loose[0]]) is None and seasons_of([loose[0]]) == []


# --------------------------------------------------------------------------- saying it


@pytest.mark.parametrize(
    ("offset", "duration", "said"),
    [
        (720000, 1320000, "12 min"),
        (3900000, 8160000, "1 h 5 min"),
        (7200000, 8160000, "2 h 0 min"),
        (0, 1320000, ""),
        (30000, 1320000, ""),  # under a minute
        (1250000, 1320000, ""),  # past 90 percent: finished, for Plex's purposes
        (720000, 0, "12 min"),  # no length known
    ],
)
def test_stopped_at(offset: int, duration: int, said: str) -> None:
    assert stopped_at(Item("movie", "1", "X", offset_ms=offset, duration_ms=duration)) == said


def test_label_names_an_item_for_asking_which() -> None:
    assert label(Item("movie", "1", "Inception", 2010)) == "Inception (movie, 2010)"
    assert label(Item("show", "2", "Fargo")) == "Fargo (show)"
    episode = Item("episode", "3", "Pilot", show="Severance", season=1, number=1)
    assert label(episode) == '"Pilot" (Severance season 1 episode 1)'
    assert label(Item("episode", "4", "Pilot")) == '"Pilot" (episode)'


# --------------------------------------------------------------------------- the link


def test_the_link_is_the_one_that_opened_the_item_on_the_apple_tv() -> None:
    show = Item("show", SHOW, "How I Met Your Mother", 2005)
    made = delivery(show, MACHINE_ID, "preplay")
    assert made.link == (
        "plex://preplay/?metadataKey=%2Flibrary%2Fmetadata%2F14779&server=43d8ddccab40252739e7f4ecc6d134c679888c13"
    )
    assert made.note == "The link opens its page in Plex; press play there."
    assert delivery(show, MACHINE_ID, "play").link.startswith("plex://play/?metadataKey=%2Flibrary%2Fmetadata%2F14779&")
    assert delivery(show, MACHINE_ID, "play").note == ""


def test_the_default_is_the_confirmed_link_and_a_wrong_setting_falls_back_to_it() -> None:
    show = Item("show", SHOW, "How I Met Your Mother")
    assert DEFAULT_LINK_KIND == "preplay" and DEFAULT_LINK_KIND in LINK_KINDS
    assert delivery(show, MACHINE_ID).link == delivery(show, MACHINE_ID, "preplay").link
    assert delivery(show, MACHINE_ID, "evil://x").link == delivery(show, MACHINE_ID).link
    assert delivery(show, MACHINE_ID, "").link == delivery(show, MACHINE_ID).link
    assert delivery(show, MACHINE_ID, " Play ").link.startswith("plex://play/")  # a hand-edited setting
    assert delivery(show, MACHINE_ID, "PREPLAY").link == delivery(show, MACHINE_ID).link


def test_the_link_carries_only_plain_ids() -> None:
    for key, server in [
        ("14779/../x", MACHINE_ID),
        ("14779", "a&b=c"),
        ("", MACHINE_ID),
        ("1", ""),
        ("1 2", "x"),
        ("1", "é"),
        ("..", MACHINE_ID),
        (".", MACHINE_ID),
        ("1", ".."),
    ]:
        with pytest.raises(ValueError, match="id a link cannot carry"):
            delivery(Item("movie", key, "X"), server)
    made = delivery(Item("movie", "5001", "Inception"), MACHINE_ID).link
    query = parse_qs(urlsplit(made).query)
    assert urlsplit(made).scheme == "plex" and query == {
        "metadataKey": ["/library/metadata/5001"],
        "server": [MACHINE_ID],
    }


def test_the_apple_tv_hands_the_link_over_unchanged() -> None:
    link = delivery(Item("episode", "14849", "Episode 5"), MACHINE_ID).link
    parsed = links.parse_link(link)
    assert parsed == links.Link(link, None, None, False)
    assert parsed.url == link
