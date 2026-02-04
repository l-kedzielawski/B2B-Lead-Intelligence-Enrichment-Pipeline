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
        """Fetch page content."""
        try:
            response = self.session.get(
                url,
                headers=self._get_headers(),
                timeout=self.timeout,
                allow_redirects=True
            )
            response.raise_for_status()
            return response.text
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
            'owner', 'founder', 'ceo', 'manager', 'director',
            'inhaber', 'gründer', 'leiter', 'geschäftsführer',  # German
            'propriétaire', 'fondateur', 'directeur',  # French
        ]
        
        # Pattern: "Name - Title" or "Name, Title"
        patterns = [
            r'([A-Z][a-z]+ [A-Z][a-z]+)\s*[-–—]\s*(owner|founder|ceo|manager|director|inhaber|gründer|geschäftsführer)',
            r'([A-Z][a-z]+ [A-Z][a-z]+),\s*(owner|founder|ceo|manager|director|inhaber|gründer|geschäftsführer)',
        ]
        
        found_names = set()
        for line in lines:
            line = line.strip()
            if len(line) > 10 and len(line) < 100:
                for pattern in patterns:
                    matches = re.finditer(pattern, line, re.IGNORECASE)
                    for match in matches:
                        name = match.group(1).strip()
                        title = match.group(2).strip().lower()
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
            f'{url}/about',
            f'{url}/about-us',
            f'{url}/team',
            f'{url}/leadership',
            f'{url}/uber-uns',  # German
            f'{url}/a-propos',  # French
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
                    if 'owner' in names_titles[0][1].lower() or 'founder' in names_titles[0][1].lower() or 'geschäftsführer' in names_titles[0][1].lower():
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
