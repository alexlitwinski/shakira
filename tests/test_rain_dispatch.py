"""Testes da rotina de chuva."""

from unittest.mock import AsyncMock, Mock

from app.alerts_catalog import AlertsCatalog
from app.alerts_runner import AlertsRunner
from app.devices_catalog import DevicesCatalog
from app.rain_dispatch_runner import (
    RainDispatchRunner,
    RainStartStatus,
    build_rain_started_message,
    cover_is_closed,
    cover_is_open,
    is_raining,
    parse_rain_volume,
    rain_started_transition,
    window_is_open,
)


def test_parse_rain_volume():
    assert parse_rain_volume("3.5") == 3.5
    assert parse_rain_volume("0") == 0.0
    assert parse_rain_volume("unavailable") == 0.0


def test_cover_states():
    assert is_raining("on")
    assert cover_is_open("open")
    assert cover_is_closed("closed")
    assert window_is_open("open")
    assert not window_is_open("closed")


def test_rain_started_transition_uses_websocket_old_new():
    assert rain_started_transition(
        rain_entity="binary_sensor.rain",
        entity_id="binary_sensor.rain",
        old_state="off",
        new_state="on",
        prev_rain_on=True,
        rain_on=True,
        source="live",
        bootstrapped=True,
    )


def test_rain_started_transition_poll_uses_prev():
    assert rain_started_transition(
        rain_entity="binary_sensor.rain",
        entity_id=None,
        old_state=None,
        new_state=None,
        prev_rain_on=False,
        rain_on=True,
        source="poll",
        bootstrapped=True,
    )


def test_build_rain_started_message_all_ok():
    msg = build_rain_started_message(
        RainStartStatus(
            open_windows=[],
            porta_vidro_open=False,
            toldo_closed=False,
        )
    )
    assert "Começou a chover" in msg
    assert "Situação:" in msg
    assert "nenhuma aberta" in msg
    assert "aberto (recolhido)" in msg
    assert "fechada" in msg


def test_build_rain_started_message_with_issues():
    msg = build_rain_started_message(
        RainStartStatus(
            open_windows=["Janela Jantar 1"],
            porta_vidro_open=True,
            toldo_closed=True,
        )
    )
    assert "Janela Jantar 1" in msg
    assert "Feche as janelas" in msg
    assert "fechado" in msg
    assert "aberta" in msg


def test_parse_rain_dispatch_from_alerts_yaml():
    yaml_text = """
rain_dispatch:
  enabled: true
  heavy_rain_mm: 5
  cooldown: 10m
  notify:
    phones: ["5511999999999"]
  entities:
    rain: binary_sensor.sensor_chuva_e_lux_rain
alerts: []
"""
    catalog = AlertsCatalog.from_yaml_string(yaml_text)
    cfg = catalog.rain_dispatch
    assert cfg.enabled is True
    assert cfg.heavy_rain_mm == 5.0
    assert cfg.cooldown_seconds == 600
    assert cfg.rain_entity == "binary_sensor.sensor_chuva_e_lux_rain"


def test_devices_entity_kinds():
    yaml_text = """
devices:
  - name: Chuva
    entities:
      - entity_id: sensor.amt_8000_zone_10
        description: Porta sala
        opening_kind: door
        sensor_kind: contact
      - entity_id: binary_sensor.sensor_chuva_e_lux_rain
        sensor_kind: rain
"""
    catalog = DevicesCatalog.from_yaml_string(yaml_text)
    ent = catalog.get_entity("sensor.amt_8000_zone_10")
    assert ent is not None
    assert ent.opening_kind == "door"
    assert ent.sensor_kind == "contact"


def test_alerts_runner_subscribes_rain_entities():
    catalog = AlertsCatalog.from_yaml_string(
        "rain_dispatch:\n  enabled: true\nalerts: []\n"
    )
    runner = AlertsRunner(settings=Mock(), ha=Mock(), evo=Mock(), catalog=catalog)
    rain = RainDispatchRunner(settings=Mock(), ha=Mock(), evo=Mock(), config=catalog.rain_dispatch)
    runner.attach_rain_dispatch(rain)
    assert "binary_sensor.sensor_chuva_e_lux_rain" in runner._live_entity_ids()


def test_rain_started_notification():
    import asyncio

    from app.alerts_catalog import AlertNotifyConfig, RainDispatchConfig

    config = RainDispatchConfig(
        enabled=True,
        notify=AlertNotifyConfig(phones=["5511999999999"]),
    )
    runner = RainDispatchRunner(
        settings=Mock(),
        ha=AsyncMock(),
        evo=Mock(),
        config=config,
    )

    async def _get_state(entity_id: str) -> dict[str, str]:
        return {
            "state": {
                config.rain_entity: "on",
                config.volume_entity: "0",
                config.porta_vidro_entity: "closed",
                config.toldo_entity: "closed",
            }[entity_id]
        }

    runner.ha.get_state = _get_state  # type: ignore[method-assign]
    runner._bootstrapped = True
    runner._last_rain_on = False
    runner._last_volume_mm = 0.0
    runner._notify = AsyncMock()  # type: ignore[method-assign]

    asyncio.run(
        runner._evaluate_all(
            source="live",
            entity_id=runner.config.rain_entity,
            old_state="off",
            new_state="on",
        )
    )
    runner._notify.assert_called()
    first_msg = runner._notify.call_args_list[0][0][1]
    assert "Começou a chover" in first_msg


def test_rain_started_lists_open_windows():
    import asyncio

    from app.alerts_catalog import AlertNotifyConfig, RainDispatchConfig

    devices_yaml = """
devices:
  - name: Alarme
    entities:
      - entity_id: sensor.amt_8000_zone_2
        description: Janela Jantar 1
        opening_kind: window
      - entity_id: sensor.amt_8000_zone_3
        description: Janela Jantar 2
        opening_kind: window
"""
    config = RainDispatchConfig(
        enabled=True,
        notify=AlertNotifyConfig(phones=["5511999999999"]),
    )
    runner = RainDispatchRunner(
        settings=Mock(),
        ha=AsyncMock(),
        evo=Mock(),
        config=config,
        devices=DevicesCatalog.from_yaml_string(devices_yaml),
    )

    async def _get_state(entity_id: str) -> dict[str, str]:
        states = {
            config.rain_entity: "on",
            config.volume_entity: "0",
            config.porta_vidro_entity: "closed",
            config.toldo_entity: "closed",
            "sensor.amt_8000_zone_2": "open",
            "sensor.amt_8000_zone_3": "closed",
        }
        return {"state": states.get(entity_id, "closed")}

    runner.ha.get_state = _get_state  # type: ignore[method-assign]
    runner._bootstrapped = True
    runner._last_rain_on = False
    runner._last_volume_mm = 0.0
    runner._notify = AsyncMock()  # type: ignore[method-assign]

    asyncio.run(
        runner._evaluate_all(
            source="live",
            entity_id=config.rain_entity,
            old_state="off",
            new_state="on",
        )
    )
    first_msg = runner._notify.call_args_list[0][0][1]
    assert "Situação:" in first_msg
    assert "Janela Jantar 1" in first_msg
    assert "Janela Jantar 2" not in first_msg


def test_rain_started_after_bootstrap_race():
    """Bootstrap lia on antes da comparacao; live off->on deve avisar mesmo com prev_rain True."""
    import asyncio

    from app.alerts_catalog import AlertNotifyConfig, RainDispatchConfig

    config = RainDispatchConfig(
        enabled=True,
        notify=AlertNotifyConfig(phones=["5511999999999"]),
    )
    runner = RainDispatchRunner(
        settings=Mock(),
        ha=AsyncMock(),
        evo=Mock(),
        config=config,
    )

    async def _get_state(entity_id: str) -> dict[str, str]:
        return {
            "state": {
                config.rain_entity: "on",
                config.volume_entity: "0",
                config.porta_vidro_entity: "closed",
                config.toldo_entity: "open",
            }[entity_id]
        }

    runner.ha.get_state = _get_state  # type: ignore[method-assign]
    runner._bootstrapped = False
    runner._notify = AsyncMock()  # type: ignore[method-assign]

    asyncio.run(
        runner._evaluate_all(
            source="live",
            entity_id=config.rain_entity,
            old_state="off",
            new_state="on",
        )
    )
    runner._notify.assert_called()
    assert "Começou a chover" in runner._notify.call_args_list[0][0][1]
