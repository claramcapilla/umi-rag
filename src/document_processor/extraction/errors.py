"""Excepción del tramo de conversión, en su propio módulo y sin dependencias pesadas.

Vive aparte de `pdf_converter` por la misma razón que `bifimed/errors.py`: quien la captura la
necesita en la cláusula `except`, o sea antes de entrar al `try`, mientras que el motor de ingesta
se importa de forma perezosa para no exigir la JVM ni el cliente del LLM salvo al procesar. Un
módulo que solo declara la clase se puede importar arriba sin arrastrar nada de eso.
"""

from __future__ import annotations


class ConversionError(RuntimeError):
    """El PDF no llegó a Markdown utilizable: falla la etapa de parseo, no la del LLM.

    Hereda de `RuntimeError` porque es lo que ya lanzaban los caminos de fallo de `convert_pdf`,
    así que quien capture `RuntimeError` no cambia de comportamiento.
    """


class FichaTecnicaNoIngerible(ConversionError):
    """El PDF es una ficha técnica, y esa variante ya no se extrae con el LLM.

    La fuente de las fichas es CIMA/AEMPS, que las sirve ya segmentadas por su numeración
    normativa: medido sobre 5 fichas, el relleno del esquema es del 100% frente al 60-81% del
    camino PDF+LLM, sin coste y sin riesgo de parafraseo sobre texto normativo. Así que este
    camino se retiró; se ingiere por número de registro.

    Hereda de `ConversionError` a propósito, y no es un atajo: ocurre **antes del LLM** y no deja
    ningún JSON en disco, que es exactamente lo que significa la etapa `PARSE`. El llamante ya
    trata así todo lo anterior al LLM, y `error_summary` usa el nombre del tipo como
    clasificador, así que el dashboard lo distingue sin ninguna rama nueva.
    """