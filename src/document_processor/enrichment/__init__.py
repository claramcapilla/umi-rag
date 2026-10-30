"""Enriquecimiento del JSON clínico con fuentes externas.

Hoy solo BiFiMed (financiación del Ministerio de Sanidad). Es la única parte del motor que sale
a una red que no controlamos, así que su cliente distingue explícitamente «no pude consultar» de
«consulté y no hay datos»: colapsarlas escribe una afirmación clínica falsa en el documento.
"""

from .bifimed_enricher import enrich_ft_with_bifimed

__all__ = ["enrich_ft_with_bifimed"]
