"""Rejected HTTP requests must not lose their response to unread-body resets."""
from io import BytesIO
from types import SimpleNamespace

import pytest

from linptech_ble.dashboard import DashboardHandler


class Socket:
    def __init__(self):
        self.timeout = None
        self.timeouts = []

    def gettimeout(self):
        return self.timeout

    def settimeout(self, value):
        self.timeout = value
        self.timeouts.append(value)


class Body(BytesIO):
    def __init__(self, value):
        super().__init__(value)
        self.reads, self.drains = [], []

    def read(self, size=-1):
        self.reads.append(size)
        return super().read(size)

    def read1(self, size=-1):
        self.drains.append(size)
        return super().read(size)


def handler(body, *, headers=None):
    request = DashboardHandler.__new__(DashboardHandler)
    request.command, request.path = "POST", "/api/sensor/direct"
    request.server = SimpleNamespace(server_address=("127.0.0.1", 8765))
    request.headers = {"Host": "127.0.0.1:8765", "Content-Length": str(len(body)),
                       "Content-Type": "application/json", "X-Linptech-Control": "1", **(headers or {})}
    request.connection, request.rfile, request.wfile = Socket(), Body(body), BytesIO()
    request.close_connection = True
    request.responses = []
    request._headers = lambda status, mime: request.responses.append(status)
    request.send_header = lambda *args: None
    request.end_headers = lambda: None
    return request


@pytest.mark.parametrize("headers", [{"Origin": "https://untrusted.example"},
                                     {"Content-Type": "text/plain"}])
def test_early_rejection_consumes_small_framed_body_before_writing_response(headers):
    body = b'{"model":"KS2BB","action":"connect"}'
    request = handler(body, headers=headers)

    def response(status, mime):
        assert request.rfile.tell() == len(body), "Closing with unread input can reset the response on Windows"
        request.responses.append(status)

    request._headers = response
    request.do_POST()
    assert request.responses == [403]
    assert request.rfile.reads == [] and request.rfile.drains == [len(body)]
    assert request.connection.timeout is None


def test_bad_json_body_is_read_once_and_never_drained_a_second_time():
    request = handler(b'{invalid-json')
    request.do_POST()
    assert request.responses == [400]
    assert request.rfile.reads == [len(b'{invalid-json')]
    assert request.rfile.drains == []
    assert request.connection.timeouts == []


def test_control_limit_stays_2048_while_small_rejected_body_can_be_drained():
    request = handler(b"x" * 2049)
    request.do_POST()
    assert request.responses == [400]
    assert request.rfile.reads == [] and request.rfile.drains == [2049]


@pytest.mark.parametrize("headers", [{"Content-Length": "999999999"},
                                     {"Content-Length": "-1"},
                                     {"Content-Length": "invalid"},
                                     {"Transfer-Encoding": "chunked"}])
def test_rejection_never_waits_for_large_or_ambiguously_framed_input(headers):
    request = handler(b"not-sent", headers={"Origin": "https://untrusted.example", **headers})
    request.do_POST()
    assert request.responses == [403]
    assert request.rfile.reads == request.rfile.drains == []
    assert request.connection.timeouts == []


def test_slow_trickle_drain_has_one_total_deadline_not_one_timeout_per_chunk(monkeypatch):
    import linptech_ble.dashboard as module
    request = handler(b"three bytes", headers={"Origin": "https://untrusted.example"})
    moments = iter((10, 10.1, 10.3))
    monkeypatch.setattr(module.time, "monotonic", lambda: next(moments))
    reads = []

    def read_one(size):
        reads.append(size)
        return b"x"

    request.rfile.read1 = read_one
    request.do_POST()
    assert request.responses == [403] and reads == [len(b"three bytes")]
    assert request.connection.timeouts[0] == pytest.approx(.15)
    assert request.connection.timeout is None


def test_drain_timeout_still_replies_and_restores_original_socket_timeout():
    request = handler(b"not-sent", headers={"Origin": "https://untrusted.example"})
    request.connection.timeout = 5

    def timeout(size):
        raise TimeoutError()

    request.rfile.read1 = timeout
    request.do_POST()
    assert request.responses == [403] and request.connection.timeout == 5
