"""Enriquecimiento bibliográfico estilo Zotero: DOI/título → Crossref + PubMed.

Dado un DOI o un título, recupera metadatos autoritativos (título, autores, revista,
año, DOI, PMID) de Crossref y completa el PMID vía PubMed (E-utilities). Offline-first:
cualquier fallo de red o respuesta inesperada devuelve ``None`` sin romper la subida.
Todo el acoplamiento externo vive aquí (ver docs/INTEGRATION.md §1).
"""

from __future__ import annotations

import re
from typing import Any, cast
from urllib.parse import quote

import httpx
from django.conf import settings

Metadata = dict[str, Any]

_MIN_TITLE_JACCARD = 0.6
_WORD = re.compile(r"[a-záéíóúñü0-9]+", re.IGNORECASE)


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in _WORD.findall(text)}


def _title_similarity(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


class BibliographyClient:
    """Cliente de metadatos bibliográficos (Crossref + PubMed)."""

    def __init__(
        self,
        *,
        crossref_url: str,
        pubmed_url: str,
        mailto: str,
        client: httpx.Client | None = None,
    ) -> None:
        self._crossref = crossref_url.rstrip("/")
        self._pubmed = pubmed_url.rstrip("/")
        self._mailto = mailto
        ua = f"docsrepo/0.1 (mailto:{mailto})" if mailto else "docsrepo/0.1"
        self._client = client or httpx.Client(timeout=15.0, headers={"User-Agent": ua})

    @classmethod
    def from_settings(cls, client: httpx.Client | None = None) -> BibliographyClient:
        return cls(
            crossref_url=settings.CROSSREF_BASE_URL,
            pubmed_url=settings.PUBMED_BASE_URL,
            mailto=settings.BIBLIO_MAILTO,
            client=client,
        )

    def enrich(self, *, doi: str = "", title: str = "") -> Metadata | None:
        """Devuelve metadatos normalizados a partir de un DOI y/o título, o ``None``."""
        result: Metadata | None = None
        if doi:
            result = self._crossref_by_doi(doi)
        if result is None and title:
            result = self._crossref_by_title(title)
        if result is None:
            return None
        if not result.get("pmid"):
            pmid = self._pubmed_pmid(result.get("doi") or doi, title or result.get("title", ""))
            if pmid:
                result["pmid"] = pmid
        return result

    def _get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        try:
            resp = self._client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError):
            return None

    def _crossref_by_doi(self, doi: str) -> Metadata | None:
        data = self._get(f"{self._crossref}/works/{quote(doi.strip())}")
        if not isinstance(data, dict) or "message" not in data:
            return None
        return _normalize_crossref(cast("dict[str, Any]", data["message"]))

    def _crossref_by_title(self, title: str) -> Metadata | None:
        params = {"query.bibliographic": title, "rows": 5}
        if self._mailto:
            params["mailto"] = self._mailto
        data = self._get(f"{self._crossref}/works", params)
        if not isinstance(data, dict):
            return None
        items = data.get("message", {}).get("items", [])
        best: Metadata | None = None
        best_score = 0.0
        for item in items:
            meta = _normalize_crossref(item)
            score = _title_similarity(title, meta["title"])
            if score > best_score:
                best, best_score = meta, score
        if best is None or best_score < _MIN_TITLE_JACCARD:
            return None
        return best

    def _pubmed_pmid(self, doi: str, title: str) -> str:
        term = f"{doi}[DOI]" if doi else title
        if not term:
            return ""
        data = self._get(
            f"{self._pubmed}/esearch.fcgi",
            {"db": "pubmed", "term": term, "retmode": "json", "retmax": 1},
        )
        if not isinstance(data, dict):
            return ""
        idlist = data.get("esearchresult", {}).get("idlist", [])
        return str(idlist[0]) if idlist else ""


def _normalize_crossref(m: dict[str, Any]) -> Metadata:
    """Normaliza un objeto ``message`` de Crossref a los campos de SourceDocument."""
    title = (m.get("title") or [""])[0] if m.get("title") else ""
    authors: list[str] = []
    for a in m.get("author", []) or []:
        name = f"{a.get('given', '')} {a.get('family', '')}".strip()
        if name:
            authors.append(name)
    journal = (m.get("container-title") or [""])[0] if m.get("container-title") else ""
    year = ""
    for key in ("published", "published-print", "published-online", "issued"):
        parts = (m.get(key) or {}).get("date-parts")
        if parts and parts[0]:
            year = str(parts[0][0])
            break
    return {
        "title": title,
        "authors": authors,
        "journal": journal,
        "year": year,
        "doi": str(m.get("DOI") or "").lower(),
        "pmid": "",
    }
