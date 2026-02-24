"""Generate Google Dork queries for manual LinkedIn and company research."""

import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class DorkQueryGenerator:
    """Generate Google dork queries for enrichment research."""
    
    @staticmethod
    def generate_queries(
        business_name: str,
        category: Optional[str] = None,
        city: Optional[str] = None
    ) -> Dict[str, str]:
        """
        Generate Google dork queries for a business.
        Includes multilingual support and industry-specific queries.
        """
        if not business_name:
            return {}
        
        clean_name = business_name.strip().strip('"')
        
        queries = {}
        
        # LinkedIn Company Search
        queries['linkedin_company'] = f'site:linkedin.com/company "{clean_name}"'
        
        # LinkedIn People Search - Owner/Founder (multilingual)
        queries['linkedin_owner'] = f'site:linkedin.com/in "{clean_name}" owner OR founder OR inhaber OR gründer OR propriétaire OR fondateur OR proprietario OR eigenaar OR właściciel'
        
        # LinkedIn People Search - Manager/Director (multilingual)
        queries['linkedin_manager'] = f'site:linkedin.com/in "{clean_name}" manager OR ceo OR director OR geschäftsführer OR directeur OR direttore OR gerente OR dyrektor'
        
        # XING Search (popular in DACH region)
        queries['xing_company'] = f'site:xing.com/companies "{clean_name}"'
        queries['xing_people'] = f'site:xing.com/profile "{clean_name}"'
        
        # CEO/Founder Name Search (multilingual)
        queries['owner_search'] = f'"{clean_name}" owner OR founder OR inhaber OR gründer OR gérant OR titolare OR propietario OR eigenaar'
        
        # Company News (multilingual)
        queries['news'] = f'"{clean_name}" news OR interview OR Pressemitteilung OR actualités OR notizie OR Nachrichten'
        
        # Company Website
        queries['website'] = f'"{clean_name}" website OR homepage OR Webseite'
        
        # Impressum search (German legal page = goldmine for contact info)
        queries['impressum'] = f'"{clean_name}" impressum OR "mentions légales" OR "aviso legal" OR "note legali"'
        
        # Ingredient/product relevance queries
        queries['ingredient_relevance'] = f'"{clean_name}" vanilla OR vanille OR cacao OR kakao OR chocolate OR schokolade OR spice OR gewürz'
        
        # Supplier/wholesale queries
        queries['supplier'] = f'"{clean_name}" supplier OR lieferant OR fournisseur OR fornitore OR proveedor OR leverancier OR dostawca'
        
        # If city provided, add location-specific search
        if city:
            queries['company_location'] = f'"{clean_name}" "{city}"'
        
        # If category provided, add category-specific search
        if category:
            queries['company_industry'] = f'"{clean_name}" {category}'
        
        return queries
    
    @staticmethod
    def generate_bulk_dork_csv(
        records: List[Dict],
        output_file: Optional[str] = None
    ) -> List[Dict]:
        """
        Generate dork queries for multiple records.
        
        Args:
            records: List of business records with 'business_name', 'category', 'city'
            output_file: Optional path to save as CSV
        
        Returns:
            List of dicts with business info + dork queries
        """
        results = []
        
        for record in records:
            biz_name = record.get('business_name', '')
            category = record.get('category', '')
            city = record.get('city', '')
            
            if not biz_name:
                continue
            
            queries = DorkQueryGenerator.generate_queries(biz_name, category, city)
            
            # Add to results with original record data
            result_record = {
                'business_name': biz_name,
                'category': category,
                'city': city,
                'website_domain': record.get('website_domain', ''),
                'phone': record.get('phone_e164', ''),
            }
            
            # Add all queries as separate columns
            result_record.update(queries)
            results.append(result_record)
        
        return results
