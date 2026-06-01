"""Unit tests for sensor.double_take_porta_vidro face recognition alerts in AlertsRunner using standard unittest."""

import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from app.alerts_catalog import AlertsCatalog
from app.alerts_runner import AlertsRunner


class TestAlertsRunnerDoubleTake(unittest.TestCase):
    def setUp(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def tearDown(self) -> None:
        self.loop.close()

    @patch("app.alerts_runner.resolve_notify_phones", new_callable=AsyncMock)
    @patch("app.alerts_runner.send_whatsapp_text", new_callable=AsyncMock)
    def test_double_take_valid_single_match(self, mock_send_wa, mock_resolve_phones) -> None:
        mock_resolve_phones.return_value = ["5531991119016"]
        catalog = AlertsCatalog.from_yaml_string("alerts: []")
        catalog.double_take_dispatch.enabled = True
        catalog.double_take_dispatch.min_confidence = 85
        runner = AlertsRunner(settings=Mock(), ha=Mock(), evo=Mock(), catalog=catalog)

        event_data = {
            "new_state": {
                "attributes": {
                    "id": "event-12345",
                    "matches": [
                        {"name": "alexandre", "confidence": 85, "match": True}
                    ]
                }
            }
        }

        self.loop.run_until_complete(
            runner.handle_live_state_change(
                entity_id="sensor.double_take_porta_vidro",
                old_state="unknown",
                new_state="alexandre",
                _event_data=event_data
            )
        )

        mock_send_wa.assert_called_once_with(
            settings=runner.settings,
            evo=runner.evo,
            number="5531991119016",
            message="🔔 *Reconhecimento:* *Alexandre* (85%) detectado(a) no Portão de Vidro!"
        )

    @patch("app.alerts_runner.resolve_notify_phones", new_callable=AsyncMock)
    @patch("app.alerts_runner.send_whatsapp_text", new_callable=AsyncMock)
    def test_double_take_valid_multiple_matches(self, mock_send_wa, mock_resolve_phones) -> None:
        mock_resolve_phones.return_value = ["5531991119016"]
        catalog = AlertsCatalog.from_yaml_string("alerts: []")
        catalog.double_take_dispatch.enabled = True
        catalog.double_take_dispatch.min_confidence = 85
        runner = AlertsRunner(settings=Mock(), ha=Mock(), evo=Mock(), catalog=catalog)

        # One match below 85% (hanna at 78%) and one match above 85% (alexandre at 92%)
        event_data = {
            "new_state": {
                "attributes": {
                    "id": "event-67890",
                    "matches": [
                        {"name": "hanna", "confidence": 78, "match": True},
                        {"name": "alexandre", "confidence": 92, "match": True}
                    ]
                }
            }
        }

        self.loop.run_until_complete(
            runner.handle_live_state_change(
                entity_id="sensor.double_take_porta_vidro",
                old_state="unknown",
                new_state="hanna",
                _event_data=event_data
            )
        )

        # Hanna should be excluded from the message since 78% < 85%, so only Alexandre is notified
        mock_send_wa.assert_called_once_with(
            settings=runner.settings,
            evo=runner.evo,
            number="5531991119016",
            message="🔔 *Reconhecimento:* *Alexandre* (92%) detectado(a) no Portão de Vidro!"
        )

    @patch("app.alerts_runner.resolve_notify_phones", new_callable=AsyncMock)
    @patch("app.alerts_runner.send_whatsapp_text", new_callable=AsyncMock)
    def test_double_take_ignores_low_confidence(self, mock_send_wa, mock_resolve_phones) -> None:
        mock_resolve_phones.return_value = ["5531991119016"]
        catalog = AlertsCatalog.from_yaml_string("alerts: []")
        catalog.double_take_dispatch.enabled = True
        catalog.double_take_dispatch.min_confidence = 85
        runner = AlertsRunner(settings=Mock(), ha=Mock(), evo=Mock(), catalog=catalog)

        # Match with 84% confidence (which is below the 85% threshold)
        event_data = {
            "new_state": {
                "attributes": {
                    "id": "event-low-conf",
                    "matches": [
                        {"name": "alexandre", "confidence": 84, "match": True}
                    ]
                }
            }
        }

        self.loop.run_until_complete(
            runner.handle_live_state_change(
                entity_id="sensor.double_take_porta_vidro",
                old_state="unknown",
                new_state="alexandre",
                _event_data=event_data
            )
        )

        mock_send_wa.assert_not_called()

    @patch("app.alerts_runner.resolve_notify_phones", new_callable=AsyncMock)
    @patch("app.alerts_runner.send_whatsapp_text", new_callable=AsyncMock)
    def test_double_take_ignores_unknowns(self, mock_send_wa, mock_resolve_phones) -> None:
        mock_resolve_phones.return_value = ["5531991119016"]
        catalog = AlertsCatalog.from_yaml_string("alerts: []")
        catalog.double_take_dispatch.enabled = True
        catalog.double_take_dispatch.min_confidence = 85
        runner = AlertsRunner(settings=Mock(), ha=Mock(), evo=Mock(), catalog=catalog)

        event_data = {
            "new_state": {
                "attributes": {
                    "id": "event-unknown",
                    "matches": [
                        {"name": "unknown", "confidence": 95, "match": False}
                    ]
                }
            }
        }

        self.loop.run_until_complete(
            runner.handle_live_state_change(
                entity_id="sensor.double_take_porta_vidro",
                old_state="unknown",
                new_state="unknown",
                _event_data=event_data
            )
        )

        mock_send_wa.assert_not_called()

    @patch("app.alerts_runner.resolve_notify_phones", new_callable=AsyncMock)
    @patch("app.alerts_runner.send_whatsapp_text", new_callable=AsyncMock)
    def test_double_take_deduplication(self, mock_send_wa, mock_resolve_phones) -> None:
        mock_resolve_phones.return_value = ["5531991119016"]
        catalog = AlertsCatalog.from_yaml_string("alerts: []")
        catalog.double_take_dispatch.enabled = True
        catalog.double_take_dispatch.min_confidence = 85
        runner = AlertsRunner(settings=Mock(), ha=Mock(), evo=Mock(), catalog=catalog)

        event_data = {
            "new_state": {
                "attributes": {
                    "id": "event-dup",
                    "matches": [
                        {"name": "alexandre", "confidence": 88, "match": True}
                    ]
                }
            }
        }

        # First trigger
        self.loop.run_until_complete(
            runner.handle_live_state_change(
                entity_id="sensor.double_take_porta_vidro",
                old_state="unknown",
                new_state="alexandre",
                _event_data=event_data
            )
        )

        # Second trigger with the same event ID
        self.loop.run_until_complete(
            runner.handle_live_state_change(
                entity_id="sensor.double_take_porta_vidro",
                old_state="alexandre",
                new_state="alexandre",
                _event_data=event_data
            )
        )

        # Should only be called once
        mock_send_wa.assert_called_once()
