"""
Country -> sales region mapping (APJ / EMEA / AMER).

Used by the Salesforce index sync to bucket each account by
``Account.BillingCountry`` so the dashboard can offer a region quick-filter
without touching the risk-scoring engine.

Behaviour:
  - Free-text input (SFDC ``BillingCountry`` is a string field; spelling
    varies — "USA", "U.S.A.", "United States", "us", etc).
  - Output is one of the constants in :data:`REGIONS` — exactly the
    four buckets the UI knows how to render.
  - Unrecognised / empty country falls into ``"Unknown"``. The dashboard
    badges this so unmapped countries are visible and easy to triage.

Adding a country: append a single entry to ``_COUNTRY_REGIONS`` keyed by
the normalised form (lowercase, ASCII-only, no punctuation, common name).
The normaliser already handles common variants (e.g. "U.K." → "uk",
"United States of America" → "united states"); add explicit aliases only
when the mainline normaliser does not converge.
"""
from __future__ import annotations

from typing import Optional

# Public surface of the module — keep stable; the UI hard-codes these.
REGIONS: tuple[str, ...] = ("APJ", "EMEA", "AMER", "Unknown")


# ── Country → region lookup ─────────────────────────────────────────
# Keyed by the *normalised* country string (see ``_normalise``).
# Curated to cover the high-volume markets; extend as needed.

_COUNTRY_REGIONS: dict[str, str] = {
    # ── AMER (North + South + LATAM) ────────────────────────────────
    "united states":                "AMER",
    "usa":                          "AMER",
    "us":                           "AMER",
    "u s a":                        "AMER",
    "u s":                          "AMER",
    "america":                      "AMER",
    "united states of america":     "AMER",
    "canada":                       "AMER",
    "mexico":                       "AMER",
    "brazil":                       "AMER",
    "brasil":                       "AMER",
    "argentina":                    "AMER",
    "chile":                        "AMER",
    "colombia":                     "AMER",
    "peru":                         "AMER",
    "venezuela":                    "AMER",
    "uruguay":                      "AMER",
    "paraguay":                     "AMER",
    "ecuador":                      "AMER",
    "bolivia":                      "AMER",
    "costa rica":                   "AMER",
    "panama":                       "AMER",
    "guatemala":                    "AMER",
    "honduras":                     "AMER",
    "el salvador":                  "AMER",
    "nicaragua":                    "AMER",
    "dominican republic":           "AMER",
    "puerto rico":                  "AMER",
    "jamaica":                      "AMER",
    "trinidad and tobago":          "AMER",
    "cuba":                         "AMER",
    "haiti":                        "AMER",
    "bahamas":                      "AMER",
    "barbados":                     "AMER",
    "guyana":                       "AMER",
    "suriname":                     "AMER",

    # ── EMEA (Europe + Middle East + Africa) ─────────────────────────
    # Western / Northern / Southern Europe
    "united kingdom":               "EMEA",
    "uk":                           "EMEA",
    "great britain":                "EMEA",
    "england":                      "EMEA",
    "scotland":                     "EMEA",
    "wales":                        "EMEA",
    "northern ireland":             "EMEA",
    "ireland":                      "EMEA",
    "germany":                      "EMEA",
    "deutschland":                  "EMEA",
    "france":                       "EMEA",
    "italy":                        "EMEA",
    "spain":                        "EMEA",
    "portugal":                     "EMEA",
    "netherlands":                  "EMEA",
    "holland":                      "EMEA",
    "belgium":                      "EMEA",
    "luxembourg":                   "EMEA",
    "switzerland":                  "EMEA",
    "austria":                      "EMEA",
    "denmark":                      "EMEA",
    "sweden":                       "EMEA",
    "norway":                       "EMEA",
    "finland":                      "EMEA",
    "iceland":                      "EMEA",
    "greece":                       "EMEA",
    "cyprus":                       "EMEA",
    "malta":                        "EMEA",

    # Central / Eastern Europe
    "poland":                       "EMEA",
    "czech republic":               "EMEA",
    "czechia":                      "EMEA",
    "slovakia":                     "EMEA",
    "hungary":                      "EMEA",
    "romania":                      "EMEA",
    "bulgaria":                     "EMEA",
    "croatia":                      "EMEA",
    "slovenia":                     "EMEA",
    "serbia":                       "EMEA",
    "bosnia and herzegovina":       "EMEA",
    "north macedonia":              "EMEA",
    "macedonia":                    "EMEA",
    "montenegro":                   "EMEA",
    "albania":                      "EMEA",
    "kosovo":                       "EMEA",
    "estonia":                      "EMEA",
    "latvia":                       "EMEA",
    "lithuania":                    "EMEA",
    "moldova":                      "EMEA",
    "ukraine":                      "EMEA",
    "belarus":                      "EMEA",
    "russia":                       "EMEA",
    "russian federation":           "EMEA",
    "turkey":                       "EMEA",
    "turkiye":                      "EMEA",

    # Middle East
    "israel":                       "EMEA",
    "united arab emirates":         "EMEA",
    "uae":                          "EMEA",
    "saudi arabia":                 "EMEA",
    "qatar":                        "EMEA",
    "kuwait":                       "EMEA",
    "bahrain":                      "EMEA",
    "oman":                         "EMEA",
    "jordan":                       "EMEA",
    "lebanon":                      "EMEA",
    "iraq":                         "EMEA",
    "iran":                         "EMEA",
    "yemen":                        "EMEA",
    "syria":                        "EMEA",
    "palestine":                    "EMEA",

    # Africa
    "south africa":                 "EMEA",
    "egypt":                        "EMEA",
    "morocco":                      "EMEA",
    "algeria":                      "EMEA",
    "tunisia":                      "EMEA",
    "libya":                        "EMEA",
    "nigeria":                      "EMEA",
    "kenya":                        "EMEA",
    "ethiopia":                     "EMEA",
    "ghana":                        "EMEA",
    "tanzania":                     "EMEA",
    "uganda":                       "EMEA",
    "angola":                       "EMEA",
    "zimbabwe":                     "EMEA",
    "zambia":                       "EMEA",
    "mozambique":                   "EMEA",
    "cameroon":                     "EMEA",
    "senegal":                      "EMEA",
    "ivory coast":                  "EMEA",
    "cote d ivoire":                "EMEA",
    "mauritius":                    "EMEA",
    "rwanda":                       "EMEA",
    "namibia":                      "EMEA",
    "botswana":                     "EMEA",
    "sudan":                        "EMEA",
    "south sudan":                  "EMEA",
    "democratic republic of the congo": "EMEA",
    "congo":                        "EMEA",
    "madagascar":                   "EMEA",

    # ── APJ (Asia + Pacific + Japan + ANZ) ──────────────────────────
    "india":                        "APJ",
    "japan":                        "APJ",
    "china":                        "APJ",
    "peoples republic of china":    "APJ",
    "prc":                          "APJ",
    "hong kong":                    "APJ",
    "hk":                           "APJ",
    "macau":                        "APJ",
    "macao":                        "APJ",
    "taiwan":                       "APJ",
    "republic of china":            "APJ",
    "south korea":                  "APJ",
    "korea":                        "APJ",
    "republic of korea":            "APJ",
    "korea south":                  "APJ",
    "north korea":                  "APJ",
    "singapore":                    "APJ",
    "malaysia":                     "APJ",
    "indonesia":                    "APJ",
    "philippines":                  "APJ",
    "thailand":                     "APJ",
    "vietnam":                      "APJ",
    "viet nam":                     "APJ",
    "cambodia":                     "APJ",
    "laos":                         "APJ",
    "myanmar":                      "APJ",
    "burma":                        "APJ",
    "brunei":                       "APJ",
    "timor leste":                  "APJ",
    "east timor":                   "APJ",
    "mongolia":                     "APJ",
    "bangladesh":                   "APJ",
    "sri lanka":                    "APJ",
    "nepal":                        "APJ",
    "bhutan":                       "APJ",
    "maldives":                     "APJ",
    "pakistan":                     "APJ",
    "afghanistan":                  "APJ",
    "kazakhstan":                   "APJ",
    "uzbekistan":                   "APJ",
    "kyrgyzstan":                   "APJ",
    "tajikistan":                   "APJ",
    "turkmenistan":                 "APJ",
    "australia":                    "APJ",
    "new zealand":                  "APJ",
    "nz":                           "APJ",
    "fiji":                         "APJ",
    "papua new guinea":             "APJ",
    "solomon islands":              "APJ",
    "vanuatu":                      "APJ",
    "samoa":                        "APJ",
    "tonga":                        "APJ",
    "guam":                         "APJ",
    "new caledonia":                "APJ",
    "french polynesia":             "APJ",
}


def _normalise(value: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace.

    SFDC ``BillingCountry`` is free-text — "U.S.A.", "U.S.", "USA", "U S A"
    should all collapse to the same key. We strip every non-alphanumeric
    character except spaces (which become a single separator) so the
    lookup table can stay small.
    """
    out_chars: list[str] = []
    last_space = False
    for ch in value:
        if ch.isalnum():
            out_chars.append(ch.lower())
            last_space = False
        elif ch.isspace() or ch in "-_,.:;/\\'\"()[]":
            if not last_space and out_chars:
                out_chars.append(" ")
                last_space = True
    return "".join(out_chars).strip()


def country_to_region(country: Optional[str]) -> str:
    """Bucket a free-text country name into ``APJ`` / ``EMEA`` / ``AMER`` / ``Unknown``.

    Empty, ``None`` or unrecognised inputs return ``"Unknown"``. Lookup is
    case-insensitive and tolerates common punctuation variants.
    """
    if not country or not isinstance(country, str):
        return "Unknown"
    key = _normalise(country)
    if not key:
        return "Unknown"
    return _COUNTRY_REGIONS.get(key, "Unknown")


def is_valid_region(value: Optional[str]) -> bool:
    """Return ``True`` if ``value`` is one of the four supported regions.

    Used by the API layer to validate the ``region`` query parameter
    (we accept ``"all"`` separately at the endpoint layer).
    """
    return isinstance(value, str) and value in REGIONS
