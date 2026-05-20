"""Testes do parser de URLs Instagram."""

from __future__ import annotations

import pytest

from app.instagram_links_parser import (
    InstagramParseError,
    canonical_profile_url,
    extract_instagram_urls,
    extract_note_without_urls,
    is_instagram_url,
    parse_instagram_url,
)


def test_profile_url():
    p = parse_instagram_url("https://www.instagram.com/chefxyz/")
    assert p.handle == "chefxyz"
    assert p.is_profile_url is True
    assert p.canonical_url == "https://www.instagram.com/chefxyz/"


def test_post_url_allowed():
    p = parse_instagram_url("https://instagram.com/p/ABC123xyz/")
    assert p.handle == ""
    assert p.is_profile_url is False


def test_extract_urls_and_note():
    text = "Olha https://www.instagram.com/foo/ restaurante italiano"
    urls = extract_instagram_urls(text)
    assert len(urls) == 1
    note = extract_note_without_urls(text, urls)
    assert "restaurante" in note


def test_not_instagram():
    with pytest.raises(InstagramParseError):
        parse_instagram_url("https://tiktok.com/@user")


def test_is_instagram_url():
    assert is_instagram_url("https://instagram.com/bar")
    assert not is_instagram_url("https://example.com")


def test_canonical_profile_url():
    assert canonical_profile_url("Chef.XYZ") == "https://www.instagram.com/chef.xyz/"
