"""The network scan: classification from recorded answers, redaction, the rate limit and failing searches.

No real network: the classifier gets recorded TXT records, SSDP descriptions and UDP replies; the probes
get a fake zeroconf, a fake tinytuya scanner, or a responder on 127.0.0.1.
"""

from __future__ import annotations

import json
import logging
import re
import socket
import struct
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

from jarvis_voice.home import bravia, homeassistant, scan, tuya
from jarvis_voice.home.model import DeviceRecord, HomeConfig
from jarvis_voice.home.scan import ScanEntry, ScanReport, Sighting, classify, clean_name, parse_udp_reply

# Ids as devices really send them; none may reach a report.
HAP_ID = "1A:2B:3C:4D:5E:6F"
TV_MAC = "AC:9B:0A:12:34:56"
TUYA_ID = "bf0123456789abcdefxyzq"
TUYA_KEY_PRODUCT = "keyj8wq3c7rsv9sd"
HUE_BRIDGE_ID = "001788FFFE0A1B2C"
CAST_ID = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"
FABRIC = "A1B2C3D4E5F60718"
SONY_DESCRIPTION = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0" xmlns:av="urn:schemas-sony-com:av">
  <device>
    <deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>
    <friendlyName>BRAVIA K-55XR70</friendlyName>
    <manufacturer>Sony Corporation</manufacturer>
    <modelName>K-55XR70</modelName>
    <UDN>uuid:00000000-0000-1010-8000-ac9b0a123456</UDN>
    <av:X_ScalarWebAPI_DeviceInfo>
      <av:X_ScalarWebAPI_ServiceList>
        <av:X_ScalarWebAPI_ServiceType>guide</av:X_ScalarWebAPI_ServiceType>
        <av:X_ScalarWebAPI_ServiceType>videoScreen</av:X_ScalarWebAPI_ServiceType>
      </av:X_ScalarWebAPI_ServiceList>
    </av:X_ScalarWebAPI_DeviceInfo>
    <serviceList><service><serviceType>urn:schemas-upnp-org:service:RenderingControl:1</serviceType></service></serviceList>
  </device>
</root>"""


@pytest.fixture(autouse=True)
def fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scan, "_history", scan._History())
    monkeypatch.setattr(bravia, "_lan_addresses", lambda: [])  # never the real adapters


def mdns(service: str, name: str, host: str | None = "192.168.1.20", server: str = "", **props: str) -> Sighting:
    return Sighting("mdns", service, host, name, {k.lower(): v for k, v in props.items()}, server)


def sony_tv(host: str = "192.168.1.40") -> list[Sighting]:
    """A Sony Google TV: SSDP with videoScreen, plus Cast, AirPlay, the Android TV remote and HomeKit."""
    return [
        Sighting(
            "ssdp", "ssdp", host, "BRAVIA K-55XR70", {"st": bravia.SSDP_ST, **scan.parse_description(SONY_DESCRIPTION)}
        ),
        mdns("_googlecast._tcp", f"BRAVIA-4K-XR-{CAST_ID}", host, fn="Living Room TV", md="BRAVIA 4K XR", id=CAST_ID),
        mdns("_airplay._tcp", "Living Room TV", host, manufacturer="Sony", model="K-55XR70", deviceid=TV_MAC),
        mdns("_androidtvremote2._tcp", "Living Room TV", host, bt=TV_MAC),
        mdns("_hap._tcp", "Living Room TV 5E6F", host, ci="31", sf="0", md="K-55XR70", id=HAP_ID),
    ]


def report_of(sightings: list[Sighting], config: HomeConfig | None = None) -> ScanReport:
    return scan._report(scan._Heard(sightings, [], 7.0), config or HomeConfig())


def no_ids(text: str) -> None:
    for secret in (HAP_ID, TV_MAC, TUYA_ID, TUYA_KEY_PRODUCT, HUE_BRIDGE_ID, CAST_ID, FABRIC):
        assert secret.lower() not in text.lower()
    assert not re.search(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?![\d.])", text), "an IPv4 address"
    assert not re.search(r"[0-9a-f]{2}(?::[0-9a-f]{2}){5}", text, re.IGNORECASE), "a MAC"
    assert not re.search(r"(?<![0-9a-z])[0-9a-f]{8,}(?![0-9a-z])", text, re.IGNORECASE), "a hex id"


# --------------------------------------------------------------------------- HomeKit and Matter


@pytest.mark.parametrize(
    ("ci", "kind"), [("5", "light"), ("7", "outlet"), ("8", "switch"), ("9", "thermostat"), ("6", "lock")]
)
def test_homekit_in_apple_home(ci: str, kind: str) -> None:
    entries = classify([mdns("_hap._tcp", "Desk Thing", ci=ci, sf="0", md="Gizmo 2", id=HAP_ID)], HomeConfig())
    assert entries == [ScanEntry(kind, "Desk Thing", "HomeKit", "apple_home")]
    text = report_of([mdns("_hap._tcp", "Desk Thing", ci=ci, sf="0", id=HAP_ID)]).text()
    # sf=0 says some HomeKit controller paired with it: usually Apple Home, but Home Assistant can too.
    heading = (
        "Already in a HomeKit home (usually Apple Home, where Siri controls them; Jarvis can't pair with them too):"
    )
    assert heading in text
    assert f'- HomeKit {kind} "Desk Thing".' in text
    no_ids(text)


@pytest.mark.parametrize(("ci", "kind"), [("3", "fan"), ("14", "blind"), ("10", "sensor")])
def test_homekit_not_in_any_home(ci: str, kind: str) -> None:
    text = report_of([mdns("_hap._tcp", "New Thing", ci=ci, sf="1", id=HAP_ID)]).text()
    assert "HomeKit devices not in any home yet:" in text
    assert f'- HomeKit {kind} "New Thing": add it in the Home app for Siri.' in text
    assert "Already in a HomeKit home" not in text


def test_homekit_without_its_status_flags_is_not_said_to_be_in_a_home() -> None:
    # A record whose TXT never arrived can't say whether the accessory is paired.
    motion = mdns("_hap._udp", "Eve Motion 1A2B", None, "Eve-Motion-1A2B.local.", ci="10")
    assert classify([motion], HomeConfig()) == [ScanEntry("other device", "", "", "ignored")]
    hue = [
        mdns("_hue._tcp", "Hue Bridge", "192.168.1.90", bridgeid=HUE_BRIDGE_ID),
        mdns("_hap._tcp", "Philips Hue - 0A1B2C", "192.168.1.90", ci="2", md="BSB002"),
    ]
    assert "HomeKit home" not in report_of(hue).text()


def test_homekit_model_names_the_brand_and_thread_devices_need_no_address() -> None:
    # A Thread accessory (via the Apple TV border router) has no IPv4 address: its records merge by host name.
    eve = [
        mdns("_hap._udp", "Eve Energy 2B3C", None, "Eve-Energy-2B3C.local.", ci="7", sf="0", md="Eve Energy 20EBO8301"),
        mdns("_matter._tcp", f"{FABRIC}-00000000000000A1", None, "Eve-Energy-2B3C.local."),
    ]
    assert classify(eve, HomeConfig()) == [ScanEntry("outlet", "Eve Energy 2B3C", "Eve", "apple_home")]


def test_matter_devices_waiting_and_in_homes() -> None:
    sightings = [
        mdns("_matterc._udp", "5A3C1E0F9B2D4C6E", "192.168.1.60", DT="257", DN="Hall bulb", D="3840", CM="1"),
        mdns("_matter._tcp", f"{FABRIC}-0000000000000001", "192.168.1.61"),
        mdns("_matter._tcp", f"{FABRIC}-0000000000000002", None, "node2.local."),
        mdns("_matter._tcp", "0F0E0D0C0B0A0908-0000000000000009", None, "node9.local."),
    ]
    entries = classify(sightings, HomeConfig())
    assert ScanEntry("light", "Hall bulb", "Matter", "matter", detail="waiting to be set up") in entries
    assert [e.count for e in entries if e.brand == "Matter" and not e.detail] == [2, 1]
    text = report_of(sightings).text()
    assert '- Matter light "Hall bulb": waiting to be set up.' in text
    assert "- 2 Matter devices in one Matter home." in text
    assert "- 1 Matter device in one Matter home." in text
    assert "found 4 smart devices" in text
    no_ids(text)


def test_a_matter_device_in_two_homes_counts_once() -> None:
    # Shared with Apple Home and Google Home: one operational name per Matter home, on the same address.
    sightings = [
        mdns("_matter._tcp", f"{FABRIC}-0000000000000001", "192.168.1.60"),
        mdns("_matter._tcp", "1122334455667788-00000000000000B2", "192.168.1.60"),
        mdns("_matter._tcp", f"{FABRIC}-0000000000000003", "192.168.1.62"),
    ]
    assert sorted((e.count, e.homes) for e in classify(sightings, HomeConfig())) == [(1, 1), (1, 2)]
    text = report_of(sightings).text()
    assert "found 2 smart devices" in text
    assert "- 1 Matter device shared between 2 Matter homes." in text
    assert "- 1 Matter device in one Matter home." in text
    no_ids(text)


# --------------------------------------------------------------------------- supported devices


def test_bravia_folds_cast_airplay_and_homekit_into_one_tv() -> None:
    entries = classify(sony_tv(), HomeConfig())
    detail = "add it in home setup; also in a HomeKit home"
    assert entries == [ScanEntry("TV", "Living Room TV", "Sony Bravia", "not_set_up", detail=detail)]
    text = report_of(sony_tv()).text()
    assert "Jarvis controls these:" in text
    assert '- Sony Bravia TV "Living Room TV": not set up yet: add it in home setup; also in a HomeKit home.' in text
    no_ids(text)


def test_bravia_set_up_by_address() -> None:
    tv = DeviceRecord("bravia-tv", "bravia", "Salon TV", "tv", settings={"address": "192.168.1.40:80"})
    entries = classify(sony_tv(), HomeConfig([tv]))
    assert [(e.status, e.jarvis_name, e.brand) for e in entries] == [("set_up", "Salon TV", "Sony Bravia")]
    assert (
        '- Sony Bravia TV "Living Room TV": set up as Salon TV; also in a HomeKit home.'
        in report_of(sony_tv(), HomeConfig([tv])).text()
    )


def test_bravia_from_cast_alone_and_sony_soundbar() -> None:
    cast_only = [mdns("_googlecast._tcp", "x", fn="Bedroom TV", md="BRAVIA 4K VH2")]
    assert classify(cast_only, HomeConfig())[0].brand == "Sony Bravia"
    soundbar = Sighting(
        "ssdp", "ssdp", "192.168.1.41", props={"scalar": "audio system", "manufacturer": "Sony Corporation"}
    )
    assert classify([soundbar], HomeConfig())[0] == ScanEntry("sound system", "", "Sony", "could_add")


def test_tuya_known_and_unknown_ids() -> None:
    plug = DeviceRecord("tuya-plug", "tuya", "Desk plug", "plug", settings={"tuya_id": TUYA_ID})
    child = DeviceRecord("tuya-sensor", "tuya", "Door sensor", "sensor", settings={"tuya_id": "c1", "parent": "gw1"})
    child2 = DeviceRecord("tuya-button", "tuya", "Hall button", "button", settings={"tuya_id": "c2", "parent": "gw1"})
    config = HomeConfig([plug, child, child2])
    known = Sighting("tuya", "tuya", "192.168.1.30", props={"id": TUYA_ID})
    gateway = Sighting("tuya", "tuya", "192.168.1.31", props={"id": "gw1"})
    stranger = Sighting("tuya", "tuya", "192.168.1.32", props={"id": "bf9999999999999999zzzz"})
    entries = classify([known, gateway, stranger], config)
    assert ScanEntry("device", "", "Tuya", "set_up", jarvis_name="Desk plug") in entries
    assert ScanEntry(
        "gateway", "", "Tuya", "set_up", detail="Jarvis reaches Door sensor and Hall button through it"
    ) in (entries)
    assert ScanEntry("device", "", "Tuya", "not_set_up", detail="refresh the Tuya link in home setup") in entries
    text = report_of([known, gateway, stranger], config).text()
    assert "- Tuya device: set up as Desk plug." in text
    assert "- Tuya device: not set up yet: refresh the Tuya link in home setup." in text
    no_ids(text)
    # With no Tuya link at all, the way in is linking it.
    assert classify([stranger], HomeConfig())[0].detail == "link Tuya in home setup"


def test_apple_tv_set_up_by_identifier_homepod_and_computers() -> None:
    atv = DeviceRecord("appletv-den", "appletv", "Den TV", "media_player", settings={"identifiers": [TV_MAC]})
    sightings = [
        mdns("_companion-link._tcp", "Den", "192.168.1.50", rpMd="AppleTV14,1"),
        mdns("_airplay._tcp", "Den", "192.168.1.50", model="AppleTV14,1", deviceid=TV_MAC),
        mdns("_raop._tcp", "AC9B0A123456@Den", "192.168.1.50", am="AppleTV14,1"),
        mdns("_airplay._tcp", "Kitchen", "192.168.1.51", model="AudioAccessory5,1"),
        mdns("_companion-link._tcp", "Rotem's MacBook Pro", "192.168.1.52", rpMd="MacBookPro18,3"),
        mdns("_companion-link._tcp", "Rotem's iPhone", "192.168.1.53"),
    ]
    entries = classify(sightings, HomeConfig([atv]))
    assert ScanEntry("Apple TV", "Den", "Apple", "set_up", jarvis_name="Den TV") in entries
    assert ScanEntry("HomePod", "Kitchen", "Apple", "could_add", detail="volume and audio only") in entries
    text = report_of(sightings, HomeConfig([atv])).text()
    assert '- Apple TV "Den": set up as Den TV.' in text
    assert "I left out 2 computers or phones." in text
    assert "MacBook" not in text and "iPhone" not in text


def test_home_assistant_matched_by_saved_url() -> None:
    ha = mdns("_home-assistant._tcp", "Home", "192.168.1.70", location_name="Home", base_url="http://192.168.1.70:8123")
    config = HomeConfig(hubs={"homeassistant": {"url": "http://192.168.1.70:8123"}})
    assert classify([ha], config) == [ScanEntry("hub", "Home", "Home Assistant", "set_up")]
    assert classify([ha], HomeConfig())[0].status == "not_set_up"


def test_home_assistant_saved_by_its_default_name(monkeypatch: pytest.MonkeyPatch) -> None:
    # Home Assistant announces its address, not homeassistant.local, which is what setup offers by default.
    ha = mdns(
        "_home-assistant._tcp",
        "Home",
        "192.168.1.70",
        location_name="Home",
        base_url="http://192.168.1.70:8123",
        internal_url="http://192.168.1.70:8123",
    )
    config = HomeConfig(hubs={"homeassistant": {"url": homeassistant.DEFAULT_URL}})
    by_name = {"homeassistant.local": frozenset({"192.168.1.70"})}
    assert classify([ha], config, by_name) == [ScanEntry("hub", "Home", "Home Assistant", "set_up")]
    # The name has another address (a Tailscale name, or a second Home Assistant). Jarvis keeps one Home
    # Assistant, so it never says to connect this one: that would replace the one that works.
    elsewhere = {"homeassistant.local": frozenset({"192.168.1.71"})}
    another = "Jarvis is connected to a Home Assistant at another address; if it's this one, there's nothing to do."
    hue = mdns("_hue._tcp", "Hue Bridge", "192.168.1.90", bridgeid=HUE_BRIDGE_ID)
    by_ip = HomeConfig(hubs={"homeassistant": {"url": "http://192.168.1.71:8123"}})
    for heard, saved in (
        (scan._Heard([ha, hue], [], 7.0, elsewhere), config),
        (scan._Heard([ha, hue], [], 7.0), by_ip),
    ):
        assert classify(heard.sightings, saved, heard.looked_up)[0].status == "unconfirmed"
        text = scan._report(heard, saved).text()
        assert f'- Home Assistant hub "Home": {another}' in text
        assert "Devices that are in your Home Assistant already work through it" in text
        assert "not set up" not in text and "connect it" not in text
        no_ids(text)
    # The name could not be looked up: Jarvis can't tell, so it never says the hub isn't set up.
    text = report_of([ha], config).text()
    assert '- Home Assistant hub "Home": Jarvis has a Home Assistant saved by name and couldn\'t check' in text
    assert "not set up" not in text

    # The scan looks the name up alongside its searches.
    looked: list[str] = []

    def getaddrinfo(host: str, *args: Any) -> list[tuple[Any, ...]]:
        looked.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.70", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    report = scan.scan(config, probes={"mdns": lambda _s, _n: [ha]}, seconds=0.1)
    assert looked == ["homeassistant.local"]
    assert '- Home Assistant hub "Home": set up.' in report.text()
    assert "homeassistant.local" not in report.text() + text
    no_ids(report.text())


def test_home_assistant_reaches_what_jarvis_has_no_driver_for() -> None:
    sightings = [
        mdns("_home-assistant._tcp", "Home", "192.168.1.70", location_name="Home", base_url="http://192.168.1.70:8123"),
        mdns("_hue._tcp", "Hue Bridge", "192.168.1.90", bridgeid=HUE_BRIDGE_ID),
        mdns("_matter._tcp", f"{FABRIC}-0000000000000001", "192.168.1.61"),
    ]
    connected = HomeConfig(hubs={"homeassistant": {"url": "http://192.168.1.70:8123"}})
    text = report_of(sightings, connected).text()
    assert "Devices that are in your Home Assistant already work through it, whatever their brand" in text
    assert "Matter devices (Jarvis has no Matter driver of its own; the app that set them up can control them):" in text
    # Found but not connected yet.
    assert "once you connect it in home setup" in report_of(sightings).text()
    # No Home Assistant at all, or nothing it could add: no word about it.
    assert "Home Assistant" not in report_of(sightings[1:]).text()
    assert "work through it" not in report_of(sightings[:1], connected).text()


# --------------------------------------------------------------------------- other brands


def test_cast_names_and_groups() -> None:
    # A Google Home speaker group is announced from the address of the speaker that leads it.
    speaker = mdns(
        "_googlecast._tcp",
        f"Google-Nest-Mini-{CAST_ID}",
        "192.168.1.80",
        fn="Kitchen speaker",
        md="Google Nest Mini",
    )
    group = mdns(
        "_googlecast._tcp", f"Google-Cast-Group-{CAST_ID}", "192.168.1.80", fn="Everywhere", md="Google Cast Group"
    )
    kitchen = [ScanEntry("speaker", "Kitchen speaker", "Google", "could_add")]
    assert classify([speaker, group], HomeConfig()) == kitchen
    assert classify([group, speaker], HomeConfig()) == kitchen
    assert classify([group], HomeConfig()) == []
    sightings = [group, speaker]
    text = report_of(sightings).text()
    assert "Jarvis could control these with a new driver:" in text
    assert '- Google speaker "Kitchen speaker".' in text
    no_ids(text)


def test_hue_bridge_and_shelly() -> None:
    sightings = [
        mdns("_hue._tcp", "Philips Hue - 0A1B2C", "192.168.1.90", bridgeid=HUE_BRIDGE_ID, modelid="BSB002"),
        mdns("_hap._tcp", "Philips Hue - 0A1B2C", "192.168.1.90", ci="2", sf="0", md="BSB002", id=HAP_ID),
        mdns("_shelly._tcp", "shellyplus1pm-a8032ab12345", "192.168.1.91", gen="2"),
        mdns("_http._tcp", "shelly1-34945475a1b2", "192.168.1.92"),
    ]
    entries = classify(sightings, HomeConfig())
    hue = ScanEntry(
        "bridge",
        "Philips Hue",
        "Philips Hue",
        "could_add",
        detail="needs the button on the bridge pressed once; also in a HomeKit home",
    )
    assert hue in entries
    assert ScanEntry("device", "shellyplus1pm", "Shelly", "could_add") in entries
    assert ScanEntry("device", "shelly1", "Shelly", "could_add") in entries
    text = report_of(sightings).text()
    assert "- Philips Hue bridge: needs the button on the bridge pressed once; also in a HomeKit home." in text
    no_ids(text)


def test_printers_routers_and_unknowns_are_only_counted() -> None:
    sightings = [
        mdns("_ipp._tcp", "HP LaserJet [A1B2C3]", "192.168.1.100", ty="HP LaserJet", UUID=CAST_ID),
        Sighting(
            "ssdp", "ssdp", "192.168.1.1", props={"devicetype": "urn:schemas-upnp-org:device:InternetGatewayDevice:1"}
        ),
        mdns("_spotify-connect._tcp", "Something", "192.168.1.101"),
    ]
    report = report_of(sightings)
    assert report.devices() == []
    text = report.text()
    assert "found no smart devices" in text
    assert "I left out 1 printer, 1 router or network box and 1 other device." in text
    assert "LaserJet" not in text


def test_udp_replies() -> None:
    sysinfo = {"system": {"get_sysinfo": {"alias": "Desk lamp", "model": "KL110(US)", "mic_type": "IOT.SMARTBULB"}}}
    kasa = parse_udp_reply(scan.kasa_crypt(json.dumps(sysinfo).encode()), "192.168.1.110")
    lifx = parse_udp_reply(
        struct.pack("<HHI8s6sBB8sHHBI", 41, 0x1400, 1, b"\1" * 8, b"\0" * 6, 0, 0, b"\0" * 8, 3, 0, 1, 56700),
        "192.168.1.111",
    )
    wiz = parse_udp_reply(
        b'{"method":"registration","env":"pro","result":{"mac":"a8bb50aabbcc","success":true}}', "192.168.1.112"
    )
    yeelight = parse_udp_reply(
        b"HTTP/1.1 200 OK\r\nCache-Control: max-age=3600\r\nLocation: yeelight://192.168.1.113:55443\r\n"
        b"id: 0x000000000015243f\r\nmodel: color\r\n\r\n",
        "192.168.1.113",
    )
    govee = parse_udp_reply(
        b'{"msg":{"cmd":"scan","data":{"ip":"192.168.1.114","sku":"H6159"}}}', "192.168.1.114", govee=True
    )
    assert [s.service if s else None for s in (kasa, lifx, wiz, yeelight, govee)] == [
        "kasa",
        "lifx",
        "wiz",
        "yeelight",
        "govee",
    ]
    entries = classify([s for s in (kasa, lifx, wiz, yeelight, govee) if s], HomeConfig())
    assert ScanEntry("light", "Desk lamp", "TP-Link Kasa", "could_add") in entries
    assert {e.brand for e in entries} == {"TP-Link Kasa", "LIFX", "WiZ", "Yeelight", "Govee"}
    # The queries themselves (as another app on the network would send them) are no answer.
    for query in (scan.KASA_QUERY, scan.LIFX_GET_SERVICE, scan.WIZ_QUERY, scan.YEELIGHT_QUERY, b"\x00garbage"):
        assert parse_udp_reply(query, "192.168.1.115") is None
    assert parse_udp_reply(scan.GOVEE_QUERY, "192.168.1.115", govee=True) is None


def test_description_parsing() -> None:
    fields = scan.parse_description(SONY_DESCRIPTION)
    assert fields["friendlyname"] == "BRAVIA K-55XR70"
    assert fields["manufacturer"] == "Sony Corporation"
    assert fields["scalar"].split() == ["guide", "videoScreen"]
    assert "MediaRenderer" in fields["devicetype"]


# --------------------------------------------------------------------------- redaction


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("Living Room TV", "Living Room TV"),
        ("Shapes 4A2B", "Shapes 4A2B"),
        ("AC9B0A123456@Kitchen", "Kitchen"),
        ("Bedroom AC:9B:0A:12:34:56", "Bedroom"),
        ("Bedroom ac-9b-0a-12-34-56", "Bedroom"),
        ("eWeLink_1000abcdef", "eWeLink"),
        ("Google-Nest-Mini-0f1e2d3c4b5a69788796a5b4c3d2e1f0", "Google-Nest-Mini"),
        ("esp-kitchen-a1b2c3", "esp-kitchen"),
        ("Philips Hue - 0A1B2C", "Philips Hue"),
        ("Plug (192.168.1.20)", "Plug"),
        ("uuid:00000000-0000-1010-8000-ac9b0a123456", ""),
        ("Office DEADBEEF01", "Office"),
        ("12345678", ""),
        # A MAC's last bytes with separators (Nanoleaf), and the serial an unrenamed Roku ends its name with.
        ("Light Panels 53:A4:F1", "Light Panels"),
        ("Plug 53-A4-F1", "Plug"),
        ("Roku 3 - 1GU48T017973", "Roku 3"),
        ("Roku Express - X004000AB123", "Roku Express"),
        # Model names stay.
        ("BRAVIA K-55XR70", "BRAVIA K-55XR70"),
        ("[LG] webOS TV OLED55C1PUB", "[LG] webOS TV OLED55C1PUB"),
        ("Sonos Play:1", "Sonos Play:1"),
        ("Eve Energy 2B3C", "Eve Energy 2B3C"),
    ],
)
def test_clean_name(raw: str, clean: str) -> None:
    assert clean_name(raw) == clean


def test_report_and_log_carry_no_ids(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    sightings = [
        *sony_tv(),
        Sighting("tuya", "tuya", "192.168.1.30", props={"id": TUYA_ID, "productkey": TUYA_KEY_PRODUCT}),
        mdns("_hue._tcp", f"Hue Bridge {HUE_BRIDGE_ID}", "192.168.1.90", bridgeid=HUE_BRIDGE_ID),
        mdns("_hap._tcp", f"Lamp {HAP_ID}", "192.168.1.91", ci="5", sf="1", id=HAP_ID),
        mdns("_matter._tcp", f"{FABRIC}-0000000000000001", "192.168.1.92"),
    ]
    report = scan.scan(HomeConfig(), seconds=0.1, probes={"fake": lambda _s, _n: sightings})
    no_ids(report.text())
    no_ids(json.dumps(report.to_json()))
    no_ids(caplog.text)
    assert TUYA_ID not in repr(sightings)


# --------------------------------------------------------------------------- the scan itself


def test_rate_limit_returns_the_last_report() -> None:
    now = [1000.0]
    calls: list[float] = []

    def probe(seconds: float, notes: list[str]) -> list[Sighting]:
        calls.append(seconds)
        return [mdns("_hue._tcp", "Hue Bridge")]

    first = scan.scan(probes={"fake": probe}, seconds=0.1, clock=lambda: now[0])
    assert len(calls) == 1 and first.note == ""
    now[0] += 30
    again = scan.scan(probes={"fake": probe}, seconds=0.1, clock=lambda: now[0])
    assert len(calls) == 1
    assert again.entries == first.entries
    assert again.text().startswith("I searched 30 seconds ago and search at most once a minute")
    now[0] += 31
    scan.scan(probes={"fake": probe}, seconds=0.1, clock=lambda: now[0])
    assert len(calls) == 2


def test_the_last_report_is_matched_against_the_setup_as_it_is_now() -> None:
    now = [1000.0]
    calls: list[float] = []

    def probe(seconds: float, notes: list[str]) -> list[Sighting]:
        calls.append(seconds)
        return sony_tv()

    first = scan.scan(HomeConfig(), probes={"fake": probe}, seconds=0.1, clock=lambda: now[0])
    assert "not set up yet: add it in home setup" in first.text()
    # The TV is added in home setup, and the user asks again within the minute.
    now[0] += 40
    tv = DeviceRecord("bravia-tv", "bravia", "Salon TV", "tv", settings={"address": "192.168.1.40:80"})
    again = scan.scan(HomeConfig([tv]), probes={"fake": probe}, seconds=0.1, clock=lambda: now[0])
    assert len(calls) == 1
    assert again.text().startswith("I searched 40 seconds ago")
    assert '- Sony Bravia TV "Living Room TV": set up as Salon TV; also in a HomeKit home.' in again.text()
    assert "not set up" not in again.text()


def test_a_second_scan_while_one_runs_is_refused() -> None:
    started, release = threading.Event(), threading.Event()

    def slow(seconds: float, notes: list[str]) -> list[Sighting]:
        started.set()
        release.wait(5)
        return []

    worker = threading.Thread(target=scan.scan, kwargs={"probes": {"slow": slow}, "seconds": 3.0})
    worker.start()
    assert started.wait(5)
    try:
        busy = scan.scan(probes={"slow": slow})
        assert busy.text() == "I'm already searching the network. Ask again in a few seconds."
        assert not busy.ran
    finally:
        release.set()
        worker.join(5)


def test_nothing_answered_points_at_the_firewall() -> None:
    report = scan.scan(probes={"mdns": lambda _s, _n: [], "ssdp": lambda _s, _n: []}, seconds=0.1)
    text = report.text()
    assert not report.answered
    assert "nothing answered" in text
    assert "Windows Firewall" in text and "Private" in text and "Public" in text


def test_only_mdns_silent_adds_a_firewall_note() -> None:
    probes = {
        "mdns": lambda _s, _n: [],
        "tuya": lambda _s, _n: [Sighting("tuya", "tuya", "192.168.1.30", props={"id": "x"})],
    }
    text = scan.scan(probes=probes, seconds=0.1).text()
    assert "Nothing answered the mDNS search" in text


def test_a_failing_search_does_not_break_the_scan(caplog: pytest.LogCaptureFixture) -> None:
    def broken(seconds: float, notes: list[str]) -> list[Sighting]:
        raise RuntimeError("boom with a secret 192.168.1.7")

    def skipped(seconds: float, notes: list[str]) -> list[Sighting]:
        notes.append("I couldn't listen for Tuya devices.")
        return []

    probes = {"ssdp": broken, "tuya": skipped, "mdns": lambda _s, _n: [mdns("_hue._tcp", "Hue Bridge")]}
    report = scan.scan(probes=probes, seconds=0.1)
    text = report.text()
    assert "- Philips Hue bridge" in text
    assert "The UPnP search failed." in text
    assert "I couldn't listen for Tuya devices." in text
    assert "boom" not in caplog.text and "192.168.1.7" not in caplog.text
    assert "RuntimeError" in caplog.text


def test_a_search_that_hangs_is_left_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scan, "JOIN_MARGIN_S", 0.1)
    stop = threading.Event()

    def hangs(seconds: float, notes: list[str]) -> list[Sighting]:
        stop.wait(5)
        return [mdns("_hue._tcp", "Late")]

    started = time.monotonic()
    try:
        report = scan.scan(probes={"udp": hangs, "mdns": lambda _s, _n: [mdns("_elg._tcp", "Key Light")]}, seconds=0.1)
    finally:
        stop.set()
    assert time.monotonic() - started < 2.0
    assert "The Kasa, LIFX, WiZ, Yeelight and Govee search took too long" in report.text()
    assert [e.brand for e in report.entries] == ["Elgato"]


def test_answers_from_this_pc_are_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bravia, "_lan_addresses", lambda: ["192.168.1.5"])
    probes = {"mdns": lambda _s, _n: [mdns("_spotify-connect._tcp", "My PC", "192.168.1.5")]}
    assert not scan.scan(probes=probes, seconds=0.1).answered


# --------------------------------------------------------------------------- the probes, with fakes


def test_tuya_probe_listens_only(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def devices(**kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {TUYA_ID: {"ip": "192.168.1.30", "version": "3.3", "productKey": TUYA_KEY_PRODUCT, "gwId": TUYA_ID}}

    monkeypatch.setattr(tuya, "_scanner", lambda: SimpleNamespace(devices=devices))
    notes: list[str] = []
    found = scan._tuya_probe(3.0, notes)
    assert seen["poll"] is False and "wantids" not in seen and seen["tuyadevices"]
    assert [(s.service, s.host, s.props) for s in found] == [("tuya", "192.168.1.30", {"id": TUYA_ID})]
    assert notes == []

    def busy(**kwargs: Any) -> dict[str, Any]:
        raise OSError(98, "Address already in use")

    monkeypatch.setattr(tuya, "_scanner", lambda: SimpleNamespace(devices=busy))
    assert scan._tuya_probe(3.0, notes) == []
    assert notes and "Tuya" in notes[0]


class FakeInfo:
    def __init__(self, service_type: str, name: str) -> None:
        self.name = name
        self.decoded_properties = FakeZeroconf.records.get(name, ({}, None))[0]
        self.server = "device.local." if name in FakeZeroconf.records else None

    def load_from_cache(self, zc: Any) -> bool:
        return self.name in FakeZeroconf.records

    def parsed_addresses(self, version: Any = None) -> list[str]:
        address = FakeZeroconf.records.get(self.name, ({}, None))[1]
        return [address] if address else []


class FakeZeroconf:
    """Enough of python-zeroconf for the mDNS probe: a browser that announces ``records`` at once, and
    ``unresolved`` names that are heard but never resolve."""

    records: ClassVar[dict[str, tuple[dict[str, str], str | None]]] = {}
    unresolved: ClassVar[list[str]] = []
    closed = False
    InterfaceChoice = SimpleNamespace(All="all")
    IPVersion = SimpleNamespace(V4Only="v4")
    ServiceStateChange = SimpleNamespace(Added="added", Removed="removed")
    ServiceInfo = FakeInfo

    class Zeroconf:
        def __init__(self, interfaces: Any, ip_version: Any) -> None:
            self.interfaces = interfaces

        def get_service_info(self, service_type: str, name: str, timeout: int) -> None:
            return None

        def close(self) -> None:
            FakeZeroconf.closed = True

    class ServiceBrowser:
        def __init__(self, zc: Any, types: list[str], handlers: list[Any]) -> None:
            assert "_hap._tcp.local." in types and "_matterc._udp.local." in types
            for name in [*FakeZeroconf.records, *FakeZeroconf.unresolved]:
                service_type = name.split(".", 1)[1]
                handlers[0](zeroconf=zc, service_type=service_type, name=name, state_change="added")
            handlers[0](
                zeroconf=zc, service_type="_ipp._tcp.local.", name="Gone._ipp._tcp.local.", state_change="removed"
            )

        def cancel(self) -> None:
            pass


def test_mdns_probe_resolves_what_it_browses(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeZeroconf.records = {
        "Desk Lamp._hap._tcp.local.": ({"ci": "5", "sf": "0", "id": HAP_ID}, "192.168.1.20"),
        "Kitchen._googlecast._tcp.local.": ({"fn": "Kitchen", "md": "Google Nest Mini"}, "192.168.1.21"),
    }
    FakeZeroconf.unresolved = []
    FakeZeroconf.closed = False
    monkeypatch.setattr(scan, "_zeroconf", lambda: FakeZeroconf)
    found = scan._mdns_probe(0.6, [])
    assert sorted((s.service, s.name, s.host) for s in found) == [
        ("_googlecast._tcp", "Kitchen", "192.168.1.21"),
        ("_hap._tcp", "Desk Lamp", "192.168.1.20"),
    ]
    assert FakeZeroconf.closed


def test_mdns_names_that_never_resolved_add_no_devices(monkeypatch: pytest.MonkeyPatch) -> None:
    # An Apple TV and a Hue bridge each answer one record in full and one by name only; a Matter
    # device is heard by name only, which still says which Matter home it is in.
    FakeZeroconf.records = {
        "Den._airplay._tcp.local.": ({"model": "AppleTV14,1"}, "192.168.1.50"),
        "Hue Bridge._hue._tcp.local.": ({"bridgeid": HUE_BRIDGE_ID}, "192.168.1.90"),
    }
    FakeZeroconf.unresolved = [
        "Den._companion-link._tcp.local.",
        "Philips Hue - 0A1B2C._hap._tcp.local.",
        f"{FABRIC}-0000000000000001._matter._tcp.local.",
    ]
    monkeypatch.setattr(scan, "_zeroconf", lambda: FakeZeroconf)
    found = scan._mdns_probe(0.6, [])
    assert len(found) == 5
    text = report_of(found).text()
    assert "found 3 smart devices" in text
    assert '- Apple TV "Den": not set up yet: add it in home setup.' in text
    assert "- 1 Matter device in one Matter home." in text
    assert "HomeKit" not in text and "computer" not in text
    no_ids(text)


def test_mdns_probe_without_the_library(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing() -> Any:
        raise ImportError("zeroconf")

    monkeypatch.setattr(scan, "_zeroconf", missing)
    notes: list[str] = []
    assert scan._mdns_probe(0.5, notes) == []
    assert notes and "missing" in notes[0]


class Description(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = SONY_DESCRIPTION.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/xml")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def description_server() -> Iterator[int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Description)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def test_ssdp_probe_on_loopback(description_server: int) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(0.05)
    searches: list[bytes] = []
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set():
            try:
                data, sender = sock.recvfrom(4096)
            except (TimeoutError, ConnectionResetError):
                continue
            except OSError:
                return
            searches.append(data)
            if bravia.SSDP_ST.encode() in data:
                location = f"http://127.0.0.1:{description_server}/dmr.xml"
                sock.sendto(f"HTTP/1.1 200 OK\r\nLOCATION: {location}\r\nST: {bravia.SSDP_ST}\r\n\r\n".encode(), sender)
                # A description elsewhere is never fetched.
                sock.sendto(
                    b"HTTP/1.1 200 OK\r\nLOCATION: http://192.0.2.1:52323/dmr.xml\r\nST: roku:ecp\r\n\r\n", sender
                )

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        notes: list[str] = []
        found = scan._ssdp_probe(1.0, notes, target=sock.getsockname())
    finally:
        stop.set()
        thread.join(2)
        sock.close()
    assert len(searches) == 2 * len(scan.SSDP_TARGETS)
    assert [s.host for s in found] == ["127.0.0.1"]
    entries = classify(found, HomeConfig())
    assert entries == [ScanEntry("TV", "BRAVIA K-55XR70", "Sony Bravia", "not_set_up", detail="add it in home setup")]
