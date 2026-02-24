"""Extract structured business data from JSON-LD / Schema.org markup in HTML."""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Schema.org type -> simple business category
SCHEMA_TYPE_MAP: Dict[str, str] = {
    'Restaurant': 'food',
    'FoodEstablishment': 'food',
    'Bakery': 'food',
    'CafeOrCoffeeShop': 'food',
    'IceCreamShop': 'food',
    'FastFoodRestaurant': 'food',
    'BarOrPub': 'food',
    'Brewery': 'food',
    'Winery': 'food',
    'Hotel': 'horeca',
    'LodgingBusiness': 'horeca',
    'Motel': 'horeca',
    'Hostel': 'horeca',
    'BedAndBreakfast': 'horeca',
    'Resort': 'horeca',
    'BeautySalon': 'beauty',
    'HealthAndBeautyBusiness': 'beauty',
    'HairSalon': 'beauty',
    'NailSalon': 'beauty',
    'DaySpa': 'beauty',
    'TattooParlor': 'beauty',
    'Store': 'retail',
    'GroceryStore': 'retail',
    'ClothingStore': 'retail',
    'ElectronicsStore': 'retail',
    'HardwareStore': 'retail',
    'ShoppingCenter': 'retail',
    'Organization': 'business',
    'LocalBusiness': 'business',
    'Corporation': 'business',
    'ProfessionalService': 'business',
    'FinancialService': 'business',
    'InsuranceAgency': 'business',
    'RealEstateAgent': 'business',
    'AutoRepair': 'business',
    'AutoDealer': 'business',
}

# Social platform domains for sameAs extraction
SOCIAL_PLATFORMS: Dict[str, str] = {
    'linkedin.com': 'linkedin',
    'facebook.com': 'facebook',
    'fb.com': 'facebook',
    'instagram.com': 'instagram',
    'twitter.com': 'twitter',
    'x.com': 'twitter',
    'tiktok.com': 'tiktok',
    'youtube.com': 'youtube',
    'youtu.be': 'youtube',
}

# Schema.org types we care about for business extraction
BUSINESS_TYPES: set = set(SCHEMA_TYPE_MAP.keys())


@dataclass
class JsonLdResult:
    """Structured data extracted from JSON-LD markup."""

    business_name: Optional[str] = None
    description: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[str] = None
    founder_name: Optional[str] = None
    employee_count: Optional[str] = None
    founding_date: Optional[str] = None
    social_links: Dict[str, str] = field(default_factory=dict)
    business_type: Optional[str] = None
    opening_hours: Optional[str] = None
    price_range: Optional[str] = None
    geo_lat: Optional[float] = None
    geo_lon: Optional[float] = None
    image_url: Optional[str] = None

    def is_empty(self) -> bool:
        """Check if no meaningful data was extracted."""
        return (
            self.business_name is None
            and self.email is None
            and self.phone is None
            and self.address is None
            and self.founder_name is None
            and not self.social_links
        )

    def completeness_score(self) -> int:
        """Return a rough score (0-100) of how complete this result is."""
        score = 0
        if self.business_name:
            score += 15
        if self.description:
            score += 5
        if self.email:
            score += 15
        if self.phone:
            score += 10
        if self.address:
            score += 10
        if self.founder_name:
            score += 10
        if self.employee_count:
            score += 5
        if self.founding_date:
            score += 5
        if self.social_links:
            score += min(len(self.social_links) * 3, 10)
        if self.business_type:
            score += 5
        if self.opening_hours:
            score += 3
        if self.price_range:
            score += 2
        if self.geo_lat is not None and self.geo_lon is not None:
            score += 3
        if self.image_url:
            score += 2
        return min(score, 100)


class JsonLdExtractor:
    """Extract structured business information from JSON-LD in HTML pages."""

    def extract(self, html: str) -> JsonLdResult:
        """
        Extract structured business data from JSON-LD blocks in HTML.

        Parses all <script type="application/ld+json"> tags, handles
        both single objects and @graph arrays, and merges data from
        multiple blocks preferring the most complete data.

        Args:
            html: Raw HTML content of the page.

        Returns:
            JsonLdResult with extracted fields (never None, fields may be None).
        """
        if not html:
            return JsonLdResult()

        json_objects = self._parse_jsonld_blocks(html)
        if not json_objects:
            return JsonLdResult()

        # Flatten @graph arrays and collect all individual objects
        flat_objects = self._flatten_graphs(json_objects)

        # Extract business-relevant objects and sort by relevance
        business_objects = self._filter_business_objects(flat_objects)

        if not business_objects:
            # No recognized business types — try to extract from any object
            logger.debug("No recognized Schema.org business types found, trying all objects")
            business_objects = flat_objects

        # Extract a result from each candidate and merge
        results: List[JsonLdResult] = []
        for obj in business_objects:
            result = self._extract_from_object(obj)
            if not result.is_empty():
                results.append(result)

        if not results:
            return JsonLdResult()

        if len(results) == 1:
            return results[0]

        return self._merge_results(results)

    # ── Parsing ──────────────────────────────────────────────────────────

    def _parse_jsonld_blocks(self, html: str) -> List[Any]:
        """
        Parse all JSON-LD script blocks from HTML.

        Returns:
            List of parsed JSON objects/arrays.
        """
        objects: List[Any] = []

        try:
            soup = BeautifulSoup(html, 'lxml')
        except Exception:
            try:
                soup = BeautifulSoup(html, 'html.parser')
            except Exception as e:
                logger.debug(f"Failed to parse HTML: {e}")
                return objects

        scripts = soup.find_all('script', attrs={'type': 'application/ld+json'})

        for script in scripts:
            raw = script.string
            if not raw:
                continue
            # Strip HTML comments that sometimes wrap JSON-LD content
            raw = raw.strip()
            if raw.startswith('<!--'):
                raw = raw[4:]
            if raw.endswith('-->'):
                raw = raw[:-3]
            raw = raw.strip()

            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    objects.extend(parsed)
                elif isinstance(parsed, dict):
                    objects.append(parsed)
            except json.JSONDecodeError as e:
                logger.debug(f"Failed to parse JSON-LD block: {e}")
                # Try to salvage with lenient parsing (trailing commas, etc.)
                cleaned = self._sanitize_json(raw)
                try:
                    parsed = json.loads(cleaned)
                    if isinstance(parsed, list):
                        objects.extend(parsed)
                    elif isinstance(parsed, dict):
                        objects.append(parsed)
                except json.JSONDecodeError:
                    pass

        return objects

    @staticmethod
    def _sanitize_json(raw: str) -> str:
        """Best-effort cleanup of malformed JSON (trailing commas, etc.)."""
        # Remove trailing commas before } or ]
        cleaned = re.sub(r',\s*([}\]])', r'\1', raw)
        # Remove JS-style single-line comments
        cleaned = re.sub(r'//.*?$', '', cleaned, flags=re.MULTILINE)
        return cleaned

    def _flatten_graphs(self, objects: List[Any]) -> List[Dict[str, Any]]:
        """Flatten @graph arrays into a flat list of objects."""
        flat: List[Dict[str, Any]] = []
        for obj in objects:
            if not isinstance(obj, dict):
                continue
            if '@graph' in obj:
                graph = obj['@graph']
                if isinstance(graph, list):
                    for item in graph:
                        if isinstance(item, dict):
                            flat.append(item)
                elif isinstance(graph, dict):
                    flat.append(graph)
            else:
                flat.append(obj)
        return flat

    def _filter_business_objects(self, objects: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Filter and sort objects to those with recognized business types."""
        business_objs: List[Dict[str, Any]] = []
        for obj in objects:
            obj_types = self._get_types(obj)
            if obj_types & BUSINESS_TYPES:
                business_objs.append(obj)
        return business_objs

    # ── Extraction from a single JSON-LD object ─────────────────────────

    def _extract_from_object(self, obj: Dict[str, Any]) -> JsonLdResult:
        """
        Extract all business-relevant fields from a single JSON-LD object.

        Args:
            obj: A parsed JSON-LD dictionary.

        Returns:
            JsonLdResult populated with whatever was found.
        """
        result = JsonLdResult()

        try:
            result.business_name = self._get_str(obj, 'name')
            result.description = self._get_str(obj, 'description')
            result.image_url = self._extract_image(obj)
            result.price_range = self._get_str(obj, 'priceRange')

            # Contact info
            result.email = self._extract_email(obj)
            result.phone = self._extract_phone(obj)

            # Address
            result.address = self._extract_address(obj)

            # Geo
            result.geo_lat, result.geo_lon = self._extract_geo(obj)

            # Founder / person
            result.founder_name = self._extract_founder(obj)

            # Employees
            result.employee_count = self._extract_employee_count(obj)

            # Founding date
            result.founding_date = self._extract_founding_date(obj)

            # Social links from sameAs
            result.social_links = self._extract_social_links(obj)

            # Business type category
            result.business_type = self._extract_business_type(obj)

            # Opening hours
            result.opening_hours = self._extract_opening_hours(obj)

        except Exception as e:
            logger.debug(f"Error extracting from JSON-LD object: {e}")

        return result

    # ── Field-level extractors ───────────────────────────────────────────

    def _extract_email(self, obj: Dict[str, Any]) -> Optional[str]:
        """Extract email from top-level or nested ContactPoint."""
        # Direct email field
        email = self._get_str(obj, 'email')
        if email:
            return self._clean_email(email)

        # ContactPoint
        contact_point = obj.get('contactPoint')
        if contact_point:
            points = contact_point if isinstance(contact_point, list) else [contact_point]
            for point in points:
                if isinstance(point, dict):
                    email = self._get_str(point, 'email')
                    if email:
                        return self._clean_email(email)

        return None

    def _extract_phone(self, obj: Dict[str, Any]) -> Optional[str]:
        """Extract phone from top-level telephone or nested ContactPoint."""
        phone = self._get_str(obj, 'telephone')
        if phone:
            return phone.strip()

        # ContactPoint
        contact_point = obj.get('contactPoint')
        if contact_point:
            points = contact_point if isinstance(contact_point, list) else [contact_point]
            for point in points:
                if isinstance(point, dict):
                    phone = self._get_str(point, 'telephone')
                    if phone:
                        return phone.strip()

        return None

    def _extract_address(self, obj: Dict[str, Any]) -> Optional[str]:
        """Extract and format postal address."""
        addr = obj.get('address')
        if addr is None:
            return None

        # String address
        if isinstance(addr, str):
            return addr.strip() if addr.strip() else None

        # List of addresses — take the first one
        if isinstance(addr, list):
            addr = addr[0] if addr else None
            if addr is None:
                return None
            if isinstance(addr, str):
                return addr.strip() if addr.strip() else None

        if not isinstance(addr, dict):
            return None

        # PostalAddress object
        parts: List[str] = []
        street = self._get_str(addr, 'streetAddress')
        if street:
            parts.append(street)

        locality = self._get_str(addr, 'addressLocality')
        region = self._get_str(addr, 'addressRegion')
        postal_code = self._get_str(addr, 'postalCode')

        city_line_parts: List[str] = []
        if postal_code:
            city_line_parts.append(postal_code)
        if locality:
            city_line_parts.append(locality)
        if region and region != locality:
            city_line_parts.append(region)
        if city_line_parts:
            parts.append(' '.join(city_line_parts))

        country = self._get_str(addr, 'addressCountry')
        if country:
            # addressCountry can be a string or {"@type": "Country", "name": "..."}
            if isinstance(obj.get('address', {}).get('addressCountry'), dict):
                country = self._get_str(obj['address']['addressCountry'], 'name') or country
            parts.append(country)

        return ', '.join(parts) if parts else None

    def _extract_geo(self, obj: Dict[str, Any]) -> tuple:
        """
        Extract latitude and longitude from GeoCoordinates.

        Returns:
            (lat, lon) tuple, both Optional[float].
        """
        geo = obj.get('geo')
        if not geo:
            return None, None

        if isinstance(geo, list):
            geo = geo[0] if geo else None
        if not isinstance(geo, dict):
            return None, None

        try:
            lat = geo.get('latitude')
            lon = geo.get('longitude')
            if lat is not None and lon is not None:
                return float(lat), float(lon)
        except (ValueError, TypeError):
            pass

        return None, None

    def _extract_founder(self, obj: Dict[str, Any]) -> Optional[str]:
        """Extract founder/owner name from Person objects."""
        # Try founder, then foundingTeam, then member
        for key in ('founder', 'foundingTeam', 'member', 'employee'):
            person = obj.get(key)
            if person is None:
                continue

            persons = person if isinstance(person, list) else [person]
            for p in persons:
                name = self._person_name(p)
                if name:
                    return name

        return None

    def _extract_employee_count(self, obj: Dict[str, Any]) -> Optional[str]:
        """Extract number of employees."""
        # numberOfEmployees can be a QuantitativeValue or plain number
        num = obj.get('numberOfEmployees')
        if num is None:
            return None

        if isinstance(num, (int, float)):
            return str(int(num))

        if isinstance(num, dict):
            # QuantitativeValue with value, minValue, maxValue
            value = num.get('value')
            if value is not None:
                return str(value)
            min_val = num.get('minValue')
            max_val = num.get('maxValue')
            if min_val is not None and max_val is not None:
                return f"{min_val}-{max_val}"
            if min_val is not None:
                return f"{min_val}+"
            if max_val is not None:
                return f"1-{max_val}"

        if isinstance(num, str):
            return num.strip() if num.strip() else None

        return None

    def _extract_founding_date(self, obj: Dict[str, Any]) -> Optional[str]:
        """Extract founding/established date."""
        for key in ('foundingDate', 'dateEstablished'):
            val = self._get_str(obj, key)
            if val:
                return val
        return None

    def _extract_social_links(self, obj: Dict[str, Any]) -> Dict[str, str]:
        """
        Extract social media links from sameAs field.

        Returns:
            Dict mapping platform name to URL, e.g. {'facebook': 'https://...'}.
        """
        links: Dict[str, str] = {}
        same_as = obj.get('sameAs')
        if same_as is None:
            return links

        urls = same_as if isinstance(same_as, list) else [same_as]

        for url in urls:
            if not isinstance(url, str):
                continue
            url_lower = url.lower()
            for domain_fragment, platform in SOCIAL_PLATFORMS.items():
                if domain_fragment in url_lower:
                    # Keep first occurrence per platform (most specific)
                    if platform not in links:
                        links[platform] = url
                    break

        return links

    def _extract_business_type(self, obj: Dict[str, Any]) -> Optional[str]:
        """Map Schema.org @type to a simple category string."""
        obj_types = self._get_types(obj)
        # Prefer more specific types over generic ones
        specific_first = ['Restaurant', 'Bakery', 'CafeOrCoffeeShop', 'IceCreamShop',
                          'FastFoodRestaurant', 'BarOrPub', 'Brewery', 'Winery',
                          'Hotel', 'BedAndBreakfast', 'Hostel', 'Motel', 'Resort',
                          'BeautySalon', 'HairSalon', 'NailSalon', 'DaySpa',
                          'HealthAndBeautyBusiness', 'TattooParlor',
                          'Store', 'GroceryStore', 'ClothingStore', 'ElectronicsStore',
                          'HardwareStore', 'ShoppingCenter',
                          'FoodEstablishment', 'LodgingBusiness',
                          'ProfessionalService', 'FinancialService',
                          'LocalBusiness', 'Organization', 'Corporation']
        for schema_type in specific_first:
            if schema_type in obj_types:
                return SCHEMA_TYPE_MAP.get(schema_type)

        return None

    def _extract_opening_hours(self, obj: Dict[str, Any]) -> Optional[str]:
        """
        Extract opening hours and format as a readable string.

        Handles both openingHoursSpecification (structured) and
        openingHours (plain string).
        """
        # Plain string format first
        plain = obj.get('openingHours')
        if isinstance(plain, str) and plain.strip():
            return plain.strip()
        if isinstance(plain, list):
            parts = [str(h).strip() for h in plain if h]
            if parts:
                return '; '.join(parts)

        # Structured openingHoursSpecification
        specs = obj.get('openingHoursSpecification')
        if not specs:
            return None

        if isinstance(specs, dict):
            specs = [specs]
        if not isinstance(specs, list):
            return None

        day_abbrev: Dict[str, str] = {
            'Monday': 'Mon', 'Tuesday': 'Tue', 'Wednesday': 'Wed',
            'Thursday': 'Thu', 'Friday': 'Fri', 'Saturday': 'Sat',
            'Sunday': 'Sun',
            'https://schema.org/Monday': 'Mon', 'https://schema.org/Tuesday': 'Tue',
            'https://schema.org/Wednesday': 'Wed', 'https://schema.org/Thursday': 'Thu',
            'https://schema.org/Friday': 'Fri', 'https://schema.org/Saturday': 'Sat',
            'https://schema.org/Sunday': 'Sun',
        }

        formatted: List[str] = []
        for spec in specs:
            if not isinstance(spec, dict):
                continue
            days_of_week = spec.get('dayOfWeek', [])
            if isinstance(days_of_week, str):
                days_of_week = [days_of_week]
            opens = self._get_str(spec, 'opens') or ''
            closes = self._get_str(spec, 'closes') or ''

            day_names = [day_abbrev.get(d, d) for d in days_of_week if isinstance(d, str)]
            if day_names and (opens or closes):
                days_str = ','.join(day_names)
                formatted.append(f"{days_str} {opens}-{closes}")

        return '; '.join(formatted) if formatted else None

    def _extract_image(self, obj: Dict[str, Any]) -> Optional[str]:
        """Extract primary image URL."""
        image = obj.get('image') or obj.get('logo')
        if image is None:
            return None

        if isinstance(image, str):
            return image.strip() if image.strip() else None

        if isinstance(image, list):
            image = image[0] if image else None
            if image is None:
                return None
            if isinstance(image, str):
                return image.strip() if image.strip() else None

        if isinstance(image, dict):
            return self._get_str(image, 'url') or self._get_str(image, 'contentUrl')

        return None

    # ── Merging ──────────────────────────────────────────────────────────

    def _merge_results(self, results: List[JsonLdResult]) -> JsonLdResult:
        """
        Merge multiple JsonLdResult objects, preferring the most complete one
        as the base and filling in gaps from others.

        Args:
            results: List of JsonLdResult objects to merge.

        Returns:
            A single merged JsonLdResult.
        """
        # Sort by completeness (most complete first)
        results.sort(key=lambda r: r.completeness_score(), reverse=True)
        merged = results[0]

        # Fill in missing fields from less-complete results
        for other in results[1:]:
            if merged.business_name is None and other.business_name is not None:
                merged.business_name = other.business_name
            if merged.description is None and other.description is not None:
                merged.description = other.description
            if merged.email is None and other.email is not None:
                merged.email = other.email
            if merged.phone is None and other.phone is not None:
                merged.phone = other.phone
            if merged.address is None and other.address is not None:
                merged.address = other.address
            if merged.founder_name is None and other.founder_name is not None:
                merged.founder_name = other.founder_name
            if merged.employee_count is None and other.employee_count is not None:
                merged.employee_count = other.employee_count
            if merged.founding_date is None and other.founding_date is not None:
                merged.founding_date = other.founding_date
            if merged.business_type is None and other.business_type is not None:
                merged.business_type = other.business_type
            if merged.opening_hours is None and other.opening_hours is not None:
                merged.opening_hours = other.opening_hours
            if merged.price_range is None and other.price_range is not None:
                merged.price_range = other.price_range
            if merged.geo_lat is None and other.geo_lat is not None:
                merged.geo_lat = other.geo_lat
                merged.geo_lon = other.geo_lon
            if merged.image_url is None and other.image_url is not None:
                merged.image_url = other.image_url
            # Merge social links (don't overwrite, only add missing platforms)
            for platform, url in other.social_links.items():
                if platform not in merged.social_links:
                    merged.social_links[platform] = url

        return merged

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _get_str(obj: Dict[str, Any], key: str) -> Optional[str]:
        """Safely get a string value from a dict, returning None if missing/empty."""
        val = obj.get(key)
        if val is None:
            return None
        if isinstance(val, str):
            return val.strip() if val.strip() else None
        if isinstance(val, (int, float)):
            return str(val)
        if isinstance(val, dict):
            # Sometimes a value is an object with a 'name' or '@value' field
            return val.get('name') or val.get('@value')
        if isinstance(val, list) and val:
            first = val[0]
            if isinstance(first, str):
                return first.strip() if first.strip() else None
        return None

    @staticmethod
    def _get_types(obj: Dict[str, Any]) -> set:
        """Get the set of @type values from a JSON-LD object."""
        raw = obj.get('@type')
        if raw is None:
            return set()
        if isinstance(raw, str):
            return {raw}
        if isinstance(raw, list):
            return {t for t in raw if isinstance(t, str)}
        return set()

    @staticmethod
    def _person_name(person: Any) -> Optional[str]:
        """Extract a person's name from a Person object or string."""
        if isinstance(person, str):
            return person.strip() if person.strip() else None
        if isinstance(person, dict):
            name = person.get('name')
            if isinstance(name, str) and name.strip():
                return name.strip()
            # Try givenName + familyName
            given = person.get('givenName', '')
            family = person.get('familyName', '')
            if isinstance(given, str) and isinstance(family, str):
                full = f"{given} {family}".strip()
                if full:
                    return full
        return None

    @staticmethod
    def _clean_email(email: str) -> Optional[str]:
        """Clean and validate an email string."""
        if not email:
            return None
        email = email.strip().lower()
        # Strip mailto: prefix
        if email.startswith('mailto:'):
            email = email[7:]
        # Basic sanity check
        if '@' in email and '.' in email.split('@')[-1]:
            return email
        return None
