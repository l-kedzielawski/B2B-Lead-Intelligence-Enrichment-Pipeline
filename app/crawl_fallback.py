"""Website crawling fallback for enrichment."""

import re
import logging
from typing import Dict, Any, Optional, List, Set, Tuple
from urllib.parse import urljoin
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
import requests
from bs4 import BeautifulSoup
from fake_useragent import UserAgent
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .enrich.providers import EnrichmentResult, Contact
from .utils import extract_domain, is_valid_email
from .enrich.jsonld_extractor import JsonLdExtractor
from .cache import is_domain_denylisted

logger = logging.getLogger(__name__)

# Hard timeout for entire fetch operation (DNS + connect + read)
FETCH_HARD_TIMEOUT = 15  # seconds

SUPPORTED_CRAWL_LANGUAGES = {"auto", "en", "de", "fr", "it", "es", "pt", "nl", "pl"}

LANGUAGE_ROUTE_HINTS = {
    "en": ["/contact", "/about", "/about-us", "/team"],
    "de": ["/kontakt", "/impressum", "/uber-uns", "/ueber-uns"],
    "fr": ["/contact", "/a-propos", "/mentions-legales"],
    "it": ["/contatti", "/contatto", "/chi-siamo", "/note-legali"],
    "es": ["/contacto", "/sobre-nosotros", "/aviso-legal"],
    "pt": [
        "/contato",
        "/contactos",
        "/fale-conosco",
        "/sobre-nos",
        "/quem-somos",
        "/avisos-legais",
    ],
    "nl": ["/contact", "/over-ons", "/juridisch", "/disclaimer"],
    "pl": ["/kontakt", "/o-firmie", "/o-nas", "/informacje-prawne"],
}


class WebsiteCrawler:
    """Lightweight website crawler for extracting emails and social links."""

    # Static user agent fallback to avoid fake_useragent hanging
    FALLBACK_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

    def __init__(
        self,
        timeout: int = 10,
        max_pages: int = 5,
        connect_timeout: int = 5,
        language: str = "auto",
    ):
        # Use tuple timeout: (connect_timeout, read_timeout)
        # Connect timeout catches DNS/handshake hangs
        self.timeout = (connect_timeout, timeout)
        self.max_pages = max_pages
        normalized_language = str(language or "auto").strip().lower()
        if normalized_language not in SUPPORTED_CRAWL_LANGUAGES:
            logger.warning(
                "Unsupported crawl language '%s' - falling back to auto",
                language,
            )
            normalized_language = "auto"
        self.language = normalized_language

        # Use static UA to avoid fake_useragent network calls that can hang
        try:
            self.ua = UserAgent(fallback=self.FALLBACK_UA)
        except Exception:
            self.ua = None

        self.session = requests.Session()

        # Disable retries completely - we handle errors ourselves
        # Retries with backoff cause hangs on 503/504 responses
        retry_strategy = Retry(
            total=0,  # No retries
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

    @staticmethod
    def _dedupe_keep_order(items: List[str]) -> List[str]:
        """Return de-duplicated list while preserving insertion order."""
        unique: List[str] = []
        seen: Set[str] = set()
        for item in items:
            if item not in seen:
                seen.add(item)
                unique.append(item)
        return unique

    def _build_candidate_pages(self, base_url: str) -> List[str]:
        """Build ordered candidate URLs, prioritizing selected crawl language."""
        ordered_relative_paths: List[str] = ["/"]

        if self.language != "auto":
            ordered_relative_paths.extend(LANGUAGE_ROUTE_HINTS.get(self.language, []))

        # Keep broad multilingual coverage after language-prioritized routes.
        for language_code in ["en", "de", "fr", "it", "es", "pt", "nl", "pl"]:
            ordered_relative_paths.extend(LANGUAGE_ROUTE_HINTS.get(language_code, []))

        ordered_relative_paths = self._dedupe_keep_order(ordered_relative_paths)

        pages: List[str] = []
        for path in ordered_relative_paths:
            if path == "/":
                pages.append(base_url)
            else:
                pages.append(urljoin(base_url, path))
        return pages

    def _get_headers(self) -> Dict[str, str]:
        """Get random user agent headers."""
        try:
            user_agent = self.ua.random if self.ua else self.FALLBACK_UA
        except Exception:
            user_agent = self.FALLBACK_UA

        return {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        }

    def _fetch_page_internal(
        self, url: str, verify: bool = True
    ) -> Tuple[Optional[str], Optional[str]]:
        """Internal fetch without hard timeout wrapper."""
        try:
            response = self.session.get(
                url,
                headers=self._get_headers(),
                timeout=self.timeout,
                allow_redirects=True,
                verify=verify,
            )
            response.raise_for_status()
            return response.text, None
        except requests.exceptions.SSLError:
            if verify:
                # Retry without SSL verification
                return self._fetch_page_internal(url, verify=False)
            return None, "ssl_error"
        except requests.exceptions.Timeout:
            logger.debug(f"Timeout fetching {url}")
            return None, "timeout"
        except requests.exceptions.ConnectionError as e:
            logger.debug(f"Connection error fetching {url}: {e}")
            return None, "connection_error"
        except requests.exceptions.HTTPError as e:
            logger.debug(f"HTTP error fetching {url}: {e}")
            return None, "http_error"
        except Exception as e:
            logger.debug(f"Failed to fetch {url}: {e}")
            return None, "unknown_error"

    def _fetch_page(self, url: str) -> Tuple[Optional[str], Optional[str]]:
        """Fetch a single page with hard timeout wrapper to prevent hangs."""
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(self._fetch_page_internal, url)
                return future.result(timeout=FETCH_HARD_TIMEOUT)
        except FuturesTimeoutError:
            logger.warning(f"Hard timeout ({FETCH_HARD_TIMEOUT}s) exceeded for {url}")
            return None, "timeout"
        except Exception as e:
            logger.debug(f"Failed to fetch {url}: {e}")
            return None, "unknown_error"

    def _crawl_candidate_pages(self, base_url: str) -> Dict[str, Any]:
        """Crawl candidate pages for one base URL (https/http)."""
        all_emails: Set[str] = set()
        social_links: Dict[str, Optional[str]] = {}
        company_info: Dict[str, Optional[str]] = {}
        pages_crawled = 0
        last_html: Optional[str] = None
        had_transport_failure = False

        candidate_pages = self._build_candidate_pages(base_url)
        pages_to_crawl = candidate_pages[: self.max_pages]

        for page_url in pages_to_crawl:
            if pages_crawled >= self.max_pages:
                break

            html, fetch_error = self._fetch_page(page_url)
            if not html:
                if fetch_error in {"ssl_error", "connection_error", "timeout"}:
                    had_transport_failure = True
                continue

            last_html = html
            pages_crawled += 1

            soup = BeautifulSoup(html, "lxml")

            page_emails = self._extract_emails(html)
            all_emails.update(page_emails)

            if pages_crawled == 1:
                social_links = self._extract_social_links(soup, base_url)
                company_info = self._extract_company_info(soup)

        jsonld_data = {}
        if pages_crawled > 0 and last_html:
            homepage_html, _ = (
                self._fetch_page(base_url) if pages_crawled > 1 else (last_html, None)
            )
            if homepage_html:
                jsonld_data = self._extract_jsonld(homepage_html)
                if jsonld_data.get("email") and is_valid_email(jsonld_data["email"]):
                    all_emails.add(jsonld_data["email"].lower())
                for platform, link in jsonld_data.get("social_links", {}).items():
                    if link and not social_links.get(platform):
                        social_links[platform] = link
                if jsonld_data.get("business_name") and not company_info.get(
                    "company_name"
                ):
                    company_info["company_name"] = jsonld_data["business_name"]
                if jsonld_data.get("description") and not company_info.get(
                    "description"
                ):
                    company_info["description"] = jsonld_data["description"]

        return {
            "all_emails": all_emails,
            "social_links": social_links,
            "company_info": company_info,
            "jsonld_data": jsonld_data,
            "pages_crawled": pages_crawled,
            "had_transport_failure": had_transport_failure,
        }

    def _extract_emails(self, text: str) -> List[str]:
        """Extract email addresses from text."""
        # Pattern to match most email addresses
        pattern = r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
        emails = re.findall(pattern, text)

        # Filter valid emails
        valid_emails = []
        for email in emails:
            email = email.lower()
            # Skip common false positives and noreply emails
            # BUT keep info@ and admin@ - these are useful for small businesses
            if (
                is_valid_email(email)
                and not email.startswith(
                    ("noreply", "no-reply", "donotreply", "do-not-reply")
                )
                and not any(
                    domain in email
                    for domain in ["example.com", "domain.com", "test.com"]
                )
            ):
                valid_emails.append(email)

        return list(set(valid_emails))

    def _extract_social_links(
        self, soup: BeautifulSoup, base_url: str
    ) -> Dict[str, Any]:
        """Extract social media links from page."""
        social_patterns = {
            "linkedin": r"linkedin\.com",
            "facebook": r"facebook\.com",
            "instagram": r"instagram\.com",
            "twitter": r"(twitter\.com|x\.com)",
            "tiktok": r"tiktok\.com",
            "youtube": r"youtube\.com",
        }

        found: Dict[str, Any] = {key: None for key in social_patterns}

        # Check all links
        for link in soup.find_all("a", href=True):
            href = str(link.get("href", ""))
            # Make absolute URL
            if href.startswith("/"):
                href = urljoin(base_url, href)

            for platform, pattern in social_patterns.items():
                if re.search(pattern, href, re.IGNORECASE) and not found[platform]:
                    found[platform] = href

        return found

    def _extract_company_info(self, soup: BeautifulSoup) -> Dict[str, Any]:
        """Extract company name and description from page."""
        result: Dict[str, Any] = {
            "company_name": None,
            "description": None,
        }

        # Try to get title
        title_tag = soup.find("title")
        if title_tag:
            title = str(title_tag.get_text()).strip()
            # Remove common suffixes
            suffixes = [" - Home", " | Home", " - Homepage", " | ", " - "]
            for suffix in suffixes:
                if suffix in title:
                    title = title.split(suffix)[0]
                    break
            result["company_name"] = title.strip()

        # Try to get meta description
        meta_desc = soup.find("meta", attrs={"name": "description"})
        if meta_desc:
            content = meta_desc.get("content", "")
            result["description"] = str(content).strip() if content else None
        else:
            # Try Open Graph description
            og_desc = soup.find("meta", property="og:description")
            if og_desc:
                content = og_desc.get("content", "")
                result["description"] = str(content).strip() if content else None

        return result

    def _extract_email_patterns(
        self, emails: Set[str], domain: str
    ) -> Optional[List[str]]:
        """Extract email pattern from found emails (e.g., firstname.lastname@domain)."""
        if not emails:
            return None

        patterns = set()
        domain_name = extract_domain(domain) or domain

        for email in emails:
            # Extract pattern part (before @)
            local_part = email.split("@")[0]

            # Common patterns:
            # firstname.lastname, first.last, firstname_lastname, firstnamelastname, f.lastname
            if "." in local_part or "_" in local_part:
                # This looks like a pattern
                patterns.add(
                    local_part.replace(
                        local_part.split(("." if "." in local_part else "_"))[0],
                        "firstname",
                    )
                )

        return list(patterns) if patterns else None

    def _extract_jsonld(self, html: str) -> dict:
        """Extract structured data from JSON-LD."""
        try:
            extractor = JsonLdExtractor()
            result = extractor.extract(html)
            if result and not result.is_empty():
                return {
                    "business_name": result.business_name,
                    "description": result.description,
                    "email": result.email,
                    "phone": result.phone,
                    "founder": result.founder_name,
                    "employee_count": result.employee_count,
                    "founding_date": result.founding_date,
                    "social_links": result.social_links or {},
                    "business_type": result.business_type,
                }
        except Exception as e:
            logger.debug(f"JSON-LD extraction failed: {e}")
        return {}

    def crawl(self, domain: str) -> EnrichmentResult:
        """
        Crawl website to extract enrichment data.

        Strategy:
        1. Check denylist
        2. Try homepage
        3. Try /contact page
        4. Try /about page
        """
        logger.info(f"Crawling website: {domain}")

        # Normalize domain and choose protocol strategy.
        raw_target = str(domain).strip()
        if raw_target.startswith(("http://", "https://")):
            url = raw_target
            domain = extract_domain(url) or domain
        else:
            domain = extract_domain(raw_target) or raw_target
            url = f"https://{domain}"

        # Check denylist
        if is_domain_denylisted(domain):
            logger.warning(f"Domain {domain} is denylisted, skipping crawl")
            return EnrichmentResult(
                success=False,
                company_name=domain.replace("-", " ").replace(".", " ").title(),
                error_message="Domain denylisted",
            )

        protocol_used = "https" if url.startswith("https://") else "http"
        crawl_data = self._crawl_candidate_pages(url)

        # HTTPS -> HTTP fallback when HTTPS could not be reached at transport layer.
        if (
            protocol_used == "https"
            and crawl_data["pages_crawled"] == 0
            and crawl_data["had_transport_failure"]
        ):
            http_url = f"http://{domain}"
            logger.info("HTTPS unavailable for %s, retrying over HTTP", domain)
            http_crawl_data = self._crawl_candidate_pages(http_url)
            if http_crawl_data["pages_crawled"] > 0:
                crawl_data = http_crawl_data
                protocol_used = "http"

        all_emails: Set[str] = crawl_data["all_emails"]
        social_links: Dict[str, Optional[str]] = crawl_data["social_links"]
        company_info: Dict[str, Optional[str]] = crawl_data["company_info"]
        jsonld_data = crawl_data["jsonld_data"]

        # Build contacts from emails actually found on website/JSON-LD
        contacts = []
        for email in list(all_emails)[:5]:  # Limit to 5
            source = (
                "jsonld"
                if (jsonld_data.get("email") and email == jsonld_data["email"].lower())
                else "scraped"
            )
            contact = Contact(email=email, email_source=source)
            contacts.append(contact)

        # Extract email patterns from found emails
        email_patterns = self._extract_email_patterns(all_emails, domain)

        # Determine success based on what we found
        success = bool(
            all_emails
            or social_links.get("linkedin")
            or company_info.get("company_name")
            or contacts
        )

        # Build raw response with all data for future use
        raw_response = {
            "social_links": social_links,
            "emails": list(all_emails),
            "jsonld_data": jsonld_data,
            "protocol_used": protocol_used,
        }

        return EnrichmentResult(
            success=success,
            company_name=company_info.get("company_name")
            or domain.replace("-", " ").replace(".", " ").title(),
            description=company_info.get("description"),
            linkedin_url=social_links.get("linkedin"),
            facebook_url=social_links.get("facebook"),
            instagram_url=social_links.get("instagram"),
            tiktok_url=social_links.get("tiktok"),
            twitter_url=social_links.get("twitter"),
            youtube_url=social_links.get("youtube"),
            email_patterns=email_patterns,
            contacts=contacts if contacts else None,
            error_message=None if success else "No enrichment data found",
            raw_response=raw_response,
        )


def crawl_website_fallback(
    domain: str,
    timeout: int = 10,
    connect_timeout: int = 5,
    max_pages: int = 5,
    crawl_language: str = "auto",
) -> EnrichmentResult:
    """
    Convenience function to crawl a website for enrichment.

    Args:
        domain: Website domain or URL
        timeout: Read timeout in seconds
        connect_timeout: Connection timeout in seconds (catches DNS/handshake hangs)
        max_pages: Maximum number of pages to crawl
        crawl_language: Preferred crawl language for route prioritization

    Returns:
        EnrichmentResult with found data
    """
    crawler = WebsiteCrawler(
        timeout=timeout,
        connect_timeout=connect_timeout,
        max_pages=max_pages,
        language=crawl_language,
    )
    return crawler.crawl(domain)


def enrich_with_fallback(
    domain: str,
    primary_provider: Optional[Any] = None,
    use_crawl_fallback: bool = True,
    cache: Optional[Any] = None,
    provider_name: str = "free",
    crawl_timeout: int = 10,
    crawl_connect_timeout: int = 5,
    crawl_max_pages: int = 5,
    crawl_language: str = "auto",
) -> EnrichmentResult:
    """
    Enrich data using primary provider, falling back to website crawl if needed.
    Caches failures to avoid re-trying broken domains.

    Args:
        domain: Website domain
        primary_provider: Optional enrichment provider instance
        use_crawl_fallback: Whether to use website crawling as fallback
        cache: Optional cache instance for storing failures
        provider_name: Provider name for cache key
        crawl_language: Preferred crawl language for route prioritization

    Returns:
        EnrichmentResult
    """
    result = None
    error_reason = None

    # Try primary provider first
    if primary_provider:
        try:
            result = primary_provider.enrich(domain)
            if result.success:
                logger.info(f"Primary provider succeeded for {domain}")
                return result
        except Exception as e:
            logger.warning(f"Primary provider failed for {domain}: {e}")
            error_reason = "api_error"

    # Use website crawling as fallback or supplement
    if use_crawl_fallback:
        # Crawl if primary failed OR to supplement primary with additional data
        should_crawl = not result or not result.success
        should_supplement = (
            result and result.success
        )  # Supplement primary with crawl data

        if should_crawl or should_supplement:
            logger.info(
                f"{'Falling back to' if should_crawl else 'Supplementing with'} website crawl for {domain}"
            )
            crawl_result = crawl_website_fallback(
                domain,
                timeout=crawl_timeout,
                connect_timeout=crawl_connect_timeout,
                max_pages=crawl_max_pages,
                crawl_language=crawl_language,
            )

            # Check if denylisted
            if crawl_result.error_message == "Domain denylisted":
                if cache:
                    cache.set_denylisted(domain, provider_name)
                if should_crawl:
                    return crawl_result
                # If supplementing, just skip the crawl data
            elif should_supplement and crawl_result.success:
                # Merge: use primary but add crawl data for missing fields
                if not result.linkedin_url and crawl_result.linkedin_url:
                    result.linkedin_url = crawl_result.linkedin_url
                if not result.facebook_url and crawl_result.facebook_url:
                    result.facebook_url = crawl_result.facebook_url
                if not result.instagram_url and crawl_result.instagram_url:
                    result.instagram_url = crawl_result.instagram_url
                if not result.twitter_url and crawl_result.twitter_url:
                    result.twitter_url = crawl_result.twitter_url
                if not result.youtube_url and crawl_result.youtube_url:
                    result.youtube_url = crawl_result.youtube_url
                if not result.tiktok_url and crawl_result.tiktok_url:
                    result.tiktok_url = crawl_result.tiktok_url
                if not result.contacts and crawl_result.contacts:
                    result.contacts = crawl_result.contacts
                if not result.email_patterns and crawl_result.email_patterns:
                    result.email_patterns = crawl_result.email_patterns
                return result
            elif should_crawl:
                # If crawl failed, determine error reason
                if not crawl_result.success:
                    error_reason = "crawl_failed"
                return crawl_result

    # Cache failure if we have a cache
    if cache and not result:
        cache.set_failure(domain, provider_name, error_reason or "unknown")

    # Return primary result even if failed
    return result or EnrichmentResult(
        success=False, error_message="No enrichment method available"
    )
