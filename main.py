#!/usr/bin/env python3
"""
Lead Cleaning & Enrichment Tool

A comprehensive CLI tool for cleaning, deduplicating, and enriching Google Maps leads.
"""

import os
import sys
import argparse
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, Optional, List
from dotenv import load_dotenv
from tqdm import tqdm
import pandas as pd

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description='Clean and enrich Google Maps leads from CSV files',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Basic usage with default free provider (website crawling only)
  python main.py --input ./leads --output ./output
  
  # Use Clearbit for enrichment with rate limiting
  python main.py --input ./leads --output ./output --provider clearbit --rate-limit 3
  
  # Use multiple providers (fallback chain)
  python main.py --input ./leads --output ./output --provider clearbit --provider2 free
  
  # With caching and concurrency
  python main.py --input ./leads --output ./output --cache ./cache.sqlite --concurrency 5
  
  # Use Hunter.io only for high-value leads (score > 60)
  python main.py --input ./leads --output ./output --provider hunter --min-score 60
  
  # Filter by category (e.g., for vanilla seller - only food businesses)
  python main.py --input ./leads --output ./output --provider free \\
      --filter-categories "bakery,restaurant,cafe,ice cream,food"
        """
    )
    
    # I/O arguments
    parser.add_argument(
        '--input', '-i',
        type=str,
        default='./leads',
        help='Input directory containing CSV files (default: ./leads)'
    )
    parser.add_argument(
        '--output', '-o',
        type=str,
        default='./output',
        help='Output directory for results (default: ./output)'
    )
    
    # Provider arguments
    parser.add_argument(
        '--provider', '-p',
        type=str,
        default='free',
        choices=['clearbit', 'apollo', 'hunter', 'free'],
        help='Primary enrichment provider (default: free)'
    )
    parser.add_argument(
        '--provider2',
        type=str,
        default=None,
        choices=['clearbit', 'apollo', 'hunter', 'free'],
        help='Secondary enrichment provider (fallback)'
    )
    
    # Performance arguments
    parser.add_argument(
        '--rate-limit',
        type=float,
        default=3,
        help='Rate limit (requests per second) for API calls (default: 3)'
    )
    parser.add_argument(
        '--concurrency', '-c',
        type=int,
        default=5,
        help='Number of concurrent workers for enrichment (default: 5)'
    )
    parser.add_argument(
        '--cache',
        type=str,
        default='./cache/cache.sqlite',
        help='SQLite cache file path (default: ./cache/cache.sqlite)'
    )
    
    # Filtering arguments
    parser.add_argument(
        '--min-score',
        type=int,
        default=None,
        help='Minimum lead quality score for paid API enrichment (default: None - only for paid providers)'
    )
    parser.add_argument(
        '--filter-categories',
        type=str,
        default=None,
        help='Comma-separated list of category keywords to filter (e.g., "bakery,restaurant,cafe")'
    )
    parser.add_argument(
        '--skip-cached',
        action='store_true',
        help='Skip domains already in cache (saves time and API credits on re-runs)'
    )
    parser.add_argument(
        '--skip-enrichment',
        action='store_true',
        help='Skip enrichment step (clean and dedupe only)'
    )
    
    # Control arguments
    parser.add_argument(
        '--no-dedupe',
        action='store_true',
        help='Skip deduplication step'
    )
    parser.add_argument(
        '--clear-cache',
        action='store_true',
        help='Clear cache before processing'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Process but do not write output files'
    )
    
    # Other arguments
    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='Enable verbose logging'
    )
    parser.add_argument(
        '--version',
        action='version',
        version='%(prog)s 1.0.0'
    )
    
    return parser.parse_args()


def check_api_keys(provider: str) -> bool:
    """Check if required API keys are configured."""
    if provider == 'free':
        return True
    
    key_mapping = {
        'clearbit': 'CLEARBIT_API_KEY',
        'apollo': 'APOLLO_API_KEY',
        'hunter': 'HUNTER_API_KEY',
    }
    
    env_var = key_mapping.get(provider)
    if not env_var:
        return True
    
    api_key = os.getenv(env_var)
    if not api_key:
        logger.error(f"Missing API key for {provider}: {env_var} not set in environment")
        logger.error(f"Set it with: export {env_var}=your_key_here")
        return False
    
    return True


def process_leads(args: argparse.Namespace) -> Dict[str, Any]:
    """Main processing pipeline."""
    from app import (
        read_all_csvs, clean_dataframe, merge_dataframes,
        deduplicate_dataframe, score_record, get_provider,
        enrich_with_fallback, EnrichmentCache, RateLimiter,
        generate_outputs, get_cache
    )
    
    # Stats tracking
    stats = {
        'files_processed': 0,
        'total_records': 0,
        'duplicates_found': 0,
        'enriched_count': 0,
        'errors': [],
        'start_time': datetime.now(),
    }
    
    try:
        # Step 1: Read all CSV files
        logger.info("Step 1: Reading CSV files...")
        csv_data = read_all_csvs(Path(args.input))
        stats['files_processed'] = len(csv_data)
        
        if not csv_data:
            logger.error("No CSV files found or readable")
            return stats
        
        # Step 2: Clean and normalize data
        logger.info("Step 2: Cleaning and normalizing data...")
        cleaned_dfs = []
        for filename, df in tqdm(csv_data, desc="Cleaning files"):
            try:
                cleaned_df = clean_dataframe(df, filename)
                cleaned_dfs.append(cleaned_df)
            except Exception as e:
                logger.error(f"Error cleaning {filename}: {e}")
                stats['errors'].append(f"Clean error: {filename}: {e}")
        
        if not cleaned_dfs:
            logger.error("No data could be cleaned")
            return stats
        
        # Merge all dataframes
        merged_df = merge_dataframes(cleaned_dfs)
        stats['total_records'] = len(merged_df)
        
        # Step 3: Deduplicate
        if not args.no_dedupe:
            logger.info("Step 3: Deduplicating records...")
            merged_df = deduplicate_dataframe(merged_df)
            stats['duplicates_found'] = merged_df['is_duplicate'].sum()
            logger.info(f"Found {stats['duplicates_found']} duplicates")
        else:
            merged_df['duplicate_group_id'] = None
            merged_df['is_duplicate'] = False
        
        # Step 4: Filter by category (if specified)
        if args.filter_categories:
            logger.info(f"Step 4: Filtering by categories: {args.filter_categories}")
            category_keywords = [kw.strip().lower() for kw in args.filter_categories.split(',')]
            
            # Filter rows where category matches any keyword
            # Check both original category AND canonical_category (for multi-language support)
            def matches_category(row):
                # Check original category
                category = row.get('category', '')
                if pd.notna(category) and category:
                    category_lower = str(category).lower()
                    if any(keyword in category_lower for keyword in category_keywords):
                        return True
                
                # Check canonical category (handles multi-language synonyms)
                canonical = row.get('canonical_category', '')
                if pd.notna(canonical) and canonical:
                    canonical_lower = str(canonical).lower()
                    if any(keyword in canonical_lower for keyword in category_keywords):
                        return True
                
                return False
            
            # Apply filter
            category_mask = merged_df.apply(matches_category, axis=1)
            filtered_count = category_mask.sum()
            removed_count = len(merged_df) - filtered_count
            
            merged_df = merged_df[category_mask].reset_index(drop=True)
            stats['total_records'] = len(merged_df)
            stats['filtered_out'] = removed_count
            
            logger.info(f"Filtered to {filtered_count} leads matching categories")
            logger.info(f"Removed {removed_count} leads not matching categories")
        
        # Step 5: Enrichment (optional)
        if not args.skip_enrichment:
            logger.info("Step 4: Enriching leads...")
            
            # Setup cache
            cache = EnrichmentCache(args.cache)
            if args.clear_cache:
                cleared = cache.clear_all()
                logger.info(f"Cleared {cleared} cache entries")
            
            # Setup providers
            providers = []
            for provider_name in [args.provider, args.provider2]:
                if provider_name and provider_name != 'free':
                    if check_api_keys(provider_name):
                        providers.append(get_provider(provider_name, rate_limit=args.rate_limit))
            
            rate_limiter = RateLimiter(args.rate_limit)
            
            # Enrich records with websites
            enrichable_df = merged_df[
                merged_df['website_domain'].notna() & 
                (merged_df['website_domain'] != '')
            ].copy()
            
            logger.info(f"Enriching {len(enrichable_df)} records with domains")
            
            # Skip cached domains if requested (saves time and API credits)
            if args.skip_cached:
                logger.info("Checking cache to skip already-enriched domains...")
                uncached_domains = []
                skipped_count = 0
                
                for idx in enrichable_df.index:
                    domain = enrichable_df.loc[idx, 'website_domain']
                    if domain and not pd.isna(domain):
                        cached = cache.get(str(domain), args.provider)
                        if cached:
                            skipped_count += 1
                            # Copy cached data to the record
                            merged_df.at[idx, 'enriched_company_name'] = cached.get('company_name')
                            merged_df.at[idx, 'enriched_linkedin_url'] = cached.get('linkedin_url')
                            merged_df.at[idx, 'enriched_emails_found'] = cached.get('emails_found', 0)
                            merged_df.at[idx, 'enriched_at'] = cached.get('enriched_at')
                            merged_df.at[idx, 'enrichment_provider'] = f"cached:{args.provider}"
                        else:
                            uncached_domains.append(idx)
                
                # Update enrichable_df to only include uncached domains
                enrichable_df = enrichable_df.loc[uncached_domains].copy()
                stats['skipped_cached'] = skipped_count
                logger.info(f"Skipped {skipped_count} cached domains")
                logger.info(f"Enriching {len(enrichable_df)} NEW domains")
            
            # Add enrichment columns
            enrichment_columns = [
                'enriched_company_name', 'enriched_description', 'enriched_industry',
                'enriched_employee_count', 'enriched_linkedin_url', 'enriched_facebook_url',
                'enriched_instagram_url', 'enriched_tiktok_url', 'enriched_twitter_url', 'enriched_youtube_url',
                'enriched_email_patterns', 'enriched_contact_name', 'enriched_contact_title',
                'enriched_contact_email', 'enriched_contact_linkedin', 'enriched_contact_confidence',
                'decision_maker_name', 'decision_maker_title', 'decision_maker_confidence',
                'estimated_employees', 'business_age_years', 'website_language',
                'enriched_generic_emails', 'enriched_at', 'enrichment_error', 'enriched_emails_found',
                'enrichment_provider'
            ]
            
            for col in enrichment_columns:
                if col not in merged_df.columns:
                    merged_df[col] = None
            
            # Process each record
            for idx in tqdm(enrichable_df.index, desc="Enriching"):
                try:
                    domain = merged_df.loc[idx, 'website_domain']
                    current_score = 0
                    
                    # Check if we should use paid API based on score threshold
                    if args.min_score is not None:
                        # Calculate current score (without enrichment yet)
                        temp_record = merged_df.loc[idx].to_dict()
                        from app.scoring import calculate_lead_score
                        current_score = calculate_lead_score(temp_record)
                    
                    # Try enrichment
                    result = None
                    
                    # Check cache first
                    cached = cache.get(domain, args.provider)
                    
                    if cached:
                        # Check if it's a cached failure
                        if cached.get('cached_status') in ('failed', 'denylisted'):
                            # Skip this domain - it's a known failure
                            merged_df.at[idx, 'enrichment_error'] = f"cached_{cached.get('cached_status')}: {cached.get('cached_error_reason', 'unknown')}"
                            merged_df.at[idx, 'enrichment_provider'] = f"cached:{args.provider}"
                            continue
                        
                        # Use cached successful result
                        merged_df.at[idx, 'enriched_company_name'] = cached.get('company_name')
                        merged_df.at[idx, 'enriched_linkedin_url'] = cached.get('linkedin_url')
                        merged_df.at[idx, 'enriched_emails_found'] = cached.get('emails_found', 0)
                        merged_df.at[idx, 'enriched_at'] = cached.get('enriched_at')
                        merged_df.at[idx, 'enrichment_provider'] = f"cached:{args.provider}"
                        stats['enriched_count'] += 1
                        continue
                    
                    # Check if domain is denylisted before trying
                    from app.cache import is_domain_denylisted
                    if is_domain_denylisted(domain):
                        logger.warning(f"Domain {domain} is denylisted, skipping")
                        cache.set_denylisted(domain, args.provider)
                        merged_df.at[idx, 'enrichment_error'] = "Domain denylisted"
                        merged_df.at[idx, 'enrichment_provider'] = "denylisted"
                        continue
                    
                    # Use primary provider or fallback
                    primary_provider = None
                    if providers and (args.min_score is None or current_score >= args.min_score):
                        primary_provider = providers[0]
                    
                    rate_limiter.wait()
                    result = enrich_with_fallback(domain, primary_provider, use_crawl_fallback=True, cache=cache, provider_name=args.provider)
                    
                    if result and result.success:
                        # Store enrichment data
                        merged_df.at[idx, 'enriched_company_name'] = result.company_name
                        merged_df.at[idx, 'enriched_description'] = result.description
                        merged_df.at[idx, 'enriched_industry'] = result.industry
                        merged_df.at[idx, 'enriched_employee_count'] = result.employee_count
                        merged_df.at[idx, 'enriched_linkedin_url'] = result.linkedin_url
                        merged_df.at[idx, 'enriched_facebook_url'] = result.facebook_url
                        merged_df.at[idx, 'enriched_instagram_url'] = result.instagram_url
                        merged_df.at[idx, 'enriched_tiktok_url'] = result.tiktok_url
                        merged_df.at[idx, 'enriched_twitter_url'] = result.twitter_url
                        merged_df.at[idx, 'enriched_youtube_url'] = result.youtube_url
                        
                        if result.email_patterns:
                            merged_df.at[idx, 'enriched_email_patterns'] = ', '.join(result.email_patterns)
                        
                        # Store first contact if available
                        if result.contacts and len(result.contacts) > 0:
                            contact = result.contacts[0]
                            merged_df.at[idx, 'enriched_contact_name'] = contact.name
                            merged_df.at[idx, 'enriched_contact_title'] = contact.title
                            merged_df.at[idx, 'enriched_contact_email'] = contact.email
                            merged_df.at[idx, 'enriched_contact_linkedin'] = contact.linkedin
                            merged_df.at[idx, 'enriched_emails_found'] = len([c for c in result.contacts if c.email])
                        
                        merged_df.at[idx, 'enriched_at'] = datetime.utcnow().isoformat()
                        merged_df.at[idx, 'enrichment_provider'] = args.provider if primary_provider else 'crawl_fallback'
                        
                        # Cache result
                        cache_data = {
                            'company_name': result.company_name,
                            'linkedin_url': result.linkedin_url,
                            'emails_found': merged_df.at[idx, 'enriched_emails_found'] or 0,
                            'enriched_at': datetime.utcnow().isoformat()
                        }
                        cache.set(domain, args.provider, cache_data)
                        
                        stats['enriched_count'] += 1
                    else:
                        merged_df.at[idx, 'enrichment_error'] = result.error_message if result else 'Unknown error'
                        
                except Exception as e:
                    error_domain = merged_df.loc[idx, 'website_domain'] if idx in merged_df.index else 'unknown'
                    logger.error(f"Error enriching {error_domain}: {e}")
                    stats['errors'].append(f"Enrichment error: {error_domain}: {e}")
                    merged_df.at[idx, 'enrichment_error'] = str(e)
        
        # Step 5: Extract decision maker + company intelligence
        logger.info("Step 5b: Extracting decision maker and company intelligence...")
        from app.enrich.decision_maker import DecisionMakerFinder
        from app.enrich.company_intel import CompanyIntelligence
        
        cache_path = args.cache if hasattr(args, 'cache') and args.cache else './cache/cache.sqlite'
        dm_finder = DecisionMakerFinder(use_cache=True, cache_path=cache_path)
        company_intel_extractor = CompanyIntelligence(use_cache=True, cache_path=cache_path)
        
        # Get all records with websites
        records_with_websites = merged_df[
            merged_df['website_domain'].notna() & 
            (merged_df['website_domain'] != '')
        ]
        
        for idx in tqdm(records_with_websites.index, desc="Decision maker + Company intel", leave=False):
            try:
                domain = merged_df.at[idx, 'website_domain']
                biz_name = merged_df.at[idx, 'business_name']
                
                if not domain or pd.isna(domain):
                    continue
                
                # Get emails found (if any)
                emails = []
                if pd.notna(merged_df.at[idx, 'enriched_contact_email']):
                    emails.append(merged_df.at[idx, 'enriched_contact_email'])
                
                # Extract decision maker
                dm = dm_finder.find_decision_maker(domain, biz_name, emails)
                if dm:
                    merged_df.at[idx, 'decision_maker_name'] = dm.name
                    merged_df.at[idx, 'decision_maker_title'] = dm.title
                    merged_df.at[idx, 'decision_maker_confidence'] = dm.confidence
                
                # Extract company intelligence
                comp_intel = company_intel_extractor.extract_company_info(domain)
                if comp_intel:
                    merged_df.at[idx, 'estimated_employees'] = comp_intel.get('estimated_employees')
                    merged_df.at[idx, 'business_age_years'] = comp_intel.get('business_age_years')
                    merged_df.at[idx, 'website_language'] = comp_intel.get('website_language')
            
            except Exception as e:
                logger.debug(f"Error extracting DM/company intel: {e}")
        
        # Step 6: Score leads
        logger.info("Step 6: Scoring leads...")
        records = merged_df.to_dict('records')
        scored_records = []
        for record in tqdm(records, desc="Scoring"):
            scored_records.append(score_record(record))
        
        final_df = pd.DataFrame(scored_records)
        
        # Step 7: Generate outputs
        if not args.dry_run:
            logger.info("Step 7: Generating outputs...")
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            outputs = generate_outputs(
                final_df, 
                Path(args.output), 
                timestamp,
                stats
            )
            
            logger.info(f"Output files generated:")
            for output_type, path in outputs.items():
                logger.info(f"  - {output_type}: {path}")
        else:
            logger.info("Dry run - no output files written")
        
        # Final statistics
        stats['end_time'] = datetime.now()
        duration = (stats['end_time'] - stats['start_time']).total_seconds()
        
        logger.info("="*50)
        logger.info("Processing Complete!")
        logger.info(f"Files processed: {stats['files_processed']}")
        logger.info(f"Total records: {stats['total_records']}")
        logger.info(f"Duplicates found: {stats['duplicates_found']}")
        logger.info(f"Enriched: {stats['enriched_count']}")
        logger.info(f"Duration: {duration:.1f}s")
        logger.info(f"Average score: {final_df['lead_quality_score'].mean():.1f}")
        logger.info("="*50)
        
        return stats
        
    except Exception as e:
        logger.error(f"Fatal error during processing: {e}")
        import traceback
        logger.error(traceback.format_exc())
        stats['errors'].append(f"Fatal error: {e}")
        return stats


def main():
    """Main entry point."""
    args = parse_args()
    
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    # Validate inputs
    input_path = Path(args.input)
    if not input_path.exists():
        logger.error(f"Input directory does not exist: {args.input}")
        sys.exit(1)
    
    # Check API keys before starting
    if not args.skip_enrichment:
        if args.provider != 'free' and not check_api_keys(args.provider):
            logger.error("Please set the required API key or use --provider free")
            sys.exit(1)
        
        if args.provider2 and args.provider2 != 'free' and not check_api_keys(args.provider2):
            logger.error("Please set the required API key for provider2")
            sys.exit(1)
    
    # Run processing
    stats = process_leads(args)
    
    # Exit with error code if there were fatal errors
    if any('Fatal error' in str(e) for e in stats.get('errors', [])):
        sys.exit(1)
    
    sys.exit(0)


if __name__ == '__main__':
    main()
