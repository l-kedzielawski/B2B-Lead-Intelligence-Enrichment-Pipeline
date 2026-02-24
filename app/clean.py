"""Data cleaning and normalization module."""

import re
import logging
import phonenumbers
from typing import Dict, Any, Optional, List
import pandas as pd
from tqdm import tqdm
from .utils import (
    clean_text,
    normalize_url,
    parse_address_components,
    BUSINESS_SUFFIXES_RAW_SORTED,
)

logger = logging.getLogger(__name__)

# Country mappings for phone number parsing
COUNTRY_CODES = {
    "US": "US",
    "USA": "US",
    "United States": "US",
    "UK": "GB",
    "United Kingdom": "GB",
    "GB": "GB",
    "CA": "CA",
    "Canada": "CA",
    "AU": "AU",
    "Australia": "AU",
    "DE": "DE",
    "Germany": "DE",
    "Deutschland": "DE",
    "FR": "FR",
    "France": "FR",
    "ES": "ES",
    "Spain": "ES",
    "España": "ES",
    "IT": "IT",
    "Italy": "IT",
    "Italia": "IT",
    "NL": "NL",
    "Netherlands": "NL",
    "BE": "BE",
    "Belgium": "BE",
    "CH": "CH",
    "Switzerland": "CH",
    "AT": "AT",
    "Austria": "AT",
    "PT": "PT",
    "Portugal": "PT",
    "PL": "PL",
    "Poland": "PL",
    "Polska": "PL",
}

# Multi-language category mappings for food/bakery businesses (for vanilla sellers)
CATEGORY_SYNONYMS = {
    # Food businesses relevant to vanilla sales
    "food": {
        # English
        "bakery",
        "bakeries",
        "boulangerie",
        "pastry",
        "patisserie",
        "confectionery",
        "cake shop",
        "dessert shop",
        "sweet shop",
        "chocolatier",
        "chocolate shop",
        "ice cream",
        "ice cream shop",
        "gelato",
        "gelateria",
        "frozen yogurt",
        "cafe",
        "coffee shop",
        "tea room",
        "cafeteria",
        "restaurant",
        "fine dining",
        "bistro",
        "brasserie",
        "catering",
        "caterer",
        "event catering",
        "food manufacturer",
        "food production",
        "flavor company",
        "ingredient supplier",
        "hotel",
        "pastry chef",
        # German
        "bäckerei",
        "konditorei",
        "cafe",
        "café",
        "confiserie",
        "schokolade",
        "eisdiele",
        "eiscafe",
        "speiseeis",
        "restaurant",
        "gastronomie",
        "grosskuche",
        "partyservice",
        "catering",
        "hotel",
        "lebensmittelhersteller",
        # French
        "boulangerie",
        "pâtisserie",
        "confiserie",
        "chocolatier",
        "chocolaterie",
        "glacier",
        "glace",
        "crêperie",
        "salon de thé",
        "café",
        "restaurant",
        "traiteur",
        "restauration",
        "catering",
        "hôtel",
        "chef pâtissier",
        # Italian
        "panetteria",
        "pasticceria",
        "confetteria",
        "cioccolateria",
        "gelateria",
        "bar",
        "caffè",
        "cafe",
        "ristorante",
        "trattoria",
        "osteria",
        "catering",
        "albergo",
        "pastry chef",
        "pasticcere",
        "chef pasticcere",
        # Spanish
        "panadería",
        "pastelería",
        "confitería",
        "chocolatería",
        "heladería",
        "cafetería",
        "café",
        "restaurante",
        "catering",
        "hotel",
        "repostería",
        # Portuguese
        "padaria",
        "pastelaria",
        "confeitaria",
        "chocolataria",
        "gelataria",
        "café",
        "cafetaria",
        "restaurante",
        "catering",
        "hotel",
        "doçaria",
        # Dutch
        "bakkerij",
        "banketbakkerij",
        "chocolaterie",
        "patisserie",
        "ijssalon",
        "cafe",
        "cafetaria",
        "restaurant",
        "catering",
        "hotel",
        "gebak",
        # Polish
        "piekarnia",
        "cukiernia",
        "lodziarnia",
        "kawiarnia",
        "restauracja",
        "catering",
        "hotel",
        "cukiernik",
        "wytwórnia",
        "producent",
        # Spice/ingredient-related
        "spice shop",
        "gewürzladen",
        "épicerie",
        "drogheria",
        "especiaria",
        "flavor company",
        "aromen",
        "food ingredient",
    },
    # Categories to exclude (NOT food businesses)
    "non_food": {
        "plumber",
        "electrician",
        "lawyer",
        "attorney",
        "accountant",
        "consultant",
        "hair salon",
        "barber",
        "auto repair",
        "mechanic",
        "car dealer",
        "gas station",
        "real estate",
        "realtor",
        "insurance",
        "bank",
        "financial",
        "tech",
        "software",
        "it services",
        "web design",
        "marketing",
        "cleaning service",
        "construction",
        "contractor",
        "handyman",
        "gym",
        "fitness",
        "yoga studio",
        "dance studio",
        # German
        "klempner",
        "elektriker",
        "anwalt",
        "rechtsanwalt",
        "steuerberater",
        "friseur",
        "autowerkstatt",
        "immobilien",
        "versicherung",
        "bank",
        "reinigung",
        "bau",
        "fitnessstudio",
        # French
        "plombier",
        "électricien",
        "avocat",
        "comptable",
        "coiffeur",
        "garage",
        "immobilier",
        "assurance",
        "banque",
        "informatique",
        "nettoyage",
        "construction",
        "salle de sport",
        # Italian
        "idraulico",
        "elettricista",
        "avvocato",
        "commercialista",
        "parrucchiere",
        "officina",
        "immobiliare",
        "assicurazione",
        "banca",
        "informatica",
        "pulizie",
        "edilizia",
        "palestra",
        # Spanish
        "fontanero",
        "electricista",
        "abogado",
        "contable",
        "peluquería",
        "taller",
        "inmobiliaria",
        "seguros",
        "banco",
        "informática",
        "limpieza",
        "construcción",
        "gimnasio",
        # Portuguese
        "encanador",
        "eletricista",
        "advogado",
        "contador",
        "cabeleireiro",
        "oficina",
        "imobiliária",
        "seguro",
        "banco",
        "tecnologia",
        "limpeza",
        "construção",
        "ginásio",
        # Dutch
        "loodgieter",
        "electricien",
        "advocaat",
        "accountant",
        "kapper",
        "garage",
        "makelaar",
        "verzekering",
        "bank",
        "ict",
        "schoonmaak",
        "bouw",
        "sportschool",
        # Polish
        "hydraulik",
        "elektryk",
        "prawnik",
        "księgowy",
        "fryzjer",
        "warsztat",
        "biuro nieruchomości",
        "ubezpieczenia",
        "bank",
        "informatyka",
        "sprzątanie",
        "budowlanka",
        "siłownia",
    },
    "beauty": {
        # English
        "beauty salon",
        "beauty studio",
        "nail salon",
        "spa",
        "wellness",
        "cosmetics",
        "cosmetology",
        "skin care",
        "aromatherapy",
        "natural cosmetics",
        "essential oils",
        "perfumery",
        # German
        "kosmetik",
        "kosmetikstudio",
        "schönheitspflege",
        "nagelstudio",
        "spa",
        "wellnesscenter",
        "naturkosmetik",
        "parfümerie",
        "aromatherapie",
        # French
        "salon de beauté",
        "institut de beauté",
        "esthétique",
        "cosmétique",
        "spa",
        "bien-être",
        "cosmétique naturelle",
        "parfumerie",
        "aromathérapie",
        # Italian
        "salone di bellezza",
        "estetica",
        "centro estetico",
        "cosmetica",
        "spa",
        "benessere",
        "cosmetica naturale",
        "profumeria",
        # Spanish
        "salón de belleza",
        "estética",
        "centro de estética",
        "cosmética",
        "spa",
        "bienestar",
        "cosmética natural",
        "perfumería",
        # Portuguese
        "salão de beleza",
        "estética",
        "centro de estética",
        "cosmética",
        "spa",
        "bem-estar",
        "cosmética natural",
        "perfumaria",
        # Dutch
        "schoonheidssalon",
        "nagelstudio",
        "spa",
        "wellness",
        "natuurlijke cosmetica",
        "parfumerie",
        # Polish
        "salon kosmetyczny",
        "kosmetyka",
        "studio urody",
        "spa",
        "wellness",
        "kosmetyki naturalne",
        "perfumeria",
    },
    "horeca": {
        # English/International
        "horeca",
        "ho.re.ca",
        "food service",
        "food distribution",
        "food wholesale",
        "wholesale",
        "food supplier",
        "ingredient supplier",
        "spice supplier",
        "spice shop",
        # German
        "großhandel",
        "grosshandel",
        "lebensmittelgroßhandel",
        "gewürzhandel",
        "zulieferer",
        "lieferant",
        "gastronomiebedarf",
        "gastro-großhandel",
        # French
        "grossiste",
        "distribution alimentaire",
        "fournisseur",
        "épicerie fine",
        # Italian
        "ingrosso",
        "distribuzione alimentare",
        "fornitore",
        "drogheria",
        # Spanish
        "mayorista",
        "distribución alimentaria",
        "proveedor",
        "especias",
        # Portuguese
        "atacado",
        "distribuição alimentar",
        "fornecedor",
        "especiaria",
        # Dutch
        "groothandel",
        "voedseldistributie",
        "leverancier",
        "specerijhandel",
        # Polish
        "hurtownia",
        "dystrybucja żywności",
        "dostawca",
        "sklep z przyprawami",
    },
}


# Map category to canonical type
def normalize_category(category: str) -> str:
    """Normalize category to canonical type for filtering."""
    if not category:
        return "other"

    cat_lower = str(category).lower().strip()

    # Check categories in order of specificity
    for cat_key in ["beauty", "horeca", "food", "non_food"]:
        for term in CATEGORY_SYNONYMS[cat_key]:
            if term in cat_lower:
                return cat_key

    return "other"


def infer_country_from_city(city: str) -> Optional[str]:
    """Infer country code from city name or context."""
    if not city:
        return None

    city = city.lower()

    # Common city-country mappings
    city_mappings = {
        # UK cities
        "london": "GB",
        "manchester": "GB",
        "birmingham": "GB",
        "leeds": "GB",
        "glasgow": "GB",
        "sheffield": "GB",
        "bradford": "GB",
        "liverpool": "GB",
        "edinburgh": "GB",
        "bristol": "GB",
        # US cities
        "new york": "US",
        "los angeles": "US",
        "chicago": "US",
        "houston": "US",
        "phoenix": "US",
        "philadelphia": "US",
        "san antonio": "US",
        "san diego": "US",
        "dallas": "US",
        "san jose": "US",
        "austin": "US",
        "jacksonville": "US",
        "san francisco": "US",
        "columbus": "US",
        "charlotte": "US",
        "fort worth": "US",
        "indianapolis": "US",
        "seattle": "US",
        "denver": "US",
        "washington": "US",
        "boston": "US",
        "el paso": "US",
        "detroit": "US",
        "nashville": "US",
        "portland": "US",
        "oklahoma city": "US",
        "las vegas": "US",
        "louisville": "US",
        "baltimore": "US",
        "milwaukee": "US",
        "albuquerque": "US",
        "tucson": "US",
        "fresno": "US",
        "sacramento": "US",
        "mesa": "US",
        "kansas city": "US",
        "atlanta": "US",
        "long beach": "US",
        "colorado springs": "US",
        "raleigh": "US",
        "miami": "US",
        "virginia beach": "US",
        "omaha": "US",
        "oakland": "US",
        "minneapolis": "US",
        "tulsa": "US",
        "arlington": "US",
        "wichita": "US",
        "bakersfield": "US",
        "tampa": "US",
        "anaheim": "US",
        "honolulu": "US",
        "aurora": "US",
        "santa ana": "US",
        "riverside": "US",
        "corpus christi": "US",
        "lexington": "US",
        "stockton": "US",
        "toledo": "US",
        "st paul": "US",
        "newark": "US",
        "greensboro": "US",
        "plano": "US",
        "henderson": "US",
        "lincoln": "US",
        "buffalo": "US",
        "jersey city": "US",
        "chula vista": "US",
        "fort wayne": "US",
        "orlando": "US",
        "st petersburg": "US",
        "chandler": "US",
        "laredo": "US",
        "norfolk": "US",
        "durham": "US",
        "madison": "US",
        "lubbock": "US",
        "irvine": "US",
        "winston salem": "US",
        "glendale": "US",
        "garland": "US",
        "hialeah": "US",
        "reno": "US",
        "chesapeake": "US",
        "gilbert": "US",
        "baton rouge": "US",
        "irving": "US",
        "scottsdale": "US",
        "north las vegas": "US",
        "fremont": "US",
        "boise": "US",
        "richmond": "US",
        # Canadian cities
        "toronto": "CA",
        "montreal": "CA",
        "vancouver": "CA",
        "calgary": "CA",
        "edmonton": "CA",
        "ottawa": "CA",
        "winnipeg": "CA",
        "quebec city": "CA",
        "hamilton": "CA",
        "kitchener": "CA",
        "london ontario": "CA",
        "victoria": "CA",
        "halifax": "CA",
        "oshawa": "CA",
        "windsor": "CA",
        "saskatoon": "CA",
        # Australian cities
        "sydney": "AU",
        "melbourne": "AU",
        "brisbane": "AU",
        "perth": "AU",
        "adelaide": "AU",
        "gold coast": "AU",
        "canberra": "AU",
        "newcastle": "AU",
        # German cities
        "berlin": "DE",
        "hamburg": "DE",
        "munich": "DE",
        "münchen": "DE",
        "cologne": "DE",
        "köln": "DE",
        "frankfurt": "DE",
        "stuttgart": "DE",
        "düsseldorf": "DE",
        "dortmund": "DE",
        "essen": "DE",
        "leipzig": "DE",
        "bremen": "DE",
        "dresden": "DE",
        "hanover": "DE",
        "hannover": "DE",
        "nuremberg": "DE",
        "nürnberg": "DE",
        "duisburg": "DE",
        "bochum": "DE",
        "wuppertal": "DE",
        "bielefeld": "DE",
        "bonn": "DE",
        # French cities
        "paris": "FR",
        "marseille": "FR",
        "lyon": "FR",
        "toulouse": "FR",
        "nice": "FR",
        "nantes": "FR",
        "strasbourg": "FR",
        "montpellier": "FR",
        "bordeaux": "FR",
        "lille": "FR",
        "rennes": "FR",
        "reims": "FR",
        # Austrian cities (AT)
        "vienna": "AT",
        "wien": "AT",
        "graz": "AT",
        "linz": "AT",
        "salzburg": "AT",
        "innsbruck": "AT",
        "klagenfurt": "AT",
        "villach": "AT",
        "wels": "AT",
        "sankt pölten": "AT",
        # Swiss cities (CH) - with German/French/Italian variants
        "zurich": "CH",
        "zürich": "CH",
        "geneva": "CH",
        "genf": "CH",
        "basel": "CH",
        "bern": "CH",
        "lausanne": "CH",
        "winterthur": "CH",
        "lucerne": "CH",
        "luzern": "CH",
        "sankt gallen": "CH",
        "biel": "CH",
        "thun": "CH",
        "köniz": "CH",
        "la chaux-de-fonds": "CH",
        # Italian cities (IT) - with English/Italian variants
        "rome": "IT",
        "roma": "IT",
        "milan": "IT",
        "milano": "IT",
        "naples": "IT",
        "napoli": "IT",
        "turin": "IT",
        "torino": "IT",
        "palermo": "IT",
        "genoa": "IT",
        "genova": "IT",
        "bologna": "IT",
        "florence": "IT",
        "firenze": "IT",
        "bari": "IT",
        "catania": "IT",
        "venice": "IT",
        "venezia": "IT",
        "verona": "IT",
        "messina": "IT",
        "padua": "IT",
        "padova": "IT",
        "trieste": "IT",
        "brescia": "IT",
        "prato": "IT",
        "taranto": "IT",
        "parma": "IT",
        "modena": "IT",
        "reggio calabria": "IT",
        "reggio emilia": "IT",
        "perugia": "IT",
        "ravenna": "IT",
        "livorno": "IT",
        "cagliari": "IT",
        "foggia": "IT",
        # Portuguese cities (PT) - with English/Portuguese variants
        "lisbon": "PT",
        "lisboa": "PT",
        "porto": "PT",
        "braga": "PT",
        "coimbra": "PT",
        "faro": "PT",
        "aveiro": "PT",
        "evora": "PT",
        "funchal": "PT",
        "setubal": "PT",
        "viana do castelo": "PT",
        "guarda": "PT",
        "covilha": "PT",
        "portimao": "PT",
        "ponta delgada": "PT",
        # Dutch cities (NL) - with English/Dutch variants
        "amsterdam": "NL",
        "rotterdam": "NL",
        "the hague": "NL",
        "den haag": "NL",
        "utrecht": "NL",
        "eindhoven": "NL",
        "tilburg": "NL",
        "groningen": "NL",
        "almere": "NL",
        "breda": "NL",
        "nijmegen": "NL",
        "enschede": "NL",
        "apeldoorn": "NL",
        "haarlem": "NL",
        "arnhem": "NL",
        "zaanstad": "NL",
        "'s-hertogenbosch": "NL",
        "den bosch": "NL",
        "maastricht": "NL",
        # Belgian cities (BE) - trilingual (Dutch/French/German)
        "brussels": "BE",
        "bruxelles": "BE",
        "brussel": "BE",
        "antwerp": "BE",
        "antwerpen": "BE",
        "anvers": "BE",
        "ghent": "BE",
        "gent": "BE",
        "gand": "BE",
        "charleroi": "BE",
        "liege": "BE",
        "luik": "BE",
        "bruges": "BE",
        "brugge": "BE",
        "namur": "BE",
        "namen": "BE",
        "leuven": "BE",
        "louvain": "BE",
        "mons": "BE",
        "bergen": "BE",
        "mechelen": "BE",
        "malines": "BE",
        # Polish cities (PL) - with English/Polish variants
        "warsaw": "PL",
        "warszawa": "PL",
        "krakow": "PL",
        "kraków": "PL",
        "lodz": "PL",
        "łódź": "PL",
        "wroclaw": "PL",
        "wrocław": "PL",
        "poznan": "PL",
        "poznań": "PL",
        "gdansk": "PL",
        "gdańsk": "PL",
        "szczecin": "PL",
        "bydgoszcz": "PL",
        "lublin": "PL",
        "katowice": "PL",
        "bialystok": "PL",
        "białystok": "PL",
        "gdynia": "PL",
        "czestochowa": "PL",
        "częstochowa": "PL",
        "radom": "PL",
        "sosnowiec": "PL",
        "torun": "PL",
        "toruń": "PL",
        "kielce": "PL",
        "rzeszow": "PL",
        "rzeszów": "PL",
        "olsztyn": "PL",
        "zabrze": "PL",
        "gorzow wielkopolski": "PL",
    }

    return city_mappings.get(city)


def parse_phone(phone: Any, city: Optional[str] = None) -> Dict[str, Any]:
    """
    Parse phone number to E.164 format.

    Returns dict with phone_e164, phone_country, phone_valid
    """
    result = {"phone_e164": None, "phone_country": None, "phone_valid": False}

    if not phone or pd.isna(phone):
        return result

    phone = str(phone).strip()

    # Infer country from city
    country = infer_country_from_city(city) if city else None

    try:
        # Parse phone number
        parsed = phonenumbers.parse(phone, country)

        if phonenumbers.is_valid_number(parsed):
            result["phone_e164"] = phonenumbers.format_number(
                parsed, phonenumbers.PhoneNumberFormat.E164
            )
            result["phone_country"] = phonenumbers.region_code_for_number(parsed)
            result["phone_valid"] = True
        else:
            # Try formatting anyway
            result["phone_e164"] = phonenumbers.format_number(
                parsed, phonenumbers.PhoneNumberFormat.E164
            )
            result["phone_country"] = phonenumbers.region_code_for_number(parsed)

    except phonenumbers.NumberParseException as e:
        logger.debug(f"Could not parse phone '{phone}': {e}")
    except Exception as e:
        logger.debug(f"Unexpected error parsing phone '{phone}': {e}")

    return result


def normalize_rating(rating: Any) -> Optional[float]:
    """Normalize rating to numeric value 0-5."""
    if rating is None or pd.isna(rating):
        return None

    try:
        # Remove any non-numeric prefix/suffix
        rating_str = str(rating).strip()

        # Extract number from strings like "4.5 out of 5" or "4.5 stars"
        match = re.search(r"(\d+(?:\.\d+)?)", rating_str)
        if match:
            rating_val = float(match.group(1))
            # Clamp to 0-5 range
            return max(0.0, min(5.0, rating_val))
    except (ValueError, TypeError):
        pass

    return None


def normalize_reviews_count(count: Any) -> Optional[int]:
    """Normalize reviews count to integer."""
    if count is None or pd.isna(count):
        return None

    try:
        count_str = str(count).strip()
        # Remove commas, parentheses, and other formatting
        count_str = re.sub(r"[\(\),]", "", count_str)
        # Extract number
        match = re.search(r"(\d+)", count_str)
        if match:
            return int(match.group(1))
    except (ValueError, TypeError):
        pass

    return None


def normalize_coordinates(lat: Any, lon: Any) -> Dict[str, Any]:
    """Normalize latitude and longitude to floats."""
    result: Dict[str, Any] = {"lat": None, "lon": None}

    try:
        if lat is not None and not pd.isna(lat):
            result["lat"] = float(lat)
        if lon is not None and not pd.isna(lon):
            result["lon"] = float(lon)
    except (ValueError, TypeError):
        pass

    return result


def map_columns(df: pd.DataFrame, source_file: str) -> pd.DataFrame:
    """
    Map input columns to canonical schema.
    Handles common variations in column names.
    """
    # Common column name mappings
    column_mappings = {
        # Business name variations
        "business_name": [
            "business_name",
            "name",
            "title",
            "store_name",
            "company_name",
            "place_name",
            "location_name",
        ],
        # Category variations
        "category": ["category", "type", "business_type", "categories", "sector"],
        # Address variations
        "address": [
            "address",
            "full_address",
            "street_address",
            "location",
            "formatted_address",
            "vicinity",
        ],
        # Phone variations
        "phone_raw": [
            "phone",
            "phone_raw",
            "phone_number",
            "telephone",
            "tel",
            "contact_number",
            "phone_no",
        ],
        # Website variations
        "website_raw": [
            "website",
            "website_raw",
            "site",
            "web",
            "url",
            "web_site",
            "business_url",
        ],
        # Email variations
        "email_raw": [
            "email",
            "email_raw",
            "business_email",
            "contact_email",
            "mail",
            "e_mail",
        ],
        # Google Maps URL variations
        "google_maps_url": [
            "google_maps_url",
            "maps_url",
            "gmaps_url",
            "google_url",
            "map_url",
            "url",
        ],
        # Rating variations
        "rating": ["rating", "star_rating", "stars", "score", "avg_rating"],
        # Reviews count variations
        "reviews_count": [
            "reviews_count",
            "review_count",
            "num_reviews",
            "total_reviews",
            "user_ratings_total",
            "reviews",
        ],
        # City variations
        "city": ["city", "town", "location_city", "city_name"],
        # Coordinates
        "lat": ["lat", "latitude", "y", "location_lat"],
        "lon": ["lon", "lng", "longitude", "x", "location_lng"],
    }

    result = {}
    used_columns = set()

    # Find matching columns
    for canonical, variations in column_mappings.items():
        for col in df.columns:
            col_lower = col.lower().strip()
            if col_lower in [v.lower() for v in variations] and col not in used_columns:
                result[canonical] = df[col]
                used_columns.add(col)
                break

    # Create DataFrame with canonical columns
    canonical_df = pd.DataFrame(result)

    # Ensure all canonical columns exist (fill with None if missing)
    for col in [
        "city",
        "business_name",
        "category",
        "address",
        "phone_raw",
        "website_raw",
        "email_raw",
        "google_maps_url",
        "rating",
        "reviews_count",
        "lat",
        "lon",
    ]:
        if col not in canonical_df.columns:
            canonical_df[col] = None

    # Add metadata
    canonical_df["source_file"] = source_file
    canonical_df["source_row_id"] = range(len(df))

    return canonical_df


def clean_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """Clean and normalize a single record."""
    cleaned = record.copy()

    # Clean text fields
    text_fields = ["business_name", "category", "address"]
    for field in text_fields:
        if field in cleaned:
            cleaned[field] = clean_text(cleaned[field])

    # Normalize category to canonical type (for multi-language filtering)
    cleaned["canonical_category"] = normalize_category(cleaned.get("category", ""))

    # Normalize business name
    if cleaned.get("business_name"):
        name = cleaned["business_name"]
        # Remove common business suffixes (shared constant, pre-sorted longest-first)
        name_lower = name.lower()
        for suffix in BUSINESS_SUFFIXES_RAW_SORTED:
            if name_lower.endswith(suffix):
                name = name[: -len(suffix)].strip()
                break
        cleaned["business_name"] = name

    # Parse and normalize phone
    phone_raw = cleaned.get("phone_raw")
    phone_info = parse_phone(
        str(phone_raw) if phone_raw is not None else None,
        str(cleaned.get("city")) if cleaned.get("city") is not None else None,
    )
    cleaned.update(phone_info)

    # Normalize website
    website_raw = cleaned.get("website_raw")
    if website_raw:
        cleaned["website_normalized"] = normalize_url(website_raw)
        # Extract domain
        from .utils import extract_domain

        website_normalized = cleaned["website_normalized"]
        cleaned["website_domain"] = (
            extract_domain(str(website_normalized)) if website_normalized else None
        )
    else:
        cleaned["website_normalized"] = None
        cleaned["website_domain"] = None

    # Normalize fallback/source email
    email_raw = cleaned.get("email_raw")
    if email_raw is not None and not pd.isna(email_raw):
        cleaned["email_raw"] = str(email_raw).strip().lower()
    else:
        cleaned["email_raw"] = None

    # Normalize rating
    cleaned["rating_normalized"] = normalize_rating(cleaned.get("rating"))

    # Normalize reviews count
    cleaned["reviews_count_normalized"] = normalize_reviews_count(
        cleaned.get("reviews_count")
    )

    # Normalize coordinates
    coord_info = normalize_coordinates(cleaned.get("lat"), cleaned.get("lon"))
    cleaned.update(coord_info)

    # Parse address components
    if cleaned.get("address"):
        addr_components = parse_address_components(cleaned["address"])
        cleaned["address_street"] = addr_components.get("street", "")
        cleaned["address_postcode"] = addr_components.get("postcode", "")
        cleaned["address_city_component"] = addr_components.get("city_component", "")
        cleaned["address_country"] = addr_components.get("country", "")

    return cleaned


def clean_dataframe(df: pd.DataFrame, source_file: str) -> pd.DataFrame:
    """Clean and normalize entire DataFrame."""
    logger.info(f"Cleaning data from {source_file}")

    # Map columns to canonical schema
    df_canonical = map_columns(df, source_file)

    # Clean each record
    records = df_canonical.to_dict("records")
    cleaned_records = []

    for record in tqdm(records, desc=f"Cleaning {source_file}", leave=False):
        cleaned_records.append(clean_record(record))

    return pd.DataFrame(cleaned_records)


def merge_dataframes(dfs: List[pd.DataFrame]) -> pd.DataFrame:
    """Merge multiple DataFrames into one."""
    if not dfs:
        return pd.DataFrame()

    merged = pd.concat(dfs, ignore_index=True)

    # Reset source_row_id to be unique across all files
    merged["source_row_id"] = merged.index

    logger.info(f"Merged {len(merged)} total records from {len(dfs)} files")
    return merged
