"""A button's command on the wire: the real tinytuya, as Jarvis drives it, against a Tuya v3.3 hub on 127.0.0.1
that decodes every frame it is sent (with tinytuya's own message helpers) and answers as each test says."""

from __future__ import annotations

import importlib
import inspect
import json
import socket
import struct
import threading
from collections.abc import Iterator
from types import ModuleType
from typing import Any

import pytest

from jarvis_voice.home import tuya

# A made-up local key for these tests only (a real one is 16 random characters).
KEY = "WIRE-TEST-KEY-01"
NODE = "f1"  # the Fingerbot's node id on the hub


@pytest.fixture(scope="module")
def tinytuya() -> Iterator[ModuleType]:
    """The real tinytuya. Importing it starts colorama, which tests should not do: its init is a no-op meanwhile."""
    with pytest.MonkeyPatch.context() as patch:
        try:
            import colorama
        except ImportError:
            pass
        else:
            patch.setattr(colorama, "init", lambda *args, **kwargs: None)
        yield importlib.import_module("tinytuya")


class Hub:
    """A Tuya v3.3 hub: records each frame as (command, its JSON), and answers a command for the Fingerbot as
    ``behaviour`` says: "echo" (an ack, then the Fingerbot's report of the new value), "ack_only", "silent", or
    "other_cid" (an ack, then another device's report). A status query is answered without a cid, as a hub that
    answers only for itself does."""

    def __init__(self, tinytuya: ModuleType, behaviour: str) -> None:
        self.helpers = importlib.import_module("tinytuya.core.message_helper")
        self.header = importlib.import_module("tinytuya.core.header")
        self.cipher = importlib.import_module("tinytuya.core.crypto_helper").AESCipher(KEY.encode("latin1"))
        self.behaviour = behaviour
        self.frames: list[tuple[int, dict[str, Any]]] = []
        self.closed = threading.Event()  # set when Jarvis has closed its connection
        self.server = socket.create_server(("127.0.0.1", 0))
        self.server.settimeout(0.2)
        self.port = self.server.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, name="fake-tuya-hub", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(5)
        self.server.close()

    def commands(self, cmd: int) -> list[dict[str, Any]]:
        return [data for command, data in self.frames if command == cmd]

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self.server.accept()
            except OSError:
                continue
            with conn:
                conn.settimeout(0.2)
                self._talk(conn)
            self.closed.set()

    def _recv(self, conn: socket.socket, length: int) -> bytes | None:
        data = b""
        while len(data) < length:
            try:
                chunk = conn.recv(length - len(data))
            except TimeoutError:
                if self._stop.is_set():
                    return None
                continue
            except OSError:
                return None
            if not chunk:
                return None
            data += chunk
        return data

    def _talk(self, conn: socket.socket) -> None:
        size = struct.calcsize(self.header.MESSAGE_HEADER_FMT_55AA)
        while True:
            head = self._recv(conn, size)
            if head is None:
                return
            header = self.helpers.parse_header(head)
            rest = self._recv(conn, header.total_length - size)
            if rest is None:
                return
            message = self.helpers.unpack_message(head + rest, header=header, no_retcode=True)
            payload = message.payload
            if payload.startswith(self.header.PROTOCOL_33_HEADER):
                payload = payload[len(self.header.PROTOCOL_33_HEADER) :]
            data = json.loads(self.cipher.decrypt(payload, False, decode_text=False))
            self.frames.append((message.cmd, data))
            for reply in self._answer(message.seqno, message.cmd, data):
                conn.sendall(reply)

    def _answer(self, seqno: int, cmd: int, data: dict[str, Any]) -> list[bytes]:
        if cmd == 10:
            return [self._frame(seqno, 10, {"dps": {"2": False}}, versioned=False)]
        if cmd != 7 or self.behaviour == "silent":
            return []
        ack = self._frame(seqno, 7, None)
        if self.behaviour == "echo":
            return [ack, self._frame(0, 8, {"dps": data["dps"], "cid": data["cid"], "t": 1})]
        if self.behaviour == "other_cid":
            return [ack, self._frame(0, 8, {"dps": data["dps"], "cid": "f9", "t": 1})]
        return [ack]

    def _frame(self, seqno: int, cmd: int, data: dict[str, Any] | None, *, versioned: bool = True) -> bytes:
        """A 3.3 device's frame: a return code, then (for a report) the version header and the encrypted JSON."""
        body = b""
        if data is not None:
            body = self.cipher.encrypt(json.dumps(data).encode(), False)
            body = (self.header.PROTOCOL_33_HEADER if versioned else b"") + body
        message = self.helpers.TuyaMessage(
            seqno, cmd, 0, struct.pack(">I", 0) + body, 0, True, self.header.PREFIX_55AA_VALUE, None
        )
        return self.helpers.pack_message(message)


@pytest.fixture
def hub(request: pytest.FixtureRequest, tinytuya: ModuleType, monkeypatch: pytest.MonkeyPatch) -> Iterator[Hub]:
    fake = Hub(tinytuya, request.param)
    monkeypatch.setattr(tuya, "LOCAL_PORT", fake.port)
    monkeypatch.setattr(tuya, "T_MAX", 0.4)  # each wait for a frame that never comes
    monkeypatch.setattr(tuya, "CONFIRM_MAX_S", 0.6)
    yield fake
    fake.stop()


def fingerbot_link() -> tuya._Link:
    conn = tuya._Conn("finger-id", "127.0.0.1", 3.3, KEY, "hub-id", NODE, (2, 8, 12))
    return tuya._Link(conn, tuya._Budget(5.0), tuya._open_device)


@pytest.mark.parametrize("hub", ["silent"], indirect=True)
def test_a_button_command_is_one_frame_even_from_a_silent_hub(hub: Hub) -> None:
    assert fingerbot_link().send_once(2, True) is False
    assert hub.closed.wait(5)  # the session the hub kept open for the echo is closed afterwards
    # One frame: no status query first, and no second send after the silence (tinytuya resends a command that
    # waited for its reply and timed out).
    assert [cmd for cmd, _ in hub.frames] == [7]
    (sent,) = hub.commands(7)
    assert sent["cid"] == NODE and sent["dps"] == {"2": True}


@pytest.mark.parametrize("hub", ["echo"], indirect=True)
def test_the_hubs_echo_confirms_a_button_command(hub: Hub) -> None:
    assert fingerbot_link().send_once(2, True) is True
    assert hub.closed.wait(5)
    assert [cmd for cmd, _ in hub.frames] == [7]


@pytest.mark.parametrize("hub", ["ack_only"], indirect=True)
def test_an_ack_alone_is_no_confirmation_and_nothing_is_sent_again(hub: Hub) -> None:
    assert fingerbot_link().send_once(2, False) is False
    assert hub.closed.wait(5)
    assert [cmd for cmd, _ in hub.frames] == [7] and hub.commands(7)[0]["dps"] == {"2": False}


@pytest.mark.parametrize("hub", ["other_cid"], indirect=True)
def test_another_devices_report_is_not_this_ones_echo(hub: Hub) -> None:
    assert fingerbot_link().send_once(2, True) is False
    assert hub.closed.wait(5)
    assert [cmd for cmd, _ in hub.frames] == [7]


@pytest.mark.parametrize("hub", ["silent"], indirect=True)
def test_a_status_reply_without_the_devices_cid_says_nothing_about_it(hub: Hub) -> None:
    # tinytuya drops a reply for another cid (or none) and then reads again, which times out: no data.
    with pytest.raises(tuya._Failure) as failure:
        fingerbot_link().read()
    assert failure.value.err == "nodata"
    assert hub.closed.wait(5)
    (query,) = hub.commands(10)
    assert query["cid"] == NODE and [cmd for cmd, _ in hub.frames] == [10]


def test_tinytuya_takes_the_arguments_jarvis_passes(tinytuya: ModuleType) -> None:
    device = tinytuya.Device
    built = set(inspect.signature(device.__init__).parameters)
    assert {"persist", "port", "cid", "parent", "connection_timeout", "connection_retry_limit"} <= built
    assert {"connection_retry_delay", "version"} <= built
    assert "nowait" in inspect.signature(device.set_value).parameters
    for method in ("receive", "set_socketTimeout", "set_version", "status", "close"):
        assert callable(getattr(device, method, None)), method
