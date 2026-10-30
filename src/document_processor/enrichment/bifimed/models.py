"""
Modelos de datos para información de financiación BIFIMED.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class IndicacionFinanciacion:
    """Estado de financiación para una indicación autorizada."""

    indicacion_autorizada: str
    situacion_expediente: str
    resolucion: str | None
    restricciones: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "authorised_indication": self.indicacion_autorizada,
            "file_status": self.situacion_expediente,
            "resolution": self.resolucion,
            "restrictions": self.restricciones,
        }


@dataclass
class PresentacionFinanciacion:
    """Información de financiación para una presentación (CN)."""

    cn: str
    nombre: str
    laboratorio: str | None = None
    estado_comercializacion: str | None = None

    situacion_financiacion: str | None = None
    estado_nomenclator: str | None = None
    aportacion_usuario: str | None = None
    fecha_alta_financiacion: str | None = None
    fecha_no_financiacion: str | None = None
    indicaciones_financiadas_texto: str | None = None
    indicaciones_no_financiadas_texto: str | None = None

    indicaciones: list[IndicacionFinanciacion] = field(default_factory=list)
    retrieved_at: str = field(default_factory=lambda: datetime.now().isoformat())
    source: str = "BIFIMED"

    @property
    def has_financing_data(self) -> bool:
        return bool(self.situacion_financiacion or self.indicaciones or self.estado_nomenclator)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "national_code": self.cn,
            "name": self.nombre,
            "laboratory": self.laboratorio,
            "marketing_status": (self.estado_comercializacion),
            "reimbursement_by_indication": [ind.to_dict() for ind in self.indicaciones],
            "retrieved_at": self.retrieved_at,
            "source": self.source,
        }
        if self.situacion_financiacion is not None:
            result["reimbursement_situation"] = self.situacion_financiacion
        if self.estado_nomenclator is not None:
            result["nomenclator_status"] = self.estado_nomenclator
        if self.aportacion_usuario is not None:
            result["patient_copayment"] = self.aportacion_usuario
        if self.fecha_alta_financiacion is not None:
            result["reimbursement_start_date"] = self.fecha_alta_financiacion
        if self.fecha_no_financiacion is not None:
            result["reimbursement_end_date"] = self.fecha_no_financiacion
        if self.indicaciones_financiadas_texto is not None:
            result["reimbursed_indications_text"] = self.indicaciones_financiadas_texto
        if self.indicaciones_no_financiadas_texto is not None:
            result["non_reimbursed_indications_text"] = self.indicaciones_no_financiadas_texto
        return result


def canonico(cn: str) -> str:
    """Forma comparable de un código nacional, para los dos lados de la comparación.

    Hace falta porque el índice se construye con lo que sale de un Excel a través de pandas, y ahí
    **el tipo de la columna depende de los datos**: una columna de enteros con una sola celda vacía
    llega como `float64`, así que el código se convierte en `'605851.0'` y no iguala al `'605851'`
    que da CIMA. La consecuencia no era un dato ausente sino uno falso: si ninguna presentación
    casaba, el enriquecedor escribía «ninguna presentación de esta ficha aparece en BIFIMED» en un
    JSON que se archiva y se cita, o sea la afirmación de no financiado que la PR #22 cerró.

    Se quedan solo los dígitos y se quitan los ceros por la izquierda, que es lo que también arregla
    el otro extremo: Excel se come el cero inicial de `007204` y CIMA no.
    """
    digitos = "".join(c for c in str(cn) if c.isdigit())
    return digitos.lstrip("0") or digitos


@dataclass
class MedicamentoFinanciacion:
    """Datos de financiación para todas las presentaciones, indexadas por código canónico."""

    principio_activo: str
    presentaciones: dict[str, PresentacionFinanciacion] = field(default_factory=dict)
    retrieved_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def get_by_cn(self, cn: str) -> PresentacionFinanciacion | None:
        return self.presentaciones.get(canonico(cn))

    def add_presentacion(self, pres: PresentacionFinanciacion) -> None:
        self.presentaciones[canonico(pres.cn)] = pres

    def to_dict(self) -> dict[str, Any]:
        return {
            "active_ingredient": self.principio_activo,
            "retrieved_at": self.retrieved_at,
            "presentations": [p.to_dict() for p in self.presentaciones.values()],
        }
