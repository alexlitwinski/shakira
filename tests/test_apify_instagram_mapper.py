"""Testes do mapper Apify Instagram."""

from __future__ import annotations

from app.apify_client import map_instagram_profile_row


def test_map_full_row():
    row = {
        "username": "chefxyz",
        "fullName": "Chef XYZ",
        "biography": "Sushi em Lisboa",
        "followersCount": 12000,
        "verified": True,
        "profilePicUrlHD": "https://cdn.example.com/pic.jpg",
    }
    m = map_instagram_profile_row(row)
    assert m["handle"] == "chefxyz"
    assert m["profile_name"] == "Chef XYZ"
    assert m["profile_bio"] == "Sushi em Lisboa"
    assert m["followers"] == 12000
    assert m["is_verified"] is True
    assert "pic.jpg" in m["avatar_url"]
