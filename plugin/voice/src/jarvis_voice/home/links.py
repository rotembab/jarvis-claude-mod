"""Links that open inside an app on a TV: a streamer's channel, a video.

Pure functions, no network, shared by the TV drivers. An app is listed in
LINK_APPS only with a primary source for its tvOS bundle id, its Android TV
package, and the paths its apple-app-site-association claims.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit


@dataclass(frozen=True, slots=True)
class LinkApp:
    """An app that opens its own web links, and what each TV needs to run it."""

    name: str  # the spoken name: "Kick"
    hosts: frozenset[str]  # the web hosts whose links it opens
    host: str  # the host sent to the device: the one its apple-app-site-association was read from
    apple_bundle: str | None  # its tvOS bundle id
    apple_needs: str  # "tvOS 26 or later"
    android_package: str | None  # its Android TV package
    android_needs: str  # "Android 8 or later"
    not_in_app: frozenset[str]  # first path segments the app doesn't claim
    channel: re.Pattern[str] | None  # a one-segment path matching it is a channel


# Kick's tvOS app (App Store id 6446202561) and Android TV app are both com.kick.mobile. kick.com's
# apple-app-site-association gives it every path but /tv_help and /go-live, a channel is
# kick.com/<slug>, and Kick's API allows a slug at most 25 characters.
KICK = LinkApp(
    name="Kick",
    hosts=frozenset({"kick.com", "www.kick.com"}),
    host="kick.com",
    apple_bundle="com.kick.mobile",
    apple_needs="tvOS 26 or later",
    android_package="com.kick.mobile",
    android_needs="Android 8 or later",
    not_in_app=frozenset({"tv_help", "go-live"}),
    channel=re.compile(r"[a-z0-9_-]{1,25}"),
)
LINK_APPS: tuple[LinkApp, ...] = (KICK,)


@dataclass(frozen=True, slots=True)
class Link:
    """A link to hand a TV, and what Jarvis knows about it."""

    url: str  # what goes to the device; always has a scheme (pyatv sends a string without one as a bundle id)
    app: LinkApp | None  # the app that claims it, when Jarvis knows it
    channel: str | None  # "xqc" for kick.com/xqc; None for longer paths
    bare: bool  # the app's own host with no path ("kick.com"): the app itself


_SCHEME_FORM = re.compile(r"^[a-z][a-z0-9+.-]*://\S+$", re.IGNORECASE)
# A dotted host, then a path: "kick.com/xqc". Bundle ids (no "/") and names like "AC/DC" (no dot) don't match.
_BARE_FORM = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+/\S*$", re.IGNORECASE)


def app_for_host(host: str) -> LinkApp | None:
    """The app that opens links to ``host``, if Jarvis knows one."""
    host = host.lower()
    return next((app for app in LINK_APPS if host in app.hosts), None)


def parse_link(text: str) -> Link | None:
    """The link in ``text``, or None when it is an app name or a bundle id."""
    text = text.strip()
    if _SCHEME_FORM.match(text):
        url = text
    elif _BARE_FORM.match(text) or app_for_host(text) is not None:
        url = f"https://{text}"
    else:
        return None
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
    except ValueError:  # e.g. a bad IPv6 address: hand it over as it is
        return Link(url, None, None, False)
    app = app_for_host(host)
    if app is None or parts.scheme not in ("http", "https"):
        return Link(url, None, None, False)
    # Universal links and app links are https only, and the app's association was read on its own host.
    segments = [segment for segment in parts.path.split("/") if segment]
    if segments and segments[0].lower() in app.not_in_app:
        return Link(urlunsplit(("https", app.host, parts.path, parts.query, parts.fragment)), None, None, False)
    path, channel = parts.path or "/", None
    if len(segments) == 1 and app.channel is not None and app.channel.fullmatch(segments[0].lower()):
        channel = segments[0].lower()
        path = f"/{channel}"
    return Link(urlunsplit(("https", app.host, path, parts.query, parts.fragment)), app, channel, not segments)
