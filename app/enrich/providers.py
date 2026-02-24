"""Enrichment provider interface and implementations."""

import os
import json
import logging
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, asdict
from datetime import datetime
import requests
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)


@dataclass
class Contact:
    """Contact information from enrichment."""
    name: Optional[str] = None
    title: Optional[str] = None
    email: Optional[str] = None
    linkedin: Optional[str] = None
    email_source: str = "unknown"  # scraped, generated, api, legal_page, jsonld


@dataclass
class EnrichmentResult:
    """Result from enrichment provider."""
    success: bool = False
    company_name: Optional[str] = None
    description: Optional[str] = None
    industry: Optional[str] = None
    employee_count: Optional[str] = None
    linkedin_url: Optional[str] = None
    facebook_url: Optional[str] = None
    instagram_url: Optional[str] = None
    tiktok_url: Optional[str] = None
    twitter_url: Optional[str] = None
    youtube_url: Optional[str] = None
    email_patterns: Optional[List[str]] = None
    contacts: Optional[List[Contact]] = None
    error_message: Optional[str] = None
    raw_response: Optional[Dict] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        result = asdict(self)
        # Convert contacts to dicts
        if self.contacts:
            result['contacts'] = [asdict(c) for c in self.contacts]
        return result


class EnrichmentProvider(ABC):
    """Abstract base class for enrichment providers."""
    
    def __init__(self, api_key: Optional[str] = None, rate_limit: int = 3):
        self.api_key = api_key
        self.rate_limit = rate_limit
        self.session = requests.Session()
        
    @abstractmethod
    def enrich(self, domain: str) -> EnrichmentResult:
        """Enrich data for a given domain."""
        pass
    
    def check_api_key(self) -> bool:
        """Check if API key is configured."""
        return bool(self.api_key)


class ClearbitProvider(EnrichmentProvider):
    """Clearbit enrichment provider."""
    
    API_BASE = "https://company.clearbit.com/v2/companies/find"
    
    def __init__(self, api_key: Optional[str] = None, rate_limit: int = 3):
        super().__init__(api_key, rate_limit)
        if not self.api_key:
            self.api_key = os.getenv('CLEARBIT_API_KEY')
    
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def enrich(self, domain: str) -> EnrichmentResult:
        """Enrich using Clearbit API."""
        if not self.api_key:
            return EnrichmentResult(
                success=False,
                error_message="Clearbit API key not configured"
            )
        
        try:
            response = self.session.get(
                self.API_BASE,
                params={'domain': domain},
                auth=(self.api_key, ''),
                timeout=30
            )
            
            if response.status_code == 404:
                return EnrichmentResult(
                    success=False,
                    error_message="Company not found"
                )
            
            response.raise_for_status()
            data = response.json()
            
            # Extract contacts if available
            contacts = []
            if 'people' in data:
                for person in data['people'][:3]:  # Limit to 3 contacts
                    contact = Contact(
                        name=f"{person.get('name', {}).get('givenName', '')} {person.get('name', {}).get('familyName', '')}".strip(),
                        title=person.get('title'),
                        email=person.get('email'),
                        linkedin=person.get('linkedin', {}).get('handle')
                    )
                    if contact.name or contact.email:
                        contacts.append(contact)
            
            return EnrichmentResult(
                success=True,
                company_name=data.get('name'),
                description=data.get('description'),
                industry=data.get('category', {}).get('industry'),
                employee_count=str(data.get('metrics', {}).get('employees', '')) if data.get('metrics', {}).get('employees') else None,
                linkedin_url=f"https://linkedin.com/company/{data.get('linkedin', {}).get('handle')}" if data.get('linkedin', {}).get('handle') else None,
                facebook_url=f"https://facebook.com/{data.get('facebook', {}).get('handle')}" if data.get('facebook', {}).get('handle') else None,
                contacts=contacts if contacts else None,
                raw_response=data
            )
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Clearbit API error for {domain}: {e}")
            return EnrichmentResult(
                success=False,
                error_message=f"API error: {str(e)}"
            )


class ApolloProvider(EnrichmentProvider):
    """Apollo.io enrichment provider."""
    
    API_BASE = "https://api.apollo.io/v1/organizations/enrich"
    
    def __init__(self, api_key: Optional[str] = None, rate_limit: int = 3):
        super().__init__(api_key, rate_limit)
        if not self.api_key:
            self.api_key = os.getenv('APOLLO_API_KEY')
    
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def enrich(self, domain: str) -> EnrichmentResult:
        """Enrich using Apollo API."""
        if not self.api_key:
            return EnrichmentResult(
                success=False,
                error_message="Apollo API key not configured"
            )
        
        try:
            headers = {
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {self.api_key}'
            }
            
            response = self.session.post(
                self.API_BASE,
                headers=headers,
                json={'domain': domain},
                timeout=30
            )
            
            response.raise_for_status()
            data = response.json()
            
            org = data.get('organization', {})
            
            # Extract contacts from people data
            contacts = []
            people = data.get('people', [])
            for person in people[:3]:  # Limit to 3 contacts
                contact = Contact(
                    name=person.get('name'),
                    title=person.get('title'),
                    email=person.get('email'),
                    linkedin=person.get('linkedin_url')
                )
                if contact.name or contact.email:
                    contacts.append(contact)
            
            return EnrichmentResult(
                success=True,
                company_name=org.get('name'),
                description=org.get('description'),
                industry=org.get('industry'),
                employee_count=str(org.get('estimated_num_employees', '')) if org.get('estimated_num_employees') else None,
                linkedin_url=org.get('linkedin_url'),
                facebook_url=org.get('facebook_url'),
                contacts=contacts if contacts else None,
                raw_response=data
            )
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Apollo API error for {domain}: {e}")
            return EnrichmentResult(
                success=False,
                error_message=f"API error: {str(e)}"
            )


class HunterProvider(EnrichmentProvider):
    """Hunter.io enrichment provider - use sparingly for promising leads."""
    
    API_BASE = "https://api.hunter.io/v2/domain-search"
    
    def __init__(self, api_key: Optional[str] = None, rate_limit: int = 2):
        super().__init__(api_key, rate_limit)
        if not self.api_key:
            self.api_key = os.getenv('HUNTER_API_KEY')
    
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def enrich(self, domain: str) -> EnrichmentResult:
        """Enrich using Hunter.io API."""
        if not self.api_key:
            return EnrichmentResult(
                success=False,
                error_message="Hunter API key not configured"
            )
        
        try:
            response = self.session.get(
                self.API_BASE,
                params={
                    'domain': domain,
                    'api_key': self.api_key
                },
                timeout=30
            )
            
            response.raise_for_status()
            data = response.json()
            
            result_data = data.get('data', {})
            
            # Extract email patterns
            patterns = result_data.get('pattern', '')
            email_patterns = [patterns] if patterns else None
            
            # Extract contacts from emails
            contacts = []
            emails = result_data.get('emails', [])
            for email_data in emails[:3]:
                contact = Contact(
                    name=f"{email_data.get('first_name', '')} {email_data.get('last_name', '')}".strip(),
                    title=email_data.get('position'),
                    email=email_data.get('value'),
                    linkedin=email_data.get('linkedin')
                )
                if contact.name or contact.email:
                    contacts.append(contact)
            
            # Try to get organization info
            org_data = result_data.get('organization', {})
            
            return EnrichmentResult(
                success=True,
                company_name=org_data.get('name') or result_data.get('organization', ''),
                industry=org_data.get('industry'),
                employee_count=str(org_data.get('employees', '')) if org_data.get('employees') else None,
                linkedin_url=org_data.get('linkedin'),
                email_patterns=email_patterns,
                contacts=contacts if contacts else None,
                raw_response=data
            )
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Hunter API error for {domain}: {e}")
            return EnrichmentResult(
                success=False,
                error_message=f"API error: {str(e)}"
            )


class FreeEnrichmentProvider(EnrichmentProvider):
    """
    Free enrichment provider that doesn't use paid APIs.
    Returns basic info and relies on website crawling for data.
    """
    
    def __init__(self, api_key: Optional[str] = None, rate_limit: int = 10):
        super().__init__(api_key, rate_limit)
        # No API key needed
    
    def enrich(self, domain: str) -> EnrichmentResult:
        """
        Free enrichment - just validates domain and returns basic structure.
        Actual enrichment will come from website crawling.
        """
        # This is a placeholder - real enrichment happens in crawl_fallback
        return EnrichmentResult(
            success=True,
            company_name=domain.split('.')[0].replace('-', ' ').title(),
            error_message=None
        )


def get_provider(provider_name: str, api_key: Optional[str] = None, rate_limit: int = 3) -> EnrichmentProvider:
    """
    Factory function to get enrichment provider by name.
    
    Args:
        provider_name: One of 'clearbit', 'apollo', 'hunter', 'free'
        api_key: Optional API key (otherwise reads from env)
        rate_limit: Requests per second limit
    """
    providers = {
        'clearbit': ClearbitProvider,
        'apollo': ApolloProvider,
        'hunter': HunterProvider,
        'free': FreeEnrichmentProvider,
    }
    
    provider_class = providers.get(provider_name.lower())
    if not provider_class:
        raise ValueError(f"Unknown provider: {provider_name}. Available: {list(providers.keys())}")
    
    return provider_class(api_key=api_key, rate_limit=rate_limit)
