"""Conversión de PDF a Markdown usando OpenDataLoader.

Módulo centralizado para la etapa PDF → Markdown del
pipeline. Usa opendataloader-pdf para extracción de
texto, tablas y headings con soporte nativo de
multi-columna y filtrado de headers/footers.

Uso básico::

    from src.document_processor.extraction import convert_pdf

    result = convert_pdf("paper.pdf")

`convert_folder` existe en este módulo pero **no se re-exporta** en `extraction/__init__.py`:
es la vía CLI y no debe usarse desde la app. Ver el docstring de ese `__init__`.
"""

from __future__ import annotations

import importlib.resources as pkg_resources
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from importlib.resources.abc import Traversable
from pathlib import Path

from src.document_processor.config import (
    PDF_CONVERT_TIMEOUT_SECONDS,
    PDF_REPAIR_TIMEOUT_SECONDS,
)

from .layout import Layout, summarize_layout
from .markdown_cleaner import fix_ligatures
from .redaction import redact_personal_data

logger = logging.getLogger(__name__)


def _minimal_subprocess_env(tmpdir: str) -> dict[str, str]:
    """Entorno mínimo para subprocesos (java, gs).

    Evita propagar variables sensibles de la app
    (claves API, secretos de Django, credenciales de
    BD...) al subprocess. Solo expone lo imprescindible:

    - PATH: localizar el binario (`java`, `gs`).
    - HOME: Java crea caches en ~/.java/.
    - LANG/LC_ALL: forzar UTF-8 en el locale del SO.
    - TMPDIR: directorio temporal escribible, propio de esta conversión.

    `tmpdir` debe ser el directorio privado de la conversión, no el `/tmp` del sistema: por
    ahí escriben la JVM y ghostscript sus intermedios, que son renderizados parciales del
    documento clínico. Es también el fallback de `HOME` cuando la variable no está definida,
    caso habitual en contenedores minimalistas, para que las cachés de Java no acaben en un
    directorio compartido.
    """
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME") or tmpdir,
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "TMPDIR": tmpdir,
    }


_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f" r"\ufdd0-\ufdef" r"\ufffe\uffff]")


def sanitize_text_to_utf8(text: str) -> str:
    """Elimina caracteres no válidos en UTF-8.

    - Surrogados sueltos (U+D800..U+DFFF).
    - Caracteres de control no imprimibles.
    - Non-characters Unicode (U+FDD0..U+FDEF, U+FFFE/FFFF).
    - Byte-order marks (BOM), tanto el inicial como los internos.

    El BOM inicial lo quitaba antes una rama propia, redundante con el `replace` que viene
    después: ninguna mutación de esa rama podía morir, porque no cambiaba el resultado.
    """
    text = _SURROGATE_RE.sub("", text)
    text = _CONTROL_RE.sub("", text)
    text = text.replace("\ufeff", "")
    text = text.encode("utf-8", errors="replace").decode("utf-8", errors="replace")
    return text


def _safe_read_md(path: Path) -> str:
    """Lee un `.md` probando encodings, de más probable a más tolerante.

    UTF-8 primero, porque es lo que emite opendataloader (se le fuerza con
    `-Dfile.encoding=UTF-8`). Latin-1 como último recurso, que nunca falla porque acepta
    cualquier byte.

    UTF-16 **solo si hay BOM de UTF-16**, y esto es importante: decodificar UTF-16 tiene éxito
    con casi cualquier cadena de bytes de longitud par, así que probarlo antes de Latin-1
    convertía un fichero UTF-8 con un único byte corrupto en un documento entero de mojibake,
    que acabaría en el LLM y en el RAG. Con Latin-1 al menos se conserva la estructura ASCII.

    El `return` final es **inalcanzable** y está marcado como tal: Latin-1 mapea los 256 bytes y
    nunca lanza, así que el bucle siempre sale por su `return`. Se conserva porque el tipo de
    retorno lo exige —el comprobador no puede saber que el bucle es exhaustivo— y porque deja de
    serlo en cuanto alguien toque la lista de encodings.
    """
    raw = path.read_bytes()

    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]

    encodings = ["utf-8"]
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        encodings.append("utf-16")
    encodings.append("latin-1")

    for enc in encodings:
        try:
            text = raw.decode(enc)
            return sanitize_text_to_utf8(text)
        except (UnicodeDecodeError, UnicodeError):
            continue

    return sanitize_text_to_utf8(raw.decode("utf-8", errors="replace"))  # pragma: no cover


@dataclass
class ConversionResult:
    """Metadatos y contenido de una conversión PDF→MD."""

    markdown: str
    source_path: Path
    elapsed_seconds: float
    n_pages: int
    n_tables: int
    original_size_bytes: int
    layout: Layout | None = None

    @property
    def markdown_chars(self) -> int:
        """Número de caracteres del Markdown generado."""
        return len(self.markdown)


@dataclass
class ConverterConfig:
    """Configuración para la conversión PDF→MD.

    Attributes:
        include_header_footer: Incluir headers/footers
            del PDF. Por defecto False (se filtran).
        keep_line_breaks: Preservar saltos de línea
            del layout original. False genera párrafos
            continuos, mejor para chunking y LLM.
        image_output: Modo de extracción de imágenes.
            "off" para no extraer (no necesarias para
            JSON clínico).
        use_struct_tree: Usar PDF structure tree para
            orden de lectura y estructura semántica.
            DESACTIVADO por defecto: en PDFs mal etiquetados
            (típico en protocolos de pharma) opendataloader
            sigue el árbol de estructura y emite solo
            headings/tablas, descartando el cuerpo del texto
            (visto: 40 K vs 520 K chars en un protocolo de
            247 págs; la sección de posología desaparecía).
            La extracción por layout (False) recupera todo el
            texto y detecta más tablas. Actívalo solo para
            PDFs correctamente etiquetados.
        table_method: Método de detección de tablas.
            "cluster" detecta tablas sin bordes además
            de las con bordes (común en docs médicos).
        markdown_page_separator: Separador entre páginas
            en el markdown de salida.
    """

    include_header_footer: bool = False
    keep_line_breaks: bool = False
    image_output: str = "off"
    use_struct_tree: bool = False
    table_method: str = "cluster"
    markdown_page_separator: str = ""
    with_layout: bool = False
    hybrid_url: str = ""
    timeout_seconds: int = PDF_CONVERT_TIMEOUT_SECONDS


def _post_process_markdown(md_text: str) -> str:
    """Post-procesamiento ligero del MD.

    Solo aplica correcciones genéricas que pueden
    aparecer con cualquier extractor PDF.
    """
    md_text = fix_ligatures(md_text)
    md_text = sanitize_text_to_utf8(md_text)
    return redact_personal_data(md_text)


def _safe_pdf_path(pdf_path: Path, tmpdir: str) -> Path:
    """Si la ruta completa del PDF tiene caracteres no-ASCII,
    lo copia a un directorio temporal con ruta ASCII segura.

    Java en WSL2/Windows no maneja bien rutas con acentos,
    eñes u otros caracteres fuera de ASCII (los corrompe
    al resolverlos contra el filesystem NTFS).
    Se comprueba la ruta COMPLETA, no solo el nombre.
    """
    full_path = str(pdf_path)
    try:
        full_path.encode("ascii")
        return pdf_path
    except UnicodeEncodeError:
        pass

    safe_name = f"_input_{os.getpid()}.pdf"
    safe_path = Path(tmpdir) / safe_name
    shutil.copy2(pdf_path, safe_path)
    logger.debug(
        "Ruta con caracteres no-ASCII, copiado a: %s",
        safe_path,
    )
    return safe_path


def _jar_ref() -> Traversable:
    """Referencia al JAR de opendataloader dentro del paquete instalado.

    Extraído para que un test pueda comprobar **esta misma** expresión. Repetir la ruta en el test
    no vigila nada: `as_file` devuelve una ruta inexistente sin lanzar, así que una 2.x que mueva o
    renombre el JAR pasa CI —todos los tests simulan el subprocess— y falla en producción con cada
    documento, como `rc != 0` seguido de una cascada de reparación que no puede arreglar nada.
    """
    return pkg_resources.files("opendataloader_pdf").joinpath("jar", "opendataloader-pdf-cli.jar")


def _run_opendataloader(
    pdf_path: Path,
    output_dir: str,
    config: ConverterConfig,
) -> None:
    """Ejecuta el JAR de opendataloader-pdf directamente.

    Fuerza UTF-8 en la JVM para evitar errores de encoding
    con caracteres especiales en PDFs médicos (símbolos,
    acentos, caracteres griegos, etc.).

    Captura bytes crudos de stdout/stderr para que errores
    de encoding en la salida de Java no aborten la
    conversión (el .md en disco se escribe correctamente).
    """
    jar_ref = _jar_ref()

    writable_tmp = output_dir

    with pkg_resources.as_file(jar_ref) as jar_path:
        cmd = [
            "java",
            "-Xmx1g",
            f"-Djava.io.tmpdir={writable_tmp}",
            "-Dfile.encoding=UTF-8",
            "-Dsun.jnu.encoding=UTF-8",
            "-Dstdout.encoding=UTF-8",
            "-Dstderr.encoding=UTF-8",
            "-jar",
            str(jar_path),
            str(pdf_path),
            "--output-dir",
            output_dir,
            "--format",
            "markdown,json" if config.with_layout else "markdown",
            "--image-output",
            config.image_output,
            "--table-method",
            config.table_method,
        ]
        if config.use_struct_tree:
            cmd.append("--use-struct-tree")
        if config.include_header_footer:
            cmd.append("--include-header-footer")
        if config.keep_line_breaks:
            cmd.append("--keep-line-breaks")
        if config.markdown_page_separator:
            cmd.extend(
                [
                    "--markdown-page-separator",
                    config.markdown_page_separator,
                ]
            )
        if config.hybrid_url:
            cmd.extend(
                [
                    "--hybrid",
                    "docling-fast",
                    "--hybrid-url",
                    config.hybrid_url,
                    "--hybrid-timeout",
                    str(config.timeout_seconds * 1000),
                ]
            )

        env = _minimal_subprocess_env(writable_tmp)

        try:
            result = subprocess.run(  # noqa: S603 - argv literal, sin shell; ver docstring
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                env=env,
                timeout=config.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"opendataloader excedió {config.timeout_seconds}s para {pdf_path.name}"
            ) from exc

        if result.returncode != 0:
            stderr_raw = result.stderr.decode("utf-8", errors="replace")
            error_lines = [
                line
                for line in stderr_raw.splitlines()
                if not line.startswith("Picked up ") and line.strip()
            ]
            error_text = "\n".join(error_lines).strip()

            if result.returncode < 0:
                sig = abs(result.returncode)
                error_text = f"Proceso Java terminado por señal {sig} (posible OOM). " + error_text

            if not error_text:
                error_text = (
                    "Java no produjo salida de error. "
                    "Posibles causas: PDF protegido/corrupto,"
                    " falta de memoria, o formato no "
                    "soportado."
                )

            logger.error(
                "opendataloader falló (rc=%d) para %s:\n%s",
                result.returncode,
                pdf_path.name,
                error_text[:1500],
            )
            raise RuntimeError(
                f"opendataloader falló (rc={result.returncode})"
                f" para {pdf_path.name}: "
                f"{error_text[:500]}"
            )


def _repair_pdf_with_ghostscript(pdf_path: Path, output_path: Path) -> None:
    """Re-emite un PDF con ghostscript para sanear xref/objects.

    VeraPDF (usado internamente por opendataloader) es estricto y
    rechaza PDFs con cross-reference tables o object streams
    ligeramente fuera de spec. Re-renderizar con `gs` reconstruye
    esas estructuras y suele permitir el segundo intento.

    `-dSAFER` es el default desde ghostscript 9.50, pero se pasa explícito: la entrada es un PDF
    subido y el aislamiento del intérprete PostScript no debe depender del default de la versión
    que traiga la imagen base.
    """
    cmd = [
        "gs",
        "-dSAFER",
        "-o",
        str(output_path),
        "-sDEVICE=pdfwrite",
        "-dPDFSETTINGS=/default",
        "-dNOPAUSE",
        "-dBATCH",
        "-dQUIET",
        str(pdf_path),
    ]
    env = _minimal_subprocess_env(str(output_path.parent))
    try:
        result = subprocess.run(  # noqa: S603 - argv literal, sin shell; ver docstring
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=env,
            timeout=PDF_REPAIR_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"ghostscript excedió {PDF_REPAIR_TIMEOUT_SECONDS}s con {pdf_path.name}"
        ) from exc
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace")
        raise RuntimeError(f"ghostscript falló al reparar {pdf_path.name}: {stderr[:300]}")
    if not output_path.exists() or output_path.stat().st_size == 0:
        raise RuntimeError(f"ghostscript no produjo PDF reparado para {pdf_path.name}")


def _repair_pdf_with_pikepdf(pdf_path: Path, output_path: Path) -> None:
    """Re-emite un PDF con pikepdf (qpdf) para sanear xref/objects.

    Más agresivo que ghostscript cuando la cross-reference table
    o un object stream están gravemente dañados. pikepdf abre
    con attempt_recovery=True por defecto y reescribe los object
    streams desde cero al guardar.
    """
    import pikepdf

    try:
        with pikepdf.open(str(pdf_path)) as pdf:
            pdf.save(
                str(output_path),
                object_stream_mode=pikepdf.ObjectStreamMode.generate,
                fix_metadata_version=True,
            )
    except pikepdf.PasswordError as exc:
        raise RuntimeError(
            f"pikepdf: PDF protegido con contraseña ({pdf_path.name}): {exc}"
        ) from exc
    except Exception as exc:
        raise RuntimeError(f"pikepdf falló al reparar {pdf_path.name}: {str(exc)[:300]}") from exc
    if not output_path.exists() or output_path.stat().st_size == 0:
        raise RuntimeError(f"pikepdf no produjo PDF reparado para {pdf_path.name}")


def _try_repair_pdf(
    safe_pdf: Path,
    tmpdir: str,
    original_name: str,
) -> Path | None:
    """Intenta reparar un PDF rechazado por VeraPDF en cascada.

    Estrategias, de menos a más invasiva:
        1. ghostscript: regenera xref re-emitiendo el PDF.
        2. pikepdf (qpdf): aggressive recovery del object stream.

    Las dos son complementarias, no redundantes: `gs` re-interpreta y re-emite el documento
    entero, así que arregla content streams dañados; qpdf reconstruye xref y object streams pero
    no reescribe contenido. El `Dockerfile` instala ambas.

    Returns:
        Path al PDF reparado si alguna estrategia funcionó,
        None si todas fallaron.
    """
    logger.warning(
        "opendataloader falló para %s, intentando reparar...",
        original_name,
    )
    strategies = (
        ("ghostscript", _repair_pdf_with_ghostscript),
        ("pikepdf", _repair_pdf_with_pikepdf),
    )
    for name, repair_fn in strategies:
        repaired = Path(tmpdir) / f"_repaired_{name}.pdf"
        try:
            repair_fn(safe_pdf, repaired)
        except Exception as e:
            logger.warning(
                "Reparación con %s falló para %s: %s",
                name,
                original_name,
                e,
            )
            continue
        logger.info("PDF reparado con %s: %s", name, original_name)
        return repaired
    return None


def _run_opendataloader_with_repair(
    safe_pdf: Path,
    tmpdir: str,
    config: ConverterConfig,
    original_name: str,
) -> None:
    """Llama a opendataloader; si VeraPDF rechaza el PDF, intenta
    repararlo con ghostscript/pikepdf y reintenta.

    Si el reparado tampoco abre, re-lanza el error original (más
    informativo que el del PDF re-emitido).
    """
    try:
        _run_opendataloader(safe_pdf, tmpdir, config)
        return
    except RuntimeError as orig_err:
        repaired = _try_repair_pdf(safe_pdf, tmpdir, original_name)
        if repaired is None:
            raise orig_err

        for pattern in ("*.md", "*.json"):
            for f in Path(tmpdir).rglob(pattern):
                f.unlink(missing_ok=True)

        try:
            _run_opendataloader(repaired, tmpdir, config)
        except RuntimeError as exc:
            raise orig_err from exc


_ADAPTIVE_MIN_PAGES = 5
_MIN_CHARS_PER_PAGE = 300


def _count_pages(pdf_path: Path) -> int | None:
    """Cuenta páginas con pikepdf; None si no se puede determinar."""
    try:
        import pikepdf

        with pikepdf.open(str(pdf_path)) as pdf:
            return len(pdf.pages)
    except Exception:
        return None


def _read_layout(md_file: Path, pdf_name: str) -> Layout:
    json_file = md_file.with_suffix(".json")
    if not json_file.exists():
        raise RuntimeError(f"opendataloader no generó .json para {pdf_name}")
    return summarize_layout(json.loads(_safe_read_md(json_file)))


def _convert_once(pdf_path: Path, config: ConverterConfig) -> ConversionResult:
    """Una pasada de conversión PDF→MD con la config dada.

    `n_pages` sale de `_count_pages`. Estaba fijo a `0`, así que el campo mentía en **todas** las
    conversiones: no lo lee nadie hoy —`entry.py` solo usa `.markdown`—, pero un campo que siempre
    dice cero es una trampa para el próximo que lo consulte. `0` sigue siendo el valor cuando
    pikepdf no puede determinarlo, que es lo que ya significaba antes.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        safe_pdf = _safe_pdf_path(pdf_path, tmpdir)

        start = time.time()
        _run_opendataloader_with_repair(safe_pdf, tmpdir, config, pdf_path.name)
        elapsed = time.time() - start

        md_files = sorted(Path(tmpdir).rglob("*.md"))
        if not md_files:
            raise RuntimeError(f"opendataloader no generó .md para {pdf_path.name}")
        if len(md_files) > 1:
            logger.warning(
                "opendataloader generó %d ficheros .md para %s; se usa %s",
                len(md_files),
                pdf_path.name,
                md_files[0].name,
            )
        md_text = _safe_read_md(md_files[0])
        layout = _read_layout(md_files[0], pdf_path.name) if config.with_layout else None

    md_text = _post_process_markdown(md_text)

    n_tables = md_text.count("| ---") + md_text.count("|---")

    return ConversionResult(
        markdown=md_text,
        source_path=pdf_path,
        elapsed_seconds=round(elapsed, 2),
        n_pages=_count_pages(pdf_path) or 0,
        n_tables=n_tables,
        original_size_bytes=pdf_path.stat().st_size,
        layout=layout,
    )


def convert_pdf(
    pdf_path: str | Path,
    config: ConverterConfig | None = None,
) -> ConversionResult:
    """Convierte un PDF a Markdown.

    Aplica un guardia adaptativo: si la extracción sale sospechosamente
    escasa (chars/página por debajo del umbral), reintenta con la
    estrategia struct-tree opuesta y se queda con la más completa.

    Una conversión sin texto es un error, no un éxito vacío. La guarda adaptativa no cubre
    este caso: necesita `_count_pages` y un PDF así suele dar `None` o `0` páginas, con lo que
    ni se dispara. Sin esto, un PDF que se abre pero no rinde Markdown se archiva como
    convertido y llega vacío al LLM. La reparación con ghostscript hace ese camino alcanzable
    —re-emite basura como un PDF válido y vacío— y un PDF escaneado sin capa de texto ya
    entraba por él antes.

    Args:
        pdf_path: Ruta al archivo PDF.
        config: Configuración del converter.
            Si None, usa defaults.

    Returns:
        ConversionResult con el Markdown y metadatos.

    Raises:
        FileNotFoundError: Si el PDF no existe.
        ValueError: Si el archivo no es un PDF.
        RuntimeError: Si opendataloader no abre el PDF ni tras la cascada de reparación, o si
            la conversión no rinde Markdown.
    """
    pdf_path = Path(pdf_path)

    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF no encontrado: {pdf_path}")
    if pdf_path.suffix.lower() != ".pdf":
        raise ValueError(f"No es un PDF: {pdf_path.suffix}")

    if config is None:
        config = ConverterConfig()

    result = _convert_once(pdf_path, config)

    pages = _count_pages(pdf_path)
    if (
        pages
        and pages >= _ADAPTIVE_MIN_PAGES
        and result.markdown_chars / pages < _MIN_CHARS_PER_PAGE
    ):
        from dataclasses import replace

        alt_config = replace(config, use_struct_tree=not config.use_struct_tree)
        logger.warning(
            "convert_pdf: extracción escasa (%d chars / %d págs = "
            "%.0f/pág) con use_struct_tree=%s; reintento con %s",
            result.markdown_chars,
            pages,
            result.markdown_chars / pages,
            config.use_struct_tree,
            alt_config.use_struct_tree,
        )
        try:
            alt = _convert_once(pdf_path, alt_config)
        except Exception as exc:
            logger.warning("convert_pdf: reintento adaptativo falló: %s", exc)
            alt = None
        if alt is not None and alt.markdown_chars > result.markdown_chars:
            logger.warning(
                "convert_pdf: la estrategia alternativa rinde más "
                "(%d vs %d chars); se usa la alternativa",
                alt.markdown_chars,
                result.markdown_chars,
            )
            result = alt

    if not result.markdown.strip():
        raise RuntimeError(
            f"conversión sin texto para {pdf_path.name}: el PDF se abrió pero no produjo Markdown"
        )

    return result


def convert_folder(
    input_folder: str | Path,
    output_folder: str | Path,
    config: ConverterConfig | None = None,
    recursive: bool = True,
    force: bool = False,
) -> list[ConversionResult]:
    """Convierte todos los PDFs de una carpeta a MD.

    Replica la estructura de subcarpetas en la salida.

    Args:
        input_folder: Carpeta con PDFs.
        output_folder: Carpeta de salida para MDs.
        config: Configuración del converter.
        recursive: Si True, busca PDFs recursivamente.
        force: Si True, sobreescribe MDs existentes.

    Returns:
        Lista de ConversionResult (uno por PDF procesado).
    """
    input_path = Path(input_folder)
    output_path = Path(output_folder)

    if not input_path.exists():
        raise FileNotFoundError(f"Carpeta no encontrada: {input_path}")

    output_path.mkdir(parents=True, exist_ok=True)

    pattern = "**/*.pdf" if recursive else "*.pdf"
    pdfs = sorted(input_path.glob(pattern))

    if not pdfs:
        logger.warning("Sin PDFs en %s", input_path)
        return []

    logger.info(
        "%d PDF(s) encontrados en %s → %s",
        len(pdfs),
        input_path,
        output_path,
    )

    results: list[ConversionResult] = []

    for pdf_file in pdfs:
        rel = pdf_file.relative_to(input_path)
        md_out = output_path / rel.with_suffix(".md")

        if not force and md_out.exists():
            logger.debug("Omitido (ya existe): %s", rel)
            continue

        md_out.parent.mkdir(parents=True, exist_ok=True)

        try:
            logger.info("Convirtiendo: %s...", rel)

            result = convert_pdf(pdf_file, config)

            md_out.write_text(result.markdown, encoding="utf-8")

            logger.info(
                "OK %s: %.1fs, %s chars, %d tablas",
                rel,
                result.elapsed_seconds,
                f"{result.markdown_chars:,}",
                result.n_tables,
            )

            results.append(result)

        except Exception as e:
            logger.error("Error convirtiendo %s: %s", rel, e)

    if results:
        total_time = sum(r.elapsed_seconds for r in results)
        total_tables = sum(r.n_tables for r in results)
        logger.info(
            "%d convertidos en %.1fs, %d tablas totales",
            len(results),
            total_time,
            total_tables,
        )

    return results