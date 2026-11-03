"""Implementa el chunking jerárquico de documentos JSON clínicos.

Recorre cada documento según el plan definido para su tipología, agrupa sus
campos en chunks semánticos y conserva metadatos de trazabilidad y límites de
tokens. Los planes de documento definen cómo se organizan los campos; este
módulo implementa la lógica para procesarlos.
"""

# ============================================================
# IMPORTS
# ============================================================

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Callable
import re

# ============================================================
# 1. ESTRUCTURAS DE DATOS
# ============================================================

@dataclass
class Chunk:
    """Texto de un segmento del documento y metadatos de su origen y tamaño.

    ``json_paths`` identifica los campos fuente y ``metadata`` contiene
    detalles adicionales del chunk, incluidos los límites de tokens aplicados.
    """

    text: str # texto del chunk
    block: str # nombre de la clave raíz del documento
    group_name: str # nombre del nodo en SplitNode
    level: int # profundidad del SplitNode
    json_paths: list[str] # ruta de los elementos representados
    token_count: int
    exceeds_target_limit: bool = False # supera el tamaño recomendado para el chunking
    exceeds_max_limit: bool = False # supera el límite del modelo de embeddings
    metadata: dict[str, Any] = field(default_factory=dict)

@dataclass
class SplitNode:
    """Nodo del plan semántico que agrupa rutas JSON o nodos hijos.

    Un nodo puede declarar ``fields``, ``children`` o ambos. Las rutas de campo
    son relativas a la raíz interna del documento y usan puntos como separador.
    """

    name: str
    fields: list[str] = field(default_factory=list)
    children: list["SplitNode"] = field(default_factory=list)

@dataclass(frozen=True)
class ResolvedField:
    """Campo de un plan ya localizado dentro de un documento JSON."""

    name: str
    path: tuple[str | int, ...]
    value: Any

# ============================================================
# 2. PLAN DE DIVISIÓN SEMÁNTICA ASOCIADO AL TIPO DE DOCUMENTO: PARÁMETRO
# ============================================================

# ============================================================
# 3. FUNCIONES AUXILIARES DE TEXTO
# ============================================================

def is_present(value: Any) -> bool:
    """Indica si un valor JSON contiene información que deba conservarse.

    ``None``, cadenas en blanco y contenedores vacíos (o con solo valores
    vacíos) no están presentes; los escalares como ``0`` y ``False`` sí.
    """
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return any(
            is_present(item)
            for item in value.values()
        )
    if isinstance(value, (list, tuple)):
        return any(
            is_present(item)
            for item in value
        )
    return True

def humanize(name: str) -> str:
    """Convierte una clave JSON en una etiqueta legible para el texto del chunk."""
    return name.replace("_", " ").strip().capitalize()


def scalar_to_text(value: Any) -> str:
    """Convierte un escalar a texto y representa booleanos como ``Sí`` o ``No``."""
    if isinstance(value, bool):
        return "Sí" if value else "No"
    return str(value)

# Nombres de claves que indican que un diccionario
# probablemente representa una tabla explícita.
TABLE_COLUMN_KEYS = {"columnas", "columns",}

TABLE_ROW_KEYS = {"filas", "rows",}

def find_matching_key(
    value: dict[str, Any],
    candidates: set[str],
) -> str | None:
    """Busca la primera clave candidata ignorando mayúsculas y espacios externos.

    Returns:
        La clave original del diccionario si coincide, o ``None``.
    """
    for key in value:
        if (
            isinstance(key, str)
            and key.strip().casefold() in candidates
        ):
            return key
    return None


def detect_explicit_table(
    value: Any,
) -> tuple[str, str] | None:
    """Detecta tablas con listas de columnas y filas.

    Las claves admitidas son ``columns``/``columnas`` y ``rows``/``filas``.
    La comparación no distingue mayúsculas ni espacios externos.

    Returns:
        Las claves reales de columnas y filas, o ``None`` si la estructura no
        cumple el formato de tabla esperado.
    """
    if not isinstance(value, dict):
        return None
    column_key = find_matching_key(
        value,
        TABLE_COLUMN_KEYS,
    )
    row_key = find_matching_key(
        value,
        TABLE_ROW_KEYS,
    )
    if column_key is None or row_key is None:
        return None
    if not isinstance(value[column_key], list):
        return None
    if not isinstance(value[row_key], list):
        return None
    return column_key, row_key

def _append_table_cell(
    lines: list[str],
    column_name: str,
    cell: Any,
    indent: int,
) -> None:
    """Añade una celda formateada y conserva la asociación columna-valor."""
    prefix = " " * indent
    label = humanize(column_name)
    # Una cadena con saltos de línea se interpreta
    # como una lista de elementos dentro de la celda.
    if isinstance(cell, str):
        cell_lines = [
            line.strip(" -\t")
            for line in cell.splitlines()
            if line.strip(" -\t")
        ]
        if not cell_lines:
            return
        # Una única línea se representa directamente.
        if len(cell_lines) == 1:
            lines.append(
                f"{prefix}{label}: "
                f"{cell_lines[0]}"
            )
        # Varias líneas se representan como una lista.
        else:
            lines.append(
                f"{prefix}{label}:"
            )
            lines.extend(
                f"{prefix}  - {line}"
                for line in cell_lines
            )
        return

    # Las listas y diccionarios internos se conservan
    # con su estructura.
    if isinstance(cell, (dict, list)):
        nested = value_to_lines(
            cell,
            indent + 2,
        )
        if nested:
            lines.append(
                f"{prefix}{label}:"
            )
            lines.extend(nested)
        return
    # Números, booleanos y otros escalares.
    lines.append(
        f"{prefix}{label}: "
        f"{scalar_to_text(cell)}"
    )

def table_to_lines(
    table: dict[str, Any],
    indent: int = 0,
) -> list[str]:
    """Serializa una tabla en líneas, repitiendo el nombre de cada columna.

    Conserva los campos adicionales de la tabla y admite filas como listas,
    diccionarios o escalares.

    Returns:
        Líneas de texto de la tabla; devuelve una lista vacía si no es una tabla
        explícita válida.
    """
    detected_keys = detect_explicit_table(table)
    if detected_keys is None:
        return []
    column_key, row_key = detected_keys
    columns = table[column_key]
    rows = table[row_key]
    prefix = " " * indent
    lines: list[str] = []
    # Conserva otros campos que pueda contener la tabla,
    # como title, source o description.
    for key, item in table.items():
        if key in {column_key, row_key}:
            continue
        if not is_present(item):
            continue
        label = humanize(str(key))
        if isinstance(item, (dict, list)):
            nested = value_to_lines(
                item,
                indent + 2,
            )
            if nested:
                lines.append(
                    f"{prefix}{label}:"
                )
                lines.extend(nested)
        else:
            lines.append(
                f"{prefix}{label}: "
                f"{scalar_to_text(item)}"
            )
    # Procesa las filas de la tabla.
    for row_index, row in enumerate(
        rows,
        start=1,
    ):
        if not is_present(row):
            continue
        lines.append(
            f"{prefix}Row {row_index}:"
        )
        # --------------------------------------------
        # Caso 1: la fila es una lista
        # --------------------------------------------
        if isinstance(row, list):
            for column_index, cell in enumerate(row):
                if not is_present(cell):
                    continue
                # Utiliza el nombre de columna correspondiente.
                if column_index < len(columns):
                    column_name = str(
                        columns[column_index]
                    )
                else:
                    column_name = (
                        f"Extra column "
                        f"{column_index + 1}"
                    )
                _append_table_cell(
                    lines=lines,
                    column_name=column_name,
                    cell=cell,
                    indent=indent + 2,
                )

        # --------------------------------------------
        # Caso 2: la fila es un diccionario
        # --------------------------------------------
        elif isinstance(row, dict):
            used_keys: set[Any] = set()
            # Primero conserva el orden indicado
            # en la lista de columnas.
            for column in columns:
                if column not in row:
                    continue
                cell = row[column]
                if not is_present(cell):
                    continue
                used_keys.add(column)
                _append_table_cell(
                    lines=lines,
                    column_name=str(column),
                    cell=cell,
                    indent=indent + 2,
                )
            # Conserva también los campos adicionales
            # que no estén declarados en columns.
            for key, cell in row.items():
                if key in used_keys:
                    continue
                if not is_present(cell):
                    continue
                _append_table_cell(
                    lines=lines,
                    column_name=str(key),
                    cell=cell,
                    indent=indent + 2,
                )

        # --------------------------------------------
        # Caso 3: la fila es un valor escalar
        # --------------------------------------------
        else:
            column_name = (
                str(columns[0])
                if columns
                else "Value"
            )
            _append_table_cell(
                lines=lines,
                column_name=column_name,
                cell=row,
                indent=indent + 2,
            )
    return lines

def value_to_lines(value: Any, indent: int = 0) -> list[str]:
    """Serializa recursivamente un valor JSON a líneas con sangría.

    Omite valores vacíos, identifica tablas explícitas y representa los
    elementos de listas como viñetas.
    """
    prefix = " " * indent

    if not is_present(value):
        return []
    if isinstance(value, dict):
        # Antes de tratarlo como un diccionario genérico,
        # comprueba si representa una tabla explícita.
        if detect_explicit_table(value) is not None:
            return table_to_lines(
                table=value,
                indent=indent,
            )
        # Si no es una tabla:
        lines: list[str] = []
        for key, item in value.items():
            if not is_present(item):
                continue
            label = humanize(key)
            if isinstance(item, (dict, list)):
                nested = value_to_lines(item, indent + 2)
                if nested:
                    lines.extend([f"{prefix}{label}:", *nested])
            else:
                lines.append(f"{prefix}{label}: {scalar_to_text(item)}")
        return lines
    if isinstance(value, list):
        lines = []
        for item in value:
            nested = value_to_lines(item, indent + 2)
            if nested:
                lines.append(f"{prefix}- {nested[0].lstrip()}")
                lines.extend(f"{prefix}  {line.lstrip()}" for line in nested[1:])
        return lines
    return [prefix + scalar_to_text(value)]


def render_fields(fields: list[ResolvedField]) -> str:
    """Renderiza campos localizados como bloques clave-valor separados por líneas."""
    blocks: list[str] = []
    for field in fields:
        label = humanize(field.name)
        if isinstance(field.value, (dict, list)):
            lines = value_to_lines(field.value, 2)
            if lines:
                blocks.append("\n".join([f"{label}:", *lines]))
        else:
            blocks.append(f"{label}: {scalar_to_text(field.value)}")
    return "\n\n".join(blocks)

# ============================================================
# 4. RESOLUCIÓN DE CAMPOS SEGÚN EL PLAN
# ============================================================

def collect_fields(node: SplitNode) -> list[str]:
    """Recoge, sin duplicados, las rutas declaradas en un nodo y sus descendientes."""
    fields = list(node.fields)
    for child in node.children:
        fields.extend(collect_fields(child))
    return list(dict.fromkeys(fields))

def resolve_fields(
    node: SplitNode,
    data: dict[str, Any],
) -> list[ResolvedField]:
    """Resuelve las rutas de un nodo contra un objeto JSON.

    Solo devuelve rutas existentes cuyos valores estén presentes. Las rutas se
    interpretan respecto a ``data`` y sus componentes se separan por puntos.
    """
    result: list[ResolvedField] = []
    for field_path in collect_fields(node):
        path = tuple(field_path.split("."))
        value: Any = data
        for key in path:
            if not isinstance(value, dict) or key not in value:
                break
            value = value[key]
        else:
            if is_present(value):
                result.append(
                    ResolvedField(
                        name=path[-1],
                        path=path,
                        value=value,
                    )
                )
    return result

# ============================================================
# 5. CONTADORES DE TOKENS
# ============================================================

def approximate_token_counter(text: str) -> int:
    """Estima tokens contando palabras separadas por espacios."""
    return len(text.split())

def make_huggingface_token_counter(tokenizer: Any) -> Callable[[str], int]:
    """Crea un contador que mide tokens con el tokenizer de Hugging Face.

    El contador incluye tokens especiales y desactiva el truncamiento.
    """
    def count_tokens(text: str) -> int:
        return len(tokenizer(
            text,
            add_special_tokens=True,
            truncation=False,
        )["input_ids"])
    return count_tokens

# ============================================================
# 6. CHUNKER RECURSIVO
# ============================================================

class HierarchicalJSONChunker:
    """Divide documentos JSON según un plan semántico y límites de tokens.

    El plan fuerza una primera subdivisión por sus hijos directos. Las ramas
    que superan ``target_tokens`` se subdividen recursivamente; los campos que
    no están cubiertos por el plan también se conservan. ``max_tokens`` es un
    límite absoluto: los chunks que lo superan se dividen mediante fallback,
    por filas en tablas simples o por unidades de texto en los demás casos.

    Los valores vacíos se omiten, mientras que ``0`` y ``False`` se conservan.
    Los escalares y diccionarios no se fragmentan; las listas que superan el
    objetivo se separan por elementos, salvo las rutas protegidas. Las tablas
    explícitas se serializan por filas con sus nombres de columna. El contenido
    no cubierto por el plan se agrupa como ``unmapped_content``. Los campos de
    contexto se añaden a los metadatos, no al texto que se vectoriza.
    """

    def __init__(
        self,
        target_tokens: int,
        max_tokens: int,
        token_counter: Callable[[str], int],
        document_plan: SplitNode,
        root_field: str | None = None,
        indivisible_list_paths: set[str] | None = None,
        context_fields: list[str] | None = None,
    ) -> None:
        """Configura el plan, los límites de tokens y las reglas por documento.

        Args:
            target_tokens: Tamaño recomendado para los chunks.
            max_tokens: Límite absoluto usado para activar el fallback.
            token_counter: Función que devuelve el número de tokens de un texto.
            document_plan: Árbol de campos y grupos semánticos.
            root_field: Clave exterior que se usará como raíz, si se especifica.
            indivisible_list_paths: Rutas relativas cuyas listas no se dividen.
            context_fields: Rutas copiadas a los metadatos de cada chunk.

        Raises:
            ValueError: Si los límites no son positivos o el objetivo supera
                el límite máximo.
        """
        if target_tokens <= 0:
            raise ValueError(
                "target_tokens debe ser mayor que cero."
            )
        if max_tokens <= 0:
                    raise ValueError(
                        "max_tokens debe ser mayor que cero."
                    )
        if target_tokens > max_tokens:
                    raise ValueError(
                        "target_tokens no puede superar max_tokens."
                    )
        self.target_tokens = target_tokens
        self.max_tokens = max_tokens
        self.token_counter = token_counter
        self.document_plan = document_plan
        self.root_field = root_field
        self.indivisible_list_paths = set(
            indivisible_list_paths or []
        )
        self.context_fields = list(dict.fromkeys(context_fields or []))

    def chunk_document(self, document: dict[str, Any]) -> list[Chunk]:
        """Divide un documento y devuelve sus chunks con metadatos de origen.

        Incluye las ramas presentes que no estén descritas por el plan, aplica
        el fallback a los chunks sobre ``max_tokens`` y añade el contexto
        configurado a los metadatos.

        Args:
            document: Objeto JSON que se va a dividir.

        Returns:
            Lista de chunks; un objeto vacío produce una lista vacía.

        Raises:
            TypeError: Si ``document`` no es un diccionario.
            KeyError: Si se configuró un ``root_field`` que no existe.
            ValueError: Si el fallback no puede representar el contenido
                respetando el límite de tokens.
        """
        if not isinstance(document, dict):
            raise TypeError("El documento debe ser un diccionario.")
        if not document:
            return []
        block, data = self._get_root(document)
        planned = resolve_fields(self.document_plan, data)
        chunks: list[Chunk] = []
        for child in self.document_plan.children:
            chunks.extend(
                self._split(
                    block=block,
                    data=data,
                    node=child,
                    level=1
                )
            )
        uncovered = self._find_uncovered(data, [field.path for field in planned])
        if uncovered:
            chunks.extend(self._pack_fields(
                block,
                SplitNode(name="unmapped_content"),
                uncovered,
                level=1,
            ))

        final_chunks: list[Chunk] = []
        for chunk in chunks:
            if chunk.token_count > self.max_tokens:
                final_chunks.extend(self._split_oversized_chunk(chunk))
            else:
                final_chunks.append(chunk)

        context = self._resolve_context(data)
        if context:
            for chunk in final_chunks:
                chunk.metadata["context"] = dict(context)
        return final_chunks


    # Solapamiento proporcional, fijado internamente en un 10 % del
    # límite del modelo
    FALLBACK_OVERLAP_RATIO = 0.1

    def _split_oversized_chunk(self, original: Chunk) -> list[Chunk]:
        """Divide un chunk que supera el límite absoluto del modelo.

        Las tablas simples se dividen por filas completas con cabecera repetida.
        En otros textos se usan unidades de oración, palabras y, si hace falta,
        caracteres. La cobertura principal conserva todo el texto de entrada;
        los fragmentos se equilibran y pueden solaparse.
        """
        limit = self.max_tokens
        # Un margen relativo pequeño evita ocupar innecesariamente el tope.
        # Los tokens especiales ya deben estar incluidos en token_counter.
        margin = max(1, min(16, int(limit * 0.01))) if limit > 32 else 0
        usable = limit - margin
        if usable <= 0:
            raise ValueError("max_tokens es demasiado pequeño para el fallback")

        # Las tablas se dividen por filas completas repitiendo su cabecera.
        table_parts = self._split_table_by_rows(original, usable)
        if table_parts is not None:
            return table_parts

        units = self._fallback_units(original.text, usable)
        if not units:
            return []

        # Cada unidad contiene su espacio/separador original, para que la
        # concatenación de la cobertura principal reproduzca el texto fuente.
        n = len(units)
        lengths = [0] * (n + 1)
        for i, u in enumerate(units):
            lengths[i + 1] = lengths[i] + len(u)

        def get_text(i: int, j: int) -> str:
            return "".join(units[i:j])

        from functools import lru_cache

        @lru_cache(maxsize=None)
        def fits(i: int, j: int) -> bool:
            return self.token_counter(get_text(i, j)) <= usable

        # Para cualquier comienzo, encontramos el extremo más lejano que
        # cabe. Se cuenta el texto serializado completo, no la suma de tokens
        # independientes (que puede diferir).
        max_end: list[int] = [0] * n
        for i in range(n):
            low, high = i + 1, n
            best = i
            while low <= high:
                mid = (low + high) // 2
                if fits(i, mid):
                    best = mid
                    low = mid + 1
                else:
                    high = mid - 1
            if best == i:
                raise ValueError("Una unidad excede max_tokens después del fallback")
            max_end[i] = best

        # Programación dinámica: mínimo número de trozos sin solapamiento.
        min_parts = [0] * (n + 1)
        min_parts[n] = 0
        for i in range(n - 1, -1, -1):
            min_parts[i] = 1 + min(min_parts[j] for j in range(i + 1, max_end[i] + 1))

        # Entre las particiones mínimas, buscamos tamaños equilibrados.
        total = self.token_counter(original.text)
        boundaries: list[tuple[int, int]] = []
        i = 0
        while i < n:
            options = [j for j in range(i + 1, max_end[i] + 1)
                       if min_parts[j] == min_parts[i] - 1]
            desired = total / max(1, min_parts[0])
            j = min(options, key=lambda end: abs(self.token_counter(get_text(i, end)) - desired))
            boundaries.append((i, j))
            i = j

        overlap_budget = max(1, round(limit * self.FALLBACK_OVERLAP_RATIO))
        output: list[Chunk] = []
        for part_index, (begin, end) in enumerate(boundaries):
            main_text = get_text(begin, end)
            prefix_start = begin
            # Repetir oraciones completas previas si caben dentro del presupuesto
            # de overlap, SIN aumentar el número de fragmentos.
            if part_index > 0:
                for candidate in range(begin - 1, -1, -1):
                    context = get_text(candidate, begin)
                    candidate_text = context + main_text
                    if (self.token_counter(context) > overlap_budget
                            or self.token_counter(candidate_text) > usable):
                        break
                    prefix_start = candidate
            text = get_text(prefix_start, end)
            count = self.token_counter(text)
            if count > limit:
                raise AssertionError("Fallback generó un chunk superior a max_tokens")
            metadata = dict(original.metadata)
            metadata.update({
                "fallback_applied": True,
                "fallback_method": "sentence_balanced_overlap",
                "fallback_part": part_index + 1,
                "fallback_total_parts": len(boundaries),
                "fallback_original_tokens": original.token_count,
                "fallback_overlap_ratio": self.FALLBACK_OVERLAP_RATIO,
                "fallback_overlap_units": begin - prefix_start,
                "fallback_overlap_tokens": (self.token_counter(get_text(prefix_start, begin))
                                            if prefix_start != begin else 0),
            })
            output.append(Chunk(
                text=text,
                block=original.block,
                group_name=original.group_name,
                level=original.level,
                json_paths=list(original.json_paths),
                token_count=count,
                exceeds_target_limit=count > self.target_tokens,
                exceeds_max_limit=False,
                metadata={**metadata,
                          "exceeds_target_limit": count > self.target_tokens,
                          "exceeds_max_limit": False},
            ))
        return output

    # Marcador de fila que genera table_to_lines ("Row 3:")
    _ROW_MARKER = re.compile(r"^(\s*)Row (\d+):\s*$")

    def _split_table_by_rows(self, original: Chunk, usable: int) -> list[Chunk] | None:
        """Divide una tabla demasiado grande por filas completas.

        Cada parte repite la cabecera (todo lo anterior a la primera fila:
        etiqueta, título...) para que los datos no queden sin contexto, y
        ninguna fila se corta por la mitad. Las líneas posteriores a las filas
        (por ejemplo, el summary) se conservan una sola vez, en la última
        parte. Devuelve None si el texto no tiene la forma de una tabla simple
        o si una fila no cabe junto a la cabecera; en ese caso se usa el
        fallback por oraciones.
        """
        count = self.token_counter
        lines = original.text.splitlines(keepends=True)
        markers = [self._ROW_MARKER.match(line.rstrip("\r\n")) for line in lines]
        first = next((i for i, m in enumerate(markers) if m), None)
        if first is None:
            return None
        first_marker = markers[first]
        if first_marker is None:
            return None
        row_indent = len(first_marker.group(1))

        # Separa cabecera, filas (con su número) y líneas finales.
        header = lines[:first]
        rows: list[tuple[int, list[str]]] = []
        trailer: list[str] = []
        for line, marker in zip(lines[first:], markers[first:]):
            if trailer:
                if marker:
                    return None  # hay otra tabla después de la primera
                trailer.append(line)
            elif marker:
                if len(marker.group(1)) != row_indent:
                    return None  # tabla anidada: no es el caso simple
                rows.append((int(marker.group(2)), [line]))
            elif not line.strip() or len(line) - len(line.lstrip()) > row_indent:
                rows[-1][1].append(line)
            else:
                trailer.append(line)

        header_text = "".join(header)
        trailer_text = "".join(trailer)
        if count(header_text) > usable // 2:
            return None  # una cabecera tan grande dejaría poco espacio a las filas

        # Agrupa filas completas mientras quepan junto a la cabecera.
        groups: list[list[tuple[int, list[str]]]] = []
        current: list[tuple[int, list[str]]] = []
        for row in rows:
            candidate = current + [row]
            candidate_text = header_text + "".join("".join(r[1]) for r in candidate)
            if current and count(candidate_text) > usable:
                groups.append(current)
                current = [row]
            else:
                current = candidate
        groups.append(current)

        texts = [header_text + "".join("".join(r[1]) for r in g) for g in groups]
        if trailer_text:
            if count(texts[-1] + trailer_text) <= usable:
                texts[-1] += trailer_text
            else:
                texts.append(header_text + trailer_text)
                groups.append([])
        if len(texts) < 2 or any(count(t) > usable for t in texts):
            return None

        output: list[Chunk] = []
        for part, (text, group) in enumerate(zip(texts, groups), start=1):
            token_count = count(text)
            metadata = dict(original.metadata)
            metadata.update({
                "fallback_applied": True,
                "fallback_method": "table_rows_with_header",
                "fallback_part": part,
                "fallback_total_parts": len(texts),
                "fallback_original_tokens": original.token_count,
                "fallback_overlap_ratio": 0,
                "fallback_overlap_units": 0,
                "fallback_overlap_tokens": 0,
                "exceeds_target_limit": token_count > self.target_tokens,
                "exceeds_max_limit": False,
            })
            if group:
                metadata["table_row_range"] = [group[0][0], group[-1][0]]
            output.append(Chunk(
                text=text,
                block=original.block,
                group_name=original.group_name,
                level=original.level,
                json_paths=list(original.json_paths),
                token_count=token_count,
                exceeds_target_limit=token_count > self.target_tokens,
                exceeds_max_limit=False,
                metadata=metadata,
            ))
        return output

    def _fallback_units(self, text: str, limit: int) -> list[str]:
        """Separa el texto en unidades ajustadas al límite sin perder caracteres.

        Conserva líneas estructuradas y separadores originales; las unidades
        demasiado largas se subdividen por palabras o por caracteres.
        """
        # Las líneas estructuradas del JSON se mantienen independientes.
        # Para texto narrativo se buscan límites de oración tras .!? y espacio.
        lines = text.splitlines(keepends=True)
        units: list[str] = []
        for line in lines:
            # Conserva los espacios originales y todos los separadores.
            parts = re.split(r"(?<=[.!?])(?=\s+[^\s])", line)
            for part in parts:
                if not part:
                    continue
                if self.token_counter(part) <= limit:
                    units.append(part)
                else:
                    units.extend(self._fallback_split_long_unit(part, limit))
        return units

    def _fallback_split_long_unit(self, text: str, limit: int) -> list[str]:
        """Divide una unidad larga por palabras y luego por caracteres.

        La concatenación del resultado reproduce exactamente ``text``.

        Raises:
            ValueError: Si ni siquiera un carácter cabe dentro de ``limit``.
        """
        pieces = re.findall(r"\s+|\S+", text)
        out: list[str] = []
        current = ""
        for piece in pieces:
            if self.token_counter(current + piece) <= limit:
                current += piece
                continue
            if current:
                out.append(current)
                current = ""
            if self.token_counter(piece) <= limit:
                current = piece
                continue
            # Una sola palabra (o espacio) excesiva: cortar caracteres.
            rest = piece
            while rest:
                lo, hi, best = 1, len(rest), 0
                while lo <= hi:
                    mid = (lo + hi) // 2
                    if self.token_counter(rest[:mid]) <= limit:
                        best = mid
                        lo = mid + 1
                    else:
                        hi = mid - 1
                if not best:
                    raise ValueError("max_tokens insuficiente para un carácter")
                out.append(rest[:best])
                rest = rest[best:]
        if current:
            out.append(current)
        assert "".join(out) == text
        return out

    def _resolve_context(self, data: dict[str, Any]) -> dict[str, Any]:
        """Resuelve los campos de contexto presentes en el documento.

        Devuelve un diccionario con rutas completas como claves y valores del
        JSON como contenido; omite las rutas ausentes o vacías.
        """
        if not self.context_fields:
            return {}
        resolved = resolve_fields(
            SplitNode(name="context", fields=self.context_fields),
            data,
        )
        return {
            ".".join(str(part) for part in field.path): field.value
            for field in resolved
        }

    def _get_root(self, document: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """Determina el bloque raíz y los datos que recorrerá el plan.

        Usa ``root_field`` si se configuró; de lo contrario, desenvuelve una
        única clave exterior cuyo valor sea un objeto. En los demás casos usa
        el documento completo.

        Raises:
            KeyError: Si la clave raíz configurada no existe.
            TypeError: Si la raíz seleccionada no contiene un objeto.
        """
        if self.root_field is not None:
            if self.root_field not in document:
                raise KeyError(f"La clave raíz '{self.root_field}' no existe.")
            block, data = self.root_field, document[self.root_field]
        elif len(document) == 1:
            possible_block, possible_data = next(iter(document.items()))
            if isinstance(possible_data, dict):
                block, data = possible_block, possible_data
            else:
                block, data = "document", document
        else:
            block, data = "document", document
        if not isinstance(data, dict):
            raise TypeError(f"El contenido de la raíz '{block}' debe ser un diccionario.")
        return block, data

    def _split(
        self,
        block: str,
        data: dict[str, Any],
        node: SplitNode,
        level: int,
    ) -> list[Chunk]:
        """Evalúa un nodo del plan y decide si conservarlo o subdividirlo."""
        fields = resolve_fields(node, data)
        if not fields:
            return []
        text = render_fields(fields)
        exceeds_target = self.token_counter(text) > self.target_tokens
        # Si supera el límite, se generan obligatoriamente
        # todas las subdivisiones inmediatas del nodo.
        if exceeds_target and node.children:
            chunks: list[Chunk] = []
            for child in node.children:
                chunks.extend(
                    self._split(
                        block=block,
                        data=data,
                        node=child,
                        level=level + 1,
                    )
                )
            return chunks
        # Una hoja excesiva se divide solo entre campos completos.
        # Una pareja clave–valor nunca se parte.
        if exceeds_target:
            return self._pack_fields(
                block=block,
                node=node,
                fields=fields,
                level=level,
            )
        return [
            self._make_chunk(
                block=block,
                node=node,
                fields=fields,
                level=level,
            )
        ]
    
    def _pack_fields(
        self,
        block: str,
        node: SplitNode,
        fields: list[ResolvedField],
        level: int,
    ) -> list[Chunk]:
        """Agrupa campos completos sin superar el tamaño objetivo cuando es posible.

        Las listas extensas se separan por elementos, salvo que su ruta esté en
        ``indivisible_list_paths``; las parejas clave-valor no se fragmentan.
        """
        chunks: list[Chunk] = []
        current: list[ResolvedField] = []

        def flush_current() -> None:
            # Convierte el grupo acumulado en un chunk
            nonlocal current

            if current:
                chunks.append(
                    self._make_chunk(
                        block=block,
                        node=node,
                        fields=current,
                        level=level,
                    )
                )
                current = []
        for resolved_field in fields:
            # Cuenta los tokens de esta pareja clave–valor
            # de manera independiente.
            field_text = render_fields(
                [resolved_field]
            )
            field_exceeds_target = (
                self.token_counter(field_text)
                > self.target_tokens
            )
            # Ruta exacta relativa a la raíz interna.
            relative_path = ".".join(
                str(part)
                for part in resolved_field.path
            )
            # Comprueba si la lista debe conservarse completa.
            list_is_indivisible = (
                relative_path
                in self.indivisible_list_paths
            )
            # Divide las listas excesivas que no estén protegidas.
            if (
                isinstance(resolved_field.value, list)
                and field_exceeds_target
                and not list_is_indivisible
            ):
                # Guarda cualquier campo anterior.
                flush_current()
                # Cada elemento genera un chunk independiente.
                for index, item in enumerate(
                    resolved_field.value
                ):
                    if not is_present(item):
                        continue
                    list_item_field = ResolvedField(
                        # Conserva el nombre de la clave original.
                        name=resolved_field.name,
                        # Añade el índice a la ruta JSON.
                        path=(
                            *resolved_field.path,
                            index,
                        ),
                        # El elemento permanece completo.
                        value=item,
                    )
                    chunks.append(
                        self._make_chunk(
                            block=block,
                            node=node,
                            fields=[list_item_field],
                            level=level,
                        )
                    )
                continue
            # Aquí entran:
            # - valores escalares;
            # - diccionarios;
            # - listas que caben;
            # - listas excesivas protegidas.
            candidate = [
                *current,
                resolved_field,
            ]
            candidate_exceeds_target = (
                self.token_counter(
                    render_fields(candidate)
                )
                > self.target_tokens
            )
            if current and candidate_exceeds_target:
                flush_current()
            current.append(resolved_field)
        flush_current()
        return chunks

    def _find_uncovered(
        self,
        data: Any,
        covered_paths: list[tuple[str | int, ...]],
    ) -> list[ResolvedField]:
        """Localiza valores presentes no cubiertos por las rutas del plan."""
        result: list[ResolvedField] = []
        def visit(value: Any, path: tuple[str | int, ...]) -> None:
            if any(path[:len(covered)] == covered for covered in covered_paths):
                return
            has_covered_descendant = any(
                covered[:len(path)] == path for covered in covered_paths
            )
            if has_covered_descendant and isinstance(value, (dict, list)):
                items = value.items() if isinstance(value, dict) else enumerate(value)
                for key, item in items:
                    if is_present(item):
                        visit(item, (*path, key))
            elif is_present(value):
                result.append(ResolvedField(str(path[-1]), path, value))
        for key, value in data.items():
            if is_present(value):
                visit(value, (key,))
        return result

    def _make_chunk(
        self,
        block: str,
        node: SplitNode,
        fields: list[ResolvedField],
        level: int,
    ) -> Chunk:
        """Construye un chunk y calcula texto, tokens, rutas y límites."""
        text = render_fields(fields)
        token_count = self.token_counter(text)
        exceeds_target = token_count > self.target_tokens
        exceeds_max = token_count > self.max_tokens
        paths = list(dict.fromkeys(
            self._format_path(block, field.path) for field in fields
        ))
        return Chunk(
            text=text,
            block=block,
            group_name=node.name,
            level=level,
            json_paths=paths,
            token_count=token_count,
            exceeds_target_limit=exceeds_target,
            exceeds_max_limit=exceeds_max,
            metadata={
                "fields": [field.name for field in fields],
                "target_tokens": self.target_tokens,
                "max_tokens": self.max_tokens,
                "exceeds_target_limit": exceeds_target,
                "exceeds_max_limit": exceeds_max,
            },
        )

    @staticmethod
    def _format_path(block: str, path: tuple[str | int, ...]) -> str:
        """Formatea una ruta JSON relativa al bloque, incluidos índices de lista."""
        result = block
        for part in path:
            result += f"[{part}]" if isinstance(part, int) else f".{part}"
        return result