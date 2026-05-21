"""Testes da rotina de disparo do alarme."""

from app.alarm_dispatch_runner import resolve_camera_ids_for_partitions
from app.alerts_catalog import AlertsCatalog
from app.cameras_catalog import CamerasCatalog


def test_resolve_camera_ids_for_partitions_by_entity_group():
    yaml_text = """
cameras:
  - id: Cozinha
    name: Cozinha
    group: Interna, alarm_control_panel.amt_8000_partition_4
  - id: Rua
    name: Rua
    group: alarm_control_panel.amt_8000_partition_1
  - id: Hall
    name: Hall
    group: Portao Social
"""
    cameras = CamerasCatalog.from_yaml_string(yaml_text)
    ids = resolve_camera_ids_for_partitions(
        cameras,
        [
            "alarm_control_panel.amt_8000_partition_1",
            "alarm_control_panel.amt_8000_partition_4",
        ],
    )
    assert ids == ["Rua", "Cozinha"]


def test_parse_alarm_dispatch_from_alerts_yaml():
    yaml_text = """
alarm_dispatch:
  enabled: true
  debounce_seconds: 3
  cooldown: 10m
  describe_cameras: true
  notify:
    phones: ["5511999999999"]
alerts: []
"""
    catalog = AlertsCatalog.from_yaml_string(yaml_text)
    cfg = catalog.alarm_dispatch
    assert cfg.enabled is True
    assert cfg.debounce_seconds == 3.0
    assert cfg.cooldown_seconds == 600
    assert cfg.describe_cameras is True
    assert cfg.notify.phones == ["5511999999999"]


def test_alerts_runner_includes_partitions_when_alarm_dispatch_attached():
    from unittest.mock import Mock

    from app.alerts_runner import AlertsRunner

    catalog = AlertsCatalog.from_yaml_string(
        "alarm_dispatch:\n  enabled: true\nalerts: []\n"
    )
    runner = AlertsRunner(settings=Mock(), ha=Mock(), evo=Mock(), catalog=catalog)
    alarm = Mock()
    alarm.config.enabled = True
    alarm.partition_entity_ids = {"alarm_control_panel.amt_8000_partition_1"}
    runner.attach_alarm_dispatch(alarm)
    assert "alarm_control_panel.amt_8000_partition_1" in runner._live_entity_ids()


def test_collect_triggered_uses_pending_when_ha_reverted():
    from unittest.mock import AsyncMock, Mock

    from app.alerts_catalog import AlarmDispatchConfig
    from app.alarm_dispatch_runner import AlarmDispatchRunner

    runner = AlarmDispatchRunner(
        settings=Mock(),
        ha=Mock(),
        evo=Mock(),
        cameras=CamerasCatalog(),
        config=AlarmDispatchConfig(enabled=True),
    )
    runner._pending_partitions.add("alarm_control_panel.amt_8000_partition_1")
    runner._fetch_triggered_partitions = AsyncMock(return_value=[])  # type: ignore[method-assign]

    import asyncio

    collected = asyncio.run(runner._collect_triggered_partitions())
    assert len(collected) == 1
    assert collected[0][0] == "alarm_control_panel.amt_8000_partition_1"


def test_validate_alarm_dispatch_structure():
    data = {
        "alarm_dispatch": {"enabled": "yes"},
        "alerts": [],
    }
    errors = AlertsCatalog.validate_structure(data)
    assert any("alarm_dispatch.enabled" in e for e in errors)
