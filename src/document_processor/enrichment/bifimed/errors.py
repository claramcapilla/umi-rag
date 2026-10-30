"""Errores de la consulta a BIFIMED.

Módulo aparte de `client` a propósito: `client` importa pandas, y quien solo necesita capturar
el error no debe pagar ese import. El enriquecedor lo importa a nivel de módulo justo por eso —
si el nombre viviera en `client` y se importase dentro del `try`, un fallo de importación
convertiría la cláusula `except` en un `NameError`.
"""


class BifimedUnavailable(RuntimeError):
    """La consulta a BIFIMED no se pudo completar.

    Existe para separar «no pude consultar» de «consulté y no hay datos». Antes los cuatro
    `except Exception` del cliente colapsaban fallo de red, dependencia ausente y parseo roto en
    el mismo `None` que devuelve la ausencia real de datos, y el enriquecedor escribía en el JSON
    clínico que el medicamento **no está financiado** — una afirmación falsa redactada como
    hecho, indistinguible de la verdadera, que acaba indexada y citada.
    """
