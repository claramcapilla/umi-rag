"""Recorre el registro de CIMA y encola la ingesta de sus fichas técnicas comercializadas.

Solo enumera —una búsqueda paginada, ligera— y encola una tarea por número de registro a la cola
`cima`; el trabajo pesado de cada ficha (detalle, secciones, BiFiMed) lo hace el worker. Así el
recorrido no se pelea con la cola de generación PDF→JSON ni bloquea nada.

Tres decisiones que sostienen que sea seguro repetirlo:

- **Solo comercializadas.** Se pide `comerc=1` al servidor y se vuelve a comprobar en cada registro,
  por si el filtro del servidor no se respetara: son ~16.000 de los ~25.000 del registro, y las no
  comercializadas son retiradas o históricas que no interesan al RAG clínico.
- **Solo fichas segmentadas.** Un medicamento sin ficha técnica segmentada (`docs` tipo 1 con
  `secc`) no tiene vía de ingesta desde la retirada del camino PDF+LLM, así que se salta aquí en vez
  de encolar un fallo seguro.
- **Reanudable e idempotente.** Se saltan los números de registro ya ingeridos (`CimaRegistration`),
  así que repetir el recorrido continúa donde se quedó. Encolar dos veces el mismo registro tampoco
  duplica: `ingest_ficha` ve el registro ya creado y vuelve. Los alias (mismo SmPC bajo otro número)
  **sí** se encolan los dos a propósito: la huella los cuelga del mismo documento al desduplicar.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, Protocol

from django.conf import settings

from document_processor.cima import CimaClient
from document_processor.cima.docs import ficha_segmentada
from document_processor.cima.errors import CimaUnavailable
from documents.models import CimaRegistration
from library.models import Collection

MAX_PAGINAS = 2000


class _Buscador(Protocol):
    def buscar(self, criterio: dict[str, Any]) -> dict[str, Any]: ...


@dataclass
class CrawlReport:
    paginas: int = 0
    vistos: int = 0
    encolados: int = 0
    ya_ingeridas: int = 0
    sin_ficha_segmentada: int = 0
    no_comercializadas: int = 0

    def as_lines(self) -> list[str]:
        return [
            f"Páginas recorridas: {self.paginas}",
            f"Medicamentos vistos: {self.vistos}",
            f"Encolados: {self.encolados}",
            f"Ya ingeridas (saltadas): {self.ya_ingeridas}",
            f"Sin ficha segmentada (saltadas): {self.sin_ficha_segmentada}",
            f"No comercializadas (saltadas): {self.no_comercializadas}",
        ]


def _entero(valor: Any) -> int | None:
    try:
        return int(valor)
    except (TypeError, ValueError):
        return None


def _hay_siguiente(pagina: int, por_pagina: int, total: int | None, en_pagina: int) -> bool:
    """Mirror de la paginación de la vista de CIMA, incluidas sus dos trampas medidas.

    El tamaño de página se lee de la respuesta y no se supone. Si falta `totalFilas` no se afirma un
    total: se ofrece otra página solo si esta vino llena, porque negar la siguiente escondería
    registros y afirmar el final con un total inventado es el bug que se arregló en la vista.
    """
    if por_pagina <= 0:
        return False
    if total is None:
        return en_pagina >= por_pagina
    return pagina * por_pagina < total


def recorrer_paginas(
    pedir: Callable[[int], dict[str, Any]], retardo: float
) -> Iterator[list[dict[str, Any]]]:
    for pagina in range(1, MAX_PAGINAS + 1):
        datos = pedir(pagina)
        resultados = datos.get("resultados")
        if not isinstance(resultados, list):
            raise CimaUnavailable(f"La página {pagina} de CIMA no trae la lista de resultados")
        yield resultados

        por_pagina = _entero(datos.get("tamanioPagina")) or len(resultados)
        total = _entero(datos.get("totalFilas"))
        if not _hay_siguiente(pagina, por_pagina, total, len(resultados)):
            return
        time.sleep(retardo)
    raise CimaUnavailable(
        f"CIMA sigue ofreciendo páginas tras {MAX_PAGINAS}: se corta el recorrido"
    )


def _encolar_por_defecto(nregistro: str, especialidad_id: str) -> None:
    from .tasks import ingest_ficha_task

    ingest_ficha_task.delay(nregistro, especialidad_id)


def crawl_fichas(
    *,
    limite: int | None = None,
    dry_run: bool = False,
    page_delay: float | None = None,
    client: _Buscador | None = None,
    enqueue: Callable[[str, str], Any] | None = None,
) -> CrawlReport:
    """Enumera CIMA y encola las fichas comercializadas aún no ingeridas.

    El bucket se resuelve una sola vez al principio (y no en `dry_run`), así que cuando corren las
    tareas ya existe: `ficha_bucket` va por la rama de lectura y no hay carrera por crear dos.
    """
    cliente: _Buscador = client or CimaClient.from_settings()
    retardo = settings.CIMA_REQUEST_DELAY if page_delay is None else page_delay
    encolar = enqueue or _encolar_por_defecto
    ya_ingeridas = set(CimaRegistration.objects.values_list("nregistro", flat=True))
    bucket_id = "" if dry_run else str(Collection.ficha_bucket().pk)

    report = CrawlReport()
    paginas = recorrer_paginas(
        lambda pagina: cliente.buscar({"comerc": 1, "pagina": pagina}), retardo
    )
    for resultados in paginas:
        report.paginas += 1

        for medicamento in resultados:
            report.vistos += 1
            nregistro = str(medicamento.get("nregistro") or "")
            if not medicamento.get("comerc"):
                report.no_comercializadas += 1
                continue
            if ficha_segmentada(medicamento) is None:
                report.sin_ficha_segmentada += 1
                continue
            if nregistro in ya_ingeridas:
                report.ya_ingeridas += 1
                continue

            if not dry_run:
                encolar(nregistro, bucket_id)
            ya_ingeridas.add(nregistro)
            report.encolados += 1
            if limite is not None and report.encolados >= limite:
                return report

    return report
