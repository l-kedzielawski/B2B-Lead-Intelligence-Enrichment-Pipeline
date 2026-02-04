"""Lead scoring module."""

import logging
from typing import Dict, Any, Optional
from datetime import datetime

logger = logging.getLogger(__name__)


def calculate_lead_score(record: Dict[str, Any]) -> int:
    """
    Calculate lead quality score (0-100) based on various signals.
    
    Scoring rules:
    + Website present: +15
    + Valid phone: +15
    + Email found (enriched): +15
    + High rating (>= 4.2): +10
    + High reviews count (>= 50): +10
    + LinkedIn present (enriched): +10
    + Contact email present: +10
    + Complete business info: +15
    """
    score = 0
    
    # Website present (15 points)
    if record.get('website_normalized') or record.get('website_raw'):
        score += 15
    
    # Valid phone (15 points)
    if record.get('phone_valid'):
        score += 15
    
    # Email found (15 points)
    emails_found = record.get('enriched_emails_found') or 0
    if emails_found and emails_found > 0:
        score += min(int(emails_found) * 5, 15)  # Up to 15 points for multiple emails
    
    # High rating (10 points)
    rating = record.get('rating_normalized')
    if rating and rating >= 4.2:
        score += 10
    
    # High reviews count (10 points)
    reviews = record.get('reviews_count_normalized')
    if reviews and reviews >= 50:
        score += 10
    elif reviews and reviews >= 20:
        score += 5
    
    # LinkedIn present (10 points)
    if record.get('enriched_linkedin_url'):
        score += 10
    
    # Contact email present (10 points)
    if record.get('enriched_contact_email'):
        score += 10
    
    # Complete business info (15 points)
    has_business_name = bool(record.get('business_name'))
    has_category = bool(record.get('category'))
    has_address = bool(record.get('address'))
    has_coords = bool(record.get('lat') and record.get('lon'))
    
    completeness = sum([has_business_name, has_category, has_address, has_coords])
    if completeness >= 3:
        score += 15
    elif completeness >= 2:
        score += 10
    
    # Cap at 100
    return min(score, 100)


def get_enrichment_status(record: Dict[str, Any]) -> str:
    """
    Determine enrichment status based on what was found.
    
    Returns:
        'ok' - Good enrichment data
        'partial' - Some data found but incomplete
        'failed' - No enrichment data
    """
    has_company_name = bool(record.get('enriched_company_name'))
    has_email = bool((record.get('enriched_emails_found') or 0) > 0)
    has_social = bool(record.get('enriched_linkedin_url') or 
                     record.get('enriched_facebook_url'))
    has_contacts = bool(record.get('enriched_contact_email'))
    
    if has_company_name and (has_email or has_contacts):
        return 'ok'
    elif has_company_name or has_social:
        return 'partial'
    else:
        return 'failed'


def score_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """
    Add scoring fields to a record.
    
    Adds:
    - lead_quality_score: int (0-100)
    - enrichment_status: str
    - scored_at: datetime
    """
    record['lead_quality_score'] = calculate_lead_score(record)
    record['enrichment_status'] = get_enrichment_status(record)
    record['scored_at'] = datetime.utcnow().isoformat()
    
    return record


def categorize_lead(score: int) -> str:
    """Categorize lead based on score."""
    if score >= 80:
        return 'hot'
    elif score >= 60:
        return 'warm'
    elif score >= 40:
        return 'cold'
    else:
        return 'poor'


def get_score_breakdown(record: Dict[str, Any]) -> Dict[str, int]:
    """Get detailed score breakdown for reporting."""
    breakdown = {
        'website': 15 if (record.get('website_normalized') or record.get('website_raw')) else 0,
        'phone': 15 if record.get('phone_valid') else 0,
        'email': 0,
        'rating': 0,
        'reviews': 0,
        'linkedin': 10 if record.get('enriched_linkedin_url') else 0,
        'contact': 10 if record.get('enriched_contact_email') else 0,
        'completeness': 0,
    }
    
    # Email breakdown
    emails_found = record.get('enriched_emails_found', 0)
    breakdown['email'] = min(emails_found * 5, 15)
    
    # Rating breakdown
    rating = record.get('rating_normalized')
    if rating and rating >= 4.2:
        breakdown['rating'] = 10
    
    # Reviews breakdown
    reviews = record.get('reviews_count_normalized')
    if reviews and reviews >= 50:
        breakdown['reviews'] = 10
    elif reviews and reviews >= 20:
        breakdown['reviews'] = 5
    
    # Completeness breakdown
    has_business_name = bool(record.get('business_name'))
    has_category = bool(record.get('category'))
    has_address = bool(record.get('address'))
    has_coords = bool(record.get('lat') and record.get('lon'))
    completeness = sum([has_business_name, has_category, has_address, has_coords])
    if completeness >= 3:
        breakdown['completeness'] = 15
    elif completeness >= 2:
        breakdown['completeness'] = 10
    
    return breakdown
