"""Parse European legal/Impressum pages to extract owner, registration, and tax information."""

import re
import logging
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from urllib.parse import urljoin
from bs4 import BeautifulSoup
import requests
from fake_useragent import UserAgent

from app.cache import get_cache

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Unicode-aware name part pattern (supports European diacritics)
# ---------------------------------------------------------------------------
# Matches a capitalised name part: Müller, François, Łukasz, João, Ørsted …
_NAME_PART = r"[A-ZÀ-ÖØ-Þ\u0100-\u017E][a-zà-öø-ÿ\u0101-\u017F]+"

# Nobiliary particles / prepositions that may appear between name parts
_PARTICLE = r"(?:von|van|de|da|di|del|della|dos|das|du|la|le|el|ten|ter|zum|zur)"

# Full name: at least two capitalised parts, optionally separated by particles
# Examples: "Max Müller", "Jean-Pierre de la Fontaine", "María del Carmen López"
_FULL_NAME = (
    rf"{_NAME_PART}"                          # first name
    rf"(?:-{_NAME_PART})?"                    # optional hyphenated first name
    rf"(?:\s+{_PARTICLE})*"                   # optional particles
    rf"(?:\s+{_NAME_PART})+"                  # surname(s)
)


@dataclass
class LegalPageResult:
    """Structured data extracted from a European legal / Impressum page."""

    owner_name: Optional[str] = None
    owner_title: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    company_registration: Optional[str] = None
    vat_id: Optional[str] = None
    confidence: str = "low"  # low, medium, high


# ---------------------------------------------------------------------------
# Title keywords per country / language
# ---------------------------------------------------------------------------
# Each tuple: (regex_fragment, canonical_english_title)
_TITLE_KEYWORDS: List[Tuple[str, str]] = [
    # German
    (r"Geschäftsführer(?:in)?",      "Managing Director"),
    (r"Inhaber(?:in)?",              "Owner"),
    (r"Einzelunternehmer(?:in)?",    "Sole Proprietor"),
    (r"Vorstand",                    "Board Member"),
    (r"Prokurist(?:in)?",           "Authorised Signatory"),
    (r"Verantwortlich(?:er|e)?",     "Responsible Person"),
    # French
    (r"Gérant(?:e)?",               "Manager"),
    (r"Directeur(?:\s+général)?",    "Director"),
    (r"Directrice(?:\s+générale)?",  "Director"),
    (r"Président(?:e)?",            "President"),
    (r"Responsable",                 "Responsible Person"),
    (r"Propriétaire",               "Owner"),
    # Spanish
    (r"Administrador(?:a)?",         "Administrator"),
    (r"Director(?:a)?(?:\s+General)?", "Director"),
    (r"Titular",                     "Holder"),
    (r"Propietario(?:a)?",          "Owner"),
    (r"Representante\s+Legal",       "Legal Representative"),
    # Italian
    (r"Titolare",                    "Holder"),
    (r"Amministratore(?:\s+Delegato)?", "Administrator"),
    (r"Legale\s+Rappresentante",     "Legal Representative"),
    (r"Proprietario(?:a)?",         "Owner"),
    (r"Direttore(?:\s+Generale)?",   "Director"),
    # Dutch
    (r"Eigenaar",                    "Owner"),
    (r"Directeur",                   "Director"),
    (r"Bestuurder",                  "Board Member"),
    (r"Beheerder",                   "Manager"),
    # Polish
    (r"Właściciel(?:ka)?",          "Owner"),
    (r"Prezes(?:\s+Zarządu)?",      "President"),
    (r"Dyrektor(?:\s+Generalny)?",   "Director"),
    (r"Kierownik",                   "Manager"),
    # Portuguese
    (r"Proprietário(?:a)?",         "Owner"),
    (r"Gerente",                     "Manager"),
    (r"Diretor(?:a)?(?:\s+Geral)?",  "Director"),
    (r"Administrador(?:a)?",         "Administrator"),
    (r"Responsável",                "Responsible Person"),
]

# Compile a single alternation of all title fragments for quick matching
_TITLE_PATTERN = "|".join(rf"(?:{kw})" for kw, _ in _TITLE_KEYWORDS)


# ---------------------------------------------------------------------------
# VAT / tax-ID patterns per country
# ---------------------------------------------------------------------------
_VAT_PATTERNS: List[Tuple[str, str]] = [
    # Germany — USt-IdNr
    (r"USt[\-\.]?\s*(?:Id[\-\.]?\s*)?Nr\.?\s*:?\s*(DE\s?\d{9})", "DE"),
    # France — TVA intracommunautaire
    (r"TVA\s*(?:intra(?:communautaire)?)?\s*:?\s*(FR\s?\d{2}\s?\d{9})", "FR"),
    # Spain — CIF / NIF / NIE
    (r"(?:CIF|NIF|NIE)\s*:?\s*([A-Z]\d{7}[A-Z0-9])", "ES"),
    # Italy — Partita IVA
    (r"P\.?\s*IVA\s*:?\s*(IT\s?\d{11})", "IT"),
    # Netherlands — BTW-nummer
    (r"BTW\s*(?:nummer|nr\.?)?\s*:?\s*(NL\d{9}B\d{2})", "NL"),
    # Netherlands — KVK (Chamber of Commerce, not strictly VAT but commonly expected)
    (r"(?:KVK|KvK)\s*(?:nummer|nr\.?)?\s*:?\s*(\d{8})", "NL_KVK"),
    # Poland — NIP
    (r"NIP\s*:?\s*(\d{3}[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2})", "PL"),
    # Portugal — NIF / NIPC
    (r"(?:NIF|NIPC)\s*:?\s*(\d{9})", "PT"),
]

# ---------------------------------------------------------------------------
# Company registration patterns
# ---------------------------------------------------------------------------
_REGISTRATION_PATTERNS: List[Tuple[str, str]] = [
    # Germany — Handelsregister
    (r"(?:Handelsregister|HR[AB])\s*:?\s*((?:HR[AB]\s*\d+|[A-Z]{2,3}\s*\d{3,})(?:\s*[A-Z])?)", "DE"),
    (r"Registergericht\s*:?\s*(.+?)(?:\n|$|,)", "DE"),
    (r"Amtsgericht\s+(\S+(?:\s+\S+)?)\s*,?\s*(?:HR[AB]\s*\d+)", "DE"),
    # France — RCS / SIRET / SIREN
    (r"RCS\s*:?\s*(.+?\d{3}\s?\d{3}\s?\d{3})", "FR"),
    (r"SIRET\s*:?\s*(\d{3}\s?\d{3}\s?\d{3}\s?\d{5})", "FR"),
    (r"SIREN\s*:?\s*(\d{3}\s?\d{3}\s?\d{3})", "FR"),
    # Spain — Registro Mercantil
    (r"Registro\s+Mercantil\s*(?:de\s+\w+)?\s*:?\s*(.+?)(?:\n|$|\.)", "ES"),
    # Italy — Registro delle Imprese / REA
    (r"Registro\s+(?:delle\s+)?Imprese\s*(?:di\s+\w+)?\s*:?\s*(.+?)(?:\n|$|\.)", "IT"),
    (r"REA\s*:?\s*([A-Z]{2}[\s\-]?\d+)", "IT"),
    # Netherlands — KVK (also used as registration)
    (r"(?:KVK|KvK)\s*(?:nummer|nr\.?)?\s*:?\s*(\d{8})", "NL"),
    # Poland — KRS / REGON
    (r"KRS\s*:?\s*(\d{10})", "PL"),
    (r"REGON\s*:?\s*(\d{9,14})", "PL"),
    # Portugal — NIPC (also serves as registration)
    (r"NIPC\s*:?\s*(\d{9})", "PT"),
]


# ---------------------------------------------------------------------------
# Legal-page URL suffixes to probe
# ---------------------------------------------------------------------------
_LEGAL_PAGE_SUFFIXES: List[str] = [
    # German / Austrian / Swiss
    "/impressum",
    "/imprint",
    # French
    "/mentions-legales",
    "/mentions_legales",
    # Spanish
    "/aviso-legal",
    "/avisolegal",
    # Italian
    "/note-legali",
    # Dutch
    "/juridisch",
    "/disclaimer",
    # Polish
    "/informacje-prawne",
    # Portuguese
    "/avisos-legais",
]


# ---------------------------------------------------------------------------
# Email / phone patterns
# ---------------------------------------------------------------------------
_EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"
)

# Broad European phone pattern: optional +/00 country code, digits/spaces/dashes
_PHONE_RE = re.compile(
    r"(?:Tel(?:efon|éphone|efono)?\.?\s*:?\s*)"  # label prefix (optional match)
    r"((?:\+|00)[\d\s\-/().]{7,20})",
    re.IGNORECASE,
)

# Fallback: standalone phone-like number without label
_PHONE_STANDALONE_RE = re.compile(
    r"((?:\+|00)\d[\d\s\-/().]{7,20})"
)


class LegalPageParser:
    """
    Parse European legal / Impressum pages to extract structured business data.

    Supports German, French, Spanish, Italian, Dutch, Polish, and Portuguese
    legal page conventions.

    Usage::

        parser = LegalPageParser()
        result = parser.parse(html_string)

        # Or fetch + parse automatically:
        result = parser.fetch_and_parse("example.de")
    """

    def __init__(
        self,
        timeout: int = 10,
        use_cache: bool = True,
        cache_path: str = "./cache/cache.sqlite",
    ):
        self.timeout = timeout
        self.ua = UserAgent()
        self.session = requests.Session()
        self.use_cache = use_cache
        self.cache = get_cache(cache_path) if use_cache else None

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _get_headers(self) -> Dict[str, str]:
        """Get request headers with a randomised user-agent."""
        return {
            "User-Agent": self.ua.random,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "de,en;q=0.9,fr;q=0.8,es;q=0.7,it;q=0.6,nl;q=0.5,pl;q=0.4,pt;q=0.3",
        }

    def _fetch_page(self, url: str) -> Optional[str]:
        """
        Fetch a page with SSL fallback.

        Tries ``verify=True`` first; on SSL errors retries with ``verify=False``.
        """
        headers = self._get_headers()
        try:
            response = self.session.get(
                url,
                headers=headers,
                timeout=self.timeout,
                allow_redirects=True,
                verify=True,
            )
            response.raise_for_status()
            return response.text
        except requests.exceptions.SSLError:
            logger.debug(f"SSL error for {url}, retrying without verification")
            try:
                response = self.session.get(
                    url,
                    headers=headers,
                    timeout=self.timeout,
                    allow_redirects=True,
                    verify=False,
                )
                response.raise_for_status()
                return response.text
            except Exception as e:
                logger.debug(f"Failed to fetch {url} (no verify): {e}")
                return None
        except Exception as e:
            logger.debug(f"Failed to fetch {url}: {e}")
            return None

    # ------------------------------------------------------------------
    # Static helpers
    # ------------------------------------------------------------------

    @staticmethod
    def get_legal_page_urls(base_url: str) -> List[str]:
        """
        Return a list of candidate legal-page URLs to probe for a given site.

        Args:
            base_url: Base website URL (e.g. ``https://example.de``).

        Returns:
            Ordered list of full URLs to try.
        """
        # Normalise base URL
        base = base_url.rstrip("/")
        if not base.startswith(("http://", "https://")):
            base = f"https://{base}"

        return [urljoin(base + "/", suffix.lstrip("/")) for suffix in _LEGAL_PAGE_SUFFIXES]

    # ------------------------------------------------------------------
    # Text extraction from HTML
    # ------------------------------------------------------------------

    @staticmethod
    def _html_to_text(html: str) -> str:
        """Strip HTML tags and collapse whitespace, preserving line breaks."""
        soup = BeautifulSoup(html, "lxml")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        # Insert newlines around block-level elements for better line splitting
        for br in soup.find_all("br"):
            br.replace_with("\n")
        text = soup.get_text(separator="\n")
        # Collapse runs of blank lines but keep single newlines
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text

    # ------------------------------------------------------------------
    # Individual extraction methods
    # ------------------------------------------------------------------

    def _extract_owner(self, text: str) -> Tuple[Optional[str], Optional[str], str]:
        """
        Extract owner / director name and title from legal page text.

        Returns:
            ``(name, english_title, confidence)``
        """
        # Strategy 1: "Title: Name" or "Title Name" on same or next line
        lines = text.split("\n")
        for i, line in enumerate(lines):
            line_stripped = line.strip()
            if not line_stripped:
                continue

            for kw_pattern, eng_title in _TITLE_KEYWORDS:
                # Match "Geschäftsführer: Max Müller" or "Inhaber Max Müller"
                pattern = rf"(?:^|\b)({kw_pattern})\s*[:\-–—]?\s*({_FULL_NAME})"
                match = re.search(pattern, line_stripped, re.IGNORECASE)
                if match:
                    title_found = match.group(1).strip()
                    name = match.group(2).strip()
                    return name, eng_title, "high"

                # Match title on its own line, name on the next non-empty line
                title_only = re.match(rf"^\s*({kw_pattern})\s*[:\-–—]?\s*$", line_stripped, re.IGNORECASE)
                if title_only and i + 1 < len(lines):
                    next_line = lines[i + 1].strip()
                    name_match = re.match(rf"^({_FULL_NAME})$", next_line)
                    if name_match:
                        return name_match.group(1).strip(), eng_title, "high"

        # Strategy 2: Look for name patterns near title keywords within a window
        for kw_pattern, eng_title in _TITLE_KEYWORDS:
            # Search entire text for keyword then name within 80 chars
            pattern = rf"({kw_pattern})\s*[:\-–—]?\s*({_FULL_NAME})"
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return match.group(2).strip(), eng_title, "medium"

        # Strategy 3: Standalone name near keywords (lower confidence)
        for kw_pattern, eng_title in _TITLE_KEYWORDS:
            kw_match = re.search(rf"\b{kw_pattern}\b", text, re.IGNORECASE)
            if kw_match:
                # Search for a full name within 200 chars after the keyword
                window = text[kw_match.end():kw_match.end() + 200]
                name_match = re.search(rf"({_FULL_NAME})", window)
                if name_match:
                    return name_match.group(1).strip(), eng_title, "low"

        return None, None, "low"

    def _extract_email(self, text: str) -> Optional[str]:
        """
        Extract the most relevant email address from legal-page text.

        Prefers personal-looking addresses over generic ones.
        """
        emails = _EMAIL_RE.findall(text)
        if not emails:
            return None

        # De-duplicate while preserving order
        seen: set = set()
        unique: List[str] = []
        for e in emails:
            lower = e.lower()
            if lower not in seen:
                seen.add(lower)
                unique.append(lower)

        # Filter out obvious non-business addresses
        filtered = [
            e for e in unique
            if not e.startswith(("noreply", "no-reply", "donotreply", "do-not-reply"))
            and "example.com" not in e
            and "domain.com" not in e
            and "sentry.io" not in e
            and "wixpress.com" not in e
        ]
        if not filtered:
            return None

        # Prefer personal-looking emails (firstname.lastname@) over generic
        for e in filtered:
            local = e.split("@")[0]
            if "." in local and not local.startswith(("info", "contact", "hello", "support", "office", "mail")):
                return e

        return filtered[0]

    def _extract_phone(self, text: str) -> Optional[str]:
        """Extract phone number from legal-page text."""
        # Try labelled pattern first (higher confidence)
        match = _PHONE_RE.search(text)
        if match:
            return self._clean_phone(match.group(1))

        # Fallback to standalone international number
        match = _PHONE_STANDALONE_RE.search(text)
        if match:
            return self._clean_phone(match.group(1))

        return None

    @staticmethod
    def _clean_phone(raw: str) -> str:
        """Normalise a raw phone string."""
        # Remove everything except digits, +, and leading 00
        cleaned = raw.strip()
        # Collapse internal whitespace / punctuation to single space for readability
        cleaned = re.sub(r"[\s\-/().]+", " ", cleaned).strip()
        return cleaned

    def _extract_vat_id(self, text: str) -> Optional[str]:
        """
        Extract VAT / tax ID from text.

        Tries all country-specific patterns and returns the first match.
        """
        for pattern, _country in _VAT_PATTERNS:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                # Return the captured group (the ID itself)
                vat = match.group(1).strip()
                # Normalise spaces
                vat = re.sub(r"\s+", "", vat)
                return vat
        return None

    def _extract_company_registration(self, text: str) -> Optional[str]:
        """
        Extract company registration number from text.

        Tries all country-specific patterns and returns the first match.
        """
        for pattern, _country in _REGISTRATION_PATTERNS:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                reg = match.group(1).strip()
                # Clean trailing punctuation
                reg = reg.rstrip(".,;:")
                return reg
        return None

    # ------------------------------------------------------------------
    # Main parse interface
    # ------------------------------------------------------------------

    def parse(self, html: str) -> LegalPageResult:
        """
        Parse raw HTML of a legal / Impressum page and extract structured data.

        Args:
            html: Raw HTML content of the legal page.

        Returns:
            :class:`LegalPageResult` with extracted fields and confidence score.
        """
        if not html or not html.strip():
            logger.warning("Empty HTML provided to LegalPageParser.parse()")
            return LegalPageResult()

        try:
            text = self._html_to_text(html)
        except Exception as e:
            logger.error(f"Failed to extract text from HTML: {e}")
            return LegalPageResult()

        owner_name, owner_title, confidence = self._extract_owner(text)
        email = self._extract_email(text)
        phone = self._extract_phone(text)
        company_registration = self._extract_company_registration(text)
        vat_id = self._extract_vat_id(text)

        # Determine overall confidence
        confidence = self._compute_confidence(
            owner_name=owner_name,
            owner_confidence=confidence,
            email=email,
            phone=phone,
            company_registration=company_registration,
            vat_id=vat_id,
        )

        return LegalPageResult(
            owner_name=owner_name,
            owner_title=owner_title,
            email=email,
            phone=phone,
            company_registration=company_registration,
            vat_id=vat_id,
            confidence=confidence,
        )

    @staticmethod
    def _compute_confidence(
        owner_name: Optional[str],
        owner_confidence: str,
        email: Optional[str],
        phone: Optional[str],
        company_registration: Optional[str],
        vat_id: Optional[str],
    ) -> str:
        """
        Compute overall result confidence from individual extractions.

        Scoring heuristic:
        - owner found with high confidence: +3
        - owner found with medium confidence: +2
        - owner found with low confidence: +1
        - email found: +1
        - phone found: +1
        - registration found: +1
        - VAT found: +1

        Result: >=4 → high, >=2 → medium, else low
        """
        score = 0
        if owner_name:
            score += {"high": 3, "medium": 2, "low": 1}.get(owner_confidence, 1)
        if email:
            score += 1
        if phone:
            score += 1
        if company_registration:
            score += 1
        if vat_id:
            score += 1

        if score >= 4:
            return "high"
        elif score >= 2:
            return "medium"
        return "low"

    # ------------------------------------------------------------------
    # Convenience: fetch + parse
    # ------------------------------------------------------------------

    def fetch_and_parse(self, domain: str) -> LegalPageResult:
        """
        Attempt to find and parse a legal page for the given domain.

        Tries each candidate URL from :meth:`get_legal_page_urls` in order,
        returning the result from the first page that yields meaningful data.
        Falls back to the best partial result if no single page is conclusive.

        Args:
            domain: Website domain (e.g. ``example.de``) or full URL.

        Returns:
            :class:`LegalPageResult` with the best data found.
        """
        if not domain:
            return LegalPageResult()

        # Check cache first
        if self.use_cache and self.cache:
            cached = self.cache.get(domain, "legal_page", max_age_days=30)
            if cached:
                cached_status = cached.get("cached_status")
                if cached_status == "failed":
                    logger.debug(f"Legal page extraction cached as failed for {domain}")
                    return LegalPageResult()
                elif cached_status == "success":
                    cached_data = {
                        k: v
                        for k, v in cached.items()
                        if k not in ("cached_status", "cached_error_reason")
                    }
                    try:
                        return LegalPageResult(**cached_data)
                    except TypeError:
                        logger.debug(f"Cached legal page data incompatible for {domain}, re-fetching")

        # Normalise to base URL
        base_url = domain
        if not base_url.startswith(("http://", "https://")):
            base_url = f"https://{base_url}"

        urls_to_try = self.get_legal_page_urls(base_url)
        best_result = LegalPageResult()
        best_field_count = 0

        for url in urls_to_try:
            html = self._fetch_page(url)
            if not html:
                continue

            result = self.parse(html)

            # Count how many non-None fields we got
            field_count = sum(
                1
                for val in (
                    result.owner_name,
                    result.email,
                    result.phone,
                    result.company_registration,
                    result.vat_id,
                )
                if val is not None
            )

            if field_count > best_field_count:
                best_result = result
                best_field_count = field_count

            # If we got high confidence, no need to try more pages
            if result.confidence == "high":
                logger.info(f"High-confidence legal page result from {url}")
                break

        # Cache result
        if self.use_cache and self.cache:
            if best_field_count > 0:
                cache_data = {
                    "owner_name": best_result.owner_name,
                    "owner_title": best_result.owner_title,
                    "email": best_result.email,
                    "phone": best_result.phone,
                    "company_registration": best_result.company_registration,
                    "vat_id": best_result.vat_id,
                    "confidence": best_result.confidence,
                }
                self.cache.set(domain, "legal_page", cache_data, status="success")
            else:
                self.cache.set_failure(domain, "legal_page", "no_legal_page_found", ttl_days=7)

        return best_result
