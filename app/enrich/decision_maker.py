"""Extract decision maker information (names, titles) from websites."""

import re
import logging
from typing import Dict, Any, Optional, List, Tuple
from dataclasses import dataclass
from bs4 import BeautifulSoup
import requests
from fake_useragent import UserAgent

from app.cache import get_cache

logger = logging.getLogger(__name__)


@dataclass
class Person:
    """Person found on website."""
    name: Optional[str] = None
    title: Optional[str] = None
    confidence: str = "low"  # low, medium, high


class DecisionMakerFinder:
    """Extract decision maker info from websites."""
    
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
    
    def _extract_names_from_html(self, html: str) -> List[Tuple[str, str]]:
        """
        Extract names and titles from HTML.
        Returns list of (name, title) tuples.
        """
        people = []
        soup = BeautifulSoup(html, 'lxml')
        
        # Remove script and style elements
        for script in soup(["script", "style"]):
            script.decompose()
        
        # Look for common team/about section patterns
        text = soup.get_text()
        lines = text.split('\n')
        
        # Common title patterns
        title_keywords = [
            # English
            'owner', 'founder', 'co-founder', 'ceo', 'manager', 'director',
            'managing director', 'general manager', 'president', 'principal',
            # Food industry specific (EN)
            'head chef', 'executive chef', 'pastry chef', 'head baker',
            'purchasing manager', 'procurement manager', 'buyer',
            # German
            'inhaber', 'inhaberin', 'gründer', 'gründerin', 'geschäftsführer', 'geschäftsführerin',
            'leiter', 'leiterin', 'direktor', 'direktorin', 'vorstand',
            'chefkonditor', 'chefkonditorin', 'küchenchef', 'einkaufsleiter', 'einkaufsleiterin',
            'bäckermeister', 'bäckermeisterin', 'konditormeister', 'konditormeisterin',
            # French
            'propriétaire', 'fondateur', 'fondatrice', 'directeur', 'directrice',
            'gérant', 'gérante', 'président', 'présidente', 'pdg',
            'chef pâtissier', 'chef cuisinier', 'responsable achats', 'maître boulanger',
            # Italian
            'proprietario', 'proprietaria', 'fondatore', 'fondatrice',
            'direttore', 'direttrice', 'amministratore delegato',
            'titolare', 'responsabile', 'responsabile acquisti',
            'chef pasticcere', 'capo cuoco', 'maestro pasticcere',
            # Spanish
            'propietario', 'propietaria', 'fundador', 'fundadora',
            'director', 'directora', 'gerente', 'presidente', 'presidenta',
            'jefe de cocina', 'jefe de compras', 'maestro pastelero',
            # Portuguese
            'proprietário', 'proprietária', 'fundador', 'fundadora',
            'diretor', 'diretora', 'gerente', 'presidente',
            'chefe de cozinha', 'responsável de compras',
            # Dutch
            'eigenaar', 'eigenares', 'oprichter', 'oprichtster',
            'directeur', 'bestuurder', 'hoofd inkoop', 'inkoopmanager',
            'meester bakker', 'chef-kok', 'patissier',
            # Polish
            'właściciel', 'właścicielka', 'założyciel', 'założycielka',
            'dyrektor', 'kierownik', 'prezes', 'szef kuchni',
            'kierownik zakupów', 'mistrz cukierniczy',
            # Cosmetology / Beauty
            'spa manager', 'beauty director', 'spa-leiterin', 'kosmetikerin',
            'directrice beauté', 'direttore spa',
            # HoReCa
            'hoteldirektor', 'food and beverage manager', 'f&b manager',
            'directeur hôtelier', 'direttore albergo',
            # Scandinavian
            'ägare', 'grundare', 'vd', 'verkställande direktör',  # Swedish
            'eier', 'grunnlegger', 'daglig leder',  # Norwegian
            'ejer', 'stifter', 'direktør',  # Danish
        ]
        
        # Pattern: "Name - Title" or "Name, Title"
        # Unicode-aware name pattern supporting European diacritics
        # Matches: "Hans Müller", "François Dupont", "Jan van der Berg", "Maria da Silva"
        _name = r'[A-ZÀ-ÖØ-Þ\u0100-\u017E][a-zà-öø-ÿ\u0101-\u017F]+(?:\s+(?:von|van|de|da|di|del|der|den|het|la|le|los|das|dos)\s+)?[A-ZÀ-ÖØ-Þ\u0100-\u017E][a-zà-öø-ÿ\u0101-\u017F]+'
        _titles = '|'.join(title_keywords)
        patterns = [
            rf'({_name})\s*[-–—]\s*({_titles})',
            rf'({_name}),\s*({_titles})',
            rf'({_titles})\s*[-–—:]\s*({_name})',  # Reversed: "Inhaber: Hans Müller"
        ]
        
        found_names = set()
        for line in lines:
            line = line.strip()
            if len(line) > 5 and len(line) < 200:
                for i, pattern in enumerate(patterns):
                    matches = re.finditer(pattern, line, re.IGNORECASE)
                    for match in matches:
                        if i < 2:  # Normal order: name - title
                            name = match.group(1).strip()
                            title = match.group(2).strip().lower()
                        else:  # Reversed: title - name
                            title = match.group(1).strip().lower()
                            name = match.group(2).strip()
                        if name not in found_names:
                            found_names.add(name)
                            people.append((name, title))
        
        return people
    
    def _extract_from_email_pattern(self, emails: List[str]) -> Optional[Tuple[str, str, str]]:
        """
        Extract name from email pattern.
        Example: john.smith@domain.com → "John Smith"
        
        Returns: (name, email, confidence)
        """
        for email in emails:
            local_part = email.split('@')[0].lower()
            
            # Check for firstname.lastname pattern
            if '.' in local_part:
                parts = local_part.split('.')
                if len(parts) == 2:
                    firstname = parts[0].capitalize()
                    lastname = parts[1].capitalize()
                    name = f"{firstname} {lastname}"
                    
                    # Likely to be decision maker if common single letter combinations
                    title = "likely_contact"
                    return (name, email, "medium")
            
            # Check for firstnamelastname pattern
            elif len(local_part) > 3:
                # Try to split camelCase
                if re.search(r'[a-z][A-Z]', local_part):
                    # Don't try to split, probably not a name
                    pass
        
        return None
    
    def find_decision_maker(
        self, 
        domain: str, 
        business_name: str = '',
        emails: Optional[List[str]] = None
    ) -> Optional[Person]:
        """
        Find decision maker (owner/manager) for a business.
        
        Args:
            domain: Website domain
            business_name: Business name for context
            emails: List of emails found on website
        
        Returns:
            Person object with name, title, confidence
        """
        if not domain:
            return None
        
        # Check cache first
        if self.use_cache and self.cache:
            cached = self.cache.get(domain, 'decision_maker', max_age_days=30)
            if cached:
                cached_status = cached.get('cached_status')
                if cached_status == 'failed':
                    logger.debug(f"Decision maker extraction cached as failed for {domain}")
                    return None
                elif cached_status == 'success':
                    person_data = {k: v for k, v in cached.items() 
                                   if k not in ('cached_status', 'cached_error_reason')}
                    if person_data:
                        return Person(**person_data)
        
        # Normalize domain to URL
        if not domain.startswith(('http://', 'https://')):
            url = f'https://{domain}'
        else:
            url = domain
        
        people_found = []
        
        # Try about page first (most likely to have owner info)
        about_urls = [
            f'{url}/about',          # EN
            f'{url}/about-us',       # EN
            f'{url}/team',           # EN/universal
            f'{url}/leadership',     # EN
            f'{url}/uber-uns',       # DE
            f'{url}/ueber-uns',      # DE (alternative)
            f'{url}/impressum',      # DE/AT/CH (legal - GOLDMINE)
            f'{url}/kontakt',        # DE
            f'{url}/a-propos',       # FR
            f'{url}/mentions-legales', # FR (legal)
            f'{url}/chi-siamo',      # IT
            f'{url}/note-legali',    # IT (legal)
            f'{url}/sobre-nosotros', # ES
            f'{url}/aviso-legal',    # ES (legal)
            f'{url}/over-ons',       # NL
            f'{url}/o-nas',          # PL
            f'{url}/sobre-nos',      # PT
        ]
        
        result = None
        for about_url in about_urls:
            html = self._fetch_page(about_url)
            if html:
                names_titles = self._extract_names_from_html(html)
                people_found.extend(names_titles)
                if names_titles:
                    logger.debug(f"Found {len(names_titles)} people on {about_url}")
                    # Take first person found - likely to be owner/decision maker
                    high_conf_titles = [
                        'owner', 'founder', 'inhaber', 'inhaberin', 'geschäftsführer', 'geschäftsführerin',
                        'gründer', 'gründerin', 'propriétaire', 'fondateur', 'fondatrice',
                        'proprietario', 'proprietaria', 'fondatore', 'titolare',
                        'propietario', 'propietaria', 'fundador', 'fundadora',
                        'proprietário', 'proprietária', 'eigenaar', 'oprichter',
                        'właściciel', 'właścicielka', 'założyciel',
                        'ägare', 'grundare', 'eier', 'grunnlegger', 'ejer', 'stifter',
                    ]
                    if any(t in names_titles[0][1].lower() for t in high_conf_titles):
                        result = Person(
                            name=names_titles[0][0],
                            title=names_titles[0][1],
                            confidence="high"
                        )
                        break
                    # Don't break yet if not a high-confidence match
        
        # Fallback to email pattern extraction
        if not result and not people_found and emails:
            email_result = self._extract_from_email_pattern(emails)
            if email_result:
                name, email, confidence = email_result
                result = Person(
                    name=name,
                    title="contact",
                    confidence=confidence
                )
        
        # Return first person found if any
        if not result and people_found:
            result = Person(
                name=people_found[0][0],
                title=people_found[0][1],
                confidence="low"
            )
        
        # Cache result
        if self.use_cache and self.cache:
            if result:
                cache_data = {
                    'name': result.name,
                    'title': result.title,
                    'confidence': result.confidence
                }
                self.cache.set(domain, 'decision_maker', cache_data, status='success')
            else:
                self.cache.set_failure(domain, 'decision_maker', 'no_decision_maker_found', ttl_days=7)
        
        return result
