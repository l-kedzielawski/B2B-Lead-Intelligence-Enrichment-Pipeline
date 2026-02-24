"""
Multi-signal company size estimator.

Combines signals from JSON-LD, API providers, legal pages, team/about pages,
review counts, category priors, and website footprint into a single best-guess
company size bucket with confidence and evidence tracking.

Buckets: 1, 2-5, 6-20, 21-50, 51-200, 200+
"""

import re
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


# ── Size buckets ──────────────────────────────────────────────────────────────

# Canonical bucket strings (ordered by midpoint for comparison)
SIZE_BUCKETS = ("1", "2-5", "6-20", "21-50", "51-200", "200+")

# Map each bucket to a numeric midpoint for weighted averaging
_BUCKET_MIDPOINTS: Dict[str, float] = {
    "1": 1,
    "2-5": 3.5,
    "6-20": 13,
    "21-50": 35,
    "51-200": 100,
    "200+": 300,
}

# Reverse: given a numeric value, pick the bucket
_BUCKET_THRESHOLDS: List[Tuple[float, str]] = [
    (1.5, "1"),
    (5.5, "2-5"),
    (20.5, "6-20"),
    (50.5, "21-50"),
    (200.5, "51-200"),
    (float("inf"), "200+"),
]


def _number_to_bucket(n: float) -> str:
    """Convert a numeric employee count to a bucket string."""
    for threshold, bucket in _BUCKET_THRESHOLDS:
        if n < threshold:
            return bucket
    return "200+"


def _parse_employee_string(raw: str) -> Optional[float]:
    """
    Parse an employee count string into a numeric midpoint.

    Handles: "18", "10-50", "50+", "2-5", "~30", "approx. 12", etc.
    Returns None if unparseable.
    """
    if not raw or not raw.strip():
        return None
    s = raw.strip().lower()
    # Remove common prefixes
    s = re.sub(
        r"^(approx\.?|approximately|circa|ca\.?|~|about|environ|ungefähr)\s*", "", s
    )

    # Range: "10-50", "10 - 50", "10–50"
    m = re.match(r"(\d+)\s*[-–—]\s*(\d+)", s)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        return (lo + hi) / 2.0

    # "50+" or "50 +"
    m = re.match(r"(\d+)\s*\+", s)
    if m:
        return float(m.group(1)) * 1.5  # conservative 1.5× multiplier

    # Plain number
    m = re.match(r"(\d+)", s)
    if m:
        return float(m.group(1))

    return None


# ── Signal sources ────────────────────────────────────────────────────────────

# Each signal has a reliability weight (higher = more trustworthy).
# The estimator collects all available signals, converts each to a numeric
# midpoint, then does a weighted average to pick the final bucket.

SIGNAL_WEIGHTS: Dict[str, float] = {
    "api": 10.0,  # Clearbit / Apollo / Hunter employee_count
    "jsonld": 9.0,  # Schema.org numberOfEmployees
    "explicit_text": 7.0,  # "team of 15", "our 12 employees" on website
    "team_page": 6.0,  # Counted team member blocks on /team, /about
    "legal_page": 5.0,  # Registration info sometimes hints at GmbH size
    "careers_page": 5.0,  # Presence/volume of job listings
    "review_count": 2.0,  # Google Maps review count → size heuristic
    "category_prior": 1.5,  # Default size guess per business category
    "social_followers": 1.0,  # Not currently scraped, placeholder
}

# Confidence thresholds (sum of weights of contributing signals)
_CONFIDENCE_HIGH = 7.0
_CONFIDENCE_MEDIUM = 3.0


# ── Category priors ───────────────────────────────────────────────────────────
# Default guess for each business category when we have NO other signals.
# Based on European averages for these business types.

CATEGORY_SIZE_PRIORS: Dict[str, str] = {
    # Small food shops
    "bakery": "2-5",
    "confectionery": "2-5",
    "ice cream": "2-5",
    "chocolatier": "2-5",
    "pastry": "2-5",
    "gelateria": "2-5",
    # Cafes & small restaurants
    "cafe": "2-5",
    "coffee shop": "2-5",
    "bistro": "2-5",
    "trattoria": "2-5",
    # Medium restaurants
    "restaurant": "6-20",
    "fine dining": "6-20",
    "brasserie": "6-20",
    # Hotels & larger HoReCa
    "hotel": "21-50",
    "resort": "51-200",
    "catering": "6-20",
    # Beauty
    "beauty salon": "2-5",
    "spa": "6-20",
    "wellness": "6-20",
    "hair salon": "2-5",
    "nail salon": "1",
    # Distributors & manufacturers (tend to be larger)
    "horeca": "21-50",
    "food distribution": "21-50",
    "food wholesale": "21-50",
    "food manufacturer": "51-200",
    "spice supplier": "6-20",
    # Generic
    "food": "6-20",
    "retail": "6-20",
    "business": "6-20",
}

# Review-count → size heuristic (Google Maps reviews)
# Based on: more reviews = more customers = usually larger business
_REVIEW_SIZE_ESTIMATES: List[Tuple[int, str]] = [
    (2000, "51-200"),
    (500, "21-50"),
    (100, "6-20"),
    (20, "2-5"),
    (0, "1"),
]


# ── Multilingual patterns for explicit employee mentions on web pages ─────────

# Patterns that capture a number + "employees" / "team members" / "Mitarbeiter" etc.
# Group 1 or 2 captures the number.
_EMPLOYEE_COUNT_PATTERNS: List[re.Pattern] = [
    # English
    re.compile(
        r"\b(\d{1,5})\s+(?:employees?|team\s*members?|staff\s*members?|workers?|people)\b",
        re.I,
    ),
    re.compile(r"\bteam\s+of\s+(\d{1,5})\b", re.I),
    re.compile(
        r"\bover\s+(\d{1,5})\s+(?:employees?|team\s*members?|staff|people)\b", re.I
    ),
    re.compile(
        r"\bmore\s+than\s+(\d{1,5})\s+(?:employees?|team\s*members?|staff|people)\b",
        re.I,
    ),
    re.compile(r"\b(?:we\s+are|we're)\s+(?:a\s+team\s+of\s+)?(\d{1,5})\b", re.I),
    re.compile(
        r"\b(\d{1,5})\s+(?:dedicated|skilled|experienced|passionate|professional)\s+(?:employees?|team\s*members?|staff|people)\b",
        re.I,
    ),
    # German
    re.compile(
        r"\b(\d{1,5})\s+(?:Mitarbeiter(?:innen)?|Angestellte[rn]?|Beschäftigte[rn]?|Teammitglieder[n]?)\b",
        re.I,
    ),
    re.compile(r"\bTeam\s+(?:von|aus|mit)\s+(\d{1,5})\b", re.I),
    re.compile(
        r"\büber\s+(\d{1,5})\s+(?:Mitarbeiter(?:innen)?|Angestellte|Beschäftigte)\b",
        re.I,
    ),
    re.compile(
        r"\bmehr\s+als\s+(\d{1,5})\s+(?:Mitarbeiter(?:innen)?|Angestellte|Beschäftigte)\b",
        re.I,
    ),
    re.compile(
        r"\brund\s+(\d{1,5})\s+(?:Mitarbeiter(?:innen)?|Angestellte|Beschäftigte)\b",
        re.I,
    ),
    # French
    re.compile(
        r"\b(\d{1,5})\s+(?:employés?|salariés?|collaborateurs?|membres?\s+d[ue]\s+l['']équipe)\b",
        re.I,
    ),
    re.compile(r"\béquipe\s+de\s+(\d{1,5})\b", re.I),
    re.compile(
        r"\bplus\s+de\s+(\d{1,5})\s+(?:employés?|salariés?|collaborateurs?)\b", re.I
    ),
    re.compile(
        r"\benviron\s+(\d{1,5})\s+(?:employés?|salariés?|collaborateurs?)\b", re.I
    ),
    # Italian
    re.compile(r"\b(\d{1,5})\s+(?:dipendenti|collaboratori|impiegati|addetti)\b", re.I),
    re.compile(r"\bteam\s+di\s+(\d{1,5})\b", re.I),
    re.compile(r"\boltre\s+(\d{1,5})\s+(?:dipendenti|collaboratori|impiegati)\b", re.I),
    re.compile(r"\bcirca\s+(\d{1,5})\s+(?:dipendenti|collaboratori|impiegati)\b", re.I),
    # Spanish
    re.compile(r"\b(\d{1,5})\s+(?:empleados?|trabajadores?|colaboradores?)\b", re.I),
    re.compile(r"\bequipo\s+de\s+(\d{1,5})\b", re.I),
    re.compile(
        r"\bmás\s+de\s+(\d{1,5})\s+(?:empleados?|trabajadores?|colaboradores?)\b", re.I
    ),
    re.compile(
        r"\baprox(?:imadamente)?\s+(\d{1,5})\s+(?:empleados?|trabajadores?)\b", re.I
    ),
    # Portuguese
    re.compile(r"\b(\d{1,5})\s+(?:funcionários|empregados|colaboradores)\b", re.I),
    re.compile(r"\bequipa?\s+(?:de|com)\s+(\d{1,5})\b", re.I),
    re.compile(
        r"\bmais\s+de\s+(\d{1,5})\s+(?:funcionários|empregados|colaboradores)\b", re.I
    ),
    # Dutch
    re.compile(r"\b(\d{1,5})\s+(?:medewerkers?|werknemers?|personeelsleden)\b", re.I),
    re.compile(r"\bteam\s+van\s+(\d{1,5})\b", re.I),
    re.compile(r"\bmeer\s+dan\s+(\d{1,5})\s+(?:medewerkers?|werknemers?)\b", re.I),
    re.compile(r"\bongeveer\s+(\d{1,5})\s+(?:medewerkers?|werknemers?)\b", re.I),
    # Polish
    re.compile(
        r"\b(\d{1,5})\s+(?:pracowników|pracowników|osób|członków\s+zespołu)\b", re.I
    ),
    re.compile(r"\bzespół\s+(?:liczący\s+)?(\d{1,5})\b", re.I),
    re.compile(r"\bponad\s+(\d{1,5})\s+(?:pracowników|osób)\b", re.I),
    re.compile(r"\bokołu?\s+(\d{1,5})\s+(?:pracowników|osób)\b", re.I),
    # Scandinavian
    re.compile(
        r"\b(\d{1,5})\s+(?:anställda|medarbetare|ansatte|medarbejdere)\b", re.I
    ),  # SV/NO/DA
    re.compile(r"\b(\d{1,5})\s+(?:työntekijää|työntekijöitä)\b", re.I),  # FI
]

# ── Team member counting on /team, /about pages ──────────────────────────────

# Patterns for team member blocks: "Name – Title" or structured card-like markup
_TEAM_MEMBER_NAME = r"[A-ZÀ-ÖØ-Þ\u0100-\u017E][a-zà-öø-ÿ\u0101-\u017F]+\s+[A-ZÀ-ÖØ-Þ\u0100-\u017E][a-zà-öø-ÿ\u0101-\u017F]+"
_TEAM_MEMBER_SEPARATOR = r"\s*[-–—|,]\s*"
_TEAM_MEMBER_TITLE = r"[A-Za-zÀ-ÖØ-öø-ÿ\u0100-\u017F ]{3,40}"
_TEAM_MEMBER_PATTERN = re.compile(
    rf"({_TEAM_MEMBER_NAME}){_TEAM_MEMBER_SEPARATOR}({_TEAM_MEMBER_TITLE})",
    re.UNICODE,
)

# Careers / job-page URL fragments that suggest hiring activity
_CAREERS_URL_FRAGMENTS = {
    "/karriere",
    "/career",
    "/careers",
    "/jobs",
    "/job",
    "/stellenangebote",
    "/emploi",
    "/lavoro",
    "/empleo",
    "/vacatures",
    "/praca",
    "/kariera",
    "/lediga-tjanster",
}


# ── Result dataclass ──────────────────────────────────────────────────────────


@dataclass
class CompanySizeEstimate:
    """Result of company size estimation."""

    bucket: Optional[str] = None  # "1", "2-5", "6-20", "21-50", "51-200", "200+"
    confidence: str = "low"  # "high", "medium", "low"
    source: Optional[str] = None  # best signal source label
    evidence: Optional[str] = None  # human-readable evidence string
    signals: List[Dict[str, Any]] = field(default_factory=list)  # all collected signals

    def to_dict(self) -> Dict[str, Any]:
        return {
            "company_size_estimate": self.bucket,
            "company_size_confidence": self.confidence,
            "company_size_source": self.source,
            "company_size_evidence": self.evidence,
        }


# ── Estimator class ───────────────────────────────────────────────────────────


class CompanySizeEstimator:
    """
    Multi-signal company size estimator.

    Usage::

        estimator = CompanySizeEstimator()
        result = estimator.estimate(
            html="<html>...",
            record=row_dict,
            jsonld_employee_count="18",
            api_employee_count=None,
        )
        print(result.bucket, result.confidence, result.evidence)
    """

    def estimate(
        self,
        *,
        html: Optional[str] = None,
        record: Optional[Dict[str, Any]] = None,
        jsonld_employee_count: Optional[str] = None,
        api_employee_count: Optional[str] = None,
        team_page_html: Optional[str] = None,
    ) -> CompanySizeEstimate:
        """
        Estimate company size from all available signals.

        Args:
            html: Homepage or about-page HTML (for explicit text + team counting).
            record: DataFrame row as dict (for review count, category, etc.).
            jsonld_employee_count: Employee count from JSON-LD extraction.
            api_employee_count: Employee count from API provider.
            team_page_html: HTML of a dedicated /team page (if fetched separately).

        Returns:
            CompanySizeEstimate with bucket, confidence, source, and evidence.
        """
        record = record or {}
        signals: List[Dict[str, Any]] = []

        # 1. API provider (highest reliability)
        self._collect_api_signal(api_employee_count, signals)

        # 2. JSON-LD numberOfEmployees
        self._collect_jsonld_signal(jsonld_employee_count, signals)

        # 3. Explicit employee count in page text
        if html:
            self._collect_explicit_text_signal(html, signals)

        # 4. Team member counting from structured page
        combined_html = (html or "") + "\n" + (team_page_html or "")
        if combined_html.strip():
            self._collect_team_page_signal(combined_html, signals)

        # 5. Careers page presence (from crawled pages or links in HTML)
        if html:
            self._collect_careers_signal(html, signals)

        # 6. Google Maps review count
        self._collect_review_signal(record, signals)

        # 7. Category prior (weakest signal, always available)
        self._collect_category_prior(record, signals)

        # Compute weighted average
        return self._compute_estimate(signals)

    # ── Signal collectors ─────────────────────────────────────────────────

    def _collect_api_signal(
        self, api_employee_count: Optional[str], signals: List[Dict[str, Any]]
    ) -> None:
        if not api_employee_count:
            return
        value = _parse_employee_string(str(api_employee_count))
        if value is not None and value > 0:
            signals.append(
                {
                    "source": "api",
                    "weight": SIGNAL_WEIGHTS["api"],
                    "midpoint": value,
                    "bucket": _number_to_bucket(value),
                    "evidence": f"api:employee_count={api_employee_count}",
                }
            )

    def _collect_jsonld_signal(
        self, jsonld_employee_count: Optional[str], signals: List[Dict[str, Any]]
    ) -> None:
        if not jsonld_employee_count:
            return
        value = _parse_employee_string(str(jsonld_employee_count))
        if value is not None and value > 0:
            signals.append(
                {
                    "source": "jsonld",
                    "weight": SIGNAL_WEIGHTS["jsonld"],
                    "midpoint": value,
                    "bucket": _number_to_bucket(value),
                    "evidence": f"jsonld:numberOfEmployees={jsonld_employee_count}",
                }
            )

    def _collect_explicit_text_signal(
        self, html: str, signals: List[Dict[str, Any]]
    ) -> None:
        """Search for explicit employee count mentions in page text."""
        try:
            soup = BeautifulSoup(html, "lxml")
            for tag in soup(["script", "style", "noscript"]):
                tag.decompose()
            text = soup.get_text(separator=" ")
        except Exception:
            return

        # Collect all matches and pick the most frequently mentioned number
        counts: List[int] = []
        evidence_parts: List[str] = []
        for pattern in _EMPLOYEE_COUNT_PATTERNS:
            for m in pattern.finditer(text):
                # The number is in group 1 (sometimes group 2 for some patterns)
                num_str = m.group(1) or (
                    m.group(2) if m.lastindex and m.lastindex >= 2 else None
                )
                if num_str:
                    try:
                        n = int(num_str)
                        if 1 <= n <= 100000:
                            counts.append(n)
                            evidence_parts.append(m.group(0).strip()[:60])
                    except ValueError:
                        pass

        if not counts:
            return

        # Use the median if multiple counts found, otherwise the single value
        counts.sort()
        median_count = counts[len(counts) // 2]

        signals.append(
            {
                "source": "explicit_text",
                "weight": SIGNAL_WEIGHTS["explicit_text"],
                "midpoint": float(median_count),
                "bucket": _number_to_bucket(float(median_count)),
                "evidence": f"explicit_text:{evidence_parts[0]}"
                if evidence_parts
                else "explicit_text",
            }
        )

    def _collect_team_page_signal(
        self, html: str, signals: List[Dict[str, Any]]
    ) -> None:
        """Count team member entries from structured 'Name – Title' patterns."""
        try:
            soup = BeautifulSoup(html, "lxml")
            for tag in soup(["script", "style", "noscript"]):
                tag.decompose()
            text = soup.get_text(separator="\n")
        except Exception:
            return

        matches = _TEAM_MEMBER_PATTERN.findall(text)
        # Deduplicate by name (first capture group)
        unique_names = set()
        for name_part, _ in matches:
            unique_names.add(name_part.strip().lower())

        count = len(unique_names)
        if count < 2:
            return  # Need at least 2 names to be meaningful

        # Team pages usually show a subset of total staff (public-facing only)
        # Apply a conservative multiplier: assume team page shows ~60-80% of staff
        estimated_total = count * 1.3

        signals.append(
            {
                "source": "team_page",
                "weight": SIGNAL_WEIGHTS["team_page"],
                "midpoint": estimated_total,
                "bucket": _number_to_bucket(estimated_total),
                "evidence": f"team_page:counted_{count}_members",
            }
        )

    def _collect_careers_signal(self, html: str, signals: List[Dict[str, Any]]) -> None:
        """Check if the website has a careers/jobs page (indicates larger company)."""
        html_lower = html.lower()
        found_careers = False
        for fragment in _CAREERS_URL_FRAGMENTS:
            # Check for href links to careers pages
            if f'href="{fragment}' in html_lower or f"href='{fragment}" in html_lower:
                found_careers = True
                break
            if f'href="/{fragment.lstrip("/")}"' in html_lower:
                found_careers = True
                break

        if found_careers:
            # Having a careers page suggests at least 6+ employees
            signals.append(
                {
                    "source": "careers_page",
                    "weight": SIGNAL_WEIGHTS["careers_page"],
                    "midpoint": _BUCKET_MIDPOINTS["6-20"],
                    "bucket": "6-20",
                    "evidence": "careers_page:job_listings_link_found",
                }
            )

    def _collect_review_signal(
        self, record: Dict[str, Any], signals: List[Dict[str, Any]]
    ) -> None:
        """Use Google Maps review count as a weak size proxy."""
        review_count = record.get("reviews_count_normalized") or record.get(
            "reviews_count"
        )
        if not review_count:
            return
        try:
            reviews = int(float(review_count))
        except (ValueError, TypeError):
            return

        if reviews <= 0:
            return

        bucket = "1"
        for threshold, b in _REVIEW_SIZE_ESTIMATES:
            if reviews >= threshold:
                bucket = b
                break

        signals.append(
            {
                "source": "review_count",
                "weight": SIGNAL_WEIGHTS["review_count"],
                "midpoint": _BUCKET_MIDPOINTS[bucket],
                "bucket": bucket,
                "evidence": f"review_count:{reviews}_reviews",
            }
        )

    def _collect_category_prior(
        self, record: Dict[str, Any], signals: List[Dict[str, Any]]
    ) -> None:
        """Use business category as a weak default guess."""
        category = str(record.get("canonical_category", "") or "").lower()
        raw_category = str(record.get("category", "") or "").lower()

        bucket = None
        matched_cat = None

        # Try canonical category first, then raw
        for cat_str in (category, raw_category):
            if not cat_str:
                continue
            for prior_key, prior_bucket in CATEGORY_SIZE_PRIORS.items():
                if prior_key in cat_str:
                    bucket = prior_bucket
                    matched_cat = prior_key
                    break
            if bucket:
                break

        if not bucket:
            return

        signals.append(
            {
                "source": "category_prior",
                "weight": SIGNAL_WEIGHTS["category_prior"],
                "midpoint": _BUCKET_MIDPOINTS[bucket],
                "bucket": bucket,
                "evidence": f"category_prior:{matched_cat}→{bucket}",
            }
        )

    # ── Weighted computation ──────────────────────────────────────────────

    def _compute_estimate(self, signals: List[Dict[str, Any]]) -> CompanySizeEstimate:
        """Compute final estimate from all collected signals."""
        if not signals:
            return CompanySizeEstimate(
                bucket=None,
                confidence="low",
                source=None,
                evidence=None,
                signals=signals,
            )

        # If we have a single very strong signal (api or jsonld), just use it
        for sig in signals:
            if sig["source"] in ("api", "jsonld") and sig["weight"] >= 9.0:
                total_weight = sum(s["weight"] for s in signals)
                confidence = self._weight_to_confidence(total_weight)
                return CompanySizeEstimate(
                    bucket=sig["bucket"],
                    confidence=confidence,
                    source=sig["source"],
                    evidence=sig["evidence"],
                    signals=signals,
                )

        # Weighted average of all signal midpoints
        total_weight = 0.0
        weighted_sum = 0.0
        best_source = None
        best_weight = 0.0
        best_evidence = None

        for sig in signals:
            w = sig["weight"]
            weighted_sum += sig["midpoint"] * w
            total_weight += w
            if w > best_weight:
                best_weight = w
                best_source = sig["source"]
                best_evidence = sig["evidence"]

        avg_midpoint = weighted_sum / total_weight
        bucket = _number_to_bucket(avg_midpoint)
        confidence = self._weight_to_confidence(total_weight)

        # Build evidence summary from top signals
        evidence_parts = sorted(signals, key=lambda s: s["weight"], reverse=True)
        evidence_str = "; ".join(
            s["evidence"] for s in evidence_parts[:3] if s.get("evidence")
        )

        return CompanySizeEstimate(
            bucket=bucket,
            confidence=confidence,
            source=best_source,
            evidence=evidence_str,
            signals=signals,
        )

    @staticmethod
    def _weight_to_confidence(total_weight: float) -> str:
        if total_weight >= _CONFIDENCE_HIGH:
            return "high"
        elif total_weight >= _CONFIDENCE_MEDIUM:
            return "medium"
        return "low"
