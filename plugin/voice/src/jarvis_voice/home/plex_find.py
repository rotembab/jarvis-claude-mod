"""What to play from a Plex library: reading the request, choosing among hits, finding the episode, the link to open it.

Pure functions over the JSON a Plex Media Server returns, with no network and no logging, so the
driver (plex.py) keeps to talking to the server and the tests can run every rule on plain data.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

KINDS = ("movie", "show", "episode")

# --------------------------------------------------------------------------- the delivery seam

# The two links Plex's Apple TV app takes. "preplay" opens the item's page (confirmed on the real Apple TV);
# "play" should start it, but has not been tried there yet. This is the one place that decides which a
# device uses unless its settings say otherwise: change it once "play" is confirmed.
LINK_KINDS = ("preplay", "play")
DEFAULT_LINK_KIND = "preplay"

# A found item's id and a server's machine identifier go into links and request paths, so only plain ones are used
# ("." and ".." are not ids).
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}")
_NOTES = {
    "preplay": "The link opens its page in Plex; press play there.",
    "play": "",
}


@dataclass(frozen=True, slots=True)
class Delivery:
    """How a found item reaches the Apple TV: ``link`` is the value for its launch_app command."""

    link: str
    note: str = ""  # what the link does, for the user, when that is not "plays it"


# SEAM (delivery): the one place that decides how a found item is handed to the Apple TV. Today that is a
# plex:// link which the model gives to the Apple TV's launch_app, unchanged; another route (a link for another
# player, a stream) would be made here, from the same item, and nothing else in the Plex driver would change.
def delivery(item: Item, server_id: str, kind: str = DEFAULT_LINK_KIND) -> Delivery:
    kind = kind.strip().lower()
    if kind not in LINK_KINDS:
        kind = DEFAULT_LINK_KIND
    if not _ID.fullmatch(item.key) or not _ID.fullmatch(server_id):
        raise ValueError("the item or the server has an id a link cannot carry")
    query = urlencode({"metadataKey": f"/library/metadata/{item.key}", "server": server_id})
    return Delivery(f"plex://{kind}/?{query}", _NOTES[kind])


# --------------------------------------------------------------------------- items


@dataclass(frozen=True, slots=True)
class Item:
    """A movie, a show or an episode, as search and the library's details report it."""

    kind: str  # movie, show or episode
    key: str  # Plex's ratingKey
    title: str
    year: int | None = None
    original_title: str = ""
    show: str = ""  # episodes: the show's title
    show_key: str = ""
    season: int | None = None  # episodes: the season's number (0 is Specials)
    number: int | None = None  # episodes: the number in the season
    viewed: bool = False
    offset_ms: int = 0  # where the user stopped
    duration_ms: int = 0
    leaves: int = 0  # shows: episodes, and how many are watched
    viewed_leaves: int = 0
    score: float = 0.0  # the server's own relevance, for a tie


def _text(value: Any) -> str:
    if isinstance(value, bool):
        return ""
    return value.strip() if isinstance(value, str) else str(value) if isinstance(value, int) else ""


TITLE_MAX = 120
_LINK_LABEL = re.compile(r"play\s*link\s*:", re.I)


def tidy(value: Any, limit: int = TITLE_MAX) -> str:
    """A name the server sent, as one short line of plain text.

    What a server (or whoever tagged a file) calls a title ends up in what the model reads and in the setup
    window, so it cannot carry a line break, a control or direction character, or a second "Play link:".
    """
    kept = "".join(
        "" if unicodedata.category(c) == "Cf" else " " if unicodedata.category(c)[0] == "C" else c for c in _text(value)
    )
    plain = _LINK_LABEL.sub("play link -", " ".join(kept.split()))
    return plain if len(plain) <= limit else plain[: limit - 1].rstrip() + "…"


def _number(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"\d{1,9}", value.strip()):
        return int(value)
    return None


def item_from(meta: Any) -> Item | None:
    """An item from one ``Metadata`` entry, or None when it is not a movie, show or episode or has no usable id."""
    if not isinstance(meta, dict) or meta.get("type") not in KINDS:
        return None
    key, title = _text(meta.get("ratingKey")), tidy(meta.get("title"))
    if not title or not _ID.fullmatch(key):
        return None
    show_key = _text(meta.get("grandparentRatingKey"))
    try:
        score = float(meta.get("score") or 0)  # a float written as a string: "0.52000"
    except (TypeError, ValueError):
        score = 0.0
    return Item(
        kind=meta["type"],
        key=key,
        title=title,
        year=_number(meta.get("year")),
        original_title=tidy(meta.get("originalTitle")),
        show=tidy(meta.get("grandparentTitle")),
        show_key=show_key if _ID.fullmatch(show_key) else "",
        season=_number(meta.get("parentIndex")),
        number=_number(meta.get("index")),
        viewed=(_number(meta.get("viewCount")) or 0) > 0,
        offset_ms=_number(meta.get("viewOffset")) or 0,
        duration_ms=_number(meta.get("duration")) or 0,
        leaves=_number(meta.get("leafCount")) or 0,
        viewed_leaves=_number(meta.get("viewedLeafCount")) or 0,
        score=score if score == score else 0.0,  # not NaN
    )


def _entries(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def unique(items: list[Item]) -> list[Item]:
    """The items in order, the same title in several libraries once."""
    found: list[Item] = []
    seen: set[tuple[Any, ...]] = set()
    for item in items:
        same = (item.kind, fold(item.title), item.year, fold(item.show), item.season, item.number)
        if same not in seen:
            seen.add(same)
            found.append(item)
    return found


def hits_from_hubs(container: Any) -> list[Item]:
    """The movies, shows and episodes of a ``/hubs/search`` answer, in the server's order, without repeats.

    Items sit in ``Metadata`` for most hubs and in ``Directory`` for the rest; people, collections and the like are
    not played and are left out. The same title in several libraries comes back once.
    """
    found: list[Item] = []
    for hub in _entries(container.get("Hub") if isinstance(container, dict) else None):
        if not isinstance(hub, dict):
            continue
        for meta in _entries(hub.get("Metadata")) or _entries(hub.get("Directory")):
            if (item := item_from(meta)) is not None:
                found.append(item)
    return unique(found)


def items_from(container: Any) -> list[Item]:
    """The items of a ``Metadata`` list (a show's episodes, say), in the server's order."""
    return [
        i for m in _entries(container.get("Metadata") if isinstance(container, dict) else None) if (i := item_from(m))
    ]


def _one(value: Any) -> Any:
    """The one entry of a value that is an object or a list of them, with or without a ``Metadata`` wrapper."""
    if isinstance(value, list):
        return _one(value[0]) if value else None
    if isinstance(value, dict) and "Metadata" in value:
        return _one(value["Metadata"])
    return value


def detail_of(container: Any) -> tuple[Item | None, Item | None]:
    """A ``/library/metadata/<key>`` answer: the item itself, and the episode On Deck when it is a show with one."""
    meta = _one(container)
    if not isinstance(meta, dict):
        return None, None
    return item_from(meta), item_from(_one(meta.get("OnDeck")))


# --------------------------------------------------------------------------- names

# Characters dropped inside a word ("Grey's" and "greys", a geresh or gershayim), and ones that join two words.
_FOLD = {
    **dict.fromkeys(map(ord, "'‘’ʼ׳״\"`"), None),
    **dict.fromkeys(map(ord, "‎‏‪‫‬‭‮"), None),  # bidirectional marks
    ord("&"): " and ",
    0x05BE: " ",  # maqaf
}


def fold(text: str) -> str:
    """Words to compare titles by: lower case, no accents, Hebrew points or punctuation, no leading "the".

    Unlike ``model.normalize`` it keeps every script's letters, so a Russian or Arabic title is not empty.
    """
    kept: list[str] = []
    for char in unicodedata.normalize("NFKD", text.translate(_FOLD)):
        if unicodedata.category(char) == "Mn" and (0x591 <= ord(char) <= 0x5C7 or (kept and kept[-1].isascii())):
            continue  # niqqud and cantillation, and the accent on a Latin letter
        kept.append(char)
    words = re.findall(r"[^\W_]+", unicodedata.normalize("NFC", "".join(kept)).casefold())
    return " ".join(words[1:] if len(words) > 1 and words[0] == "the" else words)


# --------------------------------------------------------------------------- the request


@dataclass(frozen=True, slots=True)
class Query:
    """What was asked for. ``mode`` says which episode of a show: auto (the next one), next, latest or first."""

    title: str
    season: int | None = None
    episode: int | None = None
    mode: str = "auto"
    year: int | None = None
    kind: str | None = None  # "movie" or "show" when the request said so in brackets

    @property
    def for_episode(self) -> bool:
        """Whether it asks for a particular episode, or for the latest, first or next, so only a show fits."""
        return self.season is not None or self.episode is not None or self.mode != "auto"


_LEADING = re.compile(r"^(?:(?:please|play|watch|put on|open|start|show me|find|can you|could you)\s+)+", re.I)
_TRAILING = re.compile(r"\s+(?:please|on (?:the )?(?:apple tv|tv|plex)(?:\s+please)?)$", re.I)
_SXE = re.compile(r"\bs(\d{1,2})\s*e(\d{1,3})\b", re.I)
_NXM = re.compile(r"\b(\d{1,2})x(\d{1,3})\b", re.I)
_SEASON = re.compile(r"\bseason\s*(\d{1,2})\b", re.I)
_EPISODE = re.compile(r"\b(?:episode|ep)\s*(\d{1,3})\b", re.I)
_OF = re.compile(r"^(?:of|from|in)\s+", re.I)
_DANGLING_OF = re.compile(r"\s+(?:of|from|in)$", re.I)  # "episode 5 of season 3" leaves it behind
_MODE_LEAD = re.compile(
    r"^(?:the\s+)?(?:(next|latest|newest|first)\s+(?:episode|ep|one)|(last)\s+(?:episode|ep))\b\s*(?:of|from)?\s*", re.I
)
_RESUME = re.compile(r"^(continue|resume)(?:\s+watching\b\s*|\s+)", re.I)
_MODE_TAIL = re.compile(
    r"\s+(?:(next|latest|newest|first)(?:\s+(?:episode|ep|one))?|(last)\s+(?:episode|ep)|(continue)(?:\s+watching)?"
    r"|(from)\s+(?:the\s+)?(?:start|beginning)|(start)\s+over)$",
    re.I,
)
_MODES = {
    "next": "next",
    "continue": "next",
    "resume": "next",
    "latest": "latest",
    "newest": "latest",
    "last": "latest",
}
_KIND_WORDS = "movie|film|show|series|tv show|tv series"
_KIND = re.compile(rf"\(\s*({_KIND_WORDS})\s*\)", re.I)
_KIND_LEAD = re.compile(rf"^(?:the\s+)?({_KIND_WORDS})\s+", re.I)
_KIND_TAIL = re.compile(rf"\s+({_KIND_WORDS})$", re.I)
_YEAR_BRACKETS = re.compile(r"\(\s*((?:19|20)\d\d)\s*\)")
_YEAR_TAIL = re.compile(r"\s+((?:19|20)\d\d)$")
MIN_TITLE = 2  # Plex answers a one-letter search with nothing, and a blank one with an error


def _cut(text: str, match: re.Match[str]) -> str:
    return " ".join(f"{text[: match.start()]} {text[match.end() :]}".split())


def _kind_of(word: str) -> str:
    return "movie" if word.lower() in ("movie", "film") else "show"


def _kind_word(rest: str) -> tuple[str | None, str]:
    """A "movie" or "show" said at either end of the title ("the movie Inception"), when a title is left."""
    match = _KIND_LEAD.match(rest) or _KIND_TAIL.search(rest)
    if match is None or not fold(_cut(rest, match)):
        return None, rest
    return _kind_of(match[1]), _cut(rest, match)


def parse_query(text: str, *, spoken: bool = True) -> Query:
    """Reads what the model passed to ``find``: a title, and for a show which episode.

    ``spoken``: also drop the words of a request ("play", "please", "on the Apple TV") and a said "movie" or
    "show" from the title, which a title may start with.

    ``how i met your mother`` and ``... next`` mean the next episode; ``s3e5``, ``3x05`` and ``season 3 episode 5``
    name one; ``latest`` and ``first`` (or ``last episode``) the last and the first; ``inception 2010`` or
    ``inception (2010)`` a year. The title keeps the user's spelling, since the server searches it in any language.
    A request that names no title ("the next episode") has an empty one.
    """
    rest = " ".join(text.encode("utf-8", "ignore").decode("utf-8").split())  # a lone surrogate cannot be sent
    if spoken:
        rest = _TRAILING.sub("", _LEADING.sub("", rest))
    season = episode = None
    cut = False
    if match := _SXE.search(rest) or _NXM.search(rest):
        season, episode, cut = int(match[1]), int(match[2]), True
        rest = _cut(rest, match)
    if match := _SEASON.search(rest):
        season, cut = int(match[1]), True
        rest = _cut(rest, match)
    if match := _EPISODE.search(rest):
        episode, cut = int(match[1]), True
        rest = _cut(rest, match)
    rest = _OF.sub("", _DANGLING_OF.sub("", rest) if cut else rest)
    mode, plain = "auto", rest
    if match := _MODE_LEAD.match(rest) or _RESUME.match(rest):
        word = next(g for g in match.groups() if g).lower()
        mode, rest = _MODES.get(word, "first"), rest[match.end() :]
    elif match := _MODE_TAIL.search(rest):
        word = next(g for g in match.groups() if g).lower()
        mode, rest = _MODES.get(word, "first"), rest[: match.start()]
    if fold(rest) in ("the", "a", "an"):  # "The First" is a title, not a request for the first episode
        mode, rest = "auto", plain
    kind = None
    if match := _KIND.search(rest):
        kind, rest = _kind_of(match[1]), _cut(rest, match)
    elif spoken:
        kind, rest = _kind_word(rest)
    year = None
    if match := _YEAR_BRACKETS.search(rest):
        year = int(match[1])
        rest = _cut(rest, match)
    elif (match := _YEAR_TAIL.search(rest)) and fold(rest[: match.start()]):
        year = int(match[1])
        rest = rest[: match.start()]
    if spoken and kind is None:
        kind, rest = _kind_word(rest)
    return Query(rest.strip(" ,.;:-–—"), season, episode, mode, year, kind)


# --------------------------------------------------------------------------- choosing

CUTOFF = 0.7  # below this a hit is not what was asked for
CLOSE = 0.1  # hits this near the best one are as likely
SHOWN = 4  # how many close hits an "ambiguous" answer names


def match_score(item: Item, query: Query) -> float:
    """How well a hit's title (or its original-language title) is what was said, 0 to 1."""
    wanted = fold(query.title)
    forms = [wanted] + ([f"{wanted} {query.year}"] if query.year else [])
    best = 0.0
    for name in {fold(item.title), fold(item.original_title)} - {""}:
        for position, form in enumerate(forms):
            words = set(form.split())
            if name == form:
                score = 1.0
            elif name.replace(" ", "") == form.replace(" ", ""):
                score = 0.95  # "spiderman" for "spider man"
            elif name.startswith(f"{form} "):
                score = 0.85  # "blade runner" for "Blade Runner 2049": less than the film called exactly that
            elif words <= set(name.split()):
                score = 0.8
            else:
                score = difflib.SequenceMatcher(None, form, name).ratio()
            # The bare title of a film from another year is not the film asked for.
            if position == 0 and query.year and item.year and item.year != query.year:
                score *= 0.5
            best = max(best, score)
    return best


@dataclass(frozen=True, slots=True)
class Choice:
    """The outcome of a choice: the one item, or the close ones when none stands out, or neither."""

    item: Item | None = None
    close: tuple[Item, ...] = ()


def choose(hits: list[Item], query: Query) -> Choice:
    """The hit that is what was asked for, or the close ones to ask about, or nothing."""
    pool = hits
    if query.kind is not None:
        pool = [h for h in pool if h.kind == query.kind] or pool
    elif query.for_episode:
        pool = [h for h in pool if h.kind == "show"] or pool
    scored = sorted(((match_score(h, query), h) for h in pool), key=lambda pair: (-pair[0], -pair[1].score))
    scored = [(s, h) for s, h in scored if s >= CUTOFF]
    if not scored:
        return Choice()
    top = scored[0][0]
    close = tuple(h for s, h in scored if s >= top - CLOSE)
    return Choice(item=close[0]) if len(close) == 1 else Choice(close=close[:SHOWN])


# --------------------------------------------------------------------------- episodes


def _order(episodes: list[Item]) -> list[Item]:
    return sorted(
        (e for e in episodes if e.season is not None and e.number is not None),
        key=lambda e: (e.season or 0, e.number or 0),
    )


def _regular(episodes: list[Item]) -> list[Item]:
    """The episodes in order, Specials (season 0) only when there is nothing else."""
    ordered = _order(episodes)
    return [e for e in ordered if e.season != 0] or ordered


def next_unwatched(episodes: list[Item], season: int | None = None) -> Item | None:
    """The episode to watch next: one stopped halfway counts as not watched.

    In a season: its first episode not watched. Otherwise the first one after the last episode watched, so an
    early episode skipped long ago does not come before where the user is up to; only when nothing follows
    the last one watched, the first not watched at all.
    """
    pool = _regular(episodes) if season is None else [e for e in _order(episodes) if e.season == season]
    if season is None:
        last = max((n for n, e in enumerate(pool) if e.viewed), default=-1)
        after = next((e for e in pool[last + 1 :] if not e.viewed), None)
        if after is not None:
            return after
    return next((e for e in pool if not e.viewed), None)


def first_episode(episodes: list[Item], season: int | None = None) -> Item | None:
    pool = _regular(episodes) if season is None else [e for e in _order(episodes) if e.season == season]
    return pool[0] if pool else None


def latest_episode(episodes: list[Item]) -> Item | None:
    pool = _regular(episodes)
    return pool[-1] if pool else None


def episode_at(episodes: list[Item], season: int, number: int) -> Item | None:
    return next((e for e in episodes if e.season == season and e.number == number), None)


def seasons_of(episodes: list[Item]) -> list[int]:
    return sorted({e.season for e in episodes if e.season is not None})


# --------------------------------------------------------------------------- speaking

WATCHED_PART = 0.9  # Plex counts an item watched at about 90 percent: past that, "you stopped at" is not meant


def stopped_at(item: Item) -> str:
    """``41 min`` or ``1 h 5 min`` where the user stopped, or "" for an item not started or all but finished."""
    if item.offset_ms <= 0 or (item.duration_ms and item.offset_ms >= item.duration_ms * WATCHED_PART):
        return ""
    minutes = item.offset_ms // 60_000
    if minutes < 1:
        return ""
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if hours else f"{minutes} min"


def label(item: Item) -> str:
    """How an item is named when several could be meant: ``Inception (movie, 2010)``."""
    if item.kind == "episode":
        where = f"{item.show} " if item.show else ""
        if item.season is not None and item.number is not None:
            where += f"season {item.season} episode {item.number}"
        return f'"{item.title}" ({where.strip() or "episode"})'
    return f"{item.title} ({item.kind}, {item.year})" if item.year else f"{item.title} ({item.kind})"
