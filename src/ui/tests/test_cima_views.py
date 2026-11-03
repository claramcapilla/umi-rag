"""Tests de la vista de fichas técnicas desde CIMA."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from django.contrib.messages import get_messages
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from document_processor.cima.errors import CimaUnavailable
from documents.models import (
    CimaRegistration,
    CimaSyncEvent,
    CimaSyncRun,
    DocType,
    EstadoSincronizacion,
    SourceDocument,
    Status,
    TipoEvento,
)
from library.models import Collection


def _mensajes(respuesta: Any) -> str:
    return " ".join(str(m) for m in get_messages(respuesta.wsgi_request))


RESPUESTA: dict[str, Any] = {
    "totalFilas": 2,
    "resultados": [
        {
            "nregistro": "75831",
            "nombre": "MONTELUKAST CINFA 10 mg COMPRIMIDOS",
            "labtitular": "Laboratorios Cinfa S.A.",
            "generico": True,
            "comerc": True,
            "docs": [{"tipo": 1, "secc": True}, {"tipo": 2, "secc": True}],
        },
        {
            "nregistro": "99999",
            "nombre": "VIEJO 5 mg COMPRIMIDOS",
            "labtitular": "Otro",
            "generico": False,
            "comerc": False,
            "docs": [{"tipo": 1, "secc": False}],
        },
    ],
}


@pytest.fixture(autouse=True)
def _logged_in(db: None, client: Client, django_user_model: Any) -> None:
    """Toda la UI exige sesión y `is_staff` (`StaffRequiredMiddleware`).

    El control de acceso se prueba aparte, con un cliente sin sesión.
    """
    user = django_user_model.objects.create_user(username="editora", password="x", is_staff=True)
    client.force_login(user)


@pytest.fixture
def buscar(monkeypatch: pytest.MonkeyPatch) -> Any:
    def _instalar(resultado: dict[str, Any] | Exception) -> list[dict[str, Any]]:
        criterios: list[dict[str, Any]] = []

        def _buscar(self: Any, criterio: dict[str, Any]) -> dict[str, Any]:
            criterios.append(criterio)
            if isinstance(resultado, Exception):
                raise resultado
            return resultado

        monkeypatch.setattr("document_processor.cima.CimaClient.buscar", _buscar)
        monkeypatch.setattr(
            "document_processor.cima.CimaClient.from_settings",
            classmethod(lambda cls: cls(base_url="https://cima.aemps.es/cima/rest")),
        )
        return criterios

    return _instalar


@pytest.mark.django_db
def test_sin_consulta_no_se_llama_al_registro(client: Client, buscar: Any) -> None:
    criterios = buscar(RESPUESTA)

    respuesta = client.get(reverse("ui:cima"))

    assert respuesta.status_code == 200
    assert criterios == []


@pytest.mark.django_db
def test_la_busqueda_por_principio_activo_lista_los_candidatos(client: Client, buscar: Any) -> None:
    criterios = buscar(RESPUESTA)

    respuesta = client.get(reverse("ui:cima"), {"q": "montelukast", "por": "practiv1"})

    assert criterios == [{"practiv1": "montelukast", "pagina": 1}]
    contenido = respuesta.content.decode()
    assert "MONTELUKAST CINFA 10 mg COMPRIMIDOS" in contenido
    assert "75831" in contenido


@pytest.mark.django_db
def test_una_ficha_sin_segmentar_se_marca_en_vez_de_ocultarse(client: Client, buscar: Any) -> None:
    """Si desapareciera de la lista, quien busca aprendería «no está» en vez de «no es ingerible».

    Y desde que se retiró el camino PDF+LLM esa ficha no tiene ninguna vía, así que la diferencia
    importa: lo que hay que hacer es distinto.
    """
    buscar(RESPUESTA)

    respuesta = client.get(reverse("ui:cima"), {"q": "montelukast"})

    contenido = respuesta.content.decode()
    assert "VIEJO 5 mg COMPRIMIDOS" in contenido
    assert "sin ficha segmentada en CIMA" in contenido
    assert 'value="99999"' not in contenido, "no se puede marcar lo que no es ingerible"


@pytest.mark.django_db
def test_un_registro_ya_ingerido_se_marca_como_tal(client: Client, buscar: Any) -> None:
    documento = SourceDocument.objects.create(sha256="a" * 64)
    CimaRegistration.objects.create(nregistro="75831", document=documento, content_hash="h")
    buscar(RESPUESTA)

    respuesta = client.get(reverse("ui:cima"), {"q": "montelukast"})

    contenido = respuesta.content.decode()
    assert "ya ingerida" in contenido
    assert 'value="75831"' not in contenido


@pytest.mark.django_db
def test_un_registro_ingerido_con_fallo_no_se_anuncia_como_hecho(
    client: Client, buscar: Any
) -> None:
    """Sin esto, un documento que acabó `FAILED` sin un solo chunk se pintaba como «ya ingerida».

    Y reintentarlo desde aquí no arregla nada: la ingesta es idempotente y devuelve el mismo
    documento fallido. Lo honesto es decir que falló y mandar a la reconciliación.
    """
    from documents.models import Status

    documento = SourceDocument.objects.create(sha256="a" * 64, status=Status.FAILED)
    CimaRegistration.objects.create(nregistro="75831", document=documento, content_hash="h")
    buscar(RESPUESTA)

    contenido = client.get(reverse("ui:cima"), {"q": "montelukast"}).content.decode()

    assert "ingerida con fallo" in contenido
    assert "ya ingerida" not in contenido


@pytest.mark.django_db
def test_el_reintento_masivo_no_reconvierte_las_fichas_retiradas(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cada reintento arrancaba la JVM y la cascada de reparación para acabar en la misma excepción.

    Con ello el recuento de fallidos no podía bajar nunca y nada distinguía esas filas de un fallo
    transitorio de verdad.
    """
    from documents.models import Stage, Status
    from pipeline.tasks import process_document_task

    encolados: list[str] = []
    monkeypatch.setattr(process_document_task, "delay", lambda pk: encolados.append(pk))
    SourceDocument.objects.create(
        sha256="b" * 64,
        status=Status.FAILED,
        error_stage=Stage.PARSE,
        error_msg="FichaTecnicaNoIngerible: se ingiere desde CIMA por número de registro",
    )
    SourceDocument.objects.create(
        sha256="c" * 64,
        status=Status.FAILED,
        error_stage=Stage.PARSE,
        error_msg="ConversionError: PDF corrupto",
    )

    respuesta = client.post(reverse("ui:retry_failed"))

    assert encolados == [str(SourceDocument.objects.get(sha256="c" * 64).pk)]
    assert "no se reintentan" in _mensajes(respuesta)


@pytest.mark.django_db
def test_un_criterio_inventado_cae_al_principio_activo(client: Client, buscar: Any) -> None:
    """El criterio viene de la URL: es entrada no confiable y no puede llegar tal cual al
    registro."""
    criterios = buscar(RESPUESTA)

    client.get(reverse("ui:cima"), {"q": "x", "por": "inventado"})

    assert criterios == [{"practiv1": "x", "pagina": 1}]


@pytest.mark.django_db
def test_la_pagina_pedida_llega_al_registro_y_se_ofrecen_las_dos_vecinas(
    client: Client, buscar: Any
) -> None:
    """Sin esto el cambio central de la paginación no tenía ningún test de comportamiento.

    Con `totalFilas: 2` y dos candidatos, `1*2 < 2` es falso siempre: los enlaces no se renderizaban
    en ningún test, así que cablear de vuelta `"pagina": 1` o romper los `add` dejaba la suite verde
    y devolvía el bug de los 218 registros escondidos.
    """
    criterios = buscar(
        {"totalFilas": 418, "tamanioPagina": 2, "resultados": RESPUESTA["resultados"]}
    )

    respuesta = client.get(reverse("ui:cima"), {"q": "paracetamol", "pagina": "3"})

    assert criterios == [{"practiv1": "paracetamol", "pagina": 3}]
    contenido = respuesta.content.decode()
    assert "de 418 registros" in contenido
    assert "pagina=2" in contenido, "la anterior"
    assert "pagina=4" in contenido, "la siguiente"


@pytest.mark.django_db
def test_sin_total_declarado_no_se_afirma_un_total(client: Client, buscar: Any) -> None:
    """`totalFilas` ausente no puede caer a los de esta página: eso afirma un total falso.

    Es el fallo que traía el arreglo: con el defecto a `len(candidatos)`, una consulta de 418
    resultados decía «200 de 200 registros» y no ofrecía siguiente, o sea el bug original **con un
    segundo número corroborándolo**.
    """
    buscar({"tamanioPagina": 2, "resultados": RESPUESTA["resultados"]})

    contenido = client.get(reverse("ui:cima"), {"q": "paracetamol"}).content.decode()

    assert "2 registros" in contenido
    assert "no declaró cuántos hay en total" in contenido
    assert "de 2 registros" not in contenido
    assert "siguiente" in contenido, "la página vino llena, así que puede haber más"


@pytest.mark.django_db
def test_una_pagina_pasada_del_final_no_dice_sin_resultados(client: Client, buscar: Any) -> None:
    """Decir «Sin resultados para paracetamol» de una consulta con 418 es afirmar algo falso.

    Y con los controles dentro del `{% if candidatos %}`, la página quedaba además sin ningún enlace
    de vuelta: la única salida era volver a buscar.
    """
    buscar({"totalFilas": 418, "resultados": []})

    contenido = client.get(
        reverse("ui:cima"), {"q": "paracetamol", "pagina": "50"}
    ).content.decode()

    assert "Sin resultados" not in contenido
    assert "pasada del final" in contenido
    assert "pagina=49" in contenido, "tiene que dejar volver"
    assert "siguiente" not in contenido, "no hay página siguiente de una vacía"


@pytest.mark.django_db
def test_sin_tamanio_de_pagina_no_se_esconden_resultados(client: Client, buscar: Any) -> None:
    """El defecto de `tamanioPagina` es lo servido, no cero: con 418 declarados, hay siguiente."""
    buscar({"totalFilas": 418, "resultados": RESPUESTA["resultados"]})

    contenido = client.get(reverse("ui:cima"), {"q": "paracetamol"}).content.decode()

    assert "pagina=2" in contenido


@pytest.mark.django_db
def test_si_cima_no_responde_se_dice_sin_romper_la_pagina(client: Client, buscar: Any) -> None:
    buscar(CimaUnavailable("503 del registro"))

    respuesta = client.get(reverse("ui:cima"), {"q": "montelukast"})

    assert respuesta.status_code == 200
    assert "CIMA no respondió" in respuesta.content.decode()
    assert "503" not in respuesta.content.decode(), "el detalle del fallo va al log, no a la página"


@pytest.fixture
def encolar(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    from pipeline.tasks import ingest_ficha_task

    encolados: list[tuple[str, str]] = []
    monkeypatch.setattr(ingest_ficha_task, "delay", lambda n, e: encolados.append((n, e)))
    return encolados


@pytest.fixture
def alergologia(db: None) -> Collection:
    return Collection.objects.create(name="Alergología", is_specialty_root=True)


@pytest.mark.django_db
def test_el_selector_solo_ofrece_especialidades(
    client: Client, buscar: Any, alergologia: Collection
) -> None:
    """Ofrecer una raíz sin la marca sería ofrecer un valor que el servidor va a rechazar.

    Y peor: si el servidor no lo rechazara, el documento quedaría sin especialidad que resolver y
    moriría en el indexado.
    """
    Collection.objects.create(name="Carpeta suelta", parent=None, is_specialty_root=False)
    Collection.objects.create(name="Rinitis", parent=alergologia)
    buscar(RESPUESTA)

    contenido = client.get(reverse("ui:cima"), {"q": "montelukast"}).content.decode()

    assert "Alergología" in contenido
    assert "Carpeta suelta" not in contenido
    assert "Rinitis" not in contenido


@pytest.mark.django_db
def test_ingerir_encola_los_marcados_con_su_especialidad(
    client: Client, encolar: list[tuple[str, str]], alergologia: Collection
) -> None:
    respuesta = client.post(
        reverse("ui:cima_ingest"),
        {
            "nregistro": ["75831", "74195", "75831"],
            "especialidad": str(alergologia.pk),
            "q": "montelukast",
            "por": "practiv1",
            "pagina": "3",
        },
    )

    assert encolar == [("75831", str(alergologia.pk)), ("74195", str(alergologia.pk))], (
        "el repetido se encola una vez"
    )
    assert respuesta.status_code == 302
    assert "q=montelukast" in respuesta["Location"]
    assert "pagina=3" in respuesta["Location"], (
        "sin la página se vuelve a la 1, con otros 200 registros y sin ver las marcas ganadas"
    )
    assert "encoladas" not in respuesta["Location"], "el mensaje no viaja por la URL"


@pytest.mark.django_db
def test_una_pagina_con_mala_forma_en_el_formulario_no_llega_a_la_url(
    client: Client, encolar: list[tuple[str, str]], alergologia: Collection
) -> None:
    """La página viene de un formulario, o sea entrada no confiable, igual que el resto."""
    respuesta = client.post(
        reverse("ui:cima_ingest"),
        {
            "nregistro": ["75831"],
            "especialidad": str(alergologia.pk),
            "q": "montelukast",
            "pagina": "-3; DROP",
        },
    )

    assert "pagina=1" in respuesta["Location"]


@pytest.mark.django_db
def test_sin_especialidad_no_se_encola_nada(client: Client, encolar: list[tuple[str, str]]) -> None:
    """El indexado deriva la especialidad de la colección: sin ella la ficha muere en `INDEX`.

    Y moriría después de gastar todas las peticiones al registro, que es el peor momento.
    """
    respuesta = client.post(reverse("ui:cima_ingest"), {"nregistro": ["75831"]})

    assert encolar == []
    assert "Elige la especialidad" in _mensajes(respuesta)


@pytest.mark.django_db
def test_una_subespecialidad_no_vale_como_destino(
    client: Client, encolar: list[tuple[str, str]], alergologia: Collection
) -> None:
    hija = Collection.objects.create(name="Rinitis", parent=alergologia)

    client.post(reverse("ui:cima_ingest"), {"nregistro": ["75831"], "especialidad": str(hija.pk)})

    assert encolar == []


@pytest.mark.django_db
@pytest.mark.parametrize(
    "malo",
    ["../../etc/passwd", "75831 ", "a" * 33, "75831/74195", "", "75831\n"],
)
def test_un_numero_de_registro_con_mala_forma_no_se_encola(
    client: Client, encolar: list[tuple[str, str]], alergologia: Collection, malo: str
) -> None:
    """Es la clave del registro, de 32 caracteres, y va en las peticiones a CIMA.

    Basura ahí es un fallo en el worker que el usuario no ve, mientras la página le dice que se
    encoló. El path traversal lo corta Django, pero como excepción sin clasificar.
    """
    respuesta = client.post(
        reverse("ui:cima_ingest"),
        {"nregistro": [malo], "especialidad": str(alergologia.pk)},
    )

    assert encolar == []
    assert "Ningún número de registro válido" in _mensajes(respuesta)


@pytest.mark.django_db
def test_los_numeros_de_importacion_paralela_se_encolan(
    client: Client, encolar: list[tuple[str, str]], alergologia: Collection
) -> None:
    paralelos = ["11003-29-03-1999IP1", "BE 174115", "LT-1-98-3805-025_IP"]

    client.post(
        reverse("ui:cima_ingest"),
        {"nregistro": paralelos, "especialidad": str(alergologia.pk)},
    )

    assert encolar == [(n, str(alergologia.pk)) for n in paralelos]


@pytest.mark.django_db
def test_el_lote_esta_acotado(
    client: Client, encolar: list[tuple[str, str]], alergologia: Collection
) -> None:
    """Cada ficha admite hasta 300 s de pared y decenas de peticiones, en una cola única.

    Un formulario con mil casillas bloquea la ingesta entera durante horas y convierte el
    despliegue en un amplificador contra la Administración.
    """
    from ui.views import MAX_FICHAS_POR_LOTE

    respuesta = client.post(
        reverse("ui:cima_ingest"),
        {
            "nregistro": [str(100000 + i) for i in range(MAX_FICHAS_POR_LOTE + 1)],
            "especialidad": str(alergologia.pk),
        },
    )

    assert encolar == []
    assert f"Máximo {MAX_FICHAS_POR_LOTE} fichas" in _mensajes(respuesta)


@pytest.mark.django_db
def test_el_tope_del_lote_se_cumple_justo_en_el_limite(
    client: Client, encolar: list[tuple[str, str]], alergologia: Collection
) -> None:
    from ui.views import MAX_FICHAS_POR_LOTE

    client.post(
        reverse("ui:cima_ingest"),
        {
            "nregistro": [str(100000 + i) for i in range(MAX_FICHAS_POR_LOTE)],
            "especialidad": str(alergologia.pk),
        },
    )

    assert len(encolar) == MAX_FICHAS_POR_LOTE


@pytest.mark.django_db
def test_ingerir_por_get_no_encola_nada(client: Client) -> None:
    respuesta = client.get(reverse("ui:cima_ingest"))

    assert respuesta.status_code == 302
    assert respuesta["Location"] == reverse("ui:cima")


@pytest.mark.django_db
def test_la_pagina_exige_sesion_de_staff() -> None:
    """La ingesta gasta peticiones contra la Administración y escribe en el corpus."""
    respuesta = Client().get(reverse("ui:cima"))

    assert respuesta.status_code == 302


@pytest.mark.django_db
def test_encolar_la_ingesta_exige_sesion_de_staff(encolar: list[tuple[str, str]]) -> None:
    respuesta = Client().post(reverse("ui:cima_ingest"), {"nregistro": ["75831"]})

    assert respuesta.status_code == 302
    assert encolar == [], "un anónimo no puede disparar tráfico contra el registro"


def _aviso(tipo: str = TipoEvento.ALTA, **campos: Any) -> CimaSyncEvent:
    run = CimaSyncRun.objects.first() or CimaSyncRun.objects.create(
        estado=EstadoSincronizacion.TERMINADA
    )
    return CimaSyncEvent.objects.create(run=run, nregistro="75831", tipo=tipo, **campos)


def test_el_dashboard_avisa_de_lo_anadido_y_lo_retirado_sin_ver(client: Client) -> None:
    documento = SourceDocument.objects.create(sha256="a" * 64, title="MONTELUKAST 10 mg")
    _aviso(TipoEvento.ALTA, nombre="MONTELUKAST 10 mg", document=documento)
    _aviso(TipoEvento.RETIRADA, nombre="VIEJO 5 mg", detalle="Autorización revocada")
    _aviso(TipoEvento.ACTUALIZADA, nombre="SOLO SE REGISTRA")
    _aviso(TipoEvento.ALTA, nombre="YA VISTO", visto_en=timezone.now())

    html = client.get(reverse("ui:dashboard")).content.decode()

    assert "Cambios en CIMA" in html
    assert "MONTELUKAST 10 mg" in html and reverse("ui:detail", args=[documento.pk]) in html
    assert "VIEJO 5 mg" in html and "Autorización revocada" in html
    assert "SOLO SE REGISTRA" not in html
    assert "YA VISTO" not in html
    assert "Marcar los 2 como vistos" in html


def test_el_dashboard_sin_sincronizaciones_no_muestra_el_panel(client: Client) -> None:
    assert "Cambios en CIMA" not in client.get(reverse("ui:dashboard")).content.decode()


def test_el_dashboard_acota_los_avisos_que_pinta(client: Client, settings: Any) -> None:
    from ui import views

    for _ in range(3):
        _aviso()
    views_max = views.MAX_AVISOS_CIMA
    try:
        views.MAX_AVISOS_CIMA = 2
        html = client.get(reverse("ui:dashboard")).content.decode()
    finally:
        views.MAX_AVISOS_CIMA = views_max

    assert "Se muestran 2 de 3." in html


def test_marcar_avisos_como_vistos_guarda_quien_y_cuando(
    client: Client, django_user_model: Any
) -> None:
    elegido = _aviso()
    otro = _aviso(TipoEvento.RETIRADA)

    respuesta = client.post(reverse("ui:cima_sync_visto"), {"evento": [str(elegido.pk), "x"]})

    elegido.refresh_from_db()
    otro.refresh_from_db()
    assert respuesta.status_code == 302
    assert elegido.visto_en is not None
    assert elegido.visto_por == django_user_model.objects.get(username="editora")
    assert otro.visto_en is None
    assert "1 aviso(s)" in _mensajes(respuesta)


def test_marcar_todos_no_toca_los_eventos_que_no_avisan(client: Client) -> None:
    alta = _aviso()
    error = _aviso(TipoEvento.ERROR)

    client.post(reverse("ui:cima_sync_visto"), {"todos": "1"})

    alta.refresh_from_db()
    error.refresh_from_db()
    assert alta.visto_en is not None
    assert error.visto_en is None


def test_marcar_como_visto_por_get_no_cambia_nada(client: Client) -> None:
    aviso = _aviso()

    respuesta = client.get(reverse("ui:cima_sync_visto"))

    aviso.refresh_from_db()
    assert respuesta.status_code == 302
    assert aviso.visto_en is None


def test_la_pagina_de_sincronizacion_muestra_la_ultima_ejecucion(client: Client) -> None:
    anterior = CimaSyncRun.objects.create(
        estado=EstadoSincronizacion.FALLIDA, error_msg="CimaUnavailable: caído"
    )
    CimaSyncEvent.objects.create(run=anterior, nregistro="11111", tipo=TipoEvento.ERROR)
    ultima = CimaSyncRun.objects.create(
        estado=EstadoSincronizacion.TERMINADA, desde=date(2026, 9, 20)
    )
    CimaSyncEvent.objects.create(
        run=ultima, nregistro="75831", nombre="MONTE", tipo=TipoEvento.ALTA
    )

    html = client.get(reverse("ui:cima_sync")).content.decode()

    assert "Cambios desde el 20/09/2026" in html
    assert "MONTE" in html and "75831" in html
    assert "11111" not in html
    assert "Añadido: 1" in html


def test_la_pagina_de_sincronizacion_abre_la_ejecucion_elegida(client: Client) -> None:
    elegida = CimaSyncRun.objects.create(
        estado=EstadoSincronizacion.FALLIDA, carga_inicial=True, error_msg="caído"
    )
    CimaSyncRun.objects.create(estado=EstadoSincronizacion.TERMINADA)

    html = client.get(reverse("ui:cima_sync"), {"run": elegida.pk}).content.decode()

    assert "Carga inicial" in html
    assert "caído" in html
    assert "Sin cambios que registrar" in html


def test_la_pagina_de_sincronizacion_da_404_con_una_ejecucion_que_no_existe(
    client: Client,
) -> None:
    assert client.get(reverse("ui:cima_sync"), {"run": "nada"}).status_code == 404


def test_la_pagina_de_sincronizacion_sin_ejecuciones(client: Client) -> None:
    assert "nunca" in client.get(reverse("ui:cima_sync")).content.decode()


def test_la_pagina_de_sincronizacion_acota_los_eventos(client: Client) -> None:
    from ui import views

    for _ in range(3):
        _aviso()
    maximo = views.MAX_EVENTOS_CIMA
    try:
        views.MAX_EVENTOS_CIMA = 2
        html = client.get(reverse("ui:cima_sync")).content.decode()
    finally:
        views.MAX_EVENTOS_CIMA = maximo

    assert "Se muestran 2 de 3." in html


def test_el_dashboard_avisa_de_fichas_que_no_han_salido_de_chroma_y_de_errores(
    client: Client,
) -> None:
    SourceDocument.objects.create(
        sha256="b" * 64, doc_type=DocType.FICHA_TECNICA, status=Status.INDEXED
    )
    _aviso(TipoEvento.ERROR)

    html = client.get(reverse("ui:dashboard")).content.decode()

    assert "a la espera de salir" in html
    assert "con error sin resolver" in html
