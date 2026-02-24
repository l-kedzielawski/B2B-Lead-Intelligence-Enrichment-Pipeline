"""Deduplication module with weighted matching rules."""

import logging
from typing import List, Dict, Any, Optional, Set, Tuple
from collections import defaultdict
import pandas as pd
from rapidfuzz import fuzz
from tqdm import tqdm

from .utils import score_record_completeness, BUSINESS_SUFFIXES_NORMALIZED

logger = logging.getLogger(__name__)


def normalize_for_matching(text: str) -> str:
    """Normalize text for fuzzy matching."""
    if not text:
        return ""
    text = text.lower().strip()
    # Remove common punctuation
    text = text.replace(",", "").replace(".", "").replace("-", " ")
    # Remove common business suffixes (shared constant, pre-sorted longest-first)
    for suffix in BUSINESS_SUFFIXES_NORMALIZED:
        if text.endswith(suffix):
            text = text[: -len(suffix)].strip()
            break  # Only strip one suffix
    return text


def match_by_phone(phone_e164: Optional[str]) -> bool:
    """Check if phone number is valid for matching."""
    return bool(phone_e164 and len(phone_e164) >= 10)


def match_by_domain(domain: Optional[str]) -> bool:
    """Check if domain is valid for matching."""
    if not domain:
        return False
    # Exclude common generic domains
    generic_domains = [
        "gmail.com",
        "yahoo.com",
        "hotmail.com",
        "outlook.com",
        "aol.com",
        "icloud.com",
        "live.com",
        "msn.com",
        # European free email providers
        "gmx.de",
        "gmx.at",
        "gmx.ch",
        "gmx.net",
        "web.de",
        "t-online.de",
        "freenet.de",
        "arcor.de",
        "posteo.de",
        "orange.fr",
        "laposte.net",
        "wanadoo.fr",
        "sfr.fr",
        "free.fr",
        "libero.it",
        "virgilio.it",
        "tin.it",
        "alice.it",
        "tiscali.it",
        "wp.pl",
        "onet.pl",
        "o2.pl",
        "interia.pl",
        "poczta.fm",
        "ziggo.nl",
        "home.nl",
        "kpnmail.nl",
        "xs4all.nl",
        "telenet.be",
        "skynet.be",
        "proximus.be",
        "sapo.pt",
        "clix.pt",
        "iol.pt",
        "telefonica.net",
        "terra.es",
        "bluewin.ch",
        "hispeed.ch",
        "chello.at",
        "aon.at",
        "mail.ru",
        "yandex.ru",
        "yandex.com",
        "protonmail.com",
        "protonmail.ch",
        "tutanota.com",
    ]
    return domain.lower() not in generic_domains


def fuzzy_match_name_address(name1: str, addr1: str, name2: str, addr2: str) -> float:
    """
    Calculate fuzzy match score between two name+address pairs.
    Returns score 0-100.
    """
    if not name1 or not name2:
        return 0.0

    name1_norm = normalize_for_matching(name1)
    name2_norm = normalize_for_matching(name2)

    # Name similarity (weighted more heavily)
    name_score = fuzz.token_sort_ratio(name1_norm, name2_norm)

    # If names are very similar, check address
    if name_score >= 70 and addr1 and addr2:
        addr1_norm = normalize_for_matching(addr1)
        addr2_norm = normalize_for_matching(addr2)
        addr_score = fuzz.partial_ratio(addr1_norm, addr2_norm)

        # Combined score
        return name_score * 0.7 + addr_score * 0.3

    return name_score * 0.8  # Lower weight if no address match


def find_duplicate_groups(
    records: List[Dict[str, Any]],
    phone_threshold: float = 0.95,
    domain_threshold: float = 0.95,
    fuzzy_threshold: float = 85.0,
) -> Dict[int, List[int]]:
    """
    Find duplicate groups across all records.

    Returns:
        Dict mapping group_id -> list of record indices in group
    """
    n = len(records)
    logger.info(f"Finding duplicates across {n} records")

    # Index records by phone and domain for quick lookup
    phone_index: Dict[str, List[int]] = defaultdict(list)
    domain_index: Dict[str, List[int]] = defaultdict(list)

    for idx, record in enumerate(records):
        phone = record.get("phone_e164")
        if match_by_phone(phone):
            phone_index[phone].append(idx)

        domain = record.get("website_domain")
        if match_by_domain(domain):
            domain_index[domain].append(idx)

    # Union-Find structure for grouping
    parent = list(range(n))
    rank = [0] * n

    def find(x: int) -> int:
        if parent[x] != x:
            parent[x] = find(parent[x])
        return parent[x]

    def union(x: int, y: int) -> None:
        px, py = find(x), find(y)
        if px == py:
            return
        if rank[px] < rank[py]:
            px, py = py, px
        parent[py] = px
        if rank[px] == rank[py]:
            rank[px] += 1

    # Match by phone (strong signal)
    for phone, indices in phone_index.items():
        if len(indices) > 1:
            for i in range(1, len(indices)):
                union(indices[0], indices[i])

    # Match by domain (strong signal)
    for domain, indices in domain_index.items():
        if len(indices) > 1:
            for i in range(1, len(indices)):
                union(indices[0], indices[i])

    # Fuzzy matching on name + address (medium signal)
    # Only check records that aren't already matched
    matched_sets = defaultdict(set)
    for i in range(n):
        root = find(i)
        matched_sets[root].add(i)

    # Build list of unmatched/isolated records for fuzzy matching
    unmatched_indices = []
    for root, members in matched_sets.items():
        if len(members) == 1:
            unmatched_indices.append(root)

    logger.info(f"Found {len(unmatched_indices)} isolated records for fuzzy matching")

    # Fuzzy matching (expensive, so only compare isolated records)
    for i in tqdm(unmatched_indices, desc="Fuzzy matching", leave=False):
        for j in range(i + 1, n):
            if find(i) == find(j):
                continue

            rec1 = records[i]
            rec2 = records[j]

            name1 = rec1.get("business_name", "")
            name2 = rec2.get("business_name", "")
            addr1 = rec1.get("address", "")
            addr2 = rec2.get("address", "")

            if name1 and name2:
                score = fuzzy_match_name_address(name1, addr1, name2, addr2)
                if score >= fuzzy_threshold:
                    union(i, j)

    # Build final groups
    groups: Dict[int, List[int]] = defaultdict(list)
    for i in range(n):
        root = find(i)
        groups[root].append(i)

    # Only return groups with more than 1 member
    duplicate_groups = {k: v for k, v in groups.items() if len(v) > 1}

    logger.info(
        f"Found {len(duplicate_groups)} duplicate groups affecting {sum(len(v) for v in duplicate_groups.values())} records"
    )

    return duplicate_groups


def select_best_record(group_indices: List[int], records: List[Dict[str, Any]]) -> int:
    """
    Select the best record from a duplicate group.

    Criteria:
    1. Most filled fields
    2. Has website/phone
    3. Higher reviews_count
    4. Higher rating
    """
    best_idx = group_indices[0]
    best_score = -1

    for idx in group_indices:
        record = records[idx]
        score = 0

        # Completeness score
        score += score_record_completeness(record)

        # Has website bonus
        if record.get("website_normalized"):
            score += 20

        # Has valid phone bonus
        if record.get("phone_valid"):
            score += 15

        # Reviews count
        reviews = record.get("reviews_count_normalized")
        if reviews is not None:
            try:
                score += min(int(float(reviews)) / 10, 50)  # Cap at 50
            except (ValueError, TypeError):
                pass

        # Rating
        rating = record.get("rating_normalized")
        if rating is not None:
            try:
                if float(rating) >= 4.0:
                    score += 10
            except (ValueError, TypeError):
                pass

        if score > best_score:
            best_score = score
            best_idx = idx

    return best_idx


def deduplicate_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """
    Deduplicate records: collapse duplicate groups to best record only.

    After this function, the returned DataFrame contains **only unique rows**.
    Duplicate rows are dropped; the surviving "best" row carries metadata
    about how many copies existed.

    Adds columns:
    - duplicate_group_id: Group identifier (None for singletons)
    - duplicate_count: How many copies of this business existed (1 for singletons)

    Returns:
        DataFrame with duplicates removed (fewer rows than input).
    """
    records = df.to_dict("records")
    n = len(records)

    logger.info(f"Starting deduplication of {n} records")

    # Find duplicate groups
    duplicate_groups = find_duplicate_groups(records)

    # Determine which indices to keep and metadata for survivors
    keep_indices: Set[int] = set(range(n))  # start with all
    duplicate_group_id: Dict[int, int] = {}
    duplicate_count: Dict[int, int] = {}

    total_dropped = 0

    for group_idx, group_members in enumerate(duplicate_groups.values(), 1):
        best_idx = select_best_record(group_members, records)
        group_size = len(group_members)

        # Tag the best record with group metadata
        duplicate_group_id[best_idx] = group_idx
        duplicate_count[best_idx] = group_size

        # Drop all non-best members
        for member_idx in group_members:
            if member_idx != best_idx:
                keep_indices.discard(member_idx)
                total_dropped += 1

    # Build the collapsed DataFrame (only surviving rows)
    keep_list = sorted(keep_indices)
    result_df = df.iloc[keep_list].copy().reset_index(drop=True)

    # Map old index → new index for metadata columns
    old_to_new = {old_idx: new_idx for new_idx, old_idx in enumerate(keep_list)}

    result_df["duplicate_group_id"] = [
        duplicate_group_id.get(keep_list[i]) for i in range(len(result_df))
    ]
    result_df["duplicate_count"] = [
        duplicate_count.get(keep_list[i], 1) for i in range(len(result_df))
    ]

    unique_groups = len(duplicate_groups)
    logger.info(
        f"Deduplication complete: {total_dropped} duplicates removed across "
        f"{unique_groups} groups, {len(result_df)} unique records remain"
    )

    return result_df
