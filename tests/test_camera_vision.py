"""Testes para descricao de cameras via Gemini Vision."""

import json

from app.camera_vision import (
    CameraMosaicAnalysis,
    CameraPanelInfo,
    CameraPresence,
    build_camera_mosaic_prompt,
    build_retry_notice,
    camera_names_match,
    format_analysis_message,
    grid_position_label,
    should_retry_for_missing_person,
    watched_panel_names,
)
from app.camera_vision import _parse_analysis_payload


def test_grid_position_label_five_cameras():
    assert grid_position_label(0, 5) == "superior esquerda"
    assert grid_position_label(1, 5) == "superior central"
    assert grid_position_label(2, 5) == "superior direita"
    assert grid_position_label(3, 5) == "inferior esquerda"
    assert grid_position_label(4, 5) == "inferior central"


def test_camera_names_match_rua_aliases():
    assert camera_names_match("Rua", "Rua 1")
    assert camera_names_match("Rua", "Rua")
    assert not camera_names_match("Rua 2", "Rua 1")
    assert camera_names_match("Porta de vidro", "Porta de vidro")


def test_should_retry_when_watch_cameras_have_no_person():
    panels = [
        CameraPanelInfo(name="Porta de vidro"),
        CameraPanelInfo(name="Rua"),
        CameraPanelInfo(name="Hall"),
    ]
    analysis = CameraMosaicAnalysis(
        cameras=[
            CameraPresence(name="Porta de vidro", person_detected=False),
            CameraPresence(name="Rua", person_detected=False),
            CameraPresence(name="Hall", person_detected=True),
        ],
        description="...",
    )
    assert should_retry_for_missing_person(analysis, panels, ["Porta de vidro", "Rua"])


def test_should_not_retry_when_person_on_watch_camera():
    panels = [
        CameraPanelInfo(name="Porta de vidro"),
        CameraPanelInfo(name="Rua"),
    ]
    analysis = CameraMosaicAnalysis(
        cameras=[
            CameraPresence(name="Porta de vidro", person_detected=True),
            CameraPresence(name="Rua", person_detected=False),
        ],
        description="...",
    )
    assert not should_retry_for_missing_person(analysis, panels, ["Porta de vidro", "Rua"])


def test_watched_panel_names_resolves_aliases():
    panels = [
        CameraPanelInfo(name="Porta de vidro"),
        CameraPanelInfo(name="Rua"),
        CameraPanelInfo(name="Hall"),
    ]
    names = watched_panel_names(panels, ["Porta de vidro", "Rua 1"])
    assert names == ["Porta de vidro", "Rua"]


def test_build_retry_notice():
    msg = build_retry_notice(["Porta de vidro", "Rua"])
    assert "Porta de vidro" in msg
    assert "Rua" in msg
    assert "novas imagens" in msg


def test_parse_analysis_payload():
    raw = json.dumps(
        {
            "cameras": [
                {"name": "Porta de vidro", "person_detected": False, "notes": "vazio"},
            ],
            "description": "Nada na porta.",
            "recommendation": "Aguarde.",
        }
    )
    analysis = _parse_analysis_payload(raw)
    assert analysis is not None
    assert analysis.cameras[0].person_detected is False
    assert format_analysis_message(analysis) == "Aguarde.\n\nNada na porta."


def test_build_camera_mosaic_prompt_uses_camera_names():
    panels = [
        CameraPanelInfo(name="Porta de vidro", description="Porta de vidro — acesso e movimento"),
        CameraPanelInfo(name="Lateral 1", description="Lateral da casa (angulo 1)"),
        CameraPanelInfo(name="Hall", description="Hall de entrada"),
        CameraPanelInfo(name="Rua", description="Frente da casa para a rua"),
        CameraPanelInfo(name="Rua 2", description="Frente da casa (segundo angulo)"),
    ]
    prompt = build_camera_mosaic_prompt(
        camera_panels=panels,
        context="Atencao: o interfone esta tocando!",
    )
    assert "interfone esta tocando" in prompt
    assert "MAPEAMENTO DO MOSAICO" in prompt
    assert "Painel superior esquerda → **Porta de vidro**" in prompt
    assert "person_detected" in prompt


def test_build_camera_mosaic_prompt_without_panels():
    prompt = build_camera_mosaic_prompt(camera_panels=[], context="")
    assert "Analise a imagem" in prompt
    assert "MAPEAMENTO DO MOSAICO" not in prompt


def test_describe_camera_mosaic_compat(monkeypatch):
    sample = CameraMosaicAnalysis(
        description="Na Porta de vidro ha um entregador.",
        recommendation="Pode atender.",
    )

    def fake_analyze(**kwargs):
        return sample

    monkeypatch.setattr("app.camera_vision.analyze_camera_mosaic", fake_analyze)
    from app.camera_vision import describe_camera_mosaic

    text = describe_camera_mosaic(
        api_key="test-key",
        image_bytes=b"fake",
        camera_panels=[CameraPanelInfo(name="Porta de vidro")],
    )
    assert "entregador" in text
    assert "Pode atender." in text
