#!/usr/bin/env python3
"""Verify emails in an existing CSV without running full enrichment."""

import argparse
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.enrich.email_verifier import EmailVerifier


logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Verify contact emails in a CSV file",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python scripts/verify_emails_only.py "
            "--input output/test/cleaned.csv\n"
            "\n"
            "  python scripts/verify_emails_only.py "
            "--input output/test/cleaned.csv "
            "--output output/test/cleaned_email_verified.csv\n"
            "\n"
            "  python scripts/verify_emails_only.py "
            "--input output/test/cleaned.csv "
            "--max-concurrent 5 --rate-limit 1"
        ),
    )

    parser.add_argument(
        "--input",
        "-i",
        type=str,
        required=True,
        help="Input CSV path",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=str,
        default=None,
        help="Output CSV path (default: <input>_email_verified.csv)",
    )
    parser.add_argument(
        "--email-column",
        type=str,
        default="enriched_contact_email",
        help="Column containing emails to verify (default: enriched_contact_email)",
    )
    parser.add_argument(
        "--status-column",
        type=str,
        default="email_verification_status",
        help="Column to write overall verification status (default: email_verification_status)",
    )
    parser.add_argument(
        "--max-concurrent",
        type=int,
        default=3,
        help="Parallel verification workers (default: 3)",
    )
    parser.add_argument(
        "--smtp-timeout",
        type=int,
        default=10,
        help="SMTP timeout per check in seconds (default: 10)",
    )
    parser.add_argument(
        "--rate-limit",
        type=float,
        default=2.0,
        help="SMTP checks per second (default: 2.0)",
    )
    parser.add_argument(
        "--detect-catch-all",
        action="store_true",
        help="Detect catch-all domains (slower)",
    )
    parser.add_argument(
        "--log-all-results",
        action="store_true",
        help="Print one console line per checked email (default: prints only verified)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose logging",
    )

    return parser.parse_args()


def default_output_path(input_path: Path) -> Path:
    """Return default output path based on input file name."""
    return input_path.with_name(f"{input_path.stem}_email_verified{input_path.suffix}")


def verify_emails_in_csv(
    input_path: Path,
    output_path: Path,
    email_column: str,
    status_column: str,
    max_concurrent: int,
    smtp_timeout: int,
    rate_limit: float,
    detect_catch_all: bool,
    log_all_results: bool,
) -> None:
    """Verify emails from *email_column* and write status to *status_column*."""
    if not input_path.exists():
        raise FileNotFoundError(f"Input file does not exist: {input_path}")

    df = pd.read_csv(input_path, dtype=str, keep_default_na=False)

    if email_column not in df.columns:
        raise ValueError(
            f"Column '{email_column}' not found. Available columns: {', '.join(df.columns)}"
        )

    if status_column not in df.columns:
        df[status_column] = ""

    detail_smtp_column = "email_verification_smtp_status"
    detail_conf_column = "email_verification_confidence"
    if detail_smtp_column not in df.columns:
        df[detail_smtp_column] = ""
    if detail_conf_column not in df.columns:
        df[detail_conf_column] = ""

    emails_to_verify = []
    indices = []
    for idx in df.index:
        email = str(df.at[idx, email_column]).strip()
        if email:
            emails_to_verify.append(email)
            indices.append(idx)

    logger.info("Rows in file: %d", len(df))
    logger.info("Emails to verify: %d", len(emails_to_verify))

    started_at = time.monotonic()

    if not emails_to_verify:
        logger.warning("No non-empty emails found in column '%s'", email_column)
    else:
        verifier = EmailVerifier(
            smtp_timeout=smtp_timeout,
            rate_limit=rate_limit,
            enable_smtp=True,
            detect_catch_all=detect_catch_all,
        )

        total = len(emails_to_verify)
        logger.info("Starting live verification with up to %d workers", max_concurrent)

        with ThreadPoolExecutor(max_workers=max_concurrent) as pool:
            future_map = {
                pool.submit(verifier.verify_email, email): (idx, email)
                for idx, email in zip(indices, emails_to_verify)
            }

            completed = 0
            for future in as_completed(future_map):
                row_idx, fallback_email = future_map[future]
                completed += 1

                try:
                    result = future.result()
                    email = result.email
                    overall_status = result.overall_status
                    smtp_status = result.smtp_status
                    confidence = result.confidence
                except Exception as exc:
                    logger.error("Verification failed for %s: %s", fallback_email, exc)
                    email = fallback_email
                    overall_status = "unverified"
                    smtp_status = "error"
                    confidence = "low"

                df.at[row_idx, status_column] = overall_status
                df.at[row_idx, detail_smtp_column] = smtp_status
                df.at[row_idx, detail_conf_column] = confidence

                if overall_status == "verified" or log_all_results:
                    logger.info(
                        "[%d/%d] %s -> %s (smtp=%s, confidence=%s)",
                        completed,
                        total,
                        email,
                        overall_status,
                        smtp_status,
                        confidence,
                    )
                elif completed % 25 == 0:
                    logger.info("Progress: %d/%d checked", completed, total)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path, index=False, encoding="utf-8-sig")

    logger.info("Saved verified file: %s", output_path)
    if status_column in df.columns:
        counts = (
            df[status_column]
            .fillna("unverified")
            .replace("", "unverified")
            .value_counts()
        )
        logger.info("Verification status counts:\n%s", counts.to_string())

    elapsed = time.monotonic() - started_at
    if emails_to_verify:
        logger.info(
            "Completed in %.1fs (%.2f emails/sec)",
            elapsed,
            len(emails_to_verify) / elapsed if elapsed > 0 else 0.0,
        )
    else:
        logger.info("Completed in %.1fs", elapsed)


def main() -> None:
    """CLI entry point."""
    args = parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )

    input_path = Path(args.input)
    output_path = Path(args.output) if args.output else default_output_path(input_path)

    try:
        verify_emails_in_csv(
            input_path=input_path,
            output_path=output_path,
            email_column=args.email_column,
            status_column=args.status_column,
            max_concurrent=max(1, args.max_concurrent),
            smtp_timeout=max(1, args.smtp_timeout),
            rate_limit=max(0.1, args.rate_limit),
            detect_catch_all=args.detect_catch_all,
            log_all_results=args.log_all_results,
        )
    except Exception as exc:
        logger.error("Email verification failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
