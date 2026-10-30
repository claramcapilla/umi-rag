from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from functools import partial
from typing import Any, Protocol

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Count, Q, QuerySet
from django.utils import timezone

from document_processor.cima import NREGISTRO, CimaClient
from document_processor.cima.docs import ficha_segmentada
from document_processor.cima.errors import CimaUnavailable
from documents.models import (
    TIPOS_QUE_AVISAN,
    CimaRegistration,
    CimaSyncEvent,
    CimaSyncRun,
    DocType,
    EstadoSincronizacion,
    MotivoRetirada,
    SourceDocument,
    Status,
    TipoEvento,
)
from library.models import Collection

from .cima_crawl import crawl_fichas, recorrer_paginas
from .cima_ingest import FALLOS_DEFINITIVOS, ingest_ficha, resincronizar, retirar_registro
from .errors import error_summary
from .tasks import retirar_documento_task

logger = logging.getLogger(__name__)

SOLAPE = timedelta(days=1)
ABANDONADA_TRAS = timedelta(hours=24)
_SENALES_DE_LO_NUESTRO = frozenset({"alta", "baja", "ft", "comerc", "estado", "desconocido"})
_SENALES_DE_LO_AJENO = frozenset({"alta", "ft", "comerc", "estado", "desconocido"})
_SENALES_DE_FICHA = frozenset({"ft", "desconocido"})
_RETIRABLES = (Status.JSON_READY, Status.INDEXING, Status.INDEXED, Status.FAILED)


class _ClienteCima(Protocol):
    def buscar(self, criterio: dict[str, Any]) -> dict[str, Any]: ...

    def medicamento(self, nregistro: str) -> dict[str, Any]: ...

    def cambios(self, desde: date, pagina: int) -> dict[str, Any]: ...


@dataclass(frozen=True)
class _Desenlace:
    tipo: str
    documento: SourceDocument | None = None
    detalle: str = ""


def sincronizar(
    *, desde: date | None = None, client: _ClienteCima | None = None
) -> CimaSyncRun | None:
    _cerrar_abandonadas()
    anterior = CimaSyncRun.objects.filter(
        estado__in=[EstadoSincronizacion.TERMINADA, EstadoSincronizacion.CON_ERRORES]
    ).first()
    carga_inicial = desde is None and anterior is None
    cursor = timezone.localdate(anterior.iniciada_en) - SOLAPE if anterior else None
    if desde is not None and cursor is not None and desde > cursor:
        raise ValueError(
            f"Leer desde {desde} dejaría sin leer los cambios desde {cursor}, el día anterior a "
            "la última sincronización terminada"
        )
    desde = desde or cursor

    try:
        with transaction.atomic():
            run = CimaSyncRun.objects.create(carga_inicial=carga_inicial, desde=desde)
    except IntegrityError:
        logger.warning("Ya hay una sincronización con CIMA en curso: no se lanza otra")
        return None

    try:
        _retirar_huerfanos()
        cliente = client or CimaClient.from_settings()
        if desde is None:
            _cargar_todo(run, cliente)
        else:
            _aplicar_cambios(run, cliente, desde)
    except Exception as exc:  # noqa: BLE001 - la ejecución queda registrada como fallida
        logger.exception("La sincronización con CIMA %s falló", run.pk)
        estado, error_msg = EstadoSincronizacion.FALLIDA, error_summary(exc)
    else:
        con_errores = run.eventos.filter(tipo=TipoEvento.ERROR).exists()
        estado = EstadoSincronizacion.CON_ERRORES if con_errores else EstadoSincronizacion.TERMINADA
        error_msg = ""
    cerrada = CimaSyncRun.objects.filter(pk=run.pk, estado=EstadoSincronizacion.EN_CURSO).update(
        estado=estado,
        error_msg=error_msg,
        terminada_en=timezone.now(),
        cambios_leidos=run.cambios_leidos,
        revisados=run.revisados,
    )
    if not cerrada:
        logger.warning("La sincronización con CIMA %s ya se había dado por abandonada", run.pk)
    run.refresh_from_db()
    return run


def documentos_por_retirar() -> QuerySet[SourceDocument]:
    return (
        SourceDocument.objects.filter(doc_type=DocType.FICHA_TECNICA, status__in=_RETIRABLES)
        .filter(Q(pdf_file="") | Q(pdf_file__isnull=True))
        .annotate(
            vigentes=Count(
                "cima_registrations", filter=Q(cima_registrations__retirado_en__isnull=True)
            )
        )
        .filter(vigentes=0)
    )


def resumen_de(run: CimaSyncRun) -> list[tuple[str, int]]:
    totales = dict(run.eventos.values_list("tipo").annotate(total=Count("pk")).order_by())
    return [(tipo.label, totales[tipo]) for tipo in TipoEvento if tipo in totales]


def motivo_de_retirada(medicamento: dict[str, Any]) -> str | None:
    estado = medicamento.get("estado")
    if not isinstance(estado, dict):
        raise CimaUnavailable(
            f"CIMA devolvió un estado que no es un objeto: {type(estado).__name__}"
        )
    if estado.get("rev"):
        return MotivoRetirada.REVOCADO
    if estado.get("susp"):
        return MotivoRetirada.SUSPENDIDO
    comerc = medicamento.get("comerc")
    if not isinstance(comerc, bool):
        raise CimaUnavailable("El detalle de CIMA no dice si el medicamento se comercializa")
    if not comerc:
        return MotivoRetirada.NO_COMERCIALIZADO
    if not isinstance(medicamento.get("docs"), list):
        raise CimaUnavailable("El detalle de CIMA no dice qué documentos publica")
    if ficha_segmentada(medicamento) is None:
        return MotivoRetirada.SIN_FICHA
    return None


def _cerrar_abandonadas() -> None:
    CimaSyncRun.objects.filter(
        estado=EstadoSincronizacion.EN_CURSO,
        iniciada_en__lt=timezone.now() - ABANDONADA_TRAS,
    ).update(
        estado=EstadoSincronizacion.FALLIDA,
        terminada_en=timezone.now(),
        error_msg=f"Abandonada: seguía en curso tras {ABANDONADA_TRAS}",
    )


def _cargar_todo(run: CimaSyncRun, cliente: _ClienteCima) -> None:
    existentes = sorted(CimaRegistration.objects.values_list("nregistro", flat=True))

    def dar_de_alta(nregistro: str, bucket_id: str) -> None:
        _revisar(
            run,
            nregistro,
            "",
            lambda: _Desenlace(TipoEvento.ALTA, ingest_ficha(nregistro, bucket_id)),
            visto=True,
        )

    run.cambios_leidos = crawl_fichas(client=cliente, enqueue=dar_de_alta).vistos
    bucket_id = str(Collection.ficha_bucket().pk)
    for nregistro in existentes:
        _revisar(
            run,
            nregistro,
            "",
            partial(_decidir, cliente, nregistro, {"desconocido"}, bucket_id),
        )


def _aplicar_cambios(run: CimaSyncRun, cliente: _ClienteCima, desde: date) -> None:
    senales: dict[str, set[str]] = {}
    nombres: dict[str, str] = {}
    paginas = recorrer_paginas(
        lambda pagina: cliente.cambios(desde, pagina), settings.CIMA_REQUEST_DELAY
    )
    for pagina in paginas:
        for fila in pagina:
            run.cambios_leidos += 1
            leida = _senales_de(fila)
            if leida is None:
                logger.warning("registroCambios trae una fila sin número de registro válido")
                continue
            nregistro, nombre, de_la_fila = leida
            senales.setdefault(nregistro, set()).update(de_la_fila)
            nombres[nregistro] = nombre

    pendientes = CimaSyncEvent.objects.filter(tipo=TipoEvento.ERROR, resuelto=False)
    for nregistro, nombre in pendientes.values_list("nregistro", "nombre"):
        senales.setdefault(nregistro, set()).add("desconocido")
        nombres.setdefault(nregistro, nombre)

    nuestros = set(CimaRegistration.objects.values_list("nregistro", flat=True))
    bucket_id = str(Collection.ficha_bucket().pk)
    for nregistro in sorted(senales):
        relevantes = _SENALES_DE_LO_NUESTRO if nregistro in nuestros else _SENALES_DE_LO_AJENO
        if senales[nregistro] & relevantes:
            _revisar(
                run,
                nregistro,
                nombres[nregistro],
                partial(_decidir, cliente, nregistro, senales[nregistro], bucket_id),
            )


def _senales_de(fila: object) -> tuple[str, str, set[str]] | None:
    if not isinstance(fila, dict) or not isinstance(fila.get("nregistro"), str):
        return None
    if not NREGISTRO.fullmatch(fila["nregistro"]):
        return None
    senales: set[str] = set()
    if fila.get("tipoCambio") == 1:
        senales.add("alta")
    if fila.get("tipoCambio") == 2:
        senales.add("baja")
    cambio = fila["cambio"] if "cambio" in fila else fila.get("cambios")
    if isinstance(cambio, list):
        senales.update(c for c in cambio if isinstance(c, str))
    else:
        senales.add("desconocido")
    return fila["nregistro"], str(fila.get("nombre") or ""), senales


def _decidir(
    cliente: _ClienteCima, nregistro: str, senales: set[str], bucket_id: str
) -> _Desenlace | None:
    detalle = cliente.medicamento(nregistro)
    if str(detalle.get("nregistro")) != nregistro:
        raise CimaUnavailable(f"CIMA respondió por otro registro al pedir {nregistro}")
    motivo = motivo_de_retirada(detalle)
    registro = CimaRegistration.objects.filter(pk=nregistro).first()
    if motivo is not None:
        if registro is not None and registro.activo:
            documento = retirar_registro(nregistro, motivo)
            return _Desenlace(TipoEvento.RETIRADA, documento, MotivoRetirada(motivo).label)
        return None
    if registro is None:
        return _Desenlace(TipoEvento.ALTA, ingest_ficha(nregistro, bucket_id))
    if registro.activo and not senales & _SENALES_DE_FICHA:
        return None
    documento, tipo = resincronizar(nregistro)
    return _Desenlace(tipo, documento) if tipo else None


def _revisar(
    run: CimaSyncRun,
    nregistro: str,
    nombre: str,
    accion: Callable[[], _Desenlace | None],
    *,
    visto: bool = False,
) -> None:
    run.revisados += 1
    try:
        desenlace = accion()
    except (CimaUnavailable, *FALLOS_DEFINITIVOS) as exc:
        logger.warning("CIMA %s no se sincroniza: %s", nregistro, error_summary(exc))
        desenlace = _Desenlace(TipoEvento.ERROR, detalle=error_summary(exc))
    except Exception as exc:  # noqa: BLE001 - un registro no tumba la ejecución entera
        logger.exception("CIMA %s: fallo inesperado al sincronizar", nregistro)
        desenlace = _Desenlace(TipoEvento.ERROR, detalle=error_summary(exc))

    if desenlace is None or desenlace.tipo != TipoEvento.ERROR:
        CimaSyncEvent.objects.filter(
            nregistro=nregistro, tipo=TipoEvento.ERROR, resuelto=False
        ).update(resuelto=True)
    if desenlace is not None:
        documento = desenlace.documento
        CimaSyncEvent.objects.create(
            run=run,
            nregistro=nregistro,
            nombre=(documento.display_name if documento else nombre)[:512],
            tipo=desenlace.tipo,
            document=documento,
            detalle=desenlace.detalle[:500],
            visto_en=timezone.now() if visto and desenlace.tipo in TIPOS_QUE_AVISAN else None,
        )
    run.save(update_fields=["revisados", "cambios_leidos"])
    time.sleep(settings.CIMA_REQUEST_DELAY)


def _retirar_huerfanos() -> None:
    for doc_id in documentos_por_retirar().values_list("pk", flat=True):
        logger.info("El documento %s no tiene registros vigentes: se retira", doc_id)
        retirar_documento_task.delay(str(doc_id))
