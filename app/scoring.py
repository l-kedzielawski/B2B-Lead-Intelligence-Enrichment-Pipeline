"""Lead scoring module - optimized for cold email outreach to food/HoReCa/cosmetology businesses."""

import logging
from typing import Dict, Any, Optional, List
from datetime import datetime

logger = logging.getLogger(__name__)

# Target categories for vanilla/cacao/spice business
TARGET_CATEGORIES = {
    "primary": {  # Direct buyers - highest value
        "bakery",
        "bäckerei",
        "konditorei",
        "boulangerie",
        "pâtisserie",
        "panetteria",
        "pasticceria",
        "panadería",
        "pastelería",
        "padaria",
        "bakkerij",
        "banketbakkerij",
        "piekarnia",
        "cukiernia",
        "confectionery",
        "confiserie",
        "confetteria",
        "confitería",
        "chocolatier",
        "chocolaterie",
        "cioccolateria",
        "chocolatería",
        "schokolade",
        "ice cream",
        "ice cream shop",
        "gelato",
        "gelateria",
        "eisdiele",
        "eiscafe",
        "glacier",
        "heladería",
        "ijssalon",
        "lodziarnia",
        "speiseeis",
    },
    "secondary": {  # Regular buyers
        "cafe",
        "café",
        "coffee shop",
        "cafeteria",
        "kaffehaus",
        "kawiarnia",
        "restaurant",
        "ristorante",
        "restaurante",
        "restauracja",
        "bistro",
        "brasserie",
        "trattoria",
        "osteria",
        "fine dining",
        "gastronomie",
        "hotel",
        "hôtel",
        "albergo",
        "catering",
        "caterer",
        "traiteur",
        "partyservice",
    },
    "horeca_distro": {  # Distribution = many end customers
        "horeca",
        "food distribution",
        "food wholesale",
        "grosshandel",
        "großhandel",
        "lebensmittelgroßhandel",
        "food supplier",
        "ingredient supplier",
        "spice supplier",
        "gewürzhandel",
        "épicerie",
        "drogheria",
        "especiaria",
        "specerijhandel",
        "food manufacturer",
        "lebensmittelhersteller",
    },
    "beauty": {  # Cosmetology/beauty - vanilla & cacao in cosmetics
        "beauty salon",
        "spa",
        "wellness",
        "kosmetik",
        "kosmetikstudio",
        "schönheitspflege",
        "esthétique",
        "estetica",
        "estética",
        "kosmetyka",
        "natural cosmetics",
        "naturkosmetik",
        "cosmétique naturelle",
        "cosmetica naturale",
        "perfumery",
        "parfümerie",
        "parfumerie",
    },
}

# Ingredient/product keywords that signal high relevance
INGREDIENT_KEYWORDS = {
    "vanilla",
    "vanille",
    "vaniglia",
    "vainilla",
    "baunilha",
    "wanilia",
    "cacao",
    "kakao",
    "cocoa",
    "chocolate",
    "schokolade",
    "chocolat",
    "cioccolato",
    "spice",
    "spices",
    "gewürz",
    "gewürze",
    "épice",
    "épices",
    "spezie",
    "especia",
    "especiaria",
    "kruiden",
    "przyprawy",
    "essential oil",
    "ätherisches öl",
    "huile essentielle",
    "olio essenziale",
    "flavor",
    "flavour",
    "aroma",
    "arôme",
    "geschmack",
}

# Food certifications that indicate premium/quality-focused businesses
CERTIFICATIONS = {
    "bio",
    "organic",
    "biologisch",
    "biologique",
    "biologico",
    "ecológico",
    "fair trade",
    "fairtrade",
    "fair-trade",
    "vegan",
    "vegetarian",
    "vegetarisch",
    "eu-bio",
    "demeter",
    "bioland",
    "naturland",
    "halal",
    "kosher",
    "kasher",
    "naturkosmetik",
    "bdih",
    "natrue",
    "cosmos",
    "ecocert",
}


def calculate_lead_score(record: Dict[str, Any]) -> int:
    """
    Calculate lead quality score (0-100) optimized for cold email outreach.

    Weights are designed for a vanilla/cacao/spice business targeting
    food, HoReCa, and cosmetology companies across Europe.

    Scoring breakdown:
    + Decision maker with email: +25 (THE most valuable signal)
    + Real scraped contact email: +20
    + Valid website: +10
    + Valid phone: +5
    + High rating (>= 4.0): +5
    + Decent reviews (>= 20): +5
    + LinkedIn/social presence: +5
    + Generic/guessed emails available: +5
    + Complete business info: +10
    + Prominence (duplicate_count >= 2): +5
    + Company size known: +5
    """
    score = 0

    # Decision maker with contact info (25 points - highest value)
    has_dm_name = bool(record.get("decision_maker_name"))
    has_dm_email = bool(record.get("enriched_contact_email"))
    if has_dm_name and has_dm_email:
        score += 25
    elif has_dm_name:
        score += 12
    elif has_dm_email:
        score += 15

    # Real scraped contact email (20 points)
    emails_found = record.get("enriched_emails_found") or 0
    try:
        emails_found = int(emails_found)
    except (ValueError, TypeError):
        emails_found = 0
    if emails_found > 0:
        score += min(emails_found * 7, 20)

    # Website present (10 points)
    if record.get("website_normalized") or record.get("website_raw"):
        score += 10

    # Valid phone (5 points)
    if record.get("phone_valid"):
        score += 5

    # High rating (5 points)
    rating = record.get("rating_normalized")
    try:
        rating = float(rating) if rating else 0
    except (ValueError, TypeError):
        rating = 0
    if rating >= 4.0:
        score += 5

    # Reviews count (5 points)
    reviews = record.get("reviews_count_normalized")
    try:
        reviews = int(float(reviews)) if reviews else 0
    except (ValueError, TypeError):
        reviews = 0
    if reviews >= 20:
        score += 5

    # LinkedIn/social presence (5 points)
    if (
        record.get("enriched_linkedin_url")
        or record.get("enriched_facebook_url")
        or record.get("enriched_instagram_url")
    ):
        score += 5

    # Company size known with confidence (5 points)
    size_conf = record.get("company_size_confidence", "")
    if size_conf in ("high", "medium"):
        score += 5

    # Generic emails available (5 points - less valuable than real ones)
    generic = record.get("enriched_generic_emails")
    if generic and str(generic).strip():
        score += 5

    # Complete business info (10 points)
    completeness_fields = ["business_name", "category", "address", "website_domain"]
    present = sum(
        1 for f in completeness_fields if record.get(f) and str(record[f]).strip()
    )
    if present >= 3:
        score += 10
    elif present >= 2:
        score += 5

    # Prominence bonus — appeared multiple times in source data (5 points)
    dup_count = record.get("duplicate_count")
    try:
        dup_count = int(dup_count) if dup_count else 1
    except (ValueError, TypeError):
        dup_count = 1
    if dup_count >= 2:
        score += 5

    return min(score, 100)


def calculate_target_prospect_score(record: Dict[str, Any]) -> int:
    """
    Calculate target prospect relevance score (0-100).

    Combines lead quality with business category relevance
    for vanilla/cacao/spice sales targeting.

    Components:
    + Category relevance: 0-40 points
    + Ingredient signals: 0-20 points
    + Certification signals: 0-10 points
    + Lead quality factor: 0-30 points (derived from lead_quality_score)
    """
    score = 0

    # Category relevance (40 points max)
    category = str(record.get("category", "")).lower()
    canonical = str(record.get("canonical_category", "")).lower()

    category_score = 0
    for term in TARGET_CATEGORIES["primary"]:
        if term in category or term in canonical:
            category_score = 40
            break
    if category_score == 0:
        for term in TARGET_CATEGORIES["horeca_distro"]:
            if term in category or term in canonical:
                category_score = 35  # Distributors are very high value
                break
    if category_score == 0:
        for term in TARGET_CATEGORIES["secondary"]:
            if term in category or term in canonical:
                category_score = 25
                break
    if category_score == 0:
        for term in TARGET_CATEGORIES["beauty"]:
            if term in category or term in canonical:
                category_score = 20
                break
    if category_score == 0 and canonical == "food":
        category_score = 15  # Generic food category

    score += category_score

    # Ingredient signals from website content (20 points max)
    ingredient_signals = str(record.get("ingredient_signals", "")).lower()
    website_text = str(record.get("enriched_description", "")).lower()
    combined_text = f"{ingredient_signals} {website_text}"

    ingredient_matches = sum(1 for kw in INGREDIENT_KEYWORDS if kw in combined_text)
    score += min(ingredient_matches * 5, 20)

    # Certification signals (10 points max)
    cert_text = str(record.get("certifications", "")).lower()
    combined_cert = f"{cert_text} {website_text}"
    cert_matches = sum(1 for cert in CERTIFICATIONS if cert in combined_cert)
    score += min(cert_matches * 3, 10)

    # Lead quality factor (30 points max)
    lead_score = record.get("lead_quality_score", 0)
    try:
        lead_score = int(lead_score)
    except (ValueError, TypeError):
        lead_score = 0
    score += int(lead_score * 0.3)  # 30% of lead score

    return min(score, 100)


def get_enrichment_status(record: Dict[str, Any]) -> str:
    """
    Determine enrichment status based on what was found.

    Returns:
        'ok' - Good enrichment data
        'partial' - Some data found but incomplete
        'failed' - No enrichment data
    """
    has_company_name = bool(record.get("enriched_company_name"))
    has_email = bool((record.get("enriched_emails_found") or 0) > 0)
    has_social = bool(
        record.get("enriched_linkedin_url") or record.get("enriched_facebook_url")
    )
    has_contacts = bool(record.get("enriched_contact_email"))
    has_dm = bool(record.get("decision_maker_name"))

    if has_dm and has_contacts:
        return "ok"
    elif has_company_name and (has_email or has_contacts):
        return "ok"
    elif has_company_name or has_social or has_email:
        return "partial"
    else:
        return "failed"


def score_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """
    Add scoring fields to a record.

    Adds:
    - lead_quality_score: int (0-100) - overall lead quality for outreach
    - target_prospect_score: int (0-100) - relevance to vanilla/cacao/spice business
    - enrichment_status: str - ok/partial/failed
    - lead_category: str - hot/warm/cold/poor
    - scored_at: datetime
    """
    record["lead_quality_score"] = calculate_lead_score(record)
    record["target_prospect_score"] = calculate_target_prospect_score(record)
    record["enrichment_status"] = get_enrichment_status(record)
    record["lead_category"] = categorize_lead(record["lead_quality_score"])
    record["scored_at"] = datetime.utcnow().isoformat()

    return record


def categorize_lead(score: int) -> str:
    """Categorize lead based on score."""
    if score >= 75:
        return "hot"
    elif score >= 50:
        return "warm"
    elif score >= 30:
        return "cold"
    else:
        return "poor"


def get_score_breakdown(record: Dict[str, Any]) -> Dict[str, int]:
    """Get detailed score breakdown for reporting."""
    has_dm_name = bool(record.get("decision_maker_name"))
    has_dm_email = bool(record.get("enriched_contact_email"))

    dm_score = 0
    if has_dm_name and has_dm_email:
        dm_score = 25
    elif has_dm_name:
        dm_score = 12
    elif has_dm_email:
        dm_score = 15

    emails_found = record.get("enriched_emails_found") or 0
    try:
        emails_found = int(emails_found)
    except (ValueError, TypeError):
        emails_found = 0

    rating = record.get("rating_normalized")
    try:
        rating = float(rating) if rating else 0
    except (ValueError, TypeError):
        rating = 0

    reviews = record.get("reviews_count_normalized")
    try:
        reviews = int(float(reviews)) if reviews else 0
    except (ValueError, TypeError):
        reviews = 0

    breakdown = {
        "decision_maker": dm_score,
        "email": min(emails_found * 7, 20),
        "website": 10
        if (record.get("website_normalized") or record.get("website_raw"))
        else 0,
        "phone": 5 if record.get("phone_valid") else 0,
        "rating": 5 if rating >= 4.0 else 0,
        "reviews": 5 if reviews >= 20 else 0,
        "social": 5
        if (
            record.get("enriched_linkedin_url")
            or record.get("enriched_facebook_url")
            or record.get("enriched_instagram_url")
        )
        else 0,
        "company_size": 5
        if record.get("company_size_confidence", "") in ("high", "medium")
        else 0,
        "generic_emails": 5
        if (
            record.get("enriched_generic_emails")
            and str(record.get("enriched_generic_emails", "")).strip()
        )
        else 0,
        "completeness": 0,
        "prominence": 5
        if (
            int(record.get("duplicate_count") or 1)
            if str(record.get("duplicate_count", "1")).isdigit()
            else 1
        )
        >= 2
        else 0,
    }

    completeness_fields = ["business_name", "category", "address", "website_domain"]
    present = sum(
        1 for f in completeness_fields if record.get(f) and str(record[f]).strip()
    )
    if present >= 3:
        breakdown["completeness"] = 10
    elif present >= 2:
        breakdown["completeness"] = 5

    return breakdown
