"""Unit tests for user friendly humanization of Double Take sensor states in user_friendly.py."""

import unittest
from app.devices_catalog import DevicesCatalog
from app.user_friendly import format_state_value


class TestUserFriendlyDoubleTake(unittest.TestCase):
    def test_format_double_take_valid_match_complete(self) -> None:
        state = {
            "state": "porta_vidro",
            "attributes": {
                "friendly_name": "Márcia",
                "camera": "porta_vidro",
                "timestamp": "2026-06-01T14:05:00.000Z",
                "confidence": 85
            }
        }
        res = format_state_value(
            entity_id="sensor.double_take_marcia_2",
            state=state,
            catalog=None
        )
        self.assertEqual(
            res,
            "Márcia foi visto(a) por último na câmera Porta Vidro às 14:05 com 85% de confiança."
        )

    def test_format_double_take_missing_time_and_confidence(self) -> None:
        state = {
            "state": "cozinha",
            "attributes": {
                "friendly_name": "Márcia"
            }
        }
        res = format_state_value(
            entity_id="sensor.double_take_marcia_2",
            state=state,
            catalog=None
        )
        self.assertEqual(
            res,
            "Márcia foi visto(a) por último na câmera Cozinha."
        )

    def test_format_double_take_unknown_state(self) -> None:
        state = {
            "state": "unknown",
            "attributes": {
                "friendly_name": "Márcia"
            }
        }
        res = format_state_value(
            entity_id="sensor.double_take_marcia_2",
            state=state,
            catalog=None
        )
        self.assertEqual(
            res,
            "Não tenho registros de onde Márcia foi visto(a) por último."
        )
