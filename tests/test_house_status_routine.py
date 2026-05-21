"""Testes da rotina house_status."""

from app.devices_catalog import DevicesCatalog
from app.house_status_prompts import build_house_status_prompt, vision_analysis_to_facts
from app.house_status_routine import (
    build_problem_devices_block,
    build_sensor_context_block,
    collect_house_status_entity_ids,
    describe_entity_problem,
    humanize_zone_or_sensor_state,
)
from app.camera_vision import CameraMosaicAnalysis, CameraPresence
from app.alerts_catalog import RainDispatchConfig


DEVICES_YAML = """
devices:
  - name: Alarme da casa
    entities:
      - entity_id: alarm_control_panel.amt_8000_partition_1
        description: Partição 1 — perímetro externo
      - entity_id: sensor.amt_8000_zone_10
        description: Porta sala estar
        sensor_kind: contact
      - entity_id: binary_sensor.sensor_chuva_e_lux_rain
        description: Sensor de chuva
        sensor_kind: rain
      - entity_id: sensor.sensor_chuva_e_lux_rain_rate
        description: Volume chuva 15 min
"""


def _catalog() -> DevicesCatalog:
    return DevicesCatalog.from_yaml_string(DEVICES_YAML)


def test_collect_house_status_entity_ids():
    catalog = _catalog()
    rain = RainDispatchConfig(
        rain_entity="binary_sensor.sensor_chuva_e_lux_rain",
        volume_entity="sensor.sensor_chuva_e_lux_rain_rate",
    )
    ids = collect_house_status_entity_ids(catalog, rain)
    assert "alarm_control_panel.amt_8000_partition_1" in ids
    assert "sensor.amt_8000_zone_10" in ids
    assert "binary_sensor.sensor_chuva_e_lux_rain" in ids
    assert "sensor.sensor_chuva_e_lux_rain_rate" in ids


def test_humanize_partition_state():
    assert humanize_zone_or_sensor_state(
        "alarm_control_panel.amt_8000_partition_1", "disarmed"
    ) == "desarmado"
    assert humanize_zone_or_sensor_state(
        "alarm_control_panel.amt_8000_partition_1", "triggered"
    ) == "DISPARADO"


def test_build_sensor_context_block():
    catalog = _catalog()
    rain = RainDispatchConfig(
        rain_entity="binary_sensor.sensor_chuva_e_lux_rain",
        volume_entity="sensor.sensor_chuva_e_lux_rain_rate",
    )
    states = {
        "binary_sensor.sensor_chuva_e_lux_rain": {"state": "off", "attributes": {}},
        "sensor.sensor_chuva_e_lux_rain_rate": {"state": "0.2", "attributes": {"unit_of_measurement": "mm"}},
        "alarm_control_panel.amt_8000_partition_1": {"state": "disarmed", "attributes": {}},
        "sensor.amt_8000_zone_10": {"state": "closed", "attributes": {}},
    }
    block = build_sensor_context_block(
        catalog=catalog,
        states_by_id=states,
        rain_config=rain,
    )
    assert "Não está chovendo" in block
    assert "0.2 mm" in block
    assert "desarmado" in block
    assert "Porta sala estar" in block


def test_vision_analysis_to_facts():
    analysis = CameraMosaicAnalysis(
        cameras=[CameraPresence(name="Sala", person_detected=False, notes="vazia")],
        description="Sala: vazia.",
        recommendation="Tudo tranquilo.",
    )
    facts = vision_analysis_to_facts(analysis)
    assert facts["disponivel"] is True
    assert facts["cameras"][0]["nome"] == "Sala"
    prompt = build_house_status_prompt(vision_analysis=analysis, sensor_context="Chuva: seca.")
    assert "Sala" in prompt
    assert "Chuva: seca." in prompt


def test_describe_entity_problem():
    catalog = _catalog()
    ping_issue = describe_entity_problem(
        "binary_sensor.ping_roteador",
        {"state": "off", "attributes": {}},
        catalog=catalog,
    )
    assert ping_issue is not None
    assert "offline" in ping_issue
    assert (
        describe_entity_problem(
            "sensor.temperatura_boiler",
            {"state": "47.3", "attributes": {}},
            catalog=catalog,
        )
        is None
    )
    boiler_issue = describe_entity_problem(
        "sensor.temperatura_boiler",
        {"state": "unavailable", "attributes": {}},
        catalog=catalog,
    )
    assert boiler_issue is not None
    assert "indisponível" in boiler_issue


def test_build_problem_devices_block():
    catalog = _catalog()
    states = {
        "binary_sensor.ping_roteador": {"state": "off", "attributes": {}},
        "sensor.temperatura_boiler": {"state": "47.3", "attributes": {}},
    }
    block = build_problem_devices_block(catalog=catalog, states_by_id=states)
    assert block == ""
    states["sensor.temperatura_boiler"] = {"state": "unavailable", "attributes": {}}
    block = build_problem_devices_block(catalog=catalog, states_by_id=states)
    assert "indisponível" in block
