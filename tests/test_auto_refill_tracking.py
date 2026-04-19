import json

import filament_usage_tracker
from filament_usage_tracker import FilamentUsageTracker
import spoolman_service


def test_auto_refill_updates_mapping_and_spool(monkeypatch):
  monkeypatch.setattr(filament_usage_tracker, "TRACK_LAYER_USAGE", True)
  tracker = FilamentUsageTracker()
  tracker.using_ams = True
  tracker.ams_mapping = [0]

  primary_uid = spoolman_service.trayUid(0, 0)
  secondary_uid = spoolman_service.trayUid(0, 1)

  spools = [
    {"id": 101, "filament": {"diameter": 1.75, "density": 1.24}, "extra": {"active_tray": json.dumps(primary_uid)}},
    {"id": 202, "filament": {"diameter": 1.75, "density": 1.24}, "extra": {"active_tray": json.dumps(secondary_uid)}},
  ]

  monkeypatch.setattr(filament_usage_tracker, "fetchSpools", lambda cached=False: spools)
  consume_calls = []
  monkeypatch.setattr("filament_usage_tracker.consumeSpool", lambda spool_id, use_length: consume_calls.append((spool_id, use_length)))
  monkeypatch.setattr("filament_usage_tracker.update_filament_spool", lambda *args, **kwargs: None)
  monkeypatch.setattr("filament_usage_tracker.update_filament_grams_used", lambda *args, **kwargs: None)

  assert tracker._apply_usage_for_filament(0, 100.0)
  assert consume_calls[-1][0] == 101

  tracker.on_message({"print": {"command": "push_status", "ams": {"tray_tar": "0", "tray_now": "1"}}})

  assert tracker.ams_mapping[0] == 1
  assert tracker._filament_spool_id_map.get(0) is None

  assert tracker._apply_usage_for_filament(0, 50.0)
  assert consume_calls[-1][0] == 202
