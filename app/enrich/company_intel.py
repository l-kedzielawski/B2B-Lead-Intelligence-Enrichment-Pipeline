"""Extract company intelligence: size, age, engagement, language."""

import re
import logging
from typing import Dict, Any, Optional, Tuple
from datetime import datetime
from bs4 import BeautifulSoup
import requests
from fake_useragent import UserAgent

from app.cache import get_cache

logger = logging.getLogger(__name__)


class CompanyIntelligence:
    """Extract company intelligence from websites."""
    
    def __init__(self, timeout: int = 10, use_cache: bool = True, cache_path: str = './cache/cache.sqlite'):
        self.timeout = timeout
        self.ua = UserAgent()
        self.session = requests.Session()
        self.use_cache = use_cache
        self.cache = get_cache(cache_path) if use_cache else None
    
    def _get_headers(self) -> Dict[str, str]:
        """Get headers with random user agent."""
        return {
            'User-Agent': self.ua.random,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        }
    
    def _fetch_page(self, url: str) -> Optional[str]:
        """Fetch page content."""
        try:
            response = self.session.get(
                url,
                headers=self._get_headers(),
                timeout=self.timeout,
                allow_redirects=True,
                verify=False
            )
            response.raise_for_status()
            return response.text
        except Exception as e:
            logger.debug(f"Failed to fetch {url}: {e}")
            return None
    
    def estimate_employee_count(self, html: str) -> Optional[str]:
        """
        Estimate employee count from website.
        
        Returns: "1", "2-5", "5-20", "20-50", "50+"
        """
        soup = BeautifulSoup(html, 'lxml')
        
        # Remove script and style
        for script in soup(["script", "style"]):
            script.decompose()
        
        text = soup.get_text()
        
        # Count team members on page (rough heuristic)
        # Look for patterns like "John - Manager", "Sarah - Designer"
        name_patterns = r'[A-Z][a-z]+ [A-Z][a-z]+\s*[-–—]\s*[A-Za-z ]+'
        matches = len(re.findall(name_patterns, text))
        
        if matches >= 50:
            return "50+"
        elif matches >= 20:
            return "20-50"
        elif matches >= 5:
            return "5-20"
        elif matches >= 2:
            return "2-5"
        elif matches >= 1:
            return "1"
        
        # If no clear pattern, try to estimate from content length
        # Larger sites usually have more team members mentioned
        if len(text) > 50000:
            return "20-50"
        elif len(text) > 20000:
            return "5-20"
        elif len(text) > 10000:
            return "2-5"
        
        return None
    
    def estimate_business_age(self, html: str) -> Optional[int]:
        """
        Estimate business age from founding year in content.
        
        Returns: Years in operation (current_year - founded_year)
        """
        soup = BeautifulSoup(html, 'lxml')
        text = soup.get_text()
        
        # Look for copyright year: "© 2020-2024" or "© 2020"
        copyright_patterns = [
            r'©\s*(\d{4})',
            r'Copyright\s+(\d{4})',
            r'Founded\s+(\d{4})',
            r'Gegründet\s+(\d{4})',  # German
            r'Fondée\s+(\d{4})',  # French
            r'Est\.\s+(\d{4})',
            r'Established\s+(\d{4})',
        ]
        
        years = []
        for pattern in copyright_patterns:
            matches = re.findall(pattern, text, re.IGNORECASE)
            years.extend([int(y) for y in matches if 1900 <= int(y) <= datetime.now().year])
        
        if years:
            founded_year = min(years)  # Use earliest year found
            age = datetime.now().year - founded_year
            return max(1, age)  # At least 1 year
        
        return None
    
    def detect_website_language(self, html: str) -> Optional[str]:
        """
        Detect primary website language.
        
        Returns: Language code (DE, EN, FR, IT, etc.)
        """
        soup = BeautifulSoup(html, 'lxml')
        
        # Check HTML lang attribute
        html_tag = soup.find('html')
        if html_tag:
            lang = html_tag.get('lang', '').upper()
            if lang and '-' in lang:
                lang = lang.split('-')[0]
            if lang and len(lang) == 2:
                return lang
        
        # Check meta content-language
        meta_lang = soup.find('meta', attrs={'http-equiv': 'content-language'})
        if meta_lang:
            lang = meta_lang.get('content', '').upper()
            if lang and '-' in lang:
                lang = lang.split('-')[0]
            if lang and len(lang) == 2:
                return lang
        
        # Fallback: detect from text content using simple keyword matching
        text = soup.get_text().lower()
        
        # German indicators
        if any(word in text for word in ['willkommen', 'ü', 'ö', 'ä', 'telefon', 'adresse', 'kontakt']):
            if text.count('ä') + text.count('ö') + text.count('ü') > 10:
                return 'DE'
        
        # French indicators
        if any(word in text for word in ['bienvenue', 'français', 'contactez', 'téléphone']):
            return 'FR'
        
        # Italian indicators
        if any(word in text for word in ['benvenuto', 'italiano', 'contatti', 'telefono']):
            return 'IT'
        
        # English (default if Western content)
        if any(word in text for word in ['welcome', 'contact', 'about', 'team']):
            return 'EN'
        
        return None
    
    def extract_company_info(self, domain: str) -> Dict[str, Any]:
        """
        Extract company intelligence from website.
        
        Returns:
            {
                'estimated_employees': '1', '2-5', '5-20', '20-50', '50+' or None,
                'business_age_years': int or None,
                'website_language': 'DE', 'EN', 'FR', etc. or None,
                'primary_url_accessible': bool,
            }
        """
        result = {
            'estimated_employees': None,
            'business_age_years': None,
            'website_language': None,
            'primary_url_accessible': False,
        }
        
        if not domain:
            return result
        
        # Check cache first
        if self.use_cache and self.cache:
            cached = self.cache.get(domain, 'company_intel', max_age_days=30)
            if cached:
                cached_status = cached.get('cached_status')
                if cached_status == 'failed':
                    logger.debug(f"Company intel extraction cached as failed for {domain}")
                    return result
                elif cached_status == 'success':
                    cached_data = {k: v for k, v in cached.items() 
                                   if k not in ('cached_status', 'cached_error_reason')}
                    if cached_data:
                        return cached_data
        
        # Normalize to URL
        if not domain.startswith(('http://', 'https://')):
            url = f'https://{domain}'
        else:
            url = domain
        
        # Fetch homepage
        html = self._fetch_page(url)
        if not html:
            # Cache failure
            if self.use_cache and self.cache:
                self.cache.set_failure(domain, 'company_intel', 'html_fetch_failed', ttl_days=7)
            return result
        
        result['primary_url_accessible'] = True
        
        try:
            # Estimate employees
            result['estimated_employees'] = self.estimate_employee_count(html)
            
            # Estimate age
            result['business_age_years'] = self.estimate_business_age(html)
            
            # Detect language
            result['website_language'] = self.detect_website_language(html)
        except Exception as e:
            logger.debug(f"Error extracting company info from {domain}: {e}")
            # Cache failure
            if self.use_cache and self.cache:
                self.cache.set_failure(domain, 'company_intel', f'extraction_error: {str(e)}', ttl_days=7)
            return result
        
        # Cache success
        if self.use_cache and self.cache:
            self.cache.set(domain, 'company_intel', result, status='success')
        
        return result
