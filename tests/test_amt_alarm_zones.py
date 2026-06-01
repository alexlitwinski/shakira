"""Testes de zonas AMT (setores) vs particoes."""

from app.amt_alarm_zones import (
    build_trigger_message,
    zone_entities_from_catalog,
    zone_state_is_triggered,
)
from app.devices_catalog import DevicesCatalog


def test_build_trigger_message_partitions_and_zones():
    msg = build_trigger_message(
        [
            (
                "alarm_control_panel.amt_8000_partition_1",
                "Partição 1 — perímetro externo",
            ),
        ],
        [
            ("sensor.amt_8000_zone_7", "Portão de Serviço", "open"),
            ("sensor.amt_8000_zone_10", "Porta sala estar", "open"),
        ],
    )
    assert "ALERTA: alarme disparou!" in msg
    assert "Partições em disparo:" not in msg
    assert "Partição 1 — perímetro externo" not in msg
    assert "Setores (zonas) em disparo:" in msg
    assert "Portão de Serviço" in msg
    assert "Porta sala estar" in msg
    assert "Setores com disparo:" not in msg


def test_build_trigger_message_no_zones_hint():
    msg = build_trigger_message(
        [("alarm_control_panel.amt_8000_partition_1", "Partição 1 — perímetro externo")],
        [],
    )
    assert "Setores (zonas):" in msg
    assert "nenhum sensor de zona em disparo" in msg


def test_zone_state_is_triggered():
    assert zone_state_is_triggered("open")
    assert zone_state_is_triggered("on")
    assert not zone_state_is_triggered("closed")
    assert not zone_state_is_triggered("off")
    assert not zone_state_is_triggered("unavailable")


def test_zone_entities_from_catalog_skips_meta_zone():
    yaml_text = """
devices:
  - name: Alarme
    entities:
      - entity_id: sensor.amt_8000_zone_7
        description: Portão de Serviço
      - entity_id: sensor.amt_8000_zone_61
        description: Disparo da central AMT8000
"""
    catalog = DevicesCatalog.from_yaml_string(yaml_text)
    zones = zone_entities_from_catalog(catalog)
    assert zones == [("sensor.amt_8000_zone_7", "Portão de Serviço")]
