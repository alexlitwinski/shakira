"""Testes de descricao automatica de cameras em alertas de alarme."""

from app.alerts_catalog import AlertConfig, AlertNotifyConfig
from app.alerts_runner import should_describe_alert_cameras


def test_should_describe_alarm_trigger_without_explicit_flag():
    alert = AlertConfig(
        id="alarme_particao_1_disparo",
        entity_id="alarm_control_panel.amt_8000_partition_1",
        when_state="triggered",
        message="ALERTA",
        camera_group="Portao Social",
        describe_cameras=False,
    )
    assert should_describe_alert_cameras(alert) is True


def test_should_not_describe_non_alarm_alert():
    alert = AlertConfig(
        id="memoria_alta",
        entity_id="sensor.memory_use_percent",
        when_state=">=85",
        message="Memoria alta",
        describe_cameras=False,
    )
    assert should_describe_alert_cameras(alert) is False


def test_should_describe_when_flag_explicit():
    alert = AlertConfig(
        id="interfone",
        entity_id="input_boolean.interfone_tocando",
        when_state="on",
        message="Interfone",
        describe_cameras=True,
    )
    assert should_describe_alert_cameras(alert) is True
