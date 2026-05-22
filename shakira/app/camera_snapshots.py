"""Envio de snapshots Frigate para WhatsApp (uma ou varias cameras)."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.cameras_catalog import CamerasCatalog
from app.config import AppSettings
from app.evolution import EvolutionClient
from app.frigate import FrigateClient, FrigateError
from app.camera_vision import (
    CameraPanelInfo,
    analyze_camera_mosaic,
    format_analysis_message,
)
from app.image_collage import build_image_grid
from app.whatsapp_steps import StepMessenger, pulse_whatsapp_typing, truncate_whatsapp

log = logging.getLogger(__name__)


@dataclass
class CameraSnapshotsResult:
    sent: int = 0
    failed: list[str] = field(default_factory=list)
    summary: str = ""
    image_bytes: bytes | None = None
    image_labels: list[str] = field(default_factory=list)
    image_panels: list[CameraPanelInfo] = field(default_factory=list)


def parse_camera_snapshot_targets(
    decision: dict[str, Any],
    cameras: CamerasCatalog,
) -> tuple[list[str], str | None]:
    """Extrai filtros do JSON do Gemini e resolve ids de camera."""
    all_cameras = decision.get("all_cameras") is True

    raw_ids = decision.get("camera_ids")
    camera_ids: list[str] | None = None
    if isinstance(raw_ids, list):
        camera_ids = [str(x) for x in raw_ids if str(x).strip()]
    elif isinstance(raw_ids, str) and raw_ids.strip():
        camera_ids = [s.strip() for s in raw_ids.split(",") if s.strip()]

    camera_group = decision.get("camera_group")
    group_str = str(camera_group).strip() if isinstance(camera_group, str) else None

    raw_id = decision.get("camera_id")
    camera_id = str(raw_id).strip() if raw_id is not None and str(raw_id).strip() else None

    return cameras.resolve_camera_targets(
        camera_id=camera_id,
        camera_ids=camera_ids,
        camera_group=group_str,
        all_cameras=all_cameras,
    )


def build_vision_context(*, intro: str = "", result: CameraSnapshotsResult) -> str:
    parts: list[str] = []
    if intro.strip():
        parts.append(intro.strip())
    if result.image_labels:
        parts.append("Câmeras: " + ", ".join(result.image_labels))
    return "\n".join(parts).strip()


async def send_camera_vision_description(
    *,
    settings: AppSettings,
    evo: EvolutionClient,
    phone: str,
    instance: str,
    result: CameraSnapshotsResult,
    context: str = "",
    messenger: StepMessenger | None = None,
) -> bool:
    """Analisa imagem enviada (uma ou mosaico) com Gemini Vision e manda texto ao usuario."""
    if not result.image_bytes or result.sent <= 0:
        return False

    api_key = settings.gemini_api_key.strip()
    if not api_key:
        log.warning("Descricao de cameras: gemini_api_key ausente")
        return False

    model = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
    try:
        analysis = await asyncio.to_thread(
            analyze_camera_mosaic,
            api_key=api_key,
            image_bytes=result.image_bytes,
            camera_panels=result.image_panels,
            context=context,
            model=model,
        )
    except Exception:
        log.exception("Descricao de cameras: falha Gemini Vision phone=%s", phone)
        return False

    if analysis is None:
        log.warning("Descricao de cameras: Gemini retornou vazio phone=%s", phone)
        return False

    description = format_analysis_message(analysis)
    if not description:
        return False

    evo_base = settings.evolution_base_url.strip()
    evo_key = settings.evolution_api_key.strip()
    msg = truncate_whatsapp(description)
    if not msg:
        return False

    if messenger:
        await messenger.step(msg, final=True)
    elif evo_base and evo_key and instance:
        await pulse_whatsapp_typing()
        await evo.send_text(
            base_url=evo_base,
            api_key=evo_key,
            instance=instance,
            number=phone,
            text=msg,
        )
    else:
        return False

    log.info("Descricao de cameras enviada phone=%s chars=%s", phone, len(msg))
    return True


async def send_camera_snapshots(
    *,
    settings: AppSettings,
    cameras: CamerasCatalog,
    evo: EvolutionClient,
    http: httpx.AsyncClient,
    phone: str,
    instance: str,
    camera_ids: list[str],
    intro: str = "",
    area_label: str = "",
    messenger: StepMessenger | None = None,
    send_progress: bool = True,
    send_summary: bool = True,
    quiet_send_failure: bool = False,
) -> CameraSnapshotsResult:
    """
    Obtem snapshots do Frigate e envia pelo WhatsApp.
    Reutilizavel por codigo ou pelo agente (via handle_get_camera_snapshot).
    """
    result = CameraSnapshotsResult()
    evo_base = settings.evolution_base_url.strip()
    evo_key = settings.evolution_api_key.strip()

    async def say(text: str, *, final: bool = False) -> None:
        msg = truncate_whatsapp(text)
        if not msg:
            return
        if messenger:
            await messenger.step(msg, final=final)
        elif evo_base and evo_key and instance:
            await pulse_whatsapp_typing()
            await evo.send_text(
                base_url=evo_base,
                api_key=evo_key,
                instance=instance,
                number=phone,
                text=msg,
            )

    if not evo_base or not evo_key or not instance:
        result.summary = "Evolution nao configurado."
        log.error("Envio de cameras bloqueado: Evolution nao configurado")
        return result

    if not settings.frigate_url:
        result.summary = "Frigate nao configurado."
        if intro:
            await say(intro)
        await say("Frigate não configurado. Defina frigate_url nas opções do add-on.", final=True)
        return result

    if not cameras.cameras:
        result.summary = "Nenhuma camera configurada."
        if intro:
            await say(intro)
        await say("Nenhuma câmera configurada. Crie /config/shakira_cameras.yaml.", final=True)
        return result

    if not camera_ids:
        known = ", ".join(f"{c.id} ({c.name})" for c in cameras.cameras[:8])
        groups = ", ".join(cameras.list_groups())
        hint = known
        if groups:
            hint += f". Grupos: {groups}"
        result.summary = "Nenhuma camera selecionada."
        if intro:
            await say(intro)
        await say(f"Não identifiquei quais câmeras enviar. Disponíveis: {hint}.", final=True)
        return result

    if intro and send_progress:
        await say(intro)

    total = len(camera_ids)
    if send_progress:
        if total == 1:
            cam = cameras.camera_map().get(camera_ids[0])
            label = cam.name if cam else camera_ids[0]
            await say(f"Vou buscar a imagem da câmera {label}...")
        else:
            await say(f"Vou buscar imagens de {total} câmeras e enviar numa única mensagem...")

    frigate = FrigateClient(http, base_url=settings.frigate_url)
    cam_map = cameras.camera_map()

    fetched: list[tuple[bytes, str, str, str]] = []
    for index, camera_id in enumerate(camera_ids, start=1):
        cam = cam_map.get(camera_id)
        label = cam.name if cam else camera_id
        description = cam.description if cam else ""
        log.info(
            "Frigate snapshot camera=%s (%s/%s) url=%s",
            camera_id,
            index,
            total,
            settings.frigate_url,
        )

        try:
            image_bytes = await frigate.get_latest_snapshot(camera_id)
        except FrigateError as e:
            log.error("Frigate falhou camera=%s: %s", camera_id, e, exc_info=True)
            result.failed.append(camera_id)
            continue

        fetched.append((image_bytes, label, camera_id, description))

    if not fetched:
        if total == 1:
            failed = result.failed[0] if result.failed else camera_ids[0]
            await say(f"Não consegui obter a imagem da câmera {failed}.", final=True)
        else:
            await say("Não consegui obter imagens das câmeras solicitadas.", final=True)
        result.summary = "Não foi possível enviar as imagens das câmeras."
        return result

    await pulse_whatsapp_typing()

    if len(fetched) == 1:
        image_bytes, label, camera_id, description = fetched[0]
        caption = f"Câmera: {label}"[:1024]
        fname = f"shakira_{camera_id}.jpg"
        ok = await evo.send_image_bytes(
            base_url=evo_base,
            api_key=evo_key,
            instance=instance,
            number=phone,
            image_bytes=image_bytes,
            filename=fname,
            caption=caption,
        )
        if ok is None:
            result.failed.append(camera_id)
            await say(f"Capturei {label} mas não consegui enviar pelo WhatsApp.", final=True)
        else:
            result.sent = 1
            result.summary = "1 imagem enviada."
            result.image_bytes = image_bytes
            result.image_labels = [label]
            result.image_panels = [CameraPanelInfo(name=label, description=description)]
        return result

    collage_items = [(img_bytes, label) for img_bytes, label, _cid, _desc in fetched]
    try:
        collage_bytes = build_image_grid(collage_items)
    except Exception:
        log.exception("Falha ao montar collage de %s cameras", len(collage_items))
        await say("Não consegui montar as imagens numa única mensagem.", final=True)
        result.summary = "Falha ao montar collage das cameras."
        return result

    labels = [label for _, label, _, _ in fetched]
    panels = [
        CameraPanelInfo(name=label, description=description)
        for _, label, _, description in fetched
    ]
    prefix = area_label.strip() or "Câmeras"
    caption = f"{prefix}: " + ", ".join(labels)
    if result.failed:
        caption += f" (falharam: {', '.join(result.failed)})"
    caption = caption[:1024]

    ok = await evo.send_image_bytes(
        base_url=evo_base,
        api_key=evo_key,
        instance=instance,
        number=phone,
        image_bytes=collage_bytes,
        filename="shakira_cameras.jpg",
        caption=caption,
    )
    result.image_bytes = collage_bytes
    result.image_labels = labels
    result.image_panels = panels
    if ok is None:
        result.failed.extend(camera_id for _, _, camera_id, _ in fetched)
        if not quiet_send_failure:
            await say("Capturei as câmeras mas não consegui enviar pelo WhatsApp.", final=True)
        result.summary = "Não foi possível enviar as imagens das câmeras."
        return result

    result.sent = len(fetched)
    if result.failed:
        result.summary = (
            f"{result.sent} de {total} imagem(ns) no collage."
            f" Falharam: {', '.join(result.failed)}."
        )
    else:
        result.summary = f"{result.sent} imagem(ns) enviada(s) numa única mensagem."

    if send_summary:
        await say(result.summary, final=True)
    return result


async def handle_camera_snapshot_decision(
    decision: dict[str, Any],
    *,
    settings: AppSettings,
    cameras: CamerasCatalog,
    evo: EvolutionClient,
    http: httpx.AsyncClient,
    phone: str,
    instance: str,
    user_text: str = "",
    messenger: StepMessenger | None = None,
) -> CameraSnapshotsResult:
    """Interpreta decisao Gemini e envia snapshots."""
    intro = str(decision.get("response") or "").strip()
    camera_ids, error = parse_camera_snapshot_targets(decision, cameras)

    if error and not camera_ids:
        evo_base = settings.evolution_base_url.strip()
        evo_key = settings.evolution_api_key.strip()
        msg = (intro + "\n\n" + error).strip() if intro else error
        if messenger:
            await messenger.step(msg, final=True)
        elif evo_base and evo_key and instance:
            await evo.send_text(
                base_url=evo_base,
                api_key=evo_key,
                instance=instance,
                number=phone,
                text=truncate_whatsapp(msg),
            )
        return CameraSnapshotsResult(summary=error)

    # Detecção de busca de cão
    user_text_lower = user_text.lower().strip() if user_text else ""
    is_dog_search = False
    target_dog = None

    if "katio" in user_text_lower or "kátio" in user_text_lower:
        is_dog_search = True
        target_dog = "Kátio"
    elif "otavio" in user_text_lower or "otávio" in user_text_lower:
        is_dog_search = True
        target_dog = "Otávio"
    elif any(x in user_text_lower for x in ["cachorro", "cachorros", "cão", "cães"]):
        is_dog_search = True
        target_dog = "cachorro"

    # Interceptamos apenas se for busca por cão e o alvo for múltiplas câmeras (ex.: todas as câmeras)
    if is_dog_search and camera_ids and len(camera_ids) > 2:
        log.info("Iniciando busca inteligente do cao '%s' nas cameras phone=%s", target_dog, phone)
        evo_base = settings.evolution_base_url.strip()
        evo_key = settings.evolution_api_key.strip()

        async def say(text: str, *, final: bool = False) -> None:
            msg = truncate_whatsapp(text)
            if not msg:
                return
            if messenger:
                await messenger.step(msg, final=final)
            elif evo_base and evo_key and instance:
                await pulse_whatsapp_typing()
                await evo.send_text(
                    base_url=evo_base,
                    api_key=evo_key,
                    instance=instance,
                    number=phone,
                    text=msg,
                )

        if intro:
            await say(intro)
        await say(f"Vou buscar as imagens das câmeras para tentar localizar o {target_dog}...")

        if not settings.frigate_url:
            await say("Frigate não está configurado nas opções.", final=True)
            return CameraSnapshotsResult(summary="Frigate nao configurado.")

        frigate = FrigateClient(http, base_url=settings.frigate_url)
        cam_map = cameras.camera_map()

        # Busca todos os snapshots
        fetched: list[tuple[bytes, str, str, str]] = []
        failed = []
        for index, camera_id in enumerate(camera_ids, start=1):
            cam = cam_map.get(camera_id)
            label = cam.name if cam else camera_id
            description = cam.description if cam else ""
            try:
                image_bytes = await frigate.get_latest_snapshot(camera_id)
                fetched.append((image_bytes, label, camera_id, description))
            except Exception as e:
                log.error("Frigate falhou camera=%s: %s", camera_id, e)
                failed.append(camera_id)

        if not fetched:
            await say("Não consegui obter imagens das câmeras.", final=True)
            return CameraSnapshotsResult(summary="Nenhuma imagem obtida.")

        # Envia as câmeras em lotes de 6 para a IA analisar
        batch_size = 6
        found_cameras = []
        summaries = []

        for i in range(0, len(fetched), batch_size):
            chunk = fetched[i : i + batch_size]
            log.info(
                "Processando lote de câmeras para busca do cão: %s a %s de %s",
                i + 1,
                i + len(chunk),
                len(fetched),
            )

            # Constrói o mosaico em memória apenas para a visão do Gemini para este lote
            collage_items = [(img_bytes, label) for img_bytes, label, _cid, _desc in chunk]
            try:
                collage_bytes = build_image_grid(collage_items)
            except Exception:
                log.exception("Falha ao montar collage do lote em memória")
                continue

            panels = [
                CameraPanelInfo(name=label, description=description)
                for _, label, _, description in chunk
            ]
            vision_context = (
                f"Você está procurando ativamente por **{target_dog}** (ou qualquer pessoa) nas imagens das câmeras. "
                "Por favor, seja extremamente rigoroso e adote um comportamento cético. "
                "Só afirme que o cão está presente se você puder ver claramente o contorno nítido de um cachorro (Doberman preto para Kátio, Golden Retriever branco/creme para Otávio). "
                "Se a área estiver na penumbra, escura, com sombras, ou se você apenas suspeitar mas não puder confirmar com 100% de certeza absoluta, "
                "defina obrigatoriamente a respectiva flag correspondente como `false` (ex: katio_detected ou otavio_detected) e declare nas notas (notes) "
                "que não foi possível confirmar a presença dele por falta de clareza ou escuridão. "
                "É crucial evitar falsos positivos para não confundir o morador. NUNCA invente ou alucine a presença dele ou de pessoas."
            )
            api_key = settings.gemini_api_key.strip()
            model = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")

            try:
                analysis = await asyncio.to_thread(
                    analyze_camera_mosaic,
                    api_key=api_key,
                    image_bytes=collage_bytes,
                    camera_panels=panels,
                    context=vision_context,
                    model=model,
                )
            except Exception:
                log.exception("Descrição de câmeras falhou para lote")
                analysis = None

            if not analysis:
                continue

            if analysis.description:
                summaries.append(analysis.description)

            # Procura pelo cachorro nas notas deste lote
            if analysis.cameras:
                for cam_presence in analysis.cameras:
                    cam_name = cam_presence.name
                    notes = cam_presence.notes or ""
                    notes_lower = notes.lower()

                    is_match = False
                    if target_dog == "Kátio":
                        is_match = cam_presence.katio_detected
                    elif target_dog == "Otávio":
                        is_match = cam_presence.otavio_detected
                    else:  # "cachorro" ou plural
                        is_match = cam_presence.katio_detected or cam_presence.otavio_detected

                    # Fallback seguro de texto se o boolean falhar, excluindo negações
                    if not is_match:
                        negatives = ["não", "sem", "não permite", "impossível", "invisível", "escura", "escuro", "vazio", "vazia", "ausente", "ausência"]
                        has_negative = any(neg in notes_lower for neg in negatives)
                        if not has_negative:
                            if target_dog == "Kátio":
                                if "kátio" in notes_lower or "katio" in notes_lower:
                                    is_match = True
                            elif target_dog == "Otávio":
                                if "otávio" in notes_lower or "otavio" in notes_lower:
                                    is_match = True
                            else:
                                if any(x in notes_lower for x in ["kátio", "katio", "otávio", "otavio", "cachorro", "cão"]):
                                    is_match = True

                    if is_match:
                        for img_bytes, label, cid, desc in chunk:
                            if label.lower().strip() == cam_name.lower().strip() or cid.lower().strip() == cam_name.lower().strip():
                                found_cameras.append({
                                    "cam_name": label,
                                    "notes": notes,
                                    "image_bytes": img_bytes,
                                    "camera_id": cid
                                })
                                break

            # Se encontrou o cachorro neste lote, interrompe a busca nos próximos lotes (short-circuit)
            if found_cameras:
                log.info("Cão localizado no lote! Interrompendo busca subsequente.")
                break

        if found_cameras:
            # Cachorro localizado! Envia apenas a(s) câmera(s) correspondente(s)
            for item in found_cameras[:2]:
                caption = f"Câmera: {item['cam_name']}\n\nLocalizado: {item['notes']}"
                await evo.send_image_bytes(
                    base_url=evo_base,
                    api_key=evo_key,
                    instance=instance,
                    number=phone,
                    image_bytes=item["image_bytes"],
                    filename=f"shakira_search_{item['camera_id']}.jpg",
                    caption=caption[:1024],
                )

            return CameraSnapshotsResult(
                sent=len(found_cameras),
                summary=f"Cachorro localizado e enviado em {len(found_cameras)} imagem(ns)."
            )
        else:
            # Não localizado
            no_dog_msg = f"Não consegui localizar o {target_dog} em nenhuma das câmeras."
            if summaries:
                no_dog_msg += f"\n\nResumo das câmeras analisadas:\n" + "\n".join(summaries)

            await say(no_dog_msg, final=True)
            return CameraSnapshotsResult(
                sent=0,
                summary="Cachorro nao localizado nas cameras."
            )

    result = await send_camera_snapshots(
        settings=settings,
        cameras=cameras,
        evo=evo,
        http=http,
        phone=phone,
        instance=instance,
        camera_ids=camera_ids,
        intro=intro,
        messenger=messenger,
        send_summary=False,
    )

    if result.sent > 0 and result.image_bytes:
        vision_context = build_vision_context(intro=intro, result=result)
        await send_camera_vision_description(
            settings=settings,
            evo=evo,
            phone=phone,
            instance=instance,
            result=result,
            context=vision_context,
            messenger=messenger,
        )

    return result
