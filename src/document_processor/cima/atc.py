"""Clasificación de una ficha por su código ATC: grupo anatómico y especialidad.

El código ATC (Anatomical Therapeutic Chemical, de la OMS) es la clasificación estándar de un
medicamento por área terapéutica, y CIMA lo sirve en el detalle del medicamento. De él salen aquí
dos cosas, a granularidades distintas y a propósito:

- **El grupo anatómico principal** (`main_groups`), su primer carácter —catorce grupos en total—,
  con el que se navega la ventana de fichas: ancho, estable y sin curación a mano.
- **La especialidad médica** (`specialties_for`), su subgrupo terapéutico —los tres primeros
  caracteres, p. ej. `C09`—, que es la granularidad con la que el puente casa con las especialidades
  generales: la letra sola metería toda la digestivo-metabolismo (`A`) en gastro y en endocrino a la
  vez, mientras que `A10` (diabetes) va a endocrino y `A02` (antiácidos) a gastro. El mapa se cura a
  mano contra el árbol de la OMS y **nombra las especialidades por su nombre exacto**: un subgrupo
  sin especialidad asignada, o cuyo nombre este despliegue no tenga, deja la ficha solo en su
  ventana, que es la conducta previa al puente.

Un medicamento puede tener varios códigos ATC, así que ambas clasificaciones son listas sin repetir.
"""

from __future__ import annotations

ATC_GROUP_LABELS: dict[str, str] = {
    "A": "Aparato digestivo y metabolismo",
    "B": "Sangre y órganos hematopoyéticos",
    "C": "Aparato cardiovascular",
    "D": "Dermatológicos",
    "G": "Aparato genitourinario y hormonas sexuales",
    "H": "Preparados hormonales sistémicos, excl. sexuales",
    "J": "Antiinfecciosos para uso sistémico",
    "L": "Antineoplásicos e inmunomoduladores",
    "M": "Aparato locomotor",
    "N": "Sistema nervioso",
    "P": "Antiparasitarios, insecticidas y repelentes",
    "R": "Aparato respiratorio",
    "S": "Órganos de los sentidos",
    "V": "Varios",
}


def main_groups(atc_codes: list[str] | None) -> list[str]:
    """Grupos ATC principales presentes en una lista de códigos ATC, en orden de aparición.

    Toma la primera letra de cada código y se queda con las de un grupo reconocido, sin repetir.
    Un código vacío o de una letra desconocida se ignora en vez de inventar un grupo: la fuente es
    CIMA y el mapa cubre los catorce grupos, así que lo que no encaje es dato roto, no un grupo
    nuevo.
    """
    grupos: list[str] = []
    for code in atc_codes or []:
        if not code:
            continue
        letra = str(code)[0].upper()
        if letra in ATC_GROUP_LABELS and letra not in grupos:
            grupos.append(letra)
    return grupos


ATC_SPECIALTIES: dict[str, tuple[str, ...]] = {
    "A01": ("Odontología y cirugía maxilofacial",),
    "A02": ("Gastroenterología",),
    "A03": ("Gastroenterología",),
    "A04": ("Gastroenterología",),
    "A05": ("Gastroenterología",),
    "A06": ("Gastroenterología",),
    "A07": ("Gastroenterología",),
    "A08": ("Endocrinología", "Nutrición clínica"),
    "A09": ("Gastroenterología",),
    "A10": ("Endocrinología",),
    "A11": ("Nutrición clínica",),
    "A12": ("Nutrición clínica",),
    "A13": ("Nutrición clínica",),
    "A14": ("Endocrinología",),
    "A15": ("Nutrición clínica",),
    "A16": ("Endocrinología", "Enfermedades raras"),
    "B01": ("Hemoterapia, hemostasia y trombosis",),
    "B02": ("Hemoterapia, hemostasia y trombosis",),
    "B03": ("Hemoterapia, hemostasia y trombosis",),
    "B05": ("Hemoterapia, hemostasia y trombosis", "Nutrición clínica"),
    "B06": ("Hemoterapia, hemostasia y trombosis",),
    "C01": ("Cardiología",),
    "C02": ("Cardiología",),
    "C03": ("Cardiología", "Nefrología"),
    "C04": ("Cirugía vascular",),
    "C05": ("Cirugía vascular",),
    "C07": ("Cardiología",),
    "C08": ("Cardiología",),
    "C09": ("Cardiología",),
    "C10": ("Cardiología", "Endocrinología"),
    "D01": ("Dermatología",),
    "D02": ("Dermatología",),
    "D03": ("Dermatología", "Quemaduras"),
    "D04": ("Dermatología",),
    "D05": ("Dermatología",),
    "D06": ("Dermatología",),
    "D07": ("Dermatología",),
    "D08": ("Dermatología",),
    "D09": ("Dermatología", "Quemaduras"),
    "D10": ("Dermatología",),
    "D11": ("Dermatología",),
    "G01": ("Ginecología y obstetricia",),
    "G02": ("Ginecología y obstetricia",),
    "G03": ("Ginecología y obstetricia", "Endocrinología"),
    "G04": ("Urología",),
    "H01": ("Endocrinología",),
    "H02": ("Endocrinología",),
    "H03": ("Endocrinología",),
    "H04": ("Endocrinología",),
    "H05": ("Endocrinología",),
    "J01": ("Infectología",),
    "J02": ("Infectología",),
    "J04": ("Infectología",),
    "J05": ("Infectología",),
    "J06": ("Infectología", "Inmunología"),
    "J07": ("Medicina preventiva y salud pública", "Infectología"),
    "L01": ("Oncología médica",),
    "L02": ("Oncología médica",),
    "L03": ("Inmunología",),
    "L04": ("Inmunología", "Reumatología"),
    "M01": ("Reumatología",),
    "M02": ("Traumatología",),
    "M03": ("Anestesiología",),
    "M04": ("Reumatología",),
    "M05": ("Reumatología", "Endocrinología"),
    "M09": ("Reumatología", "Enfermedades raras"),
    "N01": ("Anestesiología",),
    "N02": ("Dolor", "Neurología"),
    "N03": ("Neurología",),
    "N04": ("Neurología",),
    "N05": ("Psiquiatría",),
    "N06": ("Psiquiatría", "Neurología"),
    "N07": ("Neurología",),
    "P01": ("Infectología",),
    "P02": ("Infectología",),
    "P03": ("Infectología", "Dermatología"),
    "R01": ("Otorrinolaringología",),
    "R02": ("Otorrinolaringología",),
    "R03": ("Neumología",),
    "R05": ("Neumología",),
    "R06": ("Alergología",),
    "R07": ("Neumología",),
    "S01": ("Oftalmología",),
    "S02": ("Otorrinolaringología",),
    "S03": ("Oftalmología", "Otorrinolaringología"),
    "V01": ("Alergología",),
    "V03": ("Toxicología",),
    "V06": ("Nutrición clínica",),
    "V08": ("Radiología",),
    "V09": ("Medicina nuclear",),
    "V10": ("Medicina nuclear",),
}
"""Subgrupo terapéutico ATC (tres caracteres) → especialidades que lo tratan.

Curado a mano contra el árbol de la OMS. Nombra las especialidades por su nombre exacto en el árbol
de este repositorio; ver el docstring del módulo para por qué es el subgrupo y no la letra, y qué
pasa con lo que no está en el mapa.
"""


def specialties_for(atc_codes: list[str] | None) -> list[str]:
    """Especialidades a las que pertenece un medicamento por sus códigos ATC, sin repetir.

    Cada código aporta las especialidades de su subgrupo terapéutico (sus tres primeros caracteres);
    la unión sale en orden de aparición. Un código más corto que el subgrupo, o de un subgrupo sin
    especialidad asignada, no aporta nada.
    """
    nombres: list[str] = []
    for code in atc_codes or []:
        for nombre in ATC_SPECIALTIES.get(str(code)[:3], ()):
            if nombre not in nombres:
                nombres.append(nombre)
    return nombres
