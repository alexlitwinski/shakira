"""Testes TTL e mudanca de assunto em fluxos pendentes."""

from __future__ import annotations

import time

from app.pending_flow_utils import (
    message_changes_conversation_topic,
    should_abandon_pending_flow,
)


def test_instagram_url_abandons_menu_pending():
    created = time.monotonic()
    url = "https://www.instagram.com/foo"
    assert message_changes_conversation_topic(url)
    assert should_abandon_pending_flow(created, url, pending_kind="menu")


def test_menu_choice_not_abandoned():
    created = time.monotonic()
    assert not should_abandon_pending_flow(created, "2", pending_kind="menu")


def test_expired_pending():
    old = time.monotonic() - 99999
    assert should_abandon_pending_flow(old, "1", pending_kind="menu", ttl_sec=1800)
