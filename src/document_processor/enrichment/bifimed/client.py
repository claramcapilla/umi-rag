"""
Cliente BIFIMED para obtener datos de financiación del
Ministerio de Sanidad de España.

Descarga exports Excel por principio activo y parsea la información de financiación, indexada
por código nacional. **Cada consulta cuesta un Excel completo**, así que se pregunta por
principio activo —que trae todas sus presentaciones de una vez— y el llamante se queda con los
códigos nacionales que le corresponden.
"""

import logging
import time
import zipfile
from collections.abc import Callable
from io import BytesIO
from typing import Any, Literal

import pandas as pd
import requests

from .errors import BifimedUnavailable
from .models import (
    IndicacionFinanciacion,
    MedicamentoFinanciacion,
    PresentacionFinanciacion,
)

logger = logging.getLogger(__name__)

MAX_EXCEL_BYTES = 32 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
CHUNK_BYTES = 65536
MAX_DOWNLOAD_SECONDS = 240


_HOJA_MEDICAMENTOS = "MEDICAMENTOS"
_HOJA_INDICACIONES = "INDICACIONES CENTRALIZADOS"


class BifimedClient:
    """Cliente para BIFIMED (Base de datos de medicamentos
    financiados)."""

    BASE_URL = "https://www.sanidad.gob.es/profesionales/medicamentos.do"

    def __init__(
        self,
        max_retries: int = 3,
        retry_delay: float = 2.0,
        timeout: int = 60,
    ):
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "docsrepo/0.1 (gestion documental clinica)"})

    def _request_with_retry(
        self,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> requests.Response:
        """Ejecuta request con reintentos."""
        kwargs.setdefault("timeout", self.timeout)

        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                resp = self.session.request(method, url, **kwargs)
                resp.raise_for_status()
                return resp
            except requests.RequestException as e:
                last_error = e
                logger.warning(f"BIFIMED request falló (intento {attempt + 1}): {e}")
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_delay * (attempt + 1))

        raise last_error or Exception("Máximo de reintentos alcanzado")

    def _abrir_sesion(self) -> None:
        """Toma la cookie de sesión. El cuerpo no se usa, así que no se descarga."""
        self._request_with_retry("GET", self.BASE_URL, stream=True).close()

    def _enviar_busqueda(self, payload: dict[str, str]) -> None:
        """Fija el criterio de búsqueda en la sesión. El cuerpo tampoco se usa."""
        self._request_with_retry(
            "POST",
            f"{self.BASE_URL}?metodo=buscarMedicamentos",
            data=payload,
            stream=True,
        ).close()

    def _download_excel(self) -> bytes:
        """Descarga el Excel de resultados BIFIMED.

        Acota la respuesta a `MAX_EXCEL_BYTES`: sin tope, un endpoint comprometido o un simple
        cambio en la web del Ministerio bastaría para agotar la memoria del worker. Y no sigue
        redirecciones, porque la URL es fija y no hay motivo para acabar en otro sitio.
        """
        response = self._request_with_retry(
            "GET",
            f"{self.BASE_URL}?metodo=descargar",
            stream=True,
            allow_redirects=False,
        )

        limite = time.monotonic() + MAX_DOWNLOAD_SECONDS
        trozos: list[bytes] = []
        total = 0
        for trozo in response.iter_content(chunk_size=CHUNK_BYTES):
            total += len(trozo)
            if total > MAX_EXCEL_BYTES:
                raise BifimedUnavailable(f"La descarga de BIFIMED supera {MAX_EXCEL_BYTES} bytes")
            if time.monotonic() > limite:
                raise BifimedUnavailable(f"La descarga de BIFIMED pasa de {MAX_DOWNLOAD_SECONDS}s")
            trozos.append(trozo)
        contenido = b"".join(trozos)

        if not _parece_excel(contenido):
            raise BifimedUnavailable(f"BIFIMED no devolvió un Excel ({total} bytes)")
        return contenido

    def search_by_principio_activo(
        self,
        principio_activo: str,
    ) -> bytes | None:
        """Busca en BIFIMED por principio activo y descarga
        el Excel."""
        if not principio_activo or not principio_activo.strip():
            return None

        self._abrir_sesion()

        payload = {
            "priact_uno": principio_activo.upper().strip(),
            "buscar": "Buscar",
        }
        self._enviar_busqueda(payload)

        return self._download_excel()

    def parse_excel(
        self,
        excel_bytes: bytes,
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Parsea las dos hojas del Excel de BIFIMED.

        El motor se elige por los bytes mágicos: `PK` es un zip, o sea `.xlsx` con `openpyxl`;
        cualquier otra cosa es el `.xls` OLE2 que sirve hoy el Ministerio, con `xlrd`. Los dos
        son dependencias de runtime: cuál hace falta lo decide el Ministerio, no nosotros.

        **La hoja de indicaciones no es accesoria: en los medicamentos de autorización
        centralizada es la que manda.** Medido contra el servicio: para epinefrina trae 1 fila
        frente a 89 de MEDICAMENTOS, pero para omalizumab son 36 frente a 10 y para dupilumab
        45 frente a 6. Ahí es donde consta qué indicación concreta está financiada y con qué
        restricciones, que en un biológico es toda la respuesta.

        Por eso se distingue **ausencia de fallo de lectura**, que antes se colapsaban en un
        DataFrame vacío: si la hoja no está en el libro es una ausencia legítima, pero si está y
        no se puede leer, eso es indisponibilidad y se dice. Colapsarlas hacía que
        `financiacion_indicaciones: []` significara indistintamente «no hay indicaciones
        financiadas» y «no pudimos leerlas» — la misma afirmación falsa que ya cerró la PR #22
        en el transporte y que aquí seguía abierta.
        """
        engine: Literal["openpyxl", "xlrd"] = "openpyxl" if excel_bytes[:2] == b"PK" else "xlrd"
        _rechazar_si_se_expande_demasiado(excel_bytes)

        try:
            libro = pd.ExcelFile(BytesIO(excel_bytes), engine=engine)
        except Exception as exc:
            raise BifimedUnavailable(f"No se pudo abrir el Excel de BIFIMED: {exc}") from exc

        try:
            df_medicamentos = libro.parse(_HOJA_MEDICAMENTOS)
        except Exception as exc:
            raise BifimedUnavailable(
                f"No se pudo parsear la hoja {_HOJA_MEDICAMENTOS}: {exc}"
            ) from exc

        if _HOJA_INDICACIONES not in libro.sheet_names:
            logger.info("El Excel de BIFIMED no trae la hoja %s", _HOJA_INDICACIONES)
            return df_medicamentos, pd.DataFrame()

        try:
            df_indicaciones = libro.parse(_HOJA_INDICACIONES)
        except Exception as exc:
            raise BifimedUnavailable(
                f"No se pudo parsear la hoja {_HOJA_INDICACIONES}: {exc}"
            ) from exc

        return df_medicamentos, df_indicaciones

    def build_financing_data(
        self,
        principio_activo: str,
        df_medicamentos: pd.DataFrame,
        df_indicaciones: pd.DataFrame,
    ) -> MedicamentoFinanciacion:
        """Construye datos de financiación estructurados."""
        result = MedicamentoFinanciacion(principio_activo=principio_activo)

        cn_to_pres: dict[str, PresentacionFinanciacion] = {}

        if not df_medicamentos.empty:
            for _, row in df_medicamentos.iterrows():
                cn = _codigo_nacional(self._clean_nan(row.get("CN")))
                if not cn:
                    continue

                nombre = (
                    self._clean_nan(
                        row.get("Nombre Presentación") or row.get("Nombre del medicamento")
                    )
                    or ""
                )
                laboratorio = self._clean_nan(
                    row.get("Laboratorio") or row.get("Laboratorio titular")
                )

                pres = PresentacionFinanciacion(
                    cn=cn,
                    nombre=nombre,
                    laboratorio=laboratorio,
                    estado_comercializacion=self._clean_nan(row.get("Estado Comercialización")),
                    situacion_financiacion=self._clean_nan(
                        row.get("Situacion de Financiación") or row.get("Situación de Financiación")
                    ),
                    estado_nomenclator=self._clean_nan(
                        row.get("Estado de Noménclator") or row.get("Estado Noménclator")
                    ),
                    aportacion_usuario=self._clean_nan(row.get("Aportación usuario")),
                    fecha_alta_financiacion=self._clean_nan(
                        row.get("Fecha de alta en la financiación")
                    ),
                    fecha_no_financiacion=self._clean_nan(
                        row.get("Fecha No Financiación/Exclusión")
                    ),
                    indicaciones_financiadas_texto=(
                        self._clean_nan(row.get("Indicaciones financiadas"))
                    ),
                    indicaciones_no_financiadas_texto=(
                        self._clean_nan(row.get("Indicaciones no financiadas"))
                    ),
                )
                cn_to_pres[cn] = pres

        if not df_indicaciones.empty:
            for _, row in df_indicaciones.iterrows():
                cn = _codigo_nacional(self._clean_nan(row.get("CN")))
                if not cn:
                    continue

                if cn not in cn_to_pres:
                    cn_to_pres[cn] = PresentacionFinanciacion(
                        cn=cn,
                        nombre="",
                    )

                indicacion = IndicacionFinanciacion(
                    indicacion_autorizada=(self._clean_nan(row.get("Indicación Autorizada")) or ""),
                    situacion_expediente=(
                        self._clean_nan(row.get("Situación expediente de financiación")) or ""
                    ),
                    resolucion=self._clean_nan(row.get("Resolución expediente de financiación")),
                    restricciones=self._clean_nan(row.get("Restricciones resolución")),
                )

                cn_to_pres[cn].indicaciones.append(indicacion)

        for pres in cn_to_pres.values():
            result.add_presentacion(pres)

        logger.info(f"Datos financiación para {principio_activo}: {len(cn_to_pres)} presentaciones")

        return result

    @staticmethod
    def _clean_nan(value: Any) -> str | None:
        if value is None:
            return None
        str_val = str(value).strip()
        if str_val.lower() == "nan" or str_val == "":
            return None
        return str_val

    def _get_financing(
        self,
        etiqueta: str,
        buscar: Callable[[], bytes | None],
    ) -> MedicamentoFinanciacion | None:
        """Buscar, parsear y estructurar.

        `None` significa una sola cosa —la consulta se completó y BIFIMED no tiene datos—, y
        cualquier otro desenlace sale como `BifimedUnavailable`. Es la invariante de la que
        depende que el JSON clínico no acabe afirmando que un medicamento no está financiado.
        """
        try:
            logger.info("Consultando BIFIMED por %s", etiqueta)
            excel_bytes = buscar()

            if excel_bytes is None:
                return None

            df_med, df_ind = self.parse_excel(excel_bytes)

            if df_med.empty and df_ind.empty:
                logger.warning("Sin datos BIFIMED para %s", etiqueta)
                return None

            datos = self.build_financing_data(etiqueta, df_med, df_ind)

            if not datos.presentaciones:
                raise BifimedUnavailable(
                    f"El export de BIFIMED para {etiqueta} trae filas pero ninguna con un CN "
                    "reconocible: probable cambio de cabeceras en el origen"
                )

            return datos

        except BifimedUnavailable:
            raise
        except Exception as exc:
            raise BifimedUnavailable(f"Fallo consultando BIFIMED por {etiqueta}: {exc}") from exc

    def get_financing_by_principio_activo(
        self,
        principio_activo: str,
    ) -> MedicamentoFinanciacion | None:
        """Obtiene datos de financiación buscando por principio activo."""
        if not principio_activo or not principio_activo.strip():
            return None

        return self._get_financing(
            principio_activo,
            lambda: self.search_by_principio_activo(principio_activo),
        )


def _codigo_nacional(valor: str | None) -> str | None:
    """Quita el `.0` que pandas le pega al código cuando la columna llega como `float64`.

    Ocurre en cuanto el Excel trae una celda de CN vacía: la columna deja de ser de enteros y cada
    código se convierte en `'605851.0'`. Lo que se archiva y se cita no puede llevar eso, y el
    índice se compara aparte en forma canónica (`bifimed/models.canonico`).
    """
    if valor is None:
        return None
    return valor[:-2] if valor.endswith(".0") else valor


def _parece_excel(contenido: bytes) -> bool:
    """Magic bytes de xlsx (zip) o de xls (OLE2)."""
    return contenido[:2] == b"PK" or contenido[:4] == b"\xd0\xcf\x11\xe0"


def _rechazar_si_se_expande_demasiado(excel_bytes: bytes) -> None:
    """Guarda contra zip bomb, que el tope de descarga no cubre.

    Un `.xlsx` es un zip: unos pocos KB pueden expandirse a gigabytes al parsearlos. El tope de
    bytes de la descarga no protege de eso, y `parse_excel` es público, así que puede recibir
    bytes que nunca pasaron por `_download_excel`.
    """
    if excel_bytes[:2] != b"PK":
        return
    try:
        with zipfile.ZipFile(BytesIO(excel_bytes)) as zf:
            total = sum(info.file_size for info in zf.infolist())
    except zipfile.BadZipFile as exc:
        raise BifimedUnavailable(f"El Excel de BIFIMED no es un zip válido: {exc}") from exc
    if total > MAX_UNCOMPRESSED_BYTES:
        raise BifimedUnavailable(
            f"El Excel de BIFIMED se expande a {total} bytes, por encima del límite"
        )
