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
import asyncio  # noqa: F401 — used by future async enrichment
import aiohttp  # noqa: F401 — used by future async enrichment

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Clean and enrich Google Maps leads from CSV files",
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

  # Quick route (faster, simplified final CSV only)
  python main.py --input ./leads --output ./output --quick

  # Generate only slim final CSV from latest full output
  python main.py --slim-only --output ./output

  # Generate only slim final CSV from a specific file
  python main.py --slim-only --from-file ./output/cleaned_enriched_20260208_120000.csv
        """,
    )

    # I/O arguments
    parser.add_argument(
        "--input",
        "-i",
        type=str,
        default="./leads",
        help="Input directory containing CSV files (default: ./leads)",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=str,
        default="./output",
        help="Output directory for results (default: ./output)",
    )

    # Provider arguments
    parser.add_argument(
        "--from-file",
        type=str,
        default=None,
        help="Existing cleaned_enriched CSV to convert into final slim CSV",
    )
    parser.add_argument(
        "--slim-output",
        type=str,
        default=None,
        help="Optional output path for slim CSV (default: auto-generated next to source CSV)",
    )
    parser.add_argument(
        "--slim-only",
        action="store_true",
        help="Skip full workflow and only generate slim CSV from existing cleaned_enriched CSV",
    )

    parser.add_argument(
        "--provider",
        "-p",
        type=str,
        default="free",
        choices=["clearbit", "apollo", "hunter", "free"],
        help="Primary enrichment provider (default: free)",
    )
    parser.add_argument(
        "--provider2",
        type=str,
        default=None,
        choices=["clearbit", "apollo", "hunter", "free"],
        help="Secondary enrichment provider (fallback)",
    )

    # Performance arguments
    parser.add_argument(
        "--rate-limit",
        type=float,
        default=3,
        help="Rate limit (requests per second) for API calls (default: 3)",
    )
    parser.add_argument(
        "--concurrency",
        "-c",
        type=int,
        default=5,
        help="Number of concurrent workers for enrichment (default: 5)",
    )
    parser.add_argument(
        "--cache",
        type=str,
        default="./cache/cache.sqlite",
        help="SQLite cache file path (default: ./cache/cache.sqlite)",
    )

    # Filtering arguments
    parser.add_argument(
        "--min-score",
        type=int,
        default=None,
        help="Minimum lead quality score for paid API enrichment (default: None - only for paid providers)",
    )
    parser.add_argument(
        "--filter-categories",
        type=str,
        default=None,
        help='Comma-separated list of category keywords to filter (e.g., "bakery,restaurant,cafe")',
    )
    parser.add_argument(
        "--skip-cached",
        action="store_true",
        help="Skip domains already in cache (saves time and API credits on re-runs)",
    )
    parser.add_argument(
        "--skip-enrichment",
        action="store_true",
        help="Skip enrichment step (clean and dedupe only)",
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help=(
            "Quick route: lighter enrichment and simplified final CSV only "
            "for faster turnaround"
        ),
    )
    parser.add_argument(
        "--crawl-language",
        type=str,
        default="auto",
        choices=["auto", "en", "de", "fr", "it", "es", "pt", "nl", "pl"],
        help=(
            "Prioritize crawl routes for a language/country "
            "(e.g. pt for Portugal, it for Italy)"
        ),
    )

    # Control arguments
    parser.add_argument(
        "--no-dedupe", action="store_true", help="Skip deduplication step"
    )
    parser.add_argument(
        "--clear-cache", action="store_true", help="Clear cache before processing"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Process but do not write output files"
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from last checkpoint (if available)",
    )
    parser.add_argument(
        "--verify-emails",
        action="store_true",
        help="Verify emails using MX lookup and SMTP check (slower but more accurate)",
    )
    parser.add_argument(
        "--checkpoint-interval",
        type=int,
        default=50,
        help="Save checkpoint every N records during enrichment (default: 50)",
    )

    # Other arguments
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable verbose logging"
    )
    parser.add_argument("--version", action="version", version="%(prog)s 1.0.0")

    return parser.parse_args()


def check_api_keys(provider: str) -> bool:
    """Check if required API keys are configured."""
    if provider == "free":
        return True

    key_mapping = {
        "clearbit": "CLEARBIT_API_KEY",
        "apollo": "APOLLO_API_KEY",
        "hunter": "HUNTER_API_KEY",
    }

    env_var = key_mapping.get(provider)
    if not env_var:
        return True

    api_key = os.getenv(env_var)
    if not api_key:
        logger.error(
            f"Missing API key for {provider}: {env_var} not set in environment"
        )
        logger.error(f"Set it with: export {env_var}=your_key_here")
        return False

    return True


def process_leads(args: argparse.Namespace) -> Dict[str, Any]:
    """Main processing pipeline — delegates to app.pipeline step functions."""
    from app.pipeline import (
        step_read_csvs,
        step_clean_and_merge,
        step_deduplicate,
        step_filter_categories,
        step_enrich_core,
        step_enrich_intelligence,
        step_detect_signals,
        step_verify_emails,
        step_score,
        step_generate_outputs,
        step_generate_quick_output,
    )
    from app.checkpoint import PipelineCheckpoint

    stats: Dict[str, Any] = {
        "files_processed": 0,
        "total_records": 0,
        "duplicates_found": 0,
        "enriched_count": 0,
        "errors": [],
        "start_time": datetime.now(),
    }

    checkpoint = PipelineCheckpoint()

    # Resume from checkpoint if requested
    if hasattr(args, "resume") and args.resume:
        loaded = checkpoint.load()
        if loaded:
            merged_df, step, saved_stats = loaded
            logger.info(
                f"Resumed from checkpoint at step '{step}' with {len(merged_df)} records"
            )
            stats.update(saved_stats)

    try:
        quick_mode = bool(getattr(args, "quick", False))

        # Step 1: Read CSV files
        logger.info("Step 1: Reading CSV files...")
        csv_data, read_stats = step_read_csvs(Path(args.input))
        stats.update(read_stats)
        if not csv_data:
            logger.error("No CSV files found or readable")
            return stats

        # Step 2: Clean & merge
        logger.info("Step 2: Cleaning and normalizing data...")
        merged_df, clean_stats = step_clean_and_merge(csv_data)
        stats["errors"].extend(clean_stats.get("errors", []))
        stats["total_records"] = clean_stats["total_records"]
        if merged_df.empty:
            logger.error("No data could be cleaned")
            return stats

        # Step 3: Deduplicate
        logger.info("Step 3: Deduplicating records...")
        merged_df, dedup_stats = step_deduplicate(merged_df, skip=args.no_dedupe)
        stats["duplicates_found"] = dedup_stats["duplicates_found"]

        # Step 4: Category filter
        merged_df, filter_stats = step_filter_categories(
            merged_df, args.filter_categories
        )
        stats.update(filter_stats)

        # Step 5: Enrichment
        if not args.skip_enrichment:
            cache_path = (
                args.cache
                if hasattr(args, "cache") and args.cache
                else "./cache/cache.sqlite"
            )

            # 5a: Core enrichment (provider + crawl)
            concurrency = getattr(args, "concurrency", 1) or 1
            logger.info(f"Step 5a: Enriching leads (concurrency={concurrency})...")
            merged_df, enrich_stats = step_enrich_core(
                merged_df,
                provider_name=args.provider,
                provider2_name=args.provider2,
                rate_limit=args.rate_limit,
                cache_path=cache_path,
                clear_cache=args.clear_cache,
                skip_cached=args.skip_cached,
                min_score=args.min_score,
                concurrency=concurrency,
                quick_mode=quick_mode,
                crawl_language=args.crawl_language,
            )
            stats["enriched_count"] = enrich_stats["enriched_count"]
            stats["errors"].extend(enrich_stats.get("errors", []))
            if "skipped_cached" in enrich_stats:
                stats["skipped_cached"] = enrich_stats["skipped_cached"]

            if not quick_mode:
                # 5b: Decision-maker + company intelligence + legal pages
                logger.info(
                    f"Step 5b: Extracting decision maker and company intelligence (concurrency={concurrency})..."
                )
                merged_df = step_enrich_intelligence(
                    merged_df, cache_path=cache_path, concurrency=concurrency
                )

                # 5c: Ingredient / certification signals
                logger.info(
                    "Step 5c: Detecting ingredient signals and certifications..."
                )
                merged_df = step_detect_signals(merged_df)

        # Step 5d: Email verification (optional)
        if (
            not args.skip_enrichment
            and not quick_mode
            and hasattr(args, "verify_emails")
            and args.verify_emails
        ):
            logger.info("Step 5d: Verifying emails...")
            merged_df = step_verify_emails(merged_df)

        final_df = merged_df
        if not quick_mode:
            # Step 6: Score leads
            logger.info("Step 6: Scoring leads...")
            final_df = step_score(merged_df)

        # Step 7: Generate outputs
        if not args.dry_run:
            if quick_mode:
                logger.info("Step 7: Generating quick output...")
                step_generate_quick_output(final_df, Path(args.output), stats)
            else:
                logger.info("Step 7: Generating outputs...")
                step_generate_outputs(final_df, Path(args.output), stats)
        else:
            logger.info("Dry run - no output files written")

        # Final statistics
        stats["end_time"] = datetime.now()
        duration = (stats["end_time"] - stats["start_time"]).total_seconds()

        logger.info("=" * 50)
        logger.info("Processing Complete!")
        logger.info(f"Files processed: {stats['files_processed']}")
        logger.info(f"Total records: {stats['total_records']}")
        logger.info(f"Duplicates found: {stats['duplicates_found']}")
        logger.info(f"Enriched: {stats['enriched_count']}")
        logger.info(f"Duration: {duration:.1f}s")
        if "lead_quality_score" in final_df.columns and not final_df.empty:
            logger.info(f"Average score: {final_df['lead_quality_score'].mean():.1f}")
        if quick_mode:
            logger.info(
                "Quick mode enabled: skipped deep intelligence and heavy outputs"
            )
        logger.info("=" * 50)

        return stats

    except Exception as e:
        logger.error(f"Fatal error during processing: {e}")
        import traceback

        logger.error(traceback.format_exc())
        stats["errors"].append(f"Fatal error: {e}")
        return stats


def _find_latest_cleaned_enriched_csv(output_dir: Path) -> Optional[Path]:
    """Return latest cleaned_enriched CSV in output directory."""
    csv_files = sorted(
        output_dir.glob("cleaned_enriched_*.csv"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return csv_files[0] if csv_files else None


def _default_slim_output_path(source_csv: Path) -> Path:
    """Derive default final slim CSV path from source CSV path."""
    if source_csv.name.startswith("cleaned_enriched_"):
        output_name = source_csv.name.replace("cleaned_enriched_", "final_leads_", 1)
    else:
        output_name = f"final_leads_{source_csv.stem}.csv"
    return source_csv.with_name(output_name)


def generate_slim_only(args: argparse.Namespace) -> int:
    """Generate slim final CSV without running the full enrichment pipeline."""
    from app.io import write_csv_output
    from app.report import _build_slim_final_export

    output_dir = Path(args.output)

    source_csv = Path(args.from_file) if args.from_file else None
    if source_csv is None:
        source_csv = _find_latest_cleaned_enriched_csv(output_dir)
        if source_csv is None:
            logger.error(
                f"No cleaned_enriched_*.csv found in {output_dir}. "
                "Run full workflow first or provide --from-file."
            )
            return 1

    if not source_csv.exists():
        logger.error(f"Source file not found: {source_csv}")
        return 1

    try:
        logger.info(f"Reading source CSV for slim export: {source_csv}")
        df = pd.read_csv(source_csv)
        slim_df = _build_slim_final_export(df)

        slim_output = (
            Path(args.slim_output)
            if args.slim_output
            else _default_slim_output_path(source_csv)
        )
        write_csv_output(slim_df, slim_output)
        logger.info(f"Slim CSV written: {slim_output}")
        logger.info(f"Rows exported: {len(slim_df)}")
        return 0
    except Exception as e:
        logger.error(f"Failed to generate slim CSV: {e}")
        return 1


def main():
    """Main entry point."""
    args = parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.slim_only:
        sys.exit(generate_slim_only(args))

    # Validate inputs
    input_path = Path(args.input)
    if not input_path.exists():
        logger.error(f"Input directory does not exist: {args.input}")
        sys.exit(1)

    # Check API keys before starting
    if not args.skip_enrichment:
        if args.provider != "free" and not check_api_keys(args.provider):
            logger.error("Please set the required API key or use --provider free")
            sys.exit(1)

        if (
            args.provider2
            and args.provider2 != "free"
            and not check_api_keys(args.provider2)
        ):
            logger.error("Please set the required API key for provider2")
            sys.exit(1)

    # Run processing
    stats = process_leads(args)

    # Exit with error code if there were fatal errors
    if any("Fatal error" in str(e) for e in stats.get("errors", [])):
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
