"""Testes do armazenamento de chamadas do interfone."""

from pathlib import Path

from app.interfone_call_store import InterfoneCallStore, configure_interfone_data_root


def test_create_finalize_and_list(tmp_path: Path) -> None:
    root = configure_interfone_data_root(tmp_path)
    store = InterfoneCallStore(root)

    record = store.create_call(
        camera_id="Porta_Vidro",
        image_bytes=b"fake-jpeg",
        gemini_summary="Entregador na porta.",
        gemini_description="Homem com colete e pacote.",
        attend_window_seconds=180,
    )
    assert record.id
    assert record.image_file.endswith(".jpg")
    assert (root / "images" / record.image_file).is_file()

    finalized = store.finalize_call(
        record.id,
        portao_social_opened=True,
        portao_servico_opened=False,
        hall_person_detected=False,
    )
    assert finalized is not None
    assert finalized.attended is True
    assert finalized.portao_social_opened is True
    assert "portão social" in finalized.attend_details()

    listed = store.list_calls(limit=5)
    assert len(listed) == 1
    assert listed[0].attended_label() == "Atendida"


def test_not_attended(tmp_path: Path) -> None:
    root = configure_interfone_data_root(tmp_path / "b")
    store = InterfoneCallStore(root)
    record = store.create_call(
        camera_id="Porta_Vidro",
        image_bytes=b"x",
        gemini_summary="Visitante.",
        gemini_description="",
        attend_window_seconds=120,
    )
    done = store.finalize_call(
        record.id,
        portao_social_opened=False,
        portao_servico_opened=False,
        hall_person_detected=False,
    )
    assert done is not None
    assert done.attended is False
    assert done.attended_label() == "Não atendida"
