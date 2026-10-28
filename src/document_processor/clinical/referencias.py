from __future__ import annotations

from collections import Counter
from typing import Any

from document_processor.schema import ClinicalJson

_MAX_CARACTERES_EN_MENSAJE = 40
_REGIMENES = ("recommended_regimens", "alternative_regimens", "not_recommended")
_COLECCIONES = (
    ("conditions", "condition_id"),
    ("treatment_pathways", "pathway_id"),
    ("recommendations", "rec_id"),
    ("artifacts", "artifact_id"),
)
_PREFIJOS_DE_ID = (
    ("conditions", "condition_id", "cond"),
    ("treatment_pathways", "pathway_id", "path"),
    ("recommendations", "rec_id", "rec"),
    ("artifacts", "artifact_id", "art"),
)
_REFERENCIAS = (
    ("treatment_pathways", "condition_ref", "conditions"),
    ("recommendations", "condition_ref", "conditions"),
    ("special_populations", "condition_refs", "conditions"),
    ("therapies_index", "pathway_refs", "treatment_pathways"),
)


def _clave(texto: Any) -> str:
    return " ".join(texto.casefold().split()) if isinstance(texto, str) else ""


def _lista(valor: Any) -> list[Any]:
    return valor if isinstance(valor, list) else []


def _objetos(valor: Any) -> list[dict[str, Any]]:
    return [x for x in _lista(valor) if isinstance(x, dict)]


def _ids_de_condiciones(guia: ClinicalJson) -> dict[str, str]:
    por_nombre: dict[str, str] = {}
    ambiguos: set[str] = set()
    for condicion in _objetos(guia.get("conditions")):
        identificador = condicion.get("condition_id")
        if not isinstance(identificador, str):
            continue
        for nombre in [condicion.get("name"), *_lista(condicion.get("synonyms"))]:
            clave = _clave(nombre)
            if clave and por_nombre.setdefault(clave, identificador) != identificador:
                ambiguos.add(clave)
    return {clave: id_ for clave, id_ in por_nombre.items() if clave not in ambiguos}


def _vias_de(terapia: dict[str, Any], vias: list[dict[str, Any]]) -> list[str]:
    nombre = _clave(terapia.get("name"))
    if not nombre:
        return []
    return [
        via["pathway_id"]
        for via in vias
        if isinstance(via.get("pathway_id"), str)
        and any(
            nombre == _clave(regimen.get("name"))
            for grupo in _REGIMENES
            for regimen in _objetos(via.get(grupo))
        )
    ]


def renumerar_ids(guia: ClinicalJson) -> None:
    for coleccion, campo, prefijo in _PREFIJOS_DE_ID:
        for numero, elemento in enumerate(_objetos(guia.get(coleccion)), start=1):
            elemento[campo] = f"{prefijo}_{numero:02d}"


def resolver_referencias(guia: ClinicalJson) -> None:
    condiciones = _ids_de_condiciones(guia)
    vias = _objetos(guia.get("treatment_pathways"))
    for elemento in [*vias, *_objetos(guia.get("recommendations"))]:
        elemento["condition_ref"] = condiciones.get(_clave(elemento.get("condition")))
    for poblacion in _objetos(guia.get("special_populations")):
        nombres = poblacion.get("conditions")
        resueltas = (condiciones.get(_clave(n)) for n in _lista(nombres))
        poblacion["condition_refs"] = list(dict.fromkeys(r for r in resueltas if r))
    for terapia in _objetos(guia.get("therapies_index")):
        terapia["pathway_refs"] = _vias_de(terapia, vias)


def _valores_de(elemento: dict[str, Any], campo: str) -> list[Any]:
    valor = elemento.get(campo)
    return valor if isinstance(valor, list) else [valor]


def _ids_repetidos(coleccion: str, campo: str, valores: list[Any]) -> list[str]:
    cuenta = Counter(v for v in valores if isinstance(v, str))
    repetidos = sorted(v[:_MAX_CARACTERES_EN_MENSAJE] for v, n in cuenta.items() if n > 1)
    return [f"{coleccion}: {campo} repetido ({', '.join(repetidos)})"] if repetidos else []


def referencias_rotas(guia: ClinicalJson) -> list[str]:
    rotas: list[str] = []
    ids: dict[str, set[str]] = {}
    for coleccion, campo in _COLECCIONES:
        valores = [o.get(campo) for o in _objetos(guia.get(coleccion))]
        rotas += _ids_repetidos(coleccion, campo, valores)
        ids[coleccion] = {v for v in valores if isinstance(v, str)}
    for coleccion, campo, destino in _REFERENCIAS:
        rotas += [
            f"{coleccion}.{campo}: apunta a {str(ref)[:_MAX_CARACTERES_EN_MENSAJE]!r}, "
            f"que no existe en {destino}"
            for elemento in _objetos(guia.get(coleccion))
            for ref in _valores_de(elemento, campo)
            if ref is not None and (not isinstance(ref, str) or ref not in ids[destino])
        ]
    return rotas