"""Unit tests for the 5-minute delayed stopped cameras alert (cameras_paradas) in AlertsRunner."""

import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from app.alerts_catalog import AlertsCatalog
from app.alerts_runner import AlertsRunner


class TestAlertsRunnerCamerasParadas(unittest.TestCase):
    def setUp(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def tearDown(self) -> None:
        self.loop.close()

    @patch("app.alerts_runner.resolve_notify_phones", new_callable=AsyncMock)
    @patch("app.alerts_runner.send_whatsapp_text", new_callable=AsyncMock)
    @patch("asyncio.sleep", new_callable=AsyncMock)
    def test_cameras_paradas_still_stopped_after_5_minutes(self, mock_sleep, mock_send_wa, mock_resolve_phones) -> None:
        # Mocks
        mock_resolve_phones.return_value = ["5531991119016"]
        
        yaml_text = """
alerts:
  - id: cameras_paradas
    enabled: true
    check_interval: live
    entity_id: binary_sensor.status_cameras_paradas
    when_state: "on"
    message: "Atenção: existem câmeras com problema."
    cooldown: 2h
"""
        catalog = AlertsCatalog.from_yaml_string(yaml_text)
        runner = AlertsRunner(settings=Mock(), ha=Mock(), evo=Mock(), catalog=catalog)

        # Mock HA client to return "on" (stopped) when checked after 5 minutes
        runner.ha.get_state = AsyncMock(return_value={"state": "on"})

        # Trigger state change to "on"
        self.loop.run_until_complete(
            runner.handle_live_state_change(
                entity_id="binary_sensor.status_cameras_paradas",
                old_state="off",
                new_state="on",
                _event_data={}
            )
        )

        # Assertions
        mock_sleep.assert_called_once_with(300.0)
        runner.ha.get_state.assert_called_once_with("binary_sensor.status_cameras_paradas")
        mock_send_wa.assert_called_once_with(
            settings=runner.settings,
            evo=runner.evo,
            number="5531991119016",
            message="Atenção: existem câmeras com problema."
        )

    @patch("app.alerts_runner.resolve_notify_phones", new_callable=AsyncMock)
    @patch("app.alerts_runner.send_whatsapp_text", new_callable=AsyncMock)
    @patch("asyncio.sleep", new_callable=AsyncMock)
    def test_cameras_paradas_recovered_before_5_minutes(self, mock_sleep, mock_send_wa, mock_resolve_phones) -> None:
        # Mocks
        mock_resolve_phones.return_value = ["5531991119016"]
        
        yaml_text = """
alerts:
  - id: cameras_paradas
    enabled: true
    check_interval: live
    entity_id: binary_sensor.status_cameras_paradas
    when_state: "on"
    message: "Atenção: existem câmeras com problema."
    cooldown: 2h
"""
        catalog = AlertsCatalog.from_yaml_string(yaml_text)
        runner = AlertsRunner(settings=Mock(), ha=Mock(), evo=Mock(), catalog=catalog)

        # Mock HA client to return "off" (recovered) when checked after 5 minutes
        runner.ha.get_state = AsyncMock(return_value={"state": "off"})

        # Trigger state change to "on"
        self.loop.run_until_complete(
            runner.handle_live_state_change(
                entity_id="binary_sensor.status_cameras_paradas",
                old_state="off",
                new_state="on",
                _event_data={}
            )
        )

        # Assertions
        mock_sleep.assert_called_once_with(300.0)
        runner.ha.get_state.assert_called_once_with("binary_sensor.status_cameras_paradas")
        mock_send_wa.assert_not_called()
