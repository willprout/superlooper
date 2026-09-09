"""Issue #481 (d) — a client that walks away mid-response is a NORMAL event, not a stack trace.

Of the ~3,700 real lines the operator rescued from the 331 MB log on 2026-09-09, **282 were
``BrokenPipeError`` tracebacks from ``lib/server.py``'s ``_write``** (evidence record #477 item 1).
Every one of them is the same non-event: the front-end polls ``/api/snapshot`` every 2 seconds, and
a browser tab that is closed, reloaded, or backgrounded mid-poll drops the socket before the
response body is written. ``socketserver`` answers that with ``handle_error`` — forty dashes and a
full traceback per occurrence — which is how a routine disconnect became the second-largest
contributor to an unreadable log.

The dashboard must say it in ONE concise line, and say it in a shape the logbook's collapser can
count (a stable sentence, so 282 of them become a handful of counted records rather than 282
paragraphs). A real fault must still get its traceback — this is not a blanket silencer.
"""
import json
import re
import socket
import sys
import threading
import time

import pytest

import server


def _provider():
    return {"repos": [], "generated_at": 0}


def _handler_instance(**kw):
    """A ``_Handler`` without its socket: ``BaseHTTPRequestHandler.__init__`` *serves the request*,
    so the only way to unit-test one method is to build the instance and wire the few attributes
    that method touches. Everything real about the write path — the status/header calls and the
    body write — is then observable."""
    cls = server.make_handler(_provider, ".", **kw)
    h = cls.__new__(cls)
    h.command = "GET"
    h.path = "/api/snapshot"
    h.requestline = "GET /api/snapshot HTTP/1.1"
    h.client_address = ("127.0.0.1", 54321)
    h.request_version = "HTTP/1.1"
    h.sent = []
    h.send_response = lambda code, *a: h.sent.append(("status", code))
    h.send_header = lambda k, v: h.sent.append(("header", k, v))
    h.end_headers = lambda: h.sent.append(("end",))
    return h


class _DeadPipe:
    """A ``wfile`` whose peer has gone: every write raises, exactly as the socket does."""

    def __init__(self, exc):
        self.exc = exc

    def write(self, data):
        raise self.exc

    def flush(self):
        raise self.exc


# =============================== the write path ===============================

@pytest.mark.parametrize("exc", [BrokenPipeError(32, "Broken pipe"),
                                 ConnectionResetError(54, "Connection reset by peer"),
                                 ConnectionAbortedError(53, "Software caused connection abort")])
def test_a_disconnect_mid_body_write_logs_one_concise_line_not_a_traceback(exc, capsys):
    h = _handler_instance()
    h.wfile = _DeadPipe(exc)
    h._write(server._resp(200, "application/json", b'{"ok": true}'))
    err = capsys.readouterr().err
    assert "Traceback" not in err, err
    assert err.count("\n") == 1, "one line, not a paragraph: %r" % err
    assert "client disconnected" in err
    assert "/api/snapshot" in err, err


def test_the_disconnect_line_is_stable_so_the_logbook_can_collapse_it(capsys):
    # 282 of these in one log. The collapser keys on the sentence with numbers scrubbed, so two
    # disconnects on the same route must produce the SAME fingerprint — otherwise they stay 282
    # separate lines and nothing has been fixed.
    import logbook
    lines = []
    for port in (54321, 61002):
        h = _handler_instance()
        h.client_address = ("127.0.0.1", port)
        h.wfile = _DeadPipe(BrokenPipeError(32, "Broken pipe"))
        h._write(server._resp(200, "application/json", b"{}"))
        lines.append(capsys.readouterr().err.strip())
    assert logbook.fingerprint(lines[0]) == logbook.fingerprint(lines[1]), lines


def test_a_disconnect_before_the_headers_are_flushed_is_the_same_one_line(capsys):
    # send_response/end_headers write to wfile too, so the break can land on the header half. Same
    # non-event, same one line — never a traceback out of _write.
    h = _handler_instance()

    def boom(*a):
        raise BrokenPipeError(32, "Broken pipe")

    h.end_headers = boom
    h.wfile = _DeadPipe(BrokenPipeError(32, "Broken pipe"))
    h._write(server._resp(200, "application/json", b"{}"))
    err = capsys.readouterr().err
    assert "client disconnected" in err and "Traceback" not in err


def test_a_real_write_fault_is_not_swallowed(capsys):
    # This must not become a blanket except: a genuine bug in the write path (or a full disk) still
    # has to surface. Only the connection-gone family is the routine event.
    h = _handler_instance()
    h.wfile = _DeadPipe(OSError(28, "No space left on device"))
    with pytest.raises(OSError):
        h._write(server._resp(200, "application/json", b"{}"))


def test_a_normal_response_is_completely_unchanged(capsys):
    # No change to what the dashboard serves (definition of done, last box).
    h = _handler_instance()
    written = []
    h.wfile = type("W", (), {"write": lambda _s, d: written.append(d),
                             "flush": lambda _s: None})()
    body = json.dumps({"ok": True}).encode()
    h._write(server._resp(200, "application/json", body, {"X-Thing": "1"}))
    assert written == [body]
    assert ("status", 200) in h.sent
    assert ("header", "Content-Length", str(len(body))) in h.sent
    assert ("header", "X-Thing", "1") in h.sent
    assert capsys.readouterr().err == ""


def test_a_head_response_still_writes_no_body(capsys):
    h = _handler_instance()
    h.command = "HEAD"
    written = []
    h.wfile = type("W", (), {"write": lambda _s, d: written.append(d),
                             "flush": lambda _s: None})()
    h._write(server._resp(200, "application/json", b'{"ok": true}'))
    assert written == []


# =============================== the server's own error hook ===============================

def test_handle_error_answers_a_disconnect_with_one_line(capsys):
    # socketserver's default handle_error prints "-"*40 plus a traceback. Anything that escapes the
    # handler with a gone-peer error must get the same one-line treatment as _write's own catch —
    # that is the belt to _write's braces, since rfile reads break the same way.
    srv = server.build_server(_provider, ".", port=0)
    try:
        try:
            raise ConnectionResetError(54, "Connection reset by peer")
        except ConnectionResetError:
            srv.handle_error(None, ("127.0.0.1", 54321))
        err = capsys.readouterr().err
        assert "Traceback" not in err and "-" * 40 not in err, err
        assert err.count("\n") == 1 and "client disconnected" in err, repr(err)
    finally:
        srv.server_close()


def test_handle_error_still_reports_a_real_fault_in_full(capsys):
    srv = server.build_server(_provider, ".", port=0)
    try:
        try:
            raise ValueError("a real bug")
        except ValueError:
            srv.handle_error(None, ("127.0.0.1", 54321))
        err = capsys.readouterr().err
        assert "Traceback" in err and "a real bug" in err
    finally:
        srv.server_close()


# =============================== end to end over a real socket ===============================

def test_a_real_client_that_hangs_up_mid_response_leaves_no_traceback(capsys):
    # The actual 2026-09-09 shape, over a real loopback socket: ask for a big response, then walk
    # away without reading it. The server must come out of that with at most one concise line and
    # keep serving.
    big = {"repos": [{"slug": "will-titan/agent-360-eapp", "pad": "x" * 4000}
                     for _ in range(400)], "generated_at": 0}
    srv = server.build_server(lambda: big, ".", port=0)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    t.start()
    try:
        for _ in range(6):
            s = socket.create_connection(("127.0.0.1", port), timeout=5)
            s.sendall(b"GET /api/snapshot HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
            s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                         __import__("struct").pack("ii", 1, 0))   # RST, not a polite FIN
            s.close()
        time.sleep(0.4)
        # The server is still alive and serving after the abandoned requests.
        ok = socket.create_connection(("127.0.0.1", port), timeout=5)
        ok.sendall(b"GET /api/snapshot HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        assert b"200" in ok.recv(64)
        ok.close()
    finally:
        srv.shutdown()
        t.join(timeout=5)
        srv.server_close()
    err = capsys.readouterr().err
    assert "Traceback" not in err, err
    for line in [l for l in err.splitlines() if l.strip()]:
        assert "client disconnected" in line, line
