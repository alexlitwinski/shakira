"""Testes de envio de snapshots de cameras."""

from app.camera_snapshots import build_vision_context, parse_camera_snapshot_targets
from app.cameras_catalog import CamerasCatalog


def test_build_vision_context_combines_intro_and_labels():
    from app.camera_snapshots import CameraSnapshotsResult

    result = CameraSnapshotsResult(image_labels=["Cozinha", "Sala"])
    ctx = build_vision_context(intro="Vou mostrar as cameras internas.", result=result)
    assert "Vou mostrar as cameras internas." in ctx
    assert "Cozinha" in ctx
    assert "Sala" in ctx


def test_parse_camera_snapshot_targets_by_group():
    yaml_text = """
cameras:
  - id: Cozinha
    name: Cozinha
    group: Interna
"""
    cameras = CamerasCatalog.from_yaml_string(yaml_text)
    ids, err = parse_camera_snapshot_targets(
        {"camera_group": "Interna", "action": "get_camera_snapshot"},
        cameras,
    )
    assert err is None
    assert ids == ["Cozinha"]
