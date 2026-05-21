"""Testes da rotina de detecao do interfone."""

from app.alerts_catalog import AlertsCatalog
from app.interfone_dispatch_runner import (
    gate_opened,
    interfone_ringing,
    person_detected,
)


def test_interfone_ringing_transition() -> None:
    assert interfone_ringing("off", "on")
    assert not interfone_ringing("on", "on")
    assert not interfone_ringing("on", "off")


def test_gate_and_person_transitions() -> None:
    assert gate_opened("closed", "open")
    assert not gate_opened("open", "open")
    assert person_detected("off", "on")
    assert person_detected("unavailable", "occupied")


def test_parse_interfone_dispatch_yaml() -> None:
    yaml_text = """
interfone_dispatch:
  enabled: true
  camera_id: Porta_Vidro
  attend_window: 3m
  entities:
    interfone: input_boolean.interfone_tocando
    portao_social: sensor.amt_8000_zone_1
"""
    catalog = AlertsCatalog.from_yaml_string(yaml_text)
    cfg = catalog.interfone_dispatch
    assert cfg.enabled is True
    assert cfg.camera_id == "Porta_Vidro"
    assert cfg.attend_window_seconds == 180
    assert cfg.interfone_entity == "input_boolean.interfone_tocando"
