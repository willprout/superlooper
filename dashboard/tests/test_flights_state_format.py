"""Issue #45 — the state-format handshake, dashboard end.

The command-center reads a superlooper state home field-by-field, and every reader fails CLOSED to
empty (Task 2). So a future change to the on-disk SHAPE would silently BLANK the field rather than
error — the most likely future "why is my dashboard empty" with no diagnostic. The engine now stamps
the format version it wrote (``state/state_format.json``); ``flights.state_format_status`` turns the
raw stamp fact into the honest verdict the field binds:

  * NO stamp (an old, pre-handshake home) ⇒ GRANDFATHERED — renders normally, never blanked;
  * a version this dashboard KNOWS ⇒ compatible, silent;
  * a version it does NOT know (or a present-but-unreadable stamp) ⇒ an honest, NAMED mismatch.

The message is built here (server-side, design record B.1) because it names the versions — the JS
only binds the finished string.
"""
import flights


def test_absent_stamp_is_grandfathered_compatible():
    # None = no stamp file on disk (a state home written by a pre-handshake runner). It renders
    # normally: the missing stamp must never itself blank the field.
    st = flights.state_format_status(None)
    assert st["compatible"] is True
    assert st["present"] is False
    assert st["version"] is None
    assert st["message"] is None


def test_known_version_is_compatible_and_silent():
    st = flights.state_format_status({"version": 1})
    assert st["compatible"] is True
    assert st["present"] is True
    assert st["version"] == 1
    assert st["message"] is None


def test_current_engine_version_is_known():
    # The version the engine stamps today (see the engine's STATE_FORMAT_VERSION) must be in this
    # dashboard's supported set — otherwise a healthy pairing would false-alarm a mismatch.
    assert 1 in flights.KNOWN_STATE_FORMATS
    assert flights.state_format_status({"version": 1})["compatible"] is True


def test_unknown_newer_version_is_named_mismatch():
    st = flights.state_format_status({"version": 2})
    assert st["compatible"] is False
    assert st["present"] is True
    assert st["version"] == 2
    # The line NAMES both sides of the mismatch — the honest diagnostic that replaces a blank field.
    assert "v2" in st["message"]
    assert "v1" in st["message"]


def test_present_but_unreadable_stamp_is_a_mismatch_not_a_blank():
    # {} is the reader's fail-closed value for a present-but-corrupt stamp. We can't confirm the
    # shape, so we must NOT render as if all-clear — that is exactly the silent-blank failure. It
    # surfaces as an honest mismatch naming the unreadable stamp.
    st = flights.state_format_status({})
    assert st["compatible"] is False
    assert st["present"] is True
    assert st["version"] is None
    assert "unreadable" in st["message"].lower()


def test_typed_version_is_treated_as_unreadable():
    # A stamp whose "version" isn't an int (a string, a bool — bool is an int subclass and must be
    # rejected) can't be compared, so it's an unreadable mismatch, never accidentally compatible.
    for bad in ("1", True, None, 1.0, [1]):
        st = flights.state_format_status({"version": bad})
        assert st["compatible"] is False, bad
        assert st["version"] is None, bad


def test_unknown_but_valid_int_version_names_that_number():
    # A real integer the dashboard just doesn't know (0, 99) is named as-is — a precise diagnostic.
    st = flights.state_format_status({"version": 99})
    assert st["compatible"] is False and st["version"] == 99
    assert "v99" in st["message"]


def test_supported_list_is_reported_for_the_message():
    st = flights.state_format_status({"version": 2})
    assert st["supported"] == sorted(flights.KNOWN_STATE_FORMATS)
