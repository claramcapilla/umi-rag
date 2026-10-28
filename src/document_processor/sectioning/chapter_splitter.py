"""Agrupa un documento en capítulos por sus encabezados de nivel superior.

Una guía clínica que en realidad es un libro —un manual de 4,7 millones de caracteres— no cabe en
la ventana del modelo local (32k tokens), así que cada sección de generación recibe un extracto
ínfimo y la conversión pierde el 85-99% del documento sin dar ningún error. Partirla en capítulos,
cada uno un documento de tamaño normal, deja que el pipeline existente los convierta a fidelidad
plena.

Este módulo hace solo el troceado del Markdown; decidir *cuándo* partir (el umbral de tamaño) y
crear los documentos hijos es del llamante. Reutiliza `split_document`, que ya localiza los
encabezados con sus offsets, y agrupa por el nivel de encabezado más superficial que aparezca al
menos dos veces: una guía con un único `#` de título seguido de `##` por capítulo se parte por los
`##`, no por el título. El troceado es **sin pérdida por construcción**: el primer capítulo arranca
en el offset 0 —para llevarse el preámbulo o el título que precede al primer encabezado— y el
último acaba al final del documento, así que cada carácter cae en exactamente un capítulo.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .section_splitter import split_document
from .ventanas import partir_en_ventanas

CAPITULO_MINIMO = 20_000


@dataclass(frozen=True)
class Chapter:
    """Un capítulo: su encabezado y la porción de Markdown que le corresponde."""

    heading: str
    text: str


def split_into_chapters(
    md_text: str, minimo: int = CAPITULO_MINIMO, maximo: int | None = None
) -> list[Chapter]:
    capitulos = _por_encabezados(md_text, minimo)
    if maximo is None:
        return capitulos
    if not capitulos and len(md_text) > maximo:
        return _por_tamano(Chapter("", md_text), minimo, maximo)
    return [parte for capitulo in capitulos for parte in _sin_exceder(capitulo, minimo, maximo)]


def _sin_exceder(capitulo: Chapter, minimo: int, maximo: int) -> list[Chapter]:
    if len(capitulo.text) <= maximo:
        return [capitulo]
    subcapitulos = _por_encabezados(capitulo.text, minimo)
    if not subcapitulos:
        return _por_tamano(capitulo, minimo, maximo)
    return [
        parte
        for sub in subcapitulos
        for parte in _sin_exceder(
            Chapter(f"{capitulo.heading} › {sub.heading}", sub.text), minimo, maximo
        )
    ]


def _por_tamano(capitulo: Chapter, minimo: int, maximo: int) -> list[Chapter]:
    partes = math.ceil(len(capitulo.text) / maximo)
    trozos = partir_en_ventanas(capitulo.text, math.ceil(len(capitulo.text) / partes))
    if len(trozos[-1]) < minimo and len(trozos[-2]) + len(trozos[-1]) <= maximo:
        trozos[-2:] = [trozos[-2] + trozos[-1]]
    textos = [trozo.strip() for trozo in trozos if trozo.strip()]
    return [
        Chapter(f"{capitulo.heading} (parte {i}/{len(textos)})".lstrip(), texto)
        for i, texto in enumerate(textos, 1)
    ]


def _por_encabezados(md_text: str, minimo: int) -> list[Chapter]:
    """Parte el Markdown por su nivel de encabezado más superficial con al menos dos apariciones.

    Devuelve `[]` cuando no hay estructura por la que partir —ningún nivel de encabezado aparece
    dos veces— o cuando, tras unir los capítulos de menos de `minimo` caracteres, quedan menos de
    dos: es la señal de que el llamante debe procesar el documento entero. Nunca devuelve un solo
    capítulo: partir en uno no aportaría nada.
    """
    secciones = split_document(md_text)
    niveles = sorted({s.level for s in secciones if s.level >= 1})
    nivel_capitulo = next(
        (nivel for nivel in niveles if sum(1 for s in secciones if s.level == nivel) >= 2),
        None,
    )
    if nivel_capitulo is None:
        return []

    cabeceras = [s for s in secciones if s.level == nivel_capitulo]
    inicios = [s.char_offset for s in cabeceras]
    inicios[0] = 0
    fines = inicios[1:] + [len(md_text)]
    return _sin_capitulos_menores(
        [
            Chapter(heading=cab.heading, text=md_text[inicio:fin].strip())
            for cab, inicio, fin in zip(cabeceras, inicios, fines, strict=True)
        ],
        minimo,
    )


def _sin_capitulos_menores(capitulos: list[Chapter], minimo: int) -> list[Chapter]:
    unidos: list[Chapter] = []
    pendientes: list[str] = []
    for capitulo in capitulos:
        texto = "\n\n".join([*pendientes, capitulo.text])
        if len(texto) < minimo:
            pendientes.append(capitulo.text)
            continue
        unidos.append(Chapter(heading=capitulo.heading, text=texto))
        pendientes = []
    if pendientes and unidos:
        ultimo = unidos.pop()
        unidos.append(Chapter(ultimo.heading, "\n\n".join([ultimo.text, *pendientes])))
    return unidos if len(unidos) >= 2 else []