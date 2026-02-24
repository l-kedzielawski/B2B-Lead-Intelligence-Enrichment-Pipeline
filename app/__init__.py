"""Lead cleaning and enrichment application - Vanilla/Cacao/Spice B2B Pipeline."""

from .io import read_all_csvs, write_csv_output, write_excel_output
from .clean import clean_dataframe, merge_dataframes
from .dedupe import deduplicate_dataframe
from .scoring import score_record, calculate_lead_score, calculate_target_prospect_score
from .enrich.providers import get_provider, EnrichmentResult, Contact
from .crawl_fallback import enrich_with_fallback
from .cache import EnrichmentCache, RateLimiter, get_cache
from .report import generate_outputs, generate_markdown_report
from .utils import clean_text, normalize_url, extract_domain, generate_likely_emails
from .checkpoint import PipelineCheckpoint

__version__ = "2.0.0"

__all__ = [
    'read_all_csvs',
    'write_csv_output',
    'write_excel_output',
    'clean_dataframe',
    'merge_dataframes',
    'deduplicate_dataframe',
    'score_record',
    'calculate_lead_score',
    'calculate_target_prospect_score',
    'get_provider',
    'EnrichmentResult',
    'Contact',
    'enrich_with_fallback',
    'EnrichmentCache',
    'RateLimiter',
    'get_cache',
    'generate_outputs',
    'generate_markdown_report',
    'clean_text',
    'normalize_url',
    'extract_domain',
    'generate_likely_emails',
    'PipelineCheckpoint',
]
