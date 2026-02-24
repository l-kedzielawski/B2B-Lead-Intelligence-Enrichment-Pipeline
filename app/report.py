"""Report generation module."""

import logging
import re
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple
import pandas as pd

from .io import write_csv_output, write_excel_output

logger = logging.getLogger(__name__)


GENERIC_EMAIL_LOCAL_PARTS = {
    "info",
    "contact",
    "hello",
    "hi",
    "mail",
    "office",
    "sales",
    "support",
    "team",
    "admin",
    "service",
    "booking",
    "reservations",
}

PERSON_NAME_STOPWORDS = {
    "gmbh",
    "llc",
    "ltd",
    "inc",
    "company",
    "co",
    "restaurant",
    "bakery",
    "cafe",
    "shop",
    "hotel",
    "team",
    "office",
    "admin",
    "support",
    "info",
    "contact",
    "owner",
    "founder",
    "manager",
    "director",
    "ceo",
}

NAME_PARTICLES = {
    "de",
    "del",
    "der",
    "van",
    "von",
    "da",
    "di",
    "la",
    "le",
}

SLIM_FINAL_COLUMNS = [
    "companyName",
    "email",
    "website",
    "phone",
    "firstName",
    "lastName",
    "location",
    "rating",
    "reviewsCount",
    "jobTitle",
    "department",
]


def _clean_value(value: Any) -> str:
    """Return clean string value or empty string for missing values."""
    if value is None or pd.isna(value):
        return ""
    cleaned = str(value).strip()
    return "" if cleaned.lower() == "nan" else cleaned


def _first_non_empty(row: pd.Series, columns: List[str]) -> str:
    """Pick the first non-empty value from candidate columns."""
    for col in columns:
        value = _clean_value(row.get(col))
        if value:
            return value
    return ""


def _safe_int(value: Any) -> Optional[int]:
    """Parse int safely, returning None when unavailable."""
    try:
        if value is None or pd.isna(value):
            return None
        return int(float(value))
    except (ValueError, TypeError):
        return None


def _split_name(full_name: str) -> Tuple[str, str]:
    """Split full name into first/last (best-effort)."""
    parts = [p for p in full_name.split() if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _normalize_person_name(value: str) -> str:
    """Normalize candidate person name by removing punctuation noise."""
    if not value:
        return ""
    cleaned = re.sub(r"\([^)]*\)", " ", value)
    cleaned = re.sub(r"\b(mr|mrs|ms|dr|prof)\.?\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.replace("|", " ").replace("/", " ").replace("_", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -,.\t\n\r")
    return cleaned


def _looks_like_person_name(value: str) -> bool:
    """Heuristic validation that a string is likely a real person name."""
    if not value:
        return False
    lower_value = value.lower()
    if "@" in value or "http" in lower_value:
        return False
    if re.search(r"\d", value):
        return False

    tokens = [t for t in re.split(r"\s+", value) if t]
    if len(tokens) < 2 or len(tokens) > 5:
        return False

    meaningful_tokens = 0
    for token in tokens:
        normalized = token.strip(".,'-").lower()
        if not normalized:
            continue
        if normalized in NAME_PARTICLES:
            continue
        if normalized in PERSON_NAME_STOPWORDS:
            return False
        if not re.search(r"[A-Za-z]", normalized):
            return False
        if len(normalized) < 2:
            return False
        meaningful_tokens += 1

    return meaningful_tokens >= 2


def _extract_name_from_email(email: str) -> str:
    """Try deriving a person name from personal email local-part."""
    if not email or "@" not in email:
        return ""
    local = email.split("@", 1)[0].lower().strip()
    if not local or local in GENERIC_EMAIL_LOCAL_PARTS:
        return ""

    # Only accept explicit separators for safer extraction.
    if "." in local:
        parts = [p for p in local.split(".") if p]
    elif "_" in local:
        parts = [p for p in local.split("_") if p]
    elif "-" in local:
        parts = [p for p in local.split("-") if p]
    else:
        return ""

    if len(parts) < 2 or len(parts) > 3:
        return ""
    if any(not p.isalpha() or len(p) < 2 for p in parts):
        return ""

    candidate = " ".join(part.capitalize() for part in parts)
    return candidate if _looks_like_person_name(candidate) else ""


def _choose_person_identity(
    row: pd.Series,
    company_email: str,
    has_direct_email: bool,
) -> Tuple[str, str, str, str, bool]:
    """Pick best person identity data with quality checks."""
    person_title = _first_non_empty(
        row, ["enriched_contact_title", "decision_maker_title"]
    )

    raw_name = _first_non_empty(row, ["enriched_contact_name", "decision_maker_name"])
    normalized_name = _normalize_person_name(raw_name)

    person_name = ""
    if _looks_like_person_name(normalized_name):
        person_name = normalized_name
    elif has_direct_email:
        person_name = _extract_name_from_email(company_email)

    is_good_person_lead = bool(person_name and (person_title or has_direct_email))
    if not is_good_person_lead:
        return "", "", "", "", False

    first_name, last_name = _split_name(person_name)
    person_email = company_email if has_direct_email else ""
    return first_name, last_name, person_title, person_email, True


def _is_generic_email(email: str) -> bool:
    """Heuristic to identify generic mailbox addresses."""
    if not email or "@" not in email:
        return True
    local_part = email.split("@", 1)[0].lower().strip()
    if not local_part:
        return True
    return local_part in GENERIC_EMAIL_LOCAL_PARTS


def _extract_email_domain(email: str) -> str:
    """Extract and normalize email domain."""
    if not email or "@" not in email:
        return ""
    return email.split("@", 1)[1].lower().strip()


def _email_matches_website_domain(email: str, website_domain: str) -> bool:
    """Check whether email domain belongs to the same website domain."""
    email_domain = _extract_email_domain(email)
    domain = website_domain.lower().strip()
    if not email_domain or not domain:
        return False
    return email_domain == domain or email_domain.endswith(f".{domain}")


def _is_hunter_candidate_for_case(
    row: pd.Series,
    *,
    website: str,
    website_domain: str,
    has_direct_email: bool,
    company_size: str,
) -> bool:
    """
    Decide whether this lead should be sent to Hunter.io.

    Case-specific logic (vanilla/cacao/spice targeting):
    - already relevant and promising
    - has company domain
    - missing a strong direct person email
    """
    if not website or not website_domain or has_direct_email:
        return False

    lead_score = _safe_int(row.get("lead_quality_score")) or 0
    target_score = _safe_int(row.get("target_prospect_score")) or 0
    canonical = _clean_value(row.get("canonical_category")).lower()
    category = _clean_value(row.get("category")).lower()

    relevant_category = canonical in {"food", "horeca", "beauty"} or any(
        term in category
        for term in (
            "bakery",
            "boulangerie",
            "patisserie",
            "cafe",
            "restaurant",
            "hotel",
            "horeca",
            "ingredient",
            "spice",
            "chocolate",
            "cosmetic",
            "spa",
            "wellness",
        )
    )

    has_intent_signals = bool(_clean_value(row.get("ingredient_signals"))) or bool(
        _clean_value(row.get("certifications"))
    )
    has_dm_gap = not _clean_value(row.get("decision_maker_name"))
    has_usable_size = company_size in {
        "2-5",
        "5-20",
        "6-20",
        "20-50",
        "21-50",
        "50+",
        "51-200",
        "200+",
    }

    return bool(
        lead_score >= 58
        and (target_score >= 55 or relevant_category)
        and (has_intent_signals or has_dm_gap or has_usable_size)
    )


def _build_slim_final_export(df: pd.DataFrame) -> pd.DataFrame:
    """Build a concise final CSV tailored for outreach workflows."""
    rows: List[Dict[str, Any]] = []

    for _, row in df.iterrows():
        company_name = _first_non_empty(row, ["enriched_company_name", "business_name"])
        company_email = _first_non_empty(
            row,
            [
                "enriched_contact_email",
                "email_raw",
                "email",
                "contact_email",
                "business_email",
                "legal_page_email",
            ],
        )
        website = _first_non_empty(row, ["website_normalized", "website_raw"])
        phone = _first_non_empty(row, ["phone_e164", "phone_raw"])
        location = _first_non_empty(row, ["city", "address_city_component"])
        rating = _first_non_empty(row, ["rating_normalized", "rating"])
        reviews_count = _first_non_empty(
            row, ["reviews_count_normalized", "reviews_count"]
        )

        raw_name = _first_non_empty(
            row, ["enriched_contact_name", "decision_maker_name"]
        )
        normalized_name = _normalize_person_name(raw_name)
        if _looks_like_person_name(normalized_name):
            first_name, last_name = _split_name(normalized_name)
        else:
            first_name, last_name = "", ""

        rows.append(
            {
                "companyName": company_name,
                "email": company_email,
                "website": website,
                "phone": phone,
                "firstName": first_name,
                "lastName": last_name,
                "location": location,
                "rating": rating,
                "reviewsCount": reviews_count,
                "jobTitle": "",
                "department": "",
            }
        )

    return pd.DataFrame(rows).reindex(columns=pd.Index(SLIM_FINAL_COLUMNS))


def generate_quick_output(
    df: pd.DataFrame, output_dir: Path, timestamp: str, processing_stats: Dict[str, Any]
) -> Dict[str, Path]:
    """Generate quick output file (final slim CSV only)."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    slim_df = _build_slim_final_export(df)
    slim_csv_path = output_dir / f"final_leads_quick_{timestamp}.csv"
    write_csv_output(slim_df, slim_csv_path)
    processing_stats["final_csv_output"] = str(slim_csv_path)
    logger.info(f"Quick final CSV written: {slim_csv_path}")

    return {"final_csv": slim_csv_path}


def generate_markdown_report(
    df: pd.DataFrame, output_path: Path, processing_stats: Dict[str, Any]
) -> None:
    """Generate markdown report with summary and statistics."""

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Calculate statistics
    total_records = len(df)
    # After dedup collapse, all rows are unique. duplicate_count shows original copies.
    total_source_records = (
        int(df["duplicate_count"].sum())
        if "duplicate_count" in df.columns
        else total_records
    )
    duplicates_collapsed = total_source_records - total_records
    unique_records = total_records

    enrichment_stats = df["enrichment_status"].value_counts().to_dict()

    # Quality score distribution
    score_bins = pd.cut(
        df["lead_quality_score"],
        bins=[0, 40, 60, 80, 100],
        labels=["Poor (0-40)", "Cold (40-60)", "Warm (60-80)", "Hot (80-100)"],
    )
    score_distribution = score_bins.value_counts().to_dict()

    # Build report
    report = f"""# Lead Processing Report

Generated: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

## Processing Summary

- **Source Records**: {total_source_records:,}
- **Unique Leads (after dedup)**: {unique_records:,}
- **Duplicates Collapsed**: {duplicates_collapsed:,}
- **Files Processed**: {processing_stats.get("files_processed", 0)}

## Data Quality

### Lead Quality Distribution

| Quality Level | Count | Percentage |
|---------------|-------|------------|
"""

    for quality, count in sorted(score_distribution.items()):
        pct = (count / total_records * 100) if total_records > 0 else 0
        report += f"| {quality} | {count:,} | {pct:.1f}% |\n"

    report += f"""
### Average Lead Quality Score: {df["lead_quality_score"].mean():.1f}/100

## Enrichment Results

| Status | Count | Percentage |
|--------|-------|------------|
"""

    for status in ["ok", "partial", "failed"]:
        count = enrichment_stats.get(status, 0)
        pct = (count / total_records * 100) if total_records > 0 else 0
        report += f"| {status.capitalize()} | {count:,} | {pct:.1f}% |\n"

    # Add field completeness stats
    report += """
## Field Completeness

| Field | Filled | Percentage |
|-------|--------|------------|
"""

    fields_to_check = [
        "business_name",
        "category",
        "address",
        "phone_raw",
        "website_raw",
        "google_maps_url",
        "rating",
        "reviews_count",
    ]

    for field in fields_to_check:
        if field in df.columns:
            filled = df[field].notna().sum()
            pct = (filled / total_records * 100) if total_records > 0 else 0
            report += f"| {field} | {filled:,} | {pct:.1f}% |\n"

    # Add enriched fields
    report += """
## Enrichment Fields

| Field | Filled | Percentage |
|-------|--------|------------|
"""

    enriched_fields = [
        "enriched_company_name",
        "enriched_linkedin_url",
        "enriched_contact_email",
        "decision_maker_name",
        "decision_maker_confidence",
        "estimated_employees",
        "business_age_years",
        "website_language",
        "company_size_estimate",
        "company_size_confidence",
        "enriched_tiktok_url",
        "enriched_youtube_url",
        "enriched_instagram_url",
        "certifications",
        "ingredient_signals",
        "vat_id",
    ]

    for field in enriched_fields:
        if field in df.columns:
            filled = df[field].notna().sum()
            pct = (filled / total_records * 100) if total_records > 0 else 0
            report += f"| {field} | {filled:,} | {pct:.1f}% |\n"

    # Add top cities
    if "city" in df.columns:
        top_cities = df[df["city"].notna()]["city"].value_counts().head(10)
        if len(top_cities) > 0:
            report += """
## Top Cities

| City | Count |
|------|-------|
"""
            for city, count in top_cities.items():
                report += f"| {city} | {count:,} |\n"

    # Add top categories
    if "category" in df.columns:
        top_categories = df[df["category"].notna()]["category"].value_counts().head(10)
        if len(top_categories) > 0:
            report += """
## Top Categories

| Category | Count |
|----------|-------|
"""
            for category, count in top_categories.items():
                report += f"| {category} | {count:,} |\n"

    # Industry breakdown (food / horeca / beauty / other)
    report += "\n### Industry Breakdown\n\n"
    if "canonical_category" in df.columns:
        industry_counts = df["canonical_category"].value_counts()
        report += "| Industry | Count | % |\n"
        report += "|----------|-------|---|\n"
        for industry, count in industry_counts.items():
            pct = count / len(df) * 100
            report += f"| {industry} | {count} | {pct:.1f}% |\n"

    # Target prospect score distribution
    if "target_prospect_score" in df.columns:
        report += "\n### Target Prospect Score Distribution\n\n"
        prospect_bins = pd.cut(
            df["target_prospect_score"],
            bins=[0, 20, 40, 60, 80, 100],
            labels=[
                "Very Low (0-20)",
                "Low (21-40)",
                "Medium (41-60)",
                "High (61-80)",
                "Excellent (81-100)",
            ],
        )
        prospect_dist = prospect_bins.value_counts().sort_index()
        report += "| Prospect Rating | Count | % |\n"
        report += "|----------------|-------|---|\n"
        for label, count in prospect_dist.items():
            pct = count / len(df) * 100
            report += f"| {label} | {count} | {pct:.1f}% |\n"

        # Top prospects table
        report += "\n### Top 20 Prospects (by Target Score)\n\n"
        top_prospects = df.nlargest(20, "target_prospect_score")
        if not top_prospects.empty:
            report += "| Business | Category | Score | Prospect | City | Decision Maker | Email |\n"
            report += "|----------|----------|-------|----------|------|----------------|-------|\n"
            for _, row in top_prospects.iterrows():
                biz = str(row.get("business_name", ""))[:30]
                cat = str(row.get("category", ""))[:20]
                score = row.get("lead_quality_score", 0)
                prospect = row.get("target_prospect_score", 0)
                city = str(row.get("city", ""))[:15]
                dm = str(row.get("decision_maker_name", ""))[:20]
                email = str(row.get("enriched_contact_email", ""))[:25]
                report += f"| {biz} | {cat} | {score} | {prospect} | {city} | {dm} | {email} |\n"

    # Decision maker extraction results
    if "decision_maker_name" in df.columns:
        report += "\n### Decision Maker Extraction\n\n"
        dm_found = df["decision_maker_name"].notna().sum()
        dm_high = (df.get("decision_maker_confidence", pd.Series()) == "high").sum()
        dm_medium = (df.get("decision_maker_confidence", pd.Series()) == "medium").sum()
        report += (
            f"- Decision makers found: {dm_found} ({dm_found / len(df) * 100:.1f}%)\n"
        )
        report += f"- High confidence: {dm_high}\n"
        report += f"- Medium confidence: {dm_medium}\n"

    # Company size distribution
    if "company_size_estimate" in df.columns:
        report += "\n### Company Size Distribution\n\n"
        size_data = df[
            df["company_size_estimate"].notna() & (df["company_size_estimate"] != "")
        ]
        if len(size_data) > 0:
            # Ordered bucket display
            bucket_order = ["1", "2-5", "6-20", "21-50", "51-200", "200+"]
            size_counts = size_data["company_size_estimate"].value_counts()
            report += "| Size (Employees) | Count | % |\n"
            report += "|-----------------|-------|---|\n"
            for bucket in bucket_order:
                count = size_counts.get(bucket, 0)
                if count > 0:
                    pct = count / len(df) * 100
                    report += f"| {bucket} | {count} | {pct:.1f}% |\n"

            # Confidence breakdown
            report += "\n**Estimation confidence:**\n"
            if "company_size_confidence" in df.columns:
                conf_counts = size_data["company_size_confidence"].value_counts()
                for level in ("high", "medium", "low"):
                    count = conf_counts.get(level, 0)
                    report += f"- {level.capitalize()}: {count} ({count / len(size_data) * 100:.1f}%)\n"

            # Top source breakdown
            if "company_size_source" in df.columns:
                report += "\n**Top estimation sources:**\n"
                source_counts = size_data["company_size_source"].value_counts().head(5)
                for source, count in source_counts.items():
                    report += f"- {source}: {count}\n"
        else:
            report += "No company size estimates available.\n"

    # Certification and ingredient signals
    report += "\n### Business Intelligence\n\n"
    for col_name, label in [
        ("certifications", "Certifications found"),
        ("ingredient_signals", "Ingredient signals detected"),
        ("website_language", "Website language detected"),
        ("estimated_employees", "Employee count estimated"),
        ("business_age_years", "Business age estimated"),
        ("company_size_estimate", "Company size estimated (multi-signal)"),
    ]:
        if col_name in df.columns:
            found = df[col_name].notna().sum()
            found_non_empty = (
                df[col_name].apply(lambda x: bool(x) and str(x).strip() != "").sum()
                if found > 0
                else 0
            )
            report += f"- {label}: {found_non_empty} records\n"

    # Add high-value leads section
    high_value = (
        df[df["lead_quality_score"] >= 70]
        .sort_values("lead_quality_score", ascending=False)
        .head(20)
    )

    if len(high_value) > 0:
        report += """
## Top 20 High-Value Leads

| Business | City | Score | Phone | Website |
|----------|------|-------|-------|----------|
"""
        for _, row in high_value.iterrows():
            business = str(row.get("business_name", ""))[:30]
            city = str(row.get("city", ""))[:20]
            score = int(row.get("lead_quality_score", 0))
            phone = (
                str(row.get("phone_e164", ""))[:15] if row.get("phone_e164") else "N/A"
            )
            website = "Yes" if row.get("website_normalized") else "No"

            report += f"| {business} | {city} | {score} | {phone} | {website} |\n"

    # Add processing errors if any
    if processing_stats.get("errors"):
        report += """
## Processing Errors

"""
        for error in processing_stats["errors"][-10:]:  # Show last 10 errors
            report += f"- {error}\n"

    report += f"""
## Output Files

- CSV: `{processing_stats.get("csv_output", "N/A")}`
- Final Slim CSV: `{processing_stats.get("final_csv_output", "N/A")}`
- Excel: `{processing_stats.get("excel_output", "N/A")}`

---
*Report generated by Lead Cleaner & Enrichment Tool*
"""

    # Write report
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(report)

    logger.info(f"Report written: {output_path}")


def generate_outputs(
    df: pd.DataFrame, output_dir: Path, timestamp: str, processing_stats: Dict[str, Any]
) -> Dict[str, Path]:
    """
    Generate all output files (CSV, Excel, Markdown report).

    Returns:
        Dict mapping output type to file path
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    outputs = {}

    # CSV output
    csv_path = output_dir / f"cleaned_enriched_{timestamp}.csv"
    write_csv_output(df, csv_path)
    outputs["csv"] = csv_path
    processing_stats["csv_output"] = str(csv_path)

    # Slim final CSV output for outreach workflows
    slim_df = _build_slim_final_export(df)
    slim_csv_path = output_dir / f"final_leads_{timestamp}.csv"
    write_csv_output(slim_df, slim_csv_path)
    outputs["final_csv"] = slim_csv_path
    processing_stats["final_csv_output"] = str(slim_csv_path)

    # Excel output
    excel_path = output_dir / f"cleaned_enriched_{timestamp}.xlsx"
    write_excel_output(df, excel_path)
    outputs["excel"] = excel_path
    processing_stats["excel_output"] = str(excel_path)

    # Markdown report
    report_path = output_dir / f"report_{timestamp}.md"
    generate_markdown_report(df, report_path, processing_stats)
    outputs["report"] = report_path

    # Google Dork queries for manual LinkedIn scraping
    try:
        from app.enrich.dork_generator import DorkQueryGenerator

        dork_records = DorkQueryGenerator.generate_bulk_dork_csv(df.to_dict("records"))
        dork_df = pd.DataFrame(dork_records)
        dork_path = output_dir / f"google_dork_queries_{timestamp}.csv"
        dork_df.to_csv(dork_path, index=False, encoding="utf-8")
        outputs["dork_queries"] = dork_path
        logger.info(f"Generated Google dork queries CSV: {dork_path}")
    except Exception as e:
        logger.warning(f"Could not generate dork queries: {e}")

    logger.info(f"Generated {len(outputs)} output files in {output_dir}")

    return outputs
