import pytest

from src.ui.templatetags.especialidades import especialidad_icono


@pytest.mark.parametrize(
    "nombre,icono",
    [
        ("Cardiología", "❤️"),
        ("Neurología", "🧠"),
        ("Neumología", "🫁"),
        ("Urología", "💧"),
        ("Dermatología", "🩹"),
        ("Pediatría", "🧒"),
        ("Ginecología y Obstetricia", "🤰"),
        ("Traumatología", "🦴"),
        ("Oftalmología", "👁️"),
        ("Endocrinología", "🦋"),
        ("Oncología", "🎗️"),
    ],
)
def test_reconoce_especialidades_conocidas(nombre: str, icono: str) -> None:
    assert especialidad_icono(nombre) == icono


def test_neurologia_no_se_confunde_con_urologia() -> None:
    assert especialidad_icono("Neurología") == "🧠"


def test_es_insensible_a_mayusculas_y_tildes() -> None:
    assert especialidad_icono("CARDIOLOGIA") == "❤️"


def test_desconocida_cae_al_estetoscopio() -> None:
    assert especialidad_icono("Medicina Interna") == "🩺"
