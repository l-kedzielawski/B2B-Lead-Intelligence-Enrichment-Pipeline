"""
Pipeline step functions extracted from the monolithic process_leads().

Each function performs one logical step of the pipeline and returns the
updated DataFrame plus any relevant stats.  This makes the pipeline
easier to test, resume, and eventually convert to async.
"""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

import pandas as pd
from tqdm import tqdm

logger = logging.getLogger(__name__)


# ── Step 1: Read ──────────────────────────────────────────────────────────────


def step_read_csvs(
    input_dir: Path,
) -> Tuple[List[Tuple[str, pd.DataFrame]], Dict[str, Any]]:
    """Read all CSV files from *input_dir*.

    Returns:
        (csv_data, stats_fragment)
    """
    from app.io import read_all_csvs

    csv_data = read_all_csvs(input_dir)
    stats: Dict[str, Any] = {"files_processed": len(csv_data)}
    return csv_data, stats


# ── Step 2: Clean & merge ─────────────────────────────────────────────────────


def step_clean_and_merge(
    csv_data: List[Tuple[str, pd.DataFrame]],
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """Clean every CSV and merge into a single DataFrame.

    Returns:
        (merged_df, stats_fragment)
    """
    from app.clean import clean_dataframe, merge_dataframes

    stats: Dict[str, Any] = {"errors": []}
    cleaned_dfs: List[pd.DataFrame] = []

    for filename, df in tqdm(csv_data, desc="Cleaning files"):
        try:
            cleaned_dfs.append(clean_dataframe(df, filename))
        except Exception as e:
            logger.error(f"Error cleaning {filename}: {e}")
            stats["errors"].append(f"Clean error: {filename}: {e}")

    merged_df = merge_dataframes(cleaned_dfs) if cleaned_dfs else pd.DataFrame()
    stats["total_records"] = len(merged_df)
    return merged_df, stats


# ── Step 3: Deduplicate ──────────────────────────────────────────────────────


def step_deduplicate(
    df: pd.DataFrame, *, skip: bool = False
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """Deduplicate *df* — collapses duplicate groups to best record only.

    After this step the DataFrame is smaller: only unique records remain.
    Each surviving row has a ``duplicate_count`` showing how many copies
    existed in the source data.

    Returns:
        (deduped_df, stats_fragment)
    """
    stats: Dict[str, Any] = {"duplicates_found": 0}

    if skip:
        df["duplicate_group_id"] = None
        df["duplicate_count"] = 1
        return df, stats

    records_before = len(df)

    from app.dedupe import deduplicate_dataframe

    df = deduplicate_dataframe(df)

    stats["duplicates_found"] = records_before - len(df)
    stats["records_after_dedup"] = len(df)
    logger.info(
        f"Deduplication: {records_before} → {len(df)} records "
        f"({stats['duplicates_found']} duplicates collapsed)"
    )
    return df, stats


# ── Step 4: Category filter ──────────────────────────────────────────────────


def step_filter_categories(
    df: pd.DataFrame, filter_categories: Optional[str]
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """Filter *df* by category keywords (comma-separated string).

    Returns:
        (filtered_df, stats_fragment)
    """
    stats: Dict[str, Any] = {}

    if not filter_categories:
        return df, stats

    keywords = [kw.strip().lower() for kw in filter_categories.split(",")]

    def matches(row: pd.Series) -> bool:
        for col in ("category", "canonical_category"):
            val = row.get(col, "")
            if pd.notna(val) and val:
                if any(kw in str(val).lower() for kw in keywords):
                    return True
        return False

    mask = df.apply(matches, axis=1)
    removed = len(df) - int(mask.sum())
    df = df[mask].reset_index(drop=True)

    stats["total_records"] = len(df)
    stats["filtered_out"] = removed
    logger.info(f"Filtered to {len(df)} leads matching categories (removed {removed})")
    return df, stats


# ── Step 5a: Core enrichment (provider + crawl) ─────────────────────────────


def _ensure_enrichment_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Add all enrichment columns if not already present."""
    enrichment_columns = [
        "enriched_company_name",
        "enriched_description",
        "enriched_industry",
        "enriched_employee_count",
        "enriched_linkedin_url",
        "enriched_facebook_url",
        "enriched_instagram_url",
        "enriched_tiktok_url",
        "enriched_twitter_url",
        "enriched_youtube_url",
        "enriched_email_patterns",
        "enriched_contact_name",
        "enriched_contact_title",
        "enriched_contact_email",
        "enriched_contact_linkedin",
        "enriched_contact_confidence",
        "decision_maker_name",
        "decision_maker_title",
        "decision_maker_confidence",
        "estimated_employees",
        "business_age_years",
        "website_language",
        "enriched_generic_emails",
        "enriched_at",
        "enrichment_error",
        "enriched_emails_found",
        "enrichment_provider",
        "certifications",
        "ingredient_signals",
        "vat_id",
        "legal_page_owner",
        "legal_page_email",
        "legal_page_phone",
        "email_verification_status",
        "jsonld_business_type",
        "company_size_estimate",
        "company_size_confidence",
        "company_size_source",
        "company_size_evidence",
        "target_prospect_score",
        "lead_category",
    ]
    for col in enrichment_columns:
        if col not in df.columns:
            df[col] = None
    return df


def _enrich_single_record(
    domain: str,
    record_dict: Dict[str, Any],
    *,
    provider_name: str,
    provider2_name: Optional[str],
    providers: list,
    min_score: Optional[int],
    cache: Any,
    rate_limiter: Any,
    quick_mode: bool,
    crawl_language: str,
) -> Dict[str, Any]:
    """Enrich a single domain — safe to call from a thread.

    Returns a dict of column-name → value updates for the DataFrame row,
    plus a special ``_status`` key ("enriched" | "cached" | "skipped" | "error").
    """
    from app import enrich_with_fallback
    from app.scoring import calculate_lead_score
    from app.cache import is_domain_denylisted
    from app.utils import is_valid_email

    updates: Dict[str, Any] = {}

    def _existing_email_fallback() -> Optional[str]:
        """Return first valid existing email from the current record."""
        for key in (
            "enriched_contact_email",
            "email_raw",
            "email",
            "contact_email",
            "business_email",
            "legal_page_email",
        ):
            value = record_dict.get(key)
            if value is None or pd.isna(value):
                continue
            email = str(value).strip().lower()
            if email and is_valid_email(email):
                return email
        return None

    fallback_email = _existing_email_fallback()

    try:
        current_score = 0
        if min_score is not None:
            current_score = calculate_lead_score(record_dict)

        # Cache check
        cached = cache.get(domain, provider_name)
        if cached:
            if cached.get("cached_status") in ("failed", "denylisted"):
                updates["enrichment_error"] = (
                    f"cached_{cached.get('cached_status')}: "
                    f"{cached.get('cached_error_reason', 'unknown')}"
                )
                updates["enrichment_provider"] = f"cached:{provider_name}"
                if fallback_email:
                    updates["enriched_contact_email"] = fallback_email
                updates["_status"] = "skipped"
                return updates
            updates["enriched_company_name"] = cached.get("company_name")
            updates["enriched_linkedin_url"] = cached.get("linkedin_url")
            updates["enriched_emails_found"] = cached.get("emails_found", 0)
            updates["enriched_at"] = cached.get("enriched_at")
            updates["enrichment_provider"] = f"cached:{provider_name}"
            if fallback_email:
                updates["enriched_contact_email"] = fallback_email
            updates["_status"] = "cached"
            return updates

        if is_domain_denylisted(domain):
            logger.warning(f"Domain {domain} is denylisted, skipping")
            cache.set_denylisted(domain, provider_name)
            updates["enrichment_error"] = "Domain denylisted"
            updates["enrichment_provider"] = "denylisted"
            if fallback_email:
                updates["enriched_contact_email"] = fallback_email
            updates["_status"] = "skipped"
            return updates

        primary = (
            providers[0]
            if providers and (min_score is None or current_score >= min_score)
            else None
        )
        secondary = providers[1] if len(providers) > 1 else None

        crawl_timeout = 6 if quick_mode else 10
        crawl_connect_timeout = 3 if quick_mode else 5
        crawl_max_pages = 2 if quick_mode else 5

        rate_limiter.wait()
        result = enrich_with_fallback(
            domain,
            primary,
            use_crawl_fallback=True,
            cache=cache,
            provider_name=provider_name,
            crawl_timeout=crawl_timeout,
            crawl_connect_timeout=crawl_connect_timeout,
            crawl_max_pages=crawl_max_pages,
            crawl_language=crawl_language,
        )

        if secondary and (not result or not result.success):
            rate_limiter.wait()
            result = enrich_with_fallback(
                domain,
                secondary,
                use_crawl_fallback=True,
                cache=cache,
                provider_name=provider2_name or provider_name,
                crawl_timeout=crawl_timeout,
                crawl_connect_timeout=crawl_connect_timeout,
                crawl_max_pages=crawl_max_pages,
                crawl_language=crawl_language,
            )

        if result and result.success:
            updates["enriched_company_name"] = result.company_name
            updates["enriched_description"] = result.description
            updates["enriched_industry"] = result.industry
            updates["enriched_employee_count"] = result.employee_count
            updates["enriched_linkedin_url"] = result.linkedin_url
            updates["enriched_facebook_url"] = result.facebook_url
            updates["enriched_instagram_url"] = result.instagram_url
            updates["enriched_tiktok_url"] = result.tiktok_url
            updates["enriched_twitter_url"] = result.twitter_url
            updates["enriched_youtube_url"] = result.youtube_url

            if result.email_patterns:
                updates["enriched_email_patterns"] = ", ".join(result.email_patterns)

            if result.contacts:
                real_contacts = [
                    x
                    for x in result.contacts
                    if x.email
                    and getattr(x, "email_source", "") != "generated"
                    and is_valid_email(x.email)
                ]
                updates["enriched_emails_found"] = len(real_contacts)
                selected_contact = real_contacts[0] if real_contacts else None

                if selected_contact:
                    updates["enriched_contact_name"] = selected_contact.name
                    updates["enriched_contact_title"] = selected_contact.title
                    updates["enriched_contact_email"] = selected_contact.email
                    updates["enriched_contact_linkedin"] = selected_contact.linkedin

            if "enriched_contact_email" not in updates and fallback_email:
                updates["enriched_contact_email"] = fallback_email

            updates["enriched_at"] = datetime.utcnow().isoformat()
            updates["enrichment_provider"] = (
                provider_name if primary else "crawl_fallback"
            )

            if result.raw_response:
                jsonld = result.raw_response.get("jsonld_data", {})
                if jsonld.get("business_type"):
                    updates["jsonld_business_type"] = jsonld["business_type"]

            cache.set(
                domain,
                provider_name,
                {
                    "company_name": result.company_name,
                    "linkedin_url": result.linkedin_url,
                    "emails_found": updates.get("enriched_emails_found") or 0,
                    "enriched_at": datetime.utcnow().isoformat(),
                },
            )
            updates["_status"] = "enriched"
        else:
            updates["enrichment_error"] = (
                result.error_message if result else "Unknown error"
            )
            if fallback_email:
                updates["enriched_contact_email"] = fallback_email
            updates["_status"] = "error"

    except Exception as e:
        logger.error(f"Error enriching {domain}: {e}")
        updates["enrichment_error"] = str(e)
        if fallback_email:
            updates["enriched_contact_email"] = fallback_email
        updates["_status"] = "error"
        updates["_error_msg"] = f"Enrichment error: {domain}: {e}"

    return updates


def step_enrich_core(
    df: pd.DataFrame,
    *,
    provider_name: str = "free",
    provider2_name: Optional[str] = None,
    rate_limit: float = 3,
    cache_path: str = "./cache/cache.sqlite",
    clear_cache: bool = False,
    skip_cached: bool = False,
    min_score: Optional[int] = None,
    concurrency: int = 1,
    quick_mode: bool = False,
    crawl_language: str = "auto",
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """Run primary enrichment (API + website crawl) on every enrichable row.

    When *concurrency* > 1, records are processed in parallel using a
    thread pool.  The rate-limiter is still shared so API limits are
    respected even under concurrency.

    Returns:
        (enriched_df, stats_fragment)
    """
    from app import (
        get_provider,
        EnrichmentCache,
        RateLimiter,
    )
    from main import check_api_keys

    stats: Dict[str, Any] = {"enriched_count": 0, "errors": []}

    df = _ensure_enrichment_columns(df)

    cache = EnrichmentCache(cache_path)
    if clear_cache:
        cleared = cache.clear_all()
        logger.info(f"Cleared {cleared} cache entries")

    # Build provider list
    providers: list = []
    for pname in [provider_name, provider2_name]:
        if pname and pname != "free" and check_api_keys(pname):
            providers.append(get_provider(pname, rate_limit=rate_limit))

    rate_limiter = RateLimiter(rate_limit)

    enrichable = df[df["website_domain"].notna() & (df["website_domain"] != "")].copy()
    logger.info(f"Enriching {len(enrichable)} records with domains")

    # Skip cached domains when requested
    if skip_cached:
        logger.info("Checking cache to skip already-enriched domains...")
        uncached = []
        skipped = 0
        for idx in enrichable.index:
            domain = enrichable.loc[idx, "website_domain"]
            if domain and not pd.isna(domain):
                cached_data = cache.get(str(domain), provider_name)
                if cached_data:
                    skipped += 1
                    df.at[idx, "enriched_company_name"] = cached_data.get(
                        "company_name"
                    )
                    df.at[idx, "enriched_linkedin_url"] = cached_data.get(
                        "linkedin_url"
                    )
                    df.at[idx, "enriched_emails_found"] = cached_data.get(
                        "emails_found", 0
                    )
                    df.at[idx, "enriched_at"] = cached_data.get("enriched_at")
                    df.at[idx, "enrichment_provider"] = f"cached:{provider_name}"
                else:
                    uncached.append(idx)
        enrichable = enrichable.loc[uncached].copy()
        stats["skipped_cached"] = skipped
        logger.info(
            f"Skipped {skipped} cached domains, enriching {len(enrichable)} NEW"
        )

    # Build work items: (idx, domain, record_dict)
    work_items: List[Tuple[Any, str, Dict[str, Any]]] = []
    for idx in enrichable.index:
        domain = df.loc[idx, "website_domain"]
        work_items.append((idx, str(domain), df.loc[idx].to_dict()))

    effective_concurrency = max(1, concurrency)

    if effective_concurrency == 1:
        # Sequential path (original behaviour)
        for idx, domain, record_dict in tqdm(work_items, desc="Enriching"):
            updates = _enrich_single_record(
                domain,
                record_dict,
                provider_name=provider_name,
                provider2_name=provider2_name,
                providers=providers,
                min_score=min_score,
                cache=cache,
                rate_limiter=rate_limiter,
                quick_mode=quick_mode,
                crawl_language=crawl_language,
            )
            status = updates.pop("_status", "error")
            err_msg = updates.pop("_error_msg", None)
            for col, val in updates.items():
                df.at[idx, col] = val
            if status in ("enriched", "cached"):
                stats["enriched_count"] += 1
            if err_msg:
                stats["errors"].append(err_msg)
    else:
        # Concurrent path
        logger.info(f"Using {effective_concurrency} concurrent workers for enrichment")
        with ThreadPoolExecutor(max_workers=effective_concurrency) as pool:
            future_to_idx = {}
            for idx, domain, record_dict in work_items:
                future = pool.submit(
                    _enrich_single_record,
                    domain,
                    record_dict,
                    provider_name=provider_name,
                    provider2_name=provider2_name,
                    providers=providers,
                    min_score=min_score,
                    cache=cache,
                    rate_limiter=rate_limiter,
                    quick_mode=quick_mode,
                    crawl_language=crawl_language,
                )
                future_to_idx[future] = idx

            for future in tqdm(
                as_completed(future_to_idx), total=len(future_to_idx), desc="Enriching"
            ):
                idx = future_to_idx[future]
                try:
                    updates = future.result()
                except Exception as e:
                    domain_val = (
                        df.loc[idx, "website_domain"] if idx in df.index else "unknown"
                    )
                    logger.error(f"Worker exception enriching {domain_val}: {e}")
                    stats["errors"].append(f"Enrichment error: {domain_val}: {e}")
                    df.at[idx, "enrichment_error"] = str(e)
                    continue

                status = updates.pop("_status", "error")
                err_msg = updates.pop("_error_msg", None)
                for col, val in updates.items():
                    df.at[idx, col] = val
                if status in ("enriched", "cached"):
                    stats["enriched_count"] += 1
                if err_msg:
                    stats["errors"].append(err_msg)

    return df, stats


# ── Step 5b: Decision-maker + company intelligence + legal pages ─────────────


def _intel_single_record(
    domain: str,
    biz_name: str,
    contact_email: Optional[str],
    record_dict: Optional[Dict[str, Any]] = None,
    *,
    cache_path: str,
) -> Dict[str, Any]:
    """Extract DM + company intel + company size + legal data for one domain — thread-safe.

    Returns a dict of column updates.
    """
    from app.enrich.decision_maker import DecisionMakerFinder
    from app.enrich.company_intel import CompanyIntelligence
    from app.enrich.legal_page_parser import LegalPageParser
    from app.enrich.company_size_estimator import CompanySizeEstimator

    updates: Dict[str, Any] = {}
    record_dict = record_dict or {}

    try:
        dm_finder = DecisionMakerFinder(use_cache=True, cache_path=cache_path)
        company_intel_ext = CompanyIntelligence(use_cache=True, cache_path=cache_path)

        emails: List[str] = []
        if contact_email:
            emails.append(contact_email)

        dm = dm_finder.find_decision_maker(domain, biz_name, emails)
        if dm:
            updates["decision_maker_name"] = dm.name
            updates["decision_maker_title"] = dm.title
            updates["decision_maker_confidence"] = dm.confidence

        ci = company_intel_ext.extract_company_info(domain)
        homepage_html: Optional[str] = None
        if ci:
            updates["estimated_employees"] = ci.get("estimated_employees")
            updates["business_age_years"] = ci.get("business_age_years")
            updates["website_language"] = ci.get("website_language")

        # Fetch homepage HTML for size estimator (reuse company_intel's fetch if possible)
        import requests as _requests

        _url = (
            f"https://{domain}"
            if not domain.startswith(("http://", "https://"))
            else domain
        )
        try:
            resp = _requests.get(
                _url,
                timeout=10,
                allow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            if resp.status_code == 200:
                homepage_html = resp.text
        except Exception:
            # Try without SSL verification
            try:
                resp = _requests.get(
                    _url,
                    timeout=10,
                    allow_redirects=True,
                    verify=False,
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                if resp.status_code == 200:
                    homepage_html = resp.text
            except Exception:
                pass

        # Legal pages
        try:
            legal = LegalPageParser(
                use_cache=True, cache_path=cache_path
            ).fetch_and_parse(domain)
            if legal and legal.owner_name:
                updates["legal_page_owner"] = legal.owner_name
                updates["legal_page_email"] = legal.email
                updates["legal_page_phone"] = legal.phone
                if legal.vat_id:
                    updates["vat_id"] = legal.vat_id

                # Store legal-derived DM data with override flag for the caller
                updates["_legal_dm_name"] = legal.owner_name
                updates["_legal_dm_title"] = legal.owner_title or "owner"
                updates["_legal_dm_confidence"] = legal.confidence
                updates["_legal_email"] = legal.email
        except Exception as e:
            logger.debug(f"Legal page parsing error for {domain}: {e}")

        # Company size estimation (multi-signal)
        try:
            size_estimator = CompanySizeEstimator()
            # Gather signals from all sources
            jsonld_employees = record_dict.get("enriched_employee_count")
            api_employees = (
                record_dict.get("enriched_employee_count")
                if record_dict.get("enrichment_provider", "").startswith(
                    ("clearbit", "apollo", "hunter")
                )
                else None
            )

            size_result = size_estimator.estimate(
                html=homepage_html,
                record=record_dict,
                jsonld_employee_count=str(jsonld_employees)
                if jsonld_employees
                else None,
                api_employee_count=str(api_employees) if api_employees else None,
            )
            if size_result.bucket:
                updates["company_size_estimate"] = size_result.bucket
                updates["company_size_confidence"] = size_result.confidence
                updates["company_size_source"] = size_result.source
                updates["company_size_evidence"] = size_result.evidence
                # Also update estimated_employees for backward compatibility
                if not updates.get("estimated_employees"):
                    updates["estimated_employees"] = size_result.bucket
        except Exception as e:
            logger.debug(f"Company size estimation error for {domain}: {e}")

    except Exception as e:
        logger.debug(f"Error extracting DM/company intel for {domain}: {e}")

    return updates


def step_enrich_intelligence(
    df: pd.DataFrame,
    *,
    cache_path: str = "./cache/cache.sqlite",
    concurrency: int = 1,
) -> pd.DataFrame:
    """Extract decision-maker, company intel, and legal-page data for all rows
    that have a website domain.

    When *concurrency* > 1, records are processed in parallel.
    """
    rows = df[df["website_domain"].notna() & (df["website_domain"] != "")]

    # Build work items (include record_dict for company size estimation)
    work_items: List[Tuple[Any, str, str, Optional[str], Dict[str, Any]]] = []
    for idx in rows.index:
        domain = df.at[idx, "website_domain"]
        biz_name = df.at[idx, "business_name"] if "business_name" in df.columns else ""
        contact_email = None
        if "enriched_contact_email" in df.columns and pd.notna(
            df.at[idx, "enriched_contact_email"]
        ):
            contact_email = str(df.at[idx, "enriched_contact_email"])
        if domain and not pd.isna(domain):
            record_dict = df.loc[idx].to_dict()
            work_items.append(
                (idx, str(domain), str(biz_name or ""), contact_email, record_dict)
            )

    effective_concurrency = max(1, concurrency)

    def _apply_updates(idx: Any, updates: Dict[str, Any]) -> None:
        """Apply updates to DataFrame row, handling legal-page DM override logic."""
        legal_dm_name = updates.pop("_legal_dm_name", None)
        legal_dm_title = updates.pop("_legal_dm_title", None)
        legal_dm_confidence = updates.pop("_legal_dm_confidence", None)
        legal_email = updates.pop("_legal_email", None)

        for col, val in updates.items():
            df.at[idx, col] = val

        # Override DM if legal page has higher confidence
        if legal_dm_name:
            dm_conf = df.at[idx, "decision_maker_confidence"]
            dm_conf = dm_conf if pd.notna(dm_conf) else ""
            if legal_dm_confidence == "high" or not dm_conf:
                df.at[idx, "decision_maker_name"] = legal_dm_name
                df.at[idx, "decision_maker_title"] = legal_dm_title
                df.at[idx, "decision_maker_confidence"] = legal_dm_confidence

        # Use legal email as contact email if we don't have one
        if legal_email and not pd.notna(df.at[idx, "enriched_contact_email"]):
            df.at[idx, "enriched_contact_email"] = legal_email

    if effective_concurrency == 1:
        for idx, domain, biz_name, contact_email, record_dict in tqdm(
            work_items, desc="Decision maker + Company intel + Size", leave=False
        ):
            updates = _intel_single_record(
                domain, biz_name, contact_email, record_dict, cache_path=cache_path
            )
            _apply_updates(idx, updates)
    else:
        logger.info(
            f"Using {effective_concurrency} concurrent workers for intelligence extraction"
        )
        with ThreadPoolExecutor(max_workers=effective_concurrency) as pool:
            future_to_idx = {}
            for idx, domain, biz_name, contact_email, record_dict in work_items:
                future = pool.submit(
                    _intel_single_record,
                    domain,
                    biz_name,
                    contact_email,
                    record_dict,
                    cache_path=cache_path,
                )
                future_to_idx[future] = idx

            for future in tqdm(
                as_completed(future_to_idx),
                total=len(future_to_idx),
                desc="Intelligence extraction",
            ):
                idx = future_to_idx[future]
                try:
                    updates = future.result()
                    _apply_updates(idx, updates)
                except Exception as e:
                    logger.debug(f"Worker exception for intelligence extraction: {e}")

    return df


# ── Step 5c: Ingredient / certification detection ───────────────────────────


def step_detect_signals(df: pd.DataFrame) -> pd.DataFrame:
    """Detect ingredient keywords and certifications from enriched text."""
    from app.scoring import INGREDIENT_KEYWORDS, CERTIFICATIONS

    rows = df[df["website_domain"].notna() & (df["website_domain"] != "")]

    for idx in rows.index:
        try:
            combined = " ".join(
                str(df.at[idx, col] or "")
                for col in ("enriched_description", "enriched_company_name", "category")
            ).lower()

            found_ing = [kw for kw in INGREDIENT_KEYWORDS if kw in combined]
            if found_ing:
                df.at[idx, "ingredient_signals"] = ", ".join(found_ing[:5])

            found_cert = [cert for cert in CERTIFICATIONS if cert in combined]
            if found_cert:
                df.at[idx, "certifications"] = ", ".join(found_cert[:5])
        except Exception:
            pass

    return df


# ── Step 5d: Email verification (optional) ──────────────────────────────────


def step_verify_emails(df: pd.DataFrame) -> pd.DataFrame:
    """Verify contact emails via MX + SMTP."""
    try:
        from app.enrich.email_verifier import EmailVerifier

        verifier = EmailVerifier(enable_smtp=True, detect_catch_all=False)

        emails_to_verify: List[str] = []
        indices: List[int] = []

        for idx in df.index:
            email = (
                df.at[idx, "enriched_contact_email"]
                if "enriched_contact_email" in df.columns
                else None
            )
            if email and pd.notna(email) and str(email).strip():
                emails_to_verify.append(str(email).strip())
                indices.append(idx)

        if emails_to_verify:
            logger.info(f"Verifying {len(emails_to_verify)} emails...")
            results = verifier.verify_batch(emails_to_verify, max_concurrent=3)
            for i, result in enumerate(results):
                df.at[indices[i], "email_verification_status"] = result.overall_status
    except Exception as e:
        logger.warning(f"Email verification failed: {e}")

    return df


# ── Step 6: Score ────────────────────────────────────────────────────────────


def step_score(df: pd.DataFrame) -> pd.DataFrame:
    """Score every record and return a new DataFrame with scores."""
    from app.scoring import score_record

    records = df.to_dict("records")
    scored = [score_record(r) for r in tqdm(records, desc="Scoring")]
    return pd.DataFrame(scored)


# ── Step 7: Output ───────────────────────────────────────────────────────────


def step_generate_outputs(
    df: pd.DataFrame,
    output_dir: Path,
    stats: Dict[str, Any],
) -> Dict[str, Path]:
    """Write CSV, XLSX, dorks, and report. Returns paths dict."""
    from app.report import generate_outputs

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    outputs = generate_outputs(df, output_dir, timestamp, stats)
    for kind, path in outputs.items():
        logger.info(f"  - {kind}: {path}")
    return outputs


def step_generate_quick_output(
    df: pd.DataFrame,
    output_dir: Path,
    stats: Dict[str, Any],
) -> Dict[str, Path]:
    """Write only quick slim CSV output and return paths dict."""
    from app.report import generate_quick_output

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    outputs = generate_quick_output(df, output_dir, timestamp, stats)
    for kind, path in outputs.items():
        logger.info(f"  - {kind}: {path}")
    return outputs
