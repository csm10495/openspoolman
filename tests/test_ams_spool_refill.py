"""Tests for AMS auto-refill (spool backup) detection.

Covers https://github.com/drndos/openspoolman/issues/90 — when the printer's
auto-refill feature swaps a depleted spool for a backup spool mid-print,
OpenSpoolMan must remap the affected logical filament index so subsequent
per-layer consumption is credited to the new tray's spool.
"""

import copy
from unittest.mock import patch

import pytest

import mqtt_bambulab
from filament_usage_tracker import FilamentUsageTracker


def _make_tracker(ams_mapping):
  tracker = FilamentUsageTracker()
  tracker.using_ams = True
  tracker.ams_mapping = list(ams_mapping)
  # Prime caches so we can verify they get invalidated for the refilled index.
  tracker._filament_spool_id_map = {idx: 100 + idx for idx in range(len(ams_mapping))}
  return tracker


# ---------------------------------------------------------------------------
# FilamentUsageTracker.handle_ams_spool_refill
# ---------------------------------------------------------------------------


def test_refill_remaps_filament_index_when_new_tray_outside_planned_mapping():
  tracker = _make_tracker([0, 1])

  with patch.object(tracker, "apply_ams_mapping") as apply_mock:
    changed = tracker.handle_ams_spool_refill(previous_tray=0, new_tray=2)

  assert changed is True
  apply_mock.assert_called_once_with([2, 1])
  # Cached spool binding for the refilled filament must be dropped so the
  # next consumption resolves the new tray's spool from SpoolMan.
  assert 0 not in tracker._filament_spool_id_map


def test_refill_ignored_when_new_tray_already_in_planned_mapping():
  """A planned filament change in a multi-color print is not a refill."""
  tracker = _make_tracker([0, 1, 2])

  with patch.object(tracker, "apply_ams_mapping") as apply_mock:
    changed = tracker.handle_ams_spool_refill(previous_tray=0, new_tray=1)

  assert changed is False
  apply_mock.assert_not_called()


def test_refill_ignored_when_previous_tray_unknown():
  tracker = _make_tracker([0, 1])

  with patch.object(tracker, "apply_ams_mapping") as apply_mock:
    changed = tracker.handle_ams_spool_refill(previous_tray=3, new_tray=2)

  assert changed is False
  apply_mock.assert_not_called()


def test_refill_ignored_when_not_using_ams():
  tracker = FilamentUsageTracker()
  tracker.using_ams = False
  tracker.ams_mapping = None

  with patch.object(tracker, "apply_ams_mapping") as apply_mock:
    assert tracker.handle_ams_spool_refill(0, 2) is False
  apply_mock.assert_not_called()


def test_refill_ignored_when_previous_equals_new():
  tracker = _make_tracker([0, 1])

  with patch.object(tracker, "apply_ams_mapping") as apply_mock:
    assert tracker.handle_ams_spool_refill(0, 0) is False
  apply_mock.assert_not_called()


# ---------------------------------------------------------------------------
# mqtt_bambulab._detect_ams_spool_refill
# ---------------------------------------------------------------------------


def _state(tray_now, gcode_state="RUNNING"):
  return {"print": {"gcode_state": gcode_state, "ams": {"tray_now": tray_now}}}


@pytest.fixture(autouse=True)
def _enable_layer_tracking(monkeypatch):
  monkeypatch.setattr(mqtt_bambulab, "TRACK_LAYER_USAGE", True)


def test_detect_refill_invokes_tracker_on_tray_now_change_during_print(monkeypatch):
  calls = []

  def fake_handler(prev, new):
    calls.append((prev, new))
    return True

  monkeypatch.setattr(mqtt_bambulab.FILAMENT_TRACKER, "handle_ams_spool_refill", fake_handler)

  mqtt_bambulab._detect_ams_spool_refill(_state("2"), _state("0"))

  assert calls == [(0, 2)]


def test_detect_refill_skips_sentinel_trays(monkeypatch):
  calls = []
  monkeypatch.setattr(
      mqtt_bambulab.FILAMENT_TRACKER,
      "handle_ams_spool_refill",
      lambda prev, new: calls.append((prev, new)) or True,
  )

  # Initial load (255 -> real tray) must not trigger refill detection.
  mqtt_bambulab._detect_ams_spool_refill(_state("0"), _state("255"))
  # Transitional 254 must also be ignored.
  mqtt_bambulab._detect_ams_spool_refill(_state("0"), _state("254"))
  # Unload (real tray -> 255) must also be ignored.
  mqtt_bambulab._detect_ams_spool_refill(_state("255"), _state("0"))

  assert calls == []


def test_detect_refill_skips_when_not_running(monkeypatch):
  calls = []
  monkeypatch.setattr(
      mqtt_bambulab.FILAMENT_TRACKER,
      "handle_ams_spool_refill",
      lambda prev, new: calls.append((prev, new)) or True,
  )

  mqtt_bambulab._detect_ams_spool_refill(
      _state("2", gcode_state="IDLE"),
      _state("0", gcode_state="IDLE"),
  )

  assert calls == []


def test_detect_refill_no_op_when_layer_tracking_disabled(monkeypatch):
  monkeypatch.setattr(mqtt_bambulab, "TRACK_LAYER_USAGE", False)
  called = []
  monkeypatch.setattr(
      mqtt_bambulab.FILAMENT_TRACKER,
      "handle_ams_spool_refill",
      lambda *a, **kw: called.append(a),
  )

  mqtt_bambulab._detect_ams_spool_refill(_state("2"), _state("0"))

  assert called == []


def test_detect_refill_swallows_handler_errors(monkeypatch):
  def boom(*_a, **_kw):
    raise RuntimeError("nope")

  monkeypatch.setattr(mqtt_bambulab.FILAMENT_TRACKER, "handle_ams_spool_refill", boom)

  # Must not raise — MQTT processing must remain robust against tracker errors.
  mqtt_bambulab._detect_ams_spool_refill(_state("2"), _state("0"))


# ---------------------------------------------------------------------------
# End-to-end through processMessage
# ---------------------------------------------------------------------------


def test_process_message_triggers_refill_on_tray_now_change(monkeypatch):
  monkeypatch.setattr(mqtt_bambulab, "TRACK_LAYER_USAGE", True)
  mqtt_bambulab.PRINTER_STATE = {
      "print": {"gcode_state": "RUNNING", "ams": {"tray_now": "0"}}
  }
  mqtt_bambulab.PRINTER_STATE_LAST = copy.deepcopy(mqtt_bambulab.PRINTER_STATE)
  mqtt_bambulab.PENDING_PRINT_METADATA = {}

  calls = []
  monkeypatch.setattr(
      mqtt_bambulab.FILAMENT_TRACKER,
      "handle_ams_spool_refill",
      lambda prev, new: calls.append((prev, new)) or True,
  )

  # Note: PRINTER_STATE_LAST is updated at the end of processMessage to a deep
  # copy of PRINTER_STATE, so we synthesize a refill event by sending a
  # message that flips tray_now to a new value while gcode_state stays RUNNING.
  mqtt_bambulab.processMessage({
      "print": {"gcode_state": "RUNNING", "ams": {"tray_now": "2"}}
  })

  assert calls == [(0, 2)]
