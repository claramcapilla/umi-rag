"""Resumen de fallos apto para persistir en `SourceDocument.error_msg`."""

from __future__ import annotations

from documents.models import ERROR_MSG_LIMIT


def error_summary(exc: BaseException, *, limit: int = ERROR_MSG_LIMIT) -> str:
    """Mensaje de fallo acotado y en una sola línea.

    El detalle completo va al log, no a la base de datos: `error_msg` se replica en el historial
    de `django-simple-history` y se muestra en la UI, y los mensajes de excepción del motor de
    ingesta pueden arrastrar texto del documento. Aquí se conserva el tipo de excepción —siempre
    seguro y suficiente para clasificar el fallo— más el mensaje colapsado a una línea y recortado.

    La fuga de 500 caracteres de respuesta del LLM que traía `json_sanitizer` **está cerrada**
    desde que `src/pipeline/tasks.py` pasó a importar la copia in-repo (PR 7a de la Fase 7): la
    versión absorbida en `src/document_processor/clinical/` no mete la respuesta en el mensaje.

    Lo que queda es el residual delegado al despliegue: `clinical_formatter` registra la excepción
    de cada sección con `logger.error`, así que si el endpoint del LLM devolviera el prompt en el
    cuerpo de un error, eso saldría por el log —no por este campo—, y la retención y el control de
    acceso del log los fija el despliegue.
    """
    detail = " ".join(str(exc).split())
    summary = f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__
    return summary[:limit]


class SinEspecialidad(ValueError):
    """Se pidió ingerir sin decir bajo qué especialidad, o con una que no es raíz.

    Se comprueba **antes** de tocar el registro externo: `index_document` exige que el documento
    esté en alguna colección raíz, así que sin esto la ficha gastaría todas sus peticiones a la
    AEMPS para morir al final, en la etapa de indexado.
    """


class ColapsoDudoso(RuntimeError):
    """Dos registros con el mismo contenido pero de distinto medicamento o titular.

    No debería ocurrir: en los 195 grupos de duplicados de un censo de 1.500 fichas, **ninguno**
    cruzaba laboratorio ni principio activo. Es la guarda del hueco que queda cuando CIMA no
    segmenta las secciones 1, 2, 7 y 8 —las de identificación—, porque entonces esos campos los
    rellena el metadato estructurado y **no entran en la huella**. Sin la guarda, el fallo sería
    silencioso y produciría un documento que dice ser un registro con el texto de otro
    laboratorio.
    """


class PartialExtraction(RuntimeError):
    """El JSON del documento está incompleto o no pasó la revisión de contenido: no se indexa.

    Se lanza en `src/pipeline/index.py`, no solo en la etapa PDF→JSON: negarse a indexar un
    parcial únicamente en el camino automático dejaba la decisión a un clic del detalle del
    documento, porque `FAILED → INDEXING` es una transición permitida.
    """


class DocumentoOcupado(RuntimeError):
    pass


class RegistroRetirado(RuntimeError):
    pass


class RegistroCambiado(RuntimeError):
    pass
