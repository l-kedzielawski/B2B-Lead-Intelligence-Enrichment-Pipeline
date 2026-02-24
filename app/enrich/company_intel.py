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
        """Fetch page content with SSL fallback."""
        try:
            response = self.session.get(
                url,
                headers=self._get_headers(),
                timeout=self.timeout,
                allow_redirects=True
            )
            response.raise_for_status()
            return response.text
        except requests.exceptions.SSLError:
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
                logger.debug(f"Failed to fetch {url} (no verify): {e}")
                return None
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
        name_patterns = r'[A-ZÀ-ÖØ-Þ\u0100-\u017E][a-zà-öø-ÿ\u0101-\u017F]+\s+[A-ZÀ-ÖØ-Þ\u0100-\u017E][a-zà-öø-ÿ\u0101-\u017F]+\s*[-–—]\s*[A-Za-zÀ-ÖØ-öø-ÿ\u0100-\u017F ]+'
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
            # English
            r'Founded\s+(?:in\s+)?(\d{4})',
            r'Established\s+(?:in\s+)?(\d{4})',
            r'Est\.\s+(\d{4})',
            r'Since\s+(\d{4})',
            # German
            r'Gegründet\s+(?:im\s+)?(\d{4})',
            r'Seit\s+(\d{4})',
            r'Gründung(?:sjahr)?\s*:?\s*(\d{4})',
            # French
            r'Fondée?\s+(?:en\s+)?(\d{4})',
            r'Depuis\s+(\d{4})',
            r'Créée?\s+(?:en\s+)?(\d{4})',
            # Italian
            r'Fondata?\s+(?:nel\s+)?(\d{4})',
            r'Dal\s+(\d{4})',
            # Spanish
            r'Fundada?\s+(?:en\s+)?(\d{4})',
            r'Desde\s+(\d{4})',
            # Portuguese
            r'Fundada?\s+(?:em\s+)?(\d{4})',
            r'Desde\s+(\d{4})',
            # Dutch
            r'Opgericht\s+(?:in\s+)?(\d{4})',
            r'Sinds\s+(\d{4})',
            # Polish
            r'Założona?\s+(?:w\s+)?(\d{4})',
            r'Od\s+(\d{4})\s+roku',
            # Scandinavian
            r'Grundad\s+(\d{4})',       # Swedish
            r'Grunnlagt\s+(\d{4})',     # Norwegian
            r'Grundlagt\s+(\d{4})',     # Danish
            r'Perustettu\s+(\d{4})',    # Finnish
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
        
        # Fallback: detect from text content using keyword matching
        text = soup.get_text().lower()
        
        # Language detection rules - ordered by specificity
        # Each entry: (language_code, distinctive_words, min_matches)
        language_rules = [
            # German - distinctive umlauts and words
            ('DE', ['willkommen', 'impressum', 'datenschutz', 'geschäftsbedingungen', 'öffnungszeiten',
                     'angebote', 'unternehmen', 'startseite', 'leistungen'], 2),
            # French - distinctive accents and words
            ('FR', ['bienvenue', 'mentions légales', 'politique de confidentialité', 'accueil',
                     'entreprise', 'nos services', 'à propos', 'contactez-nous'], 2),
            # Italian - distinctive words
            ('IT', ['benvenuto', 'benvenuti', 'chi siamo', 'azienda', 'servizi',
                     'contatti', 'note legali', 'privacy', 'prodotti'], 2),
            # Spanish - distinctive words
            ('ES', ['bienvenido', 'bienvenidos', 'quiénes somos', 'empresa', 'servicios',
                     'aviso legal', 'política de privacidad', 'inicio', 'nosotros'], 2),
            # Portuguese - distinctive words
            ('PT', ['bem-vindo', 'bem-vindos', 'quem somos', 'empresa', 'serviços',
                     'avisos legais', 'política de privacidade', 'início'], 2),
            # Dutch - distinctive words
            ('NL', ['welkom', 'over ons', 'diensten', 'privacybeleid', 'algemene voorwaarden',
                     'producten', 'bedrijf', 'thuispagina', 'openingstijden'], 2),
            # Polish - distinctive characters and words
            ('PL', ['witamy', 'o nas', 'usługi', 'polityka prywatności', 'regulamin',
                     'produkty', 'kontakt', 'strona główna', 'oferta'], 2),
            # Romanian
            ('RO', ['bine ați venit', 'despre noi', 'servicii', 'produse',
                     'politica de confidențialitate', 'termeni și condiții'], 2),
            # Czech
            ('CS', ['vítejte', 'o nás', 'služby', 'produkty', 'ochrana osobních údajů',
                     'obchodní podmínky', 'úvod', 'kontakt'], 2),
            # Hungarian
            ('HU', ['üdvözöljük', 'rólunk', 'szolgáltatások', 'termékek',
                     'adatvédelmi irányelvek', 'kapcsolat', 'főoldal'], 2),
            # Swedish
            ('SV', ['välkommen', 'om oss', 'tjänster', 'produkter', 'integritetspolicy',
                     'villkor', 'startsida', 'kontakta oss'], 2),
            # Danish
            ('DA', ['velkommen', 'om os', 'tjenester', 'produkter', 'privatlivspolitik',
                     'betingelser', 'forside', 'kontakt'], 2),
            # Norwegian
            ('NO', ['velkommen', 'om oss', 'tjenester', 'produkter', 'personvern',
                     'vilkår', 'forside', 'kontakt oss'], 2),
            # Finnish
            ('FI', ['tervetuloa', 'meistä', 'palvelut', 'tuotteet', 'tietosuoja',
                     'ehdot', 'etusivu', 'yhteystiedot'], 2),
            # Greek
            ('EL', ['καλώς ήρθατε', 'σχετικά', 'υπηρεσίες', 'επικοινωνία',
                     'πολιτική απορρήτου', 'προϊόντα'], 2),
            # Bulgarian
            ('BG', ['добре дошли', 'за нас', 'услуги', 'продукти',
                     'политика за поверителност', 'контакт'], 2),
            # Croatian
            ('HR', ['dobrodošli', 'o nama', 'usluge', 'proizvodi',
                     'pravila privatnosti', 'kontakt'], 2),
            # English (last - most common fallback)
            ('EN', ['welcome', 'about us', 'services', 'products', 'privacy policy',
                     'terms', 'contact us', 'home'], 2),
        ]
        
        for lang_code, keywords, min_matches in language_rules:
            matches = sum(1 for kw in keywords if kw in text)
            if matches >= min_matches:
                return lang_code
        
        # Additional heuristic: check for German umlauts
        umlaut_count = text.count('ä') + text.count('ö') + text.count('ü') + text.count('ß')
        if umlaut_count > 10:
            return 'DE'
        
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
