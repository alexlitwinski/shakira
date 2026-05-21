"""Alertas nao disparam no primeiro ciclo apos arranque."""

import time
from unittest.mock import Mock

from app.alerts_catalog import AlertConfig, AlertsCatalog
from app.alerts_runner import AlertsRunner


def test_polling_skips_immediate_check_after_start():
    catalog = AlertsCatalog.from_yaml_string(
        """
alerts:
  - id: teste_cpu
    enabled: true
    check_interval: 5m
    entity_id: sensor.processor_use
    when_state: ">=90"
    message: "CPU alta"
    cooldown: 1h
"""
    )
    runner = AlertsRunner(settings=Mock(), ha=Mock(), evo=Mock(), catalog=catalog)
    runner.start()
    rt = runner._runtime("teste_cpu")
    assert rt.last_check_at > 0
    assert time.monotonic() - rt.last_check_at < 5
