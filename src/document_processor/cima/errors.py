"""Errores de la ingesta desde CIMA, en módulo propio y solo con stdlib.

Van aparte por la misma razón que `bifimed/errors.py` y `extraction/errors.py`: hacen falta en la
cláusula `except`, o sea antes del `try`, y un import que arrastrase el cliente HTTP convertiría
el manejo del fallo en otro fallo.

Son **dos** y la distinción es la misma que sostiene todo el enriquecimiento: «no pude
preguntar» no puede escribirse como «pregunté y no hay». Un número de registro que no existe es
una ausencia cierta y se le dice al usuario que no existe; el registro caído es una
indisponibilidad y se reintenta.
"""

from __future__ import annotations


class CimaUnavailable(RuntimeError):
    """La consulta a CIMA no se pudo completar: red, formato inesperado o tope superado."""


class CimaSinFicha(LookupError):
    """CIMA respondió, y ese medicamento no tiene ficha técnica segmentada que ingerir.

    Cubre las tres formas de la misma ausencia: el número de registro no existe, existe pero no
    publica ficha técnica, o la publica sin segmentar. Las tres son definitivas —reintentar no
    las arregla— y desde que se retiró el camino PDF+LLM no tienen vía alternativa, así que lo
    que corresponde es decirlo en claro y no dejar el documento a medias.
    """
