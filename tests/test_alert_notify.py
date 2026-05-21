"""Testes de resolucao de telefones para alertas."""

from __future__ import annotations

import pytest

from app.alert_notify import resolve_notify_phones


class _FakeHa:
    pass


@pytest.mark.asyncio
async def test_merge_default_and_rule_phones() -> None:
    phones = await resolve_notify_phones(
        _FakeHa(),
        phones=["553198946418"],
        default_phones=["5531991119016", "553198946418"],
    )
    assert phones == ["553198946418", "5531991119016"]


@pytest.mark.asyncio
async def test_default_only() -> None:
    phones = await resolve_notify_phones(
        _FakeHa(),
        phones=[],
        default_phones=["5531991119016", "553198946418"],
    )
    assert phones == ["553198946418", "5531991119016"]
