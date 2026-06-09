"""
Employee count verification — cross-references Salesforce data with
Wikidata (free, structured, no API key) to produce a best-estimate
employee count with confidence scoring.

Data sources (priority order):
  1. Wikidata SPARQL  (P1128 — "number of employees")
  2. Salesforce D&B enrichment  (D_B_Number_of_Employees__c)
  3. Salesforce imputed  (Imputed_Number_of_Employees__c)
  4. Salesforce standard  (NumberOfEmployees)
"""
from __future__ import annotations

import logging
import statistics
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
_SPARQL_TIMEOUT = 12  # seconds

_SPARQL_TEMPLATE = """
SELECT ?item ?itemLabel ?employees WHERE {{
  ?item rdfs:label "{name}"@en ;
        wdt:P31/wdt:P279* wd:Q4830453 ;
        wdt:P1128 ?employees .
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
ORDER BY DESC(?employees)
LIMIT 5
"""

_SPARQL_FUZZY_TEMPLATE = """
SELECT ?item ?itemLabel ?employees WHERE {{
  SERVICE wikibase:mwapi {{
    bd:serviceParam wikibase:endpoint "www.wikidata.org" ;
                    wikibase:api "EntitySearch" ;
                    mwapi:search "{name}" ;
                    mwapi:language "en" .
    ?item wikibase:apiOutputItem mwapi:item .
  }}
  ?item wdt:P31/wdt:P279* wd:Q4830453 ;
        wdt:P1128 ?employees .
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
ORDER BY DESC(?employees)
LIMIT 5
"""

_COMPANY_SUFFIXES = (
    ", Inc.", ", Inc", " Inc.", " Inc",
    " Corporation", " Corp.", " Corp",
    " Limited", " Ltd.", " Ltd",
    " LLC", " L.L.C.",
    " PLC", " Plc",
    " Group", " Holdings",
    " Co.", " Co",
    " AG", " SA", " SE", " NV", " N.V.",
    " GmbH", " S.A.", " S.p.A.",
)

# Salesforce-specific naming noise (e.g. "Microsoft Ultimate Parent")
_SF_NOISE_SUFFIXES = (
    " Ultimate Parent", " Parent Account", " Parent",
    " Global", " Worldwide", " International",
    " - APAC", " - EMEA", " - Americas", " - NA",
)


def _normalise_company_name(raw: str) -> list[str]:
    """Return candidate search names, most specific first."""
    name = raw.strip()
    candidates = [name]

    # Strip SF-specific noise first
    upper = name.upper()
    for noise in _SF_NOISE_SUFFIXES:
        if upper.endswith(noise.upper()):
            name = name[: len(name) - len(noise)].strip()
            if name and name not in candidates:
                candidates.append(name)
            upper = name.upper()
            break

    # Strip corporate legal suffixes
    for suffix in _COMPANY_SUFFIXES:
        if upper.endswith(suffix.upper()):
            stripped = name[: len(name) - len(suffix)].strip()
            if stripped and stripped not in candidates:
                candidates.append(stripped)
            break

    return candidates


class WikidataEmployeeLookup:
    """Queries the public Wikidata SPARQL endpoint for employee count."""

    def __init__(self) -> None:
        self._client = httpx.Client(
            timeout=_SPARQL_TIMEOUT,
            headers={"User-Agent": "NutanixRiskAnalyzer/1.0 (internal tool)"},
        )

    def lookup(self, company_name: str) -> int | None:
        """Return the employee count from Wikidata, or None if not found."""
        candidates = _normalise_company_name(company_name)
        for name in candidates:
            result = self._try_exact(name)
            if result is not None:
                return result
        for name in candidates:
            result = self._try_fuzzy(name)
            if result is not None:
                return result
        return None

    def _try_exact(self, name: str) -> int | None:
        safe = name.replace('"', '\\"')
        sparql = _SPARQL_TEMPLATE.format(name=safe)
        return self._execute(sparql)

    def _try_fuzzy(self, name: str) -> int | None:
        safe = name.replace('"', '\\"')
        sparql = _SPARQL_FUZZY_TEMPLATE.format(name=safe)
        return self._execute(sparql)

    def _execute(self, sparql: str) -> int | None:
        try:
            resp = self._client.get(
                _WIKIDATA_SPARQL,
                params={"query": sparql, "format": "json"},
            )
            resp.raise_for_status()
            bindings = resp.json().get("results", {}).get("bindings", [])
            if not bindings:
                return None
            val = bindings[0].get("employees", {}).get("value")
            if val is not None:
                return int(float(val))
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            logger.debug("Wikidata SPARQL query failed: %s", exc)
        return None


_wikidata = WikidataEmployeeLookup()


def verify_employee_count(
    company_name: str,
    sf_standard: int | None = None,
    sf_dб: int | None = None,
    sf_imputed: int | None = None,
) -> dict[str, Any]:
    """
    Cross-reference multiple employee-count sources and pick the best
    estimate.  Returns a dict with all source values, the chosen value,
    and a confidence label.
    """
    wikidata_val = _wikidata.lookup(company_name)

    sources: dict[str, int | None] = {
        "wikidata": wikidata_val,
        "sf_dnb": int(sf_dб) if sf_dб else None,
        "sf_imputed": int(sf_imputed) if sf_imputed else None,
        "sf_standard": int(sf_standard) if sf_standard else None,
    }

    non_zero = {k: v for k, v in sources.items() if v and v > 0}

    if not non_zero:
        return {
            "chosen_value": None,
            "chosen_source": None,
            "confidence": "none",
            "sources": sources,
        }

    values = list(non_zero.values())

    # Check agreement: if 3+ values within 30% of median, use median
    if len(values) >= 3:
        med = statistics.median(values)
        close = [v for v in values if abs(v - med) / med <= 0.30]
        if len(close) >= 3:
            chosen = int(med)
            return {
                "chosen_value": chosen,
                "chosen_source": "consensus",
                "confidence": "high",
                "sources": sources,
            }

    # If 2+ values within 30% of each other, use their average
    if len(values) >= 2:
        med = statistics.median(values)
        close = [v for v in values if abs(v - med) / med <= 0.30]
        if len(close) >= 2:
            chosen = int(statistics.mean(close))
            return {
                "chosen_value": chosen,
                "chosen_source": "consensus",
                "confidence": "high",
                "sources": sources,
            }

    # Preference cascade: wikidata > D&B > imputed > standard
    priority = ["wikidata", "sf_dnb", "sf_imputed", "sf_standard"]
    for key in priority:
        if key in non_zero:
            confidence = "medium" if key in ("wikidata", "sf_dnb") else "low"
            return {
                "chosen_value": non_zero[key],
                "chosen_source": key,
                "confidence": confidence,
                "sources": sources,
            }

    first_key = next(iter(non_zero))
    return {
        "chosen_value": non_zero[first_key],
        "chosen_source": first_key,
        "confidence": "low",
        "sources": sources,
    }
