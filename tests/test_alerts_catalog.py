"""Testes do catalogo de alertas."""

from app.alerts_catalog import AlertsCatalog


def test_parse_alert_with_describe_cameras():
    yaml_text = """
alerts:
  - id: interfone_tocando
    entity_id: input_boolean.interfone_tocando
    when_state: "on"
    message: "Interfone tocando"
    camera_group: Portao Social
    describe_cameras: true
"""
    catalog = AlertsCatalog.from_yaml_string(yaml_text)
    assert len(catalog.alerts) == 1
    alert = catalog.alerts[0]
    assert alert.describe_cameras is True
    assert alert.camera_group == "Portao Social"


def test_validate_describe_cameras_requires_camera_group():
    data = {
        "alerts": [
            {
                "id": "bad_alert",
                "entity_id": "binary_sensor.test",
                "when_state": "on",
                "message": "Teste",
                "describe_cameras": True,
            }
        ]
    }
    errors = AlertsCatalog.validate_structure(data)
    assert any("describe_cameras" in e and "camera_group" in e for e in errors)


def test_parse_double_take_dispatch():
    yaml_text = """
double_take_dispatch:
  enabled: true
  min_confidence: 88
alerts: []
"""
    catalog = AlertsCatalog.from_yaml_string(yaml_text)
    assert catalog.double_take_dispatch.enabled is True
    assert catalog.double_take_dispatch.min_confidence == 88


def test_validate_double_take_dispatch_structure():
    data = {
        "double_take_dispatch": {"enabled": "yes", "min_confidence": "high"},
        "alerts": []
    }
    errors = AlertsCatalog.validate_structure(data)
    assert any("double_take_dispatch.enabled" in e for e in errors)
    assert any("double_take_dispatch.min_confidence" in e for e in errors)

