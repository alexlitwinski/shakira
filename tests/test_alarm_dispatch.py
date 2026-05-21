"""Testes da rotina de disparo do alarme."""

from app.alarm_dispatch_runner import (
    build_trigger_message,
    resolve_camera_ids_for_partitions,
)
from app.alerts_catalog import AlertsCatalog
from app.cameras_catalog import CamerasCatalog


def test_build_trigger_message_lists_sectors():
    msg = build_trigger_message(
        [
            "Partição 1 — perímetro externo",
            "Partição 4 — sensores internos",
        ]
    )
    assert "ALERTA: alarme disparou!" in msg
    assert "Setores com disparo:" in msg
    assert "Partição 1 — perímetro externo" in msg
    assert "Partição 4 — sensores internos" in msg


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


def test_validate_alarm_dispatch_structure():
    data = {
        "alarm_dispatch": {"enabled": "yes"},
        "alerts": [],
    }
    errors = AlertsCatalog.validate_structure(data)
    assert any("alarm_dispatch.enabled" in e for e in errors)
