"""Construye el JSON clínico de una ficha técnica desde CIMA, sin pasar por el LLM.

CIMA sirve la ficha **ya segmentada por su numeración normativa** y los metadatos del medicamento
de forma estructurada, así que el LLM no aporta nada aquí: solo añadiría coste, latencia y riesgo
de parafraseo sobre un texto que ya es autoritativo y está limpio. Medido sobre 5 fichas, el
relleno del esquema es del 100% frente al 60-81% del camino PDF+LLM.

El mapeo sección → campo usa los 25 campos que **ya declara** `SECTIONS_FT`, así que el JSON
resultante lo trocea el chunker actual sin tocar el esquema ni el contrato con codigo-rojo.

**Los 25 campos se emiten como cadena, y no se pasa por `sanitize_clinical_json`.** Es una decisión,
no un olvido, y va contra lo que parecía obvio: por aquí ningún campo es una lista de ítems, es un
bloque de texto de su sección. Las listas del corpus congelado son de **un solo elemento**
envolviendo la sección entera, artefacto del script de evaluación ya retirado. Y aplicar el saneador
no acercaría a esa línea base sino que la movería a un tercer sitio: los dos conjuntos ni coinciden
—medido, el corpus tiene lista `adverse_reactions` y cadena `warnings_and_precautions` y
`marketing_authorisation_number`, y el saneador declara justo lo contrario—. El chunker renderiza
cadena y lista por igual, así que no hay nada que romper; lo que había era una ambigüedad, y esto la
cierra. Las fichas del corpus, ya regeneradas, son todas cadena.

Lo que sí se aplica es `redact_personal_data`, el mismo y único control de minimización que corre
sobre el Markdown en el camino del PDF. No es que se hayan medido correos en fichas técnicas —no se
han medido—: es que **el momento de decidirlo es este**. Redactar cambia el texto y con él los
`section_hashes`, así que hacerlo más tarde invalidaría todos los `content_hash` guardados y cada
ficha ya ingerida parecería modificada.
"""

from __future__ import annotations

import hashlib
import logging
import re
from html.parser import HTMLParser
from typing import Any 

from document_processor.chunking import SCHEMA_VERSION
from document_processor.extraction.redaction import redact_personal_data
from document_processor.fidelity import build_cima_fidelity_report
from document_processor.sections import SECTIONS_FT

from .client import CimaClient
from .docs import documento_de_ficha
from .errors import CimaSinFicha, CimaUnavailable

logger = logging.getLogger(__name__)

SECCION_A_CAMPO = {
    "1": "medicine_name",
    "2": "composition",
    "3": "pharmaceutical_form",
    "4.1": "indications",
    "4.2": "posology",
    "4.3": "contraindications",
    "4.4": "warnings_and_precautions",
    "4.5": "interactions",
    "4.6": "pregnancy_and_lactation",
    "4.7": "effects_on_driving",
    "4.8": "adverse_reactions",
    "4.9": "overdose",
    "5.1": "pharmacodynamics",
    "5.2": "pharmacokinetics",
    "6.1": "excipients",
    "6.2": "incompatibilities",
    "6.3": "shelf_life",
    "6.4": "storage_conditions",
    "6.5": "container",
    "6.6": "handling_instructions",
    "7": "marketing_authorisation_holder",
    "8": "marketing_authorisation_number",
}

CAMPOS_DEL_ESQUEMA: tuple[str, ...] = tuple(f for s in SECTIONS_FT.values() for f in s["fields"])
"""Los 25 campos en el orden en que los declara `SECTIONS_FT`, y el orden importa.

Con un `frozenset` el orden de las claves del JSON archivado dependía de `PYTHONHASHSEED`
—medido: tres órdenes distintos en tres procesos—, así que la misma ficha se archivaba con bytes
distintos entre ejecuciones. No cambiaba los chunks, que el chunker renderiza por este mismo orden,
pero contradice la reproducibilidad con la que se cerró la fase 1.
"""


_ESPACIO = re.compile(r"\s+")
_BLOQUES = frozenset(
    {
        "address",
        "article",
        "blockquote",
        "caption",
        "center",
        "dd",
        "div",
        "dl",
        "dt",
        "figcaption",
        "figure",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "tbody",
        "tfoot",
        "thead",
        "tr",
        "ul",
    }
)
_CELDAS = frozenset({"td", "th"})
_OCULTOS = frozenset({"style", "script"})
_ETIQUETAS_HTML = (
    "a|b|br|div|em|font|h[1-6]|i|img|li|ol|p|span|strong|sub|sup|table|tbody|td|th|thead|tr|u|ul"
)
_MARCADO = re.compile(
    rf"</?(?:{_ETIQUETAS_HTML})(?:\s+[\w:-]+\s*=[^<>]*)?\s*/?>"
    r"|&(?:#[0-9]+|#[xX][0-9A-Fa-f]+|nbsp|amp|lt|gt|quot|apos|[A-Za-z](?:acute|grave|circ|tilde|uml));",
    re.IGNORECASE,
)


class _LectorDeHtml(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lineas: list[str] = []
        self._linea: list[str] = []
        self.ocultos = 0

    def cerrar_linea(self) -> None:
        linea = _ESPACIO.sub(" ", "".join(self._linea)).strip()
        if linea:
            self.lineas.append(linea)
        self._linea = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _OCULTOS:
            self.ocultos += 1
        elif tag in _BLOQUES or tag == "br":
            self.cerrar_linea()
        elif tag in _CELDAS:
            self._linea.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _OCULTOS:
            self.ocultos = max(0, self.ocultos - 1)
        elif tag in _BLOQUES:
            self.cerrar_linea()

    def handle_data(self, data: str) -> None:
        if not self.ocultos:
            self._linea.append(data)


def _texto_de_html(html: str) -> str:
    lector = _LectorDeHtml()
    lector.feed(html)
    lector.close()
    if lector.ocultos:
        raise CimaUnavailable(
            "CIMA sirvió un <style> o <script> sin cerrar, que ocultaría el resto"
        )
    lector.cerrar_linea()
    return "\n".join(lector.lineas)


def _texto_de_seccion(nregistro: str, seccion: object) -> tuple[str, str, str]:
    malformada = CimaUnavailable(f"CIMA sirvió la ficha de {nregistro} con una sección malformada")
    if not isinstance(seccion, dict):
        raise malformada
    numero = seccion.get("seccion")
    titulo = seccion.get("titulo") or ""
    html = seccion.get("contenido") or ""
    if not isinstance(numero, str) or not isinstance(titulo, str) or not isinstance(html, str):
        raise malformada
    texto = _texto_de_html(html)
    if _MARCADO.search(texto):
        raise CimaUnavailable(
            f"CIMA sirvió la sección {numero} de {nregistro} con marcado tras leer su HTML"
        )
    titulo_limpio = redact_personal_data(_ESPACIO.sub(" ", titulo).strip())
    return numero, titulo_limpio, redact_personal_data(texto)


def _hash(texto: str) -> str:
    return hashlib.sha256(texto.encode()).hexdigest()[:16]


def _ficha_publicada(medicamento: dict[str, Any], nregistro: str) -> dict[str, Any]:
    """El documento de ficha técnica, o la ausencia dicha en claro.

    Las tres salidas son la misma ausencia definitiva y ninguna se arregla reintentando: desde la
    retirada del camino PDF+LLM, un medicamento sin ficha segmentada no tiene vía de ingesta y lo
    que corresponde es decirlo, no archivar un documento a medias.
    """
    ficha = documento_de_ficha(medicamento)
    if ficha is None:
        raise CimaSinFicha(f"{nregistro} no publica ficha técnica en CIMA")
    if not ficha.get("secc"):
        raise CimaSinFicha(f"{nregistro} publica la ficha sin segmentar, así que no es ingerible")
    return ficha


def _campo_de(numero: str) -> str | None:
    partes = numero.split(".")
    for corte in range(len(partes), 0, -1):
        if campo := SECCION_A_CAMPO.get(".".join(partes[:corte])):
            return campo
    return None


def _tiene_contenido(texto: str) -> bool:
    return any(caracter.isalnum() for caracter in texto)


def _partes_redundantes(partes: list[str]) -> list[str]:
    """Partes de un campo que otra ya contiene: texto que se archivaría dos veces.

    Es exacta y sin umbral —contención de subcadena, no parecido—.

    Verificada contra el daño real: en el corpus congelado, el script que lo construyó colgaba cada
    subsección del campo de su padre, y para 61905 las secciones 4.6.1 (578 caracteres), 4.6.2 (308)
    y 4.6.3 (217) son subcadenas exactas de 4.6 (1.174). Tres de las cinco fichas lo llevan.

    Solo **informa**: quitarlas cambiaría el contenido archivado y sus hashes, y en esta fase el
    revisor mide.
    """
    return sorted(
        {a for a in partes if _tiene_contenido(a) for b in partes if a is not b and a in b}
    )


def _recoger_secciones(
    nregistro: str, secciones: list[dict[str, Any]]
) -> tuple[dict[str, list[str]], list[dict[str, Any]], int]:
    trozos: dict[str, list[str]] = {}
    descartadas: list[dict[str, Any]] = []
    total = 0
    for seccion in secciones:
        numero, titulo, texto = _texto_de_seccion(nregistro, seccion)
        if not texto:
            continue
        total += len(texto)
        campo = _campo_de(numero)
        if campo is None:
            descartadas.append({"section": numero, "chars": len(texto)})
            continue
        propia = numero in SECCION_A_CAMPO
        trozos.setdefault(campo, []).append(texto if propia or not titulo else f"{titulo}\n{texto}")
    return trozos, descartadas, total


def build_ficha_from_cima(
    nregistro: str,
    client: CimaClient | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """JSON clínico de la ficha y su informe de fidelidad.

    Devuelve los dos por separado, con el mismo criterio que `pdf_bytes_to_clinical_json`: el
    informe no viaja dentro del JSON que se archiva y se trocea.

    Todos los campos del esquema quedan presentes, con `None` los que CIMA no segmentó. Rellenar
    con nulos no borra aquí ninguna evidencia —que es lo que hacía peligroso a
    `_ensure_required_fields`— porque la evidencia vive en el informe, que nombra los campos
    ausentes y mide la cobertura; y el validador declara los campos opcionales, así que la
    ausencia de la clave sería un problema y su valor nulo no.
    """
    cliente = client or CimaClient.from_settings()

    medicamento = cliente.medicamento(nregistro)
    ficha = _ficha_publicada(medicamento, nregistro)
    trozos, descartadas, total = _recoger_secciones(nregistro, cliente.ficha(nregistro))

    if not trozos:
        raise CimaSinFicha(
            f"{nregistro} declara ficha segmentada pero ninguna sección normativa trae texto"
        )

    clinical_json: dict[str, Any] = dict.fromkeys(CAMPOS_DEL_ESQUEMA)
    hashes: dict[str, str] = {}
    duplicados: dict[str, int] = {}
    for campo, partes in trozos.items():
        if redundantes := _partes_redundantes(partes):
            duplicados[campo] = sum(len(t) for t in redundantes)
            logger.warning(
                "Ficha %s: el campo %s se compone de partes que se contienen entre sí (%d "
                "caracteres duplicados)",
                nregistro,
                campo,
                duplicados[campo],
            )
        unido = "\n\n".join(partes)
        clinical_json[campo] = unido
        hashes[campo] = _hash(unido)

    principios = medicamento.get("principiosActivos") or []
    salino = ", ".join(p["nombre"] for p in principios if p.get("nombre"))
    normalizado = (medicamento.get("vtm") or {}).get("nombre") or ""
    clinical_json["active_ingredient"] = (normalizado or salino).upper()
    clinical_json["strength"] = ", ".join(
        f"{p.get('cantidad', '')} {p.get('unidad', '')}".strip() for p in principios
    )
    clinical_json["route_of_administration"] = ", ".join(
        v["nombre"] for v in (medicamento.get("viasAdministracion") or []) if v.get("nombre")
    )
    if not clinical_json["marketing_authorisation_holder"]:
        clinical_json["marketing_authorisation_holder"] = medicamento.get("labtitular") or ""
    if not clinical_json["marketing_authorisation_number"]:
        clinical_json["marketing_authorisation_number"] = nregistro
    if not clinical_json["pharmaceutical_form"]:
        clinical_json["pharmaceutical_form"] = (medicamento.get("formaFarmaceutica") or {}).get(
            "nombre", ""
        )

    presentaciones = medicamento.get("presentaciones") or []
    clinical_json["national_codes"] = [p["cn"] for p in presentaciones if p.get("cn")]
    clinical_json["schema_version"] = SCHEMA_VERSION
    clinical_json["_source"] = {
        "origin": "CIMA/AEMPS docSegmentado",
        "registration_number": nregistro,
        "name": medicamento.get("nombre"),
        "atc": [a.get("codigo") for a in (medicamento.get("atcs") or []) if a.get("nivel") == 5]
        or [a.get("codigo") for a in (medicamento.get("atcs") or [])],
        "national_codes": clinical_json["national_codes"],
        "generic": bool(medicamento.get("generico")),
        "smpc_date": ficha.get("fecha"),
        "pdf_url": ficha.get("url"),
        "active_ingredient_salt": salino,
        "section_hashes": hashes,
    }

    esperados = set(SECCION_A_CAMPO.values())
    informe = build_cima_fidelity_report(
        nregistro=nregistro,
        doc_chars=total,
        campos={campo for campo in esperados if clinical_json.get(campo)},
        esperados=esperados,
        cubierto=total - sum(d["chars"] for d in descartadas),
        descartadas=descartadas,
        duplicados=duplicados,
    )
    logger.info(
        "Ficha %s construida: %d campos, cobertura %.3f",
        nregistro,
        len(trozos),
        informe["coverage"]["global_coverage"],
    )
    return clinical_json, informe
