"""Utility functions for lead processing."""

import re
import logging
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode
from typing import Optional, Dict, Any, List

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# ── Shared business suffix constants ──────────────────────────────────────────
# Used by both clean.py (name normalization) and dedupe.py (fuzzy matching).
# clean.py uses the raw forms (with dots/punctuation); dedupe.py normalizes
# punctuation away before matching, so it uses a parallel list.  Both are
# derived from this single source of truth.

BUSINESS_SUFFIXES_RAW: List[str] = [
    # English
    ' llc', ' inc', ' ltd', ' limited', ' corp', ' corporation', ' co', ' company',
    # German-speaking (DE, AT, CH)
    ' gmbh', ' ag', ' ug', ' kg', ' ohg', ' e.k.', ' e.kfm.',
    ' gmbh & co. kg', ' ag & co. kg', ' gmbh & co kg', ' ag & co kg',
    # Austria-specific
    ' og',
    # Switzerland-specific (French/German/Italian)
    ' sa', ' sarl', ' s.à r.l.', ' sagl',
    # Italy (IT)
    ' s.r.l.', ' srl', ' s.p.a.', ' spa', ' s.n.c.', ' snc',
    ' s.a.s.', ' sas', ' s.s.', ' ss',
    # Portugal (PT)
    ' lda.', ' lda', ' ltda', ' s.a.', ' unipessoal lda.', ' crl',
    # Netherlands (NL)
    ' b.v.', ' bv', ' n.v.', ' nv', ' v.o.f.', ' vof', ' c.v.', ' cv',
    # Belgium (BE)
    ' comm.v', ' bvba', ' nvsa', ' sprl', ' scrl',
    # Poland (PL)
    ' sp. z o.o.', ' sp z o.o.', ' sp.z o.o.', ' sp. z oo', ' sp z oo',
    ' sp. j.', ' sp j', ' sp. p.', ' sp p', ' sp. k.', ' sp k',
    ' sp. z o.o. sp.k.', ' sp z oo sp k',
    # Spanish
    ' s.l.', ' sl', ' s.c.', ' sc',
    # Scandinavian (Denmark, Sweden, Norway, Finland)
    ' ab', ' hb', ' kb', ' as', ' a/s', ' aps', ' i/s', ' oy', ' oyj',
    # Czech/Slovak
    ' s.r.o.', ' sro', ' a.s.',
    # Hungarian
    ' kft', ' rt', ' bt', ' zrt', ' nyrt',
    # Romanian
    ' s.r.l.',
    # Croatian/Serbian
    ' d.o.o.', ' doo', ' d.d.', ' dd',
    # Irish
    ' teo', ' teoranta',
]

# Pre-sorted longest-first for correct matching
BUSINESS_SUFFIXES_RAW_SORTED: List[str] = sorted(BUSINESS_SUFFIXES_RAW, key=len, reverse=True)

# Normalized version (dots/punctuation stripped, lowered) for fuzzy matching
# in dedupe.py where input is already punctuation-stripped.
BUSINESS_SUFFIXES_NORMALIZED: List[str] = sorted(
    list({s.replace('.', '').replace('/', '').replace('à', 'a') for s in BUSINESS_SUFFIXES_RAW}),
    key=len, reverse=True,
)


def clean_text(text: Any) -> str:
    """Clean and normalize text fields."""
    if text is None:
        return ''
    try:
        import pandas as pd
        if pd.isna(text):
            return ''
    except ImportError:
        pass
    text = str(text).strip()
    # Collapse multiple spaces
    text = re.sub(r'\s+', ' ', text)
    return text


def extract_domain(url: str) -> Optional[str]:
    """Extract domain from URL."""
    if not url:
        return None
    try:
        normalized = normalize_url(url)
        if normalized:
            parsed = urlparse(normalized)
            if parsed.netloc:
                try:
                    import tldextract
                    ext = tldextract.extract(parsed.netloc)
                    return f"{ext.domain}.{ext.suffix}"
                except ImportError:
                    # Fallback if tldextract not available
                    return parsed.netloc
    except Exception:
        pass
    return None


def normalize_url(url: Any) -> Optional[str]:
    """Normalize URL to https://domain format, remove tracking params."""
    if url is None:
        return None
    try:
        import pandas as pd
        if pd.isna(url):
            return None
    except ImportError:
        pass
    
    url = str(url).strip()
    
    # Add protocol if missing
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url
    
    try:
        parsed = urlparse(url)
        
        # Remove common tracking parameters
        tracking_params = [
            'utm_source', 'utm_medium', 'utm_campaign', 'utm_content',
            'fbclid', 'gclid', 'msclkid', 'ref', 'source', 'si'
        ]
        
        query = parse_qs(parsed.query)
        filtered_query = {k: v for k, v in query.items() 
                         if k.lower() not in tracking_params}
        
        # Rebuild URL
        new_query = urlencode(filtered_query, doseq=True) if filtered_query else ''
        
        # Ensure https
        scheme = 'https'
        
        normalized = urlunparse((
            scheme,
            parsed.netloc.lower(),
            parsed.path,
            parsed.params,
            new_query,
            ''  # Remove fragment
        ))
        
        # Remove trailing slash
        normalized = normalized.rstrip('/')
        
        return normalized
    except Exception:
        return url


def score_record_completeness(record: Dict[str, Any]) -> int:
    """Score how complete a record is (0-100)."""
    import pandas as pd
    
    score = 0
    
    # Core fields (10 points each)
    core_fields = ['business_name', 'category', 'address', 'phone_raw', 'google_maps_url']
    for field in core_fields:
        if record.get(field) and str(record[field]).strip():
            score += 10
    
    # Bonus fields
    if record.get('website_raw') and str(record['website_raw']).strip():
        score += 15
    
    # Rating - handle NaN and invalid values
    rating = record.get('rating')
    if rating and not pd.isna(rating):
        try:
            if float(rating) > 0:
                score += 10
        except (ValueError, TypeError):
            pass
    
    # Reviews count - handle NaN and invalid values
    reviews = record.get('reviews_count')
    if reviews and not pd.isna(reviews):
        try:
            if int(float(reviews)) > 0:
                score += 10
        except (ValueError, TypeError):
            pass
    
    # Coordinates
    try:
        lat = record.get('lat')
        lon = record.get('lon')
        if lat and lon and not pd.isna(lat) and not pd.isna(lon):
            float(lat)  # Validate it's a number
            float(lon)  # Validate it's a number
            score += 10
    except (ValueError, TypeError):
        pass
    
    return min(score, 100)


def is_valid_email(email: Any) -> bool:
    """Basic email validation."""
    if email is None:
        return False
    try:
        import pandas as pd
        if pd.isna(email):
            return False
    except ImportError:
        pass
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return bool(re.match(pattern, str(email).strip()))


def parse_address_components(address: str) -> Dict[str, str]:
    """Best-effort address parsing into components."""
    if not address:
        return {}
    
    components = {
        'street': '',
        'postcode': '',
        'city_component': '',
        'country': ''
    }
    
    try:
        # Extract postcode (various formats)
        postcode_patterns = [
            r'\b[A-Z]{1,2}[0-9][A-Z0-9]?\s?[0-9][A-Z]{2}\b',  # UK
            r'\b[0-9]{5}(?:-[0-9]{4})?\b',  # US
            # European formats
            r'\b[0-9]{4}\s?[A-Z]{2}\b',  # Netherlands (1012 AB)
            r'\b[0-9]{2}-[0-9]{3}\b',  # Poland (00-001)
            r'\b[0-9]{4}-[0-9]{3}\b',  # Portugal (1000-001)
            r'\b[0-9]{4,5}\b',  # Generic 4-5 digit (DE, AT, CH, IT, BE, etc.)
        ]
        
        for pattern in postcode_patterns:
            match = re.search(pattern, address)
            if match:
                components['postcode'] = match.group()
                break
        
        # Simple heuristics for other components
        parts = [p.strip() for p in address.split(',')]
        
        if len(parts) >= 1:
            components['street'] = parts[0]
        if len(parts) >= 2:
            components['city_component'] = parts[-2] if len(parts) > 2 else parts[-1]
        if len(parts) >= 3:
            components['country'] = parts[-1]
            
    except Exception:
        pass
    
    return components


def generate_likely_emails(domain: str, business_name: str = '') -> list[str]:
    """
    Generate likely email addresses for a domain.
    Works for most small businesses that don't have complex email systems.
    """
    if not domain:
        return []
    
    # Clean domain
    domain = domain.lower().strip()
    # Accept any reasonable TLD - European and global
    if not re.search(r'\.[a-zA-Z]{2,}$', domain):
        return []
    
    emails = []
    
    # Common email prefixes for small businesses (multilingual)
    common_prefixes = [
        'info',
        'hello',
        'contact',
        'support',
        'sales',
        'service',
        'hallo',      # German
        'kontakt',    # German
        'anfrage',    # German (inquiry)
        'bestellen',  # German (order)
        'bonjour',    # French
        'contatto',   # Italian
        'contacto',   # Spanish
        'contato',    # Portuguese
        'welkom',     # Dutch
    ]
    
    # Add business name derived emails if available
    if business_name:
        clean_name = business_name.lower()
        clean_name = re.sub(r'[^a-z0-9]', '', clean_name)
        if clean_name:
            common_prefixes.insert(0, clean_name[:20])  # First 20 chars
    
    # Generate emails
    for prefix in common_prefixes:
        prefix = prefix.rstrip('@')  # Safety: strip any trailing @
        email = f"{prefix}@{domain}"
        if is_valid_email(email):
            emails.append(email)
    
    return list(set(emails))  # Remove duplicates



