"""Cliente del registro de medicamentos de la AEMPS (CIMA).

CIMA es la **fuente** de las fichas técnicas: sirve la ficha ya segmentada por su numeración
normativa y los metadatos del medicamento de forma estructurada, así que no hay que reconstruir
con un LLM lo que el registro oficial ya da exacto.

Aquí no sale texto del corpus, solo entra, pero el destino se valida igual: de este origen se
archiva e indexa contenido que luego se cita como autoritativo.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date
from typing import Any, cast

import httpx
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from .errors import CimaUnavailable

logger = logging.getLogger(__name__)

_CARACTER_DE_NREGISTRO = (
    r"[^\s/\\\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff\ud800-\udfff]"
)
NREGISTRO = re.compile(
    rf"{_CARACTER_DE_NREGISTRO}(?:(?:{_CARACTER_DE_NREGISTRO}| ){{0,30}}{_CARACTER_DE_NREGISTRO})?"
)


def get_cima_base_url() -> str:
    """Base de CIMA, validada contra `CIMA_ALLOWED_ENDPOINTS`.

    Copia deliberada de la guarda de `llm_config.get_llm_base_url`, incluidas sus dos razones: el
    `list(...)` evita que una setting que llegue como cadena —y cadena es lo que se escribe en el
    `.env`— convierta la comprobación en una comparación de subcadena, y la igualdad es exacta
    porque comparar por prefijo autorizaría `https://cima.aemps.es.de-un-tercero.example`.
    """
    url: str = settings.CIMA_BASE_URL
    if not url:
        raise ImproperlyConfigured("CIMA_BASE_URL no está definida y no tiene valor por defecto.")
    allowed: list[str] = list(settings.CIMA_ALLOWED_ENDPOINTS)
    if url not in allowed:
        raise ImproperlyConfigured(
            f"CIMA_BASE_URL={url!r} no está en CIMA_ALLOWED_ENDPOINTS ({allowed!r}). "
            "Añádelo explícitamente si de verdad quieres ingerir fichas de ahí."
        )
    logger.info("CIMA endpoint en uso: %s", url)
    return url


class CimaClient:
    def __init__(
        self,
        base_url: str,
        client: httpx.Client | None = None,
        max_bytes: int | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._max_bytes = max_bytes if max_bytes is not None else settings.CIMA_MAX_BYTES
        self._client = client or httpx.Client(
            timeout=float(settings.CIMA_TIMEOUT),
            trust_env=False,
            headers={"User-Agent": settings.CIMA_USER_AGENT},
        )

    @classmethod
    def from_settings(cls, client: httpx.Client | None = None) -> CimaClient:
        """Construye el cliente desde `settings`, validando el destino.

        La validación va aquí, y no en `__init__`, para que sea la costura que lee la
        configuración la que falle cerrada, igual que en `EmbeddingClient.from_settings`.
        """
        return cls(base_url=get_cima_base_url(), client=client)

    def _leer(
        self,
        url: str,
        tope: int,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> bytes:
        """GET con tope de bytes, sin seguir redirecciones y sin leer el cuerpo de golpe.

        El tope se comprueba mientras se acumula: sin eso, un origen comprometido o un simple
        cambio en el registro bastaría para agotar la memoria del worker, que es el mismo y único
        que procesa toda la cola.

        Se exige **200 exacto** en vez de `raise_for_status`, que solo lanza a partir del 400: con
        él, un 3xx sin seguir dejaba un cuerpo vacío que pasaba por una respuesta legítima.
        """
        try:
            with self._client.stream(
                "GET", url, params=params, headers=headers, follow_redirects=False
            ) as resp:
                if resp.status_code != httpx.codes.OK:
                    raise CimaUnavailable(f"CIMA respondió {resp.status_code} a {url}")
                trozos: list[bytes] = []
                total = 0
                for trozo in resp.iter_bytes():
                    total += len(trozo)
                    if total > tope:
                        raise CimaUnavailable(f"La respuesta de {url} supera el tope de {tope} B")
                    trozos.append(trozo)
        except httpx.HTTPError as exc:
            raise CimaUnavailable(f"Error consultando {url}: {type(exc).__name__}") from exc
        return b"".join(trozos)

    def _json(
        self, ruta: str, params: dict[str, Any], headers: dict[str, str] | None = None
    ) -> Any:
        """JSON de una ruta, con cualquier cuerpo indescifrable como indisponibilidad.

        Se captura `ValueError` y no `json.JSONDecodeError` porque son **dos** los decodificadores
        que corren aquí: `json.loads` sobre bytes los decodifica como UTF-8 primero, y un cuerpo
        que no lo sea —una página de error en latin-1, o uno cortado dentro de un multibyte, y el
        texto de CIMA está lleno de eñes y acentos— lanza `UnicodeDecodeError`, que **no** es
        subclase de `JSONDecodeError`. Escapaba de aquí, de la guarda de tipo y de los `except` de
        las dos vistas, para salir como 500. Las dos sí son `ValueError`.
        """
        crudo = self._leer(f"{self._base_url}/{ruta}", self._max_bytes, params, headers)
        try:
            return json.loads(crudo)
        except ValueError as exc:
            raise CimaUnavailable(
                f"CIMA devolvió algo que no es JSON en {ruta}: {type(exc).__name__}: {exc}"
            ) from exc

    def _objeto(self, ruta: str, params: dict[str, Any]) -> dict[str, Any]:
        """JSON que **tiene que ser** un objeto, comprobado y no supuesto.

        Con un `cast` a secas, un día que el registro devolviera una lista o un escalar el
        `AttributeError` salía como 500 en la vista de búsqueda en vez de como la indisponibilidad
        que es.
        """
        datos = self._json(ruta, params)
        if not isinstance(datos, dict):
            raise CimaUnavailable(f"CIMA no devolvió un objeto en {ruta}: {type(datos).__name__}")
        return cast("dict[str, Any]", datos)

    def _lista(
        self, ruta: str, params: dict[str, Any], headers: dict[str, str] | None = None
    ) -> list[dict[str, Any]]:
        datos = self._json(ruta, params, headers)
        if not isinstance(datos, list):
            raise CimaUnavailable(f"CIMA no devolvió una lista en {ruta}: {type(datos).__name__}")
        return cast("list[dict[str, Any]]", datos)

    def buscar(self, criterio: dict[str, Any]) -> dict[str, Any]:
        """Busca medicamentos. El criterio va tal cual: `nombre`, `practiv1`, `pagina`…

        Ojo, y es una trampa medida: la respuesta de búsqueda **no incluye `presentaciones`**, así
        que de aquí no se pueden sacar los códigos nacionales. Para eso está `medicamento`.
        """
        return self._objeto("medicamentos", criterio)

    def medicamento(self, nregistro: str) -> dict[str, Any]:
        """Detalle de un medicamento, que es la única fuente de sus códigos nacionales.

        Construir el conjunto de códigos desde la búsqueda daría cero para todo medicamento de
        autorización centralizada, y entonces el filtro de BIFIMED descartaría justo sus
        indicaciones financiadas: medido, 45 filas en dupilumab y 36 en omalizumab.
        """
        return self._objeto("medicamento", {"nregistro": nregistro})

    def ficha(self, nregistro: str) -> list[dict[str, Any]]:
        return self._lista(
            "docSegmentado/contenido/1",
            {"nregistro": nregistro},
            {"Accept": "application/json"},
        )

    def cambios(self, desde: date, pagina: int) -> dict[str, Any]:
        return self._objeto(
            "registroCambios", {"fecha": desde.strftime("%d/%m/%Y"), "pagina": pagina}
        )
