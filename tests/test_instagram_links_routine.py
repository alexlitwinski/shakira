"""Testes de classificacao de respostas do fluxo Instagram."""

from __future__ import annotations

import re

_YES_RE = re.compile(r"^\s*(sim|s|quero|com\s+descri[cç][aã]o|1)\s*\.?\s*$", re.I)
_NO_RE = re.compile(r"^\s*(n[aã]o|nao|n|sem\s+descri[cç][aã]o|pular|2)\s*\.?\s*$", re.I)


def test_yes_no_patterns():
    assert _YES_RE.match("sim")
    assert _YES_RE.match("Sim")
    assert _NO_RE.match("nao")
    assert _NO_RE.match("não")
    assert not _YES_RE.match("nao")
