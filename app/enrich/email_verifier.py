"""Free email verification via DNS MX lookups and SMTP RCPT TO checks."""

import re
import time
import uuid
import socket
import smtplib
import logging
import threading
from typing import Dict, List, Optional
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed

import dns.resolver

logger = logging.getLogger(__name__)

# Sender identity used for SMTP HELO and MAIL FROM
_SENDER_ADDRESS = "verify@enrichment-pipeline.local"
_SENDER_DOMAIN = "enrichment-pipeline.local"

# RFC 5322 simplified email regex — covers the vast majority of real-world addresses
_EMAIL_RE = re.compile(
    r"^[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+"
    r"@[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
    r"(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*"
    r"\.[a-zA-Z]{2,}$"
)


@dataclass
class EmailVerificationResult:
    """Result of verifying a single email address.

    Attributes:
        email: The email address that was verified.
        is_valid_syntax: Whether the address passes RFC-style syntax validation.
        has_mx_record: Whether the domain has at least one MX record.
        smtp_status: Result of the SMTP RCPT TO probe.
            One of: verified, unverified, bounced, catch_all, timeout, error.
        overall_status: Aggregate verdict.
            One of: verified, likely_valid, unverified, invalid.
        confidence: Confidence in *overall_status*.
            One of: high, medium, low.
    """

    email: str
    is_valid_syntax: bool = False
    has_mx_record: bool = False
    smtp_status: str = "unverified"      # verified | unverified | bounced | catch_all | timeout | error
    overall_status: str = "unverified"   # verified | likely_valid | unverified | invalid
    confidence: str = "low"              # high | medium | low


@dataclass
class _MXCacheEntry:
    """Internal cache entry for MX lookup results."""

    has_mx: bool
    mx_hosts: List[str] = field(default_factory=list)


class EmailVerifier:
    """Verify email addresses using DNS MX lookups and SMTP RCPT TO probes.

    Args:
        smtp_timeout: Seconds to wait for each SMTP operation.
        rate_limit: Maximum SMTP checks per second (across all threads).
        enable_smtp: If False, skip SMTP checks and rely on MX-only validation.
        detect_catch_all: If True, probe a random address to detect catch-all domains.
    """

    def __init__(
        self,
        smtp_timeout: int = 10,
        rate_limit: float = 2.0,
        enable_smtp: bool = True,
        detect_catch_all: bool = True,
    ):
        self.smtp_timeout = smtp_timeout
        self.rate_limit = rate_limit
        self.enable_smtp = enable_smtp
        self.detect_catch_all = detect_catch_all

        # Thread-safe MX cache: domain -> _MXCacheEntry
        self._mx_cache: Dict[str, _MXCacheEntry] = {}
        self._mx_cache_lock = threading.Lock()

        # Thread-safe catch-all cache: domain -> bool (True = catch-all)
        self._catch_all_cache: Dict[str, bool] = {}
        self._catch_all_lock = threading.Lock()

        # Rate-limiter: enforce minimum interval between SMTP connections
        self._rate_interval = 1.0 / rate_limit if rate_limit > 0 else 0.0
        self._last_smtp_time = 0.0
        self._rate_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def verify_email(self, email: str) -> EmailVerificationResult:
        """Verify a single email address.

        Runs syntax check, MX lookup, and (optionally) SMTP RCPT TO probe.

        Args:
            email: The email address to verify.

        Returns:
            An EmailVerificationResult with all fields populated.
        """
        email = email.strip().lower()
        result = EmailVerificationResult(email=email)

        # Step 1 — Syntax
        result.is_valid_syntax = self._check_syntax(email)
        if not result.is_valid_syntax:
            result.overall_status = "invalid"
            result.confidence = "high"
            logger.debug("Syntax invalid: %s", email)
            return result

        domain = email.rsplit("@", 1)[1]

        # Step 2 — MX lookup
        result.has_mx_record = self.check_mx(domain)
        if not result.has_mx_record:
            result.overall_status = "invalid"
            result.confidence = "high"
            logger.debug("No MX records for domain: %s", domain)
            return result

        # Step 3 — SMTP probe (optional)
        if not self.enable_smtp:
            result.smtp_status = "unverified"
            result.overall_status = "likely_valid"
            result.confidence = "medium"
            return result

        smtp_status = self.check_smtp(email)
        result.smtp_status = smtp_status

        # Step 4 — Derive overall status
        result.overall_status, result.confidence = self._derive_status(smtp_status)
        return result

    def verify_batch(
        self,
        emails: List[str],
        max_concurrent: int = 5,
    ) -> List[EmailVerificationResult]:
        """Verify a list of email addresses concurrently.

        Args:
            emails: Email addresses to verify.
            max_concurrent: Maximum number of parallel verification threads.

        Returns:
            A list of EmailVerificationResult in the same order as *emails*.
        """
        if not emails:
            return []

        results: Dict[int, EmailVerificationResult] = {}

        with ThreadPoolExecutor(max_workers=max_concurrent) as pool:
            future_to_idx = {
                pool.submit(self.verify_email, email): idx
                for idx, email in enumerate(emails)
            }
            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                try:
                    results[idx] = future.result()
                except Exception as exc:
                    logger.error("Verification failed for email at index %d: %s", idx, exc)
                    results[idx] = EmailVerificationResult(
                        email=emails[idx],
                        smtp_status="error",
                        overall_status="unverified",
                        confidence="low",
                    )

        return [results[i] for i in range(len(emails))]

    def check_mx(self, domain: str) -> bool:
        """Check whether *domain* has at least one MX record (cached).

        Args:
            domain: The domain part of an email address.

        Returns:
            True if one or more MX records were found.
        """
        domain = domain.lower()

        with self._mx_cache_lock:
            if domain in self._mx_cache:
                return self._mx_cache[domain].has_mx

        # Perform DNS query outside the lock to avoid blocking other threads
        mx_hosts: List[str] = []
        has_mx = False
        try:
            answers = dns.resolver.resolve(domain, "MX")
            mx_hosts = [
                str(rdata.exchange).rstrip(".")
                for rdata in sorted(answers, key=lambda r: r.preference)
            ]
            has_mx = len(mx_hosts) > 0
            logger.debug("MX records for %s: %s", domain, mx_hosts)
        except dns.resolver.NoAnswer:
            logger.debug("No MX answer for %s, trying A record fallback", domain)
            has_mx = self._has_a_record(domain)
            if has_mx:
                mx_hosts = [domain]
        except dns.resolver.NXDOMAIN:
            logger.debug("Domain does not exist: %s", domain)
        except dns.resolver.NoNameservers:
            logger.warning("No nameservers reachable for %s", domain)
        except dns.resolver.LifetimeTimeout:
            logger.warning("DNS timeout for %s", domain)
        except Exception as exc:
            logger.warning("DNS lookup error for %s: %s", domain, exc)

        entry = _MXCacheEntry(has_mx=has_mx, mx_hosts=mx_hosts)
        with self._mx_cache_lock:
            self._mx_cache[domain] = entry

        return has_mx

    def check_smtp(self, email: str) -> str:
        """Perform an SMTP RCPT TO probe for *email*.

        Args:
            email: A syntactically valid email address.

        Returns:
            One of: "verified", "bounced", "catch_all", "timeout", "error".
        """
        domain = email.rsplit("@", 1)[1]
        mx_hosts = self._get_mx_hosts(domain)

        if not mx_hosts:
            return "error"

        # Rate-limit SMTP connections
        self._rate_wait()

        for mx_host in mx_hosts:
            status = self._smtp_probe(email, mx_host)
            if status is not None:
                # If the mailbox was accepted, check for catch-all behaviour
                if status == "verified" and self.detect_catch_all:
                    if self._is_catch_all(domain, mx_host):
                        return "catch_all"
                return status

        # All MX hosts failed — fall back gracefully
        logger.debug("All MX hosts unreachable for %s, falling back", email)
        return "error"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _check_syntax(email: str) -> bool:
        """Return True if *email* looks syntactically valid."""
        if not email or len(email) > 254:
            return False
        if "@" not in email:
            return False
        local, _, domain = email.rpartition("@")
        if not local or len(local) > 64:
            return False
        if not domain:
            return False
        return _EMAIL_RE.match(email) is not None

    @staticmethod
    def _has_a_record(domain: str) -> bool:
        """Fallback: check if the domain has an A record (implicit MX)."""
        try:
            dns.resolver.resolve(domain, "A")
            return True
        except Exception:
            return False

    def _get_mx_hosts(self, domain: str) -> List[str]:
        """Return cached MX hosts for *domain*, running a lookup if needed."""
        domain = domain.lower()
        with self._mx_cache_lock:
            entry = self._mx_cache.get(domain)
        if entry is not None:
            return entry.mx_hosts

        # Populate cache via check_mx
        self.check_mx(domain)

        with self._mx_cache_lock:
            entry = self._mx_cache.get(domain)
        return entry.mx_hosts if entry else []

    def _rate_wait(self) -> None:
        """Block until the rate limiter allows the next SMTP connection."""
        with self._rate_lock:
            now = time.monotonic()
            elapsed = now - self._last_smtp_time
            if elapsed < self._rate_interval:
                sleep_for = self._rate_interval - elapsed
                time.sleep(sleep_for)
            self._last_smtp_time = time.monotonic()

    def _smtp_probe(self, email: str, mx_host: str) -> Optional[str]:
        """Try a single SMTP RCPT TO against *mx_host*.

        Returns:
            "verified"  — 250 response to RCPT TO
            "bounced"   — 550 / 551 / 552 / 553 / mailbox-not-found response
            "timeout"   — connection or command timed out
            None        — transient failure (try next MX host)
        """
        smtp: Optional[smtplib.SMTP] = None
        try:
            smtp = smtplib.SMTP(timeout=self.smtp_timeout)
            smtp.connect(mx_host, 25)
            smtp.ehlo_or_helo_if_needed()

            # Some servers require a valid EHLO hostname
            smtp.ehlo(_SENDER_DOMAIN)

            code_mail, _ = smtp.mail(_SENDER_ADDRESS)
            if code_mail != 250:
                logger.debug("MAIL FROM rejected by %s (code %d)", mx_host, code_mail)
                return None

            code_rcpt, msg_rcpt = smtp.rcpt(email)
            msg_text = msg_rcpt.decode("utf-8", errors="replace") if isinstance(msg_rcpt, bytes) else str(msg_rcpt)

            if code_rcpt == 250:
                logger.debug("RCPT TO accepted for %s at %s", email, mx_host)
                return "verified"

            if code_rcpt in (550, 551, 552, 553, 554):
                logger.debug("RCPT TO bounced for %s at %s (code %d: %s)", email, mx_host, code_rcpt, msg_text)
                return "bounced"

            # 450/451/452 — greylisting or temporary rejection
            if 400 <= code_rcpt < 500:
                logger.debug(
                    "Temporary rejection for %s at %s (code %d: %s), treating as greylisting",
                    email, mx_host, code_rcpt, msg_text,
                )
                # Retry once after a short pause for greylisting
                time.sleep(2)
                self._rate_wait()
                code_rcpt2, msg_rcpt2 = smtp.rcpt(email)
                if code_rcpt2 == 250:
                    return "verified"
                if code_rcpt2 in (550, 551, 552, 553, 554):
                    return "bounced"
                # Still temporary — can't determine, try next MX
                return None

            # Unexpected code
            logger.debug("Unexpected RCPT code %d from %s for %s", code_rcpt, mx_host, email)
            return None

        except smtplib.SMTPServerDisconnected:
            logger.debug("Server %s disconnected during probe for %s", mx_host, email)
            return None
        except (socket.timeout, TimeoutError):
            logger.debug("Timeout connecting to %s for %s", mx_host, email)
            return "timeout"
        except ConnectionRefusedError:
            logger.debug("Connection refused by %s for %s", mx_host, email)
            return None
        except OSError as exc:
            logger.debug("Network error with %s for %s: %s", mx_host, email, exc)
            return None
        except smtplib.SMTPException as exc:
            logger.debug("SMTP error with %s for %s: %s", mx_host, email, exc)
            return None
        except Exception as exc:
            logger.error("Unexpected error probing %s via %s: %s", email, mx_host, exc)
            return None
        finally:
            if smtp is not None:
                try:
                    smtp.quit()
                except Exception:
                    try:
                        smtp.close()
                    except Exception:
                        pass

    def _is_catch_all(self, domain: str, mx_host: str) -> bool:
        """Detect catch-all domains by probing a random, nonexistent address.

        Results are cached per domain.
        """
        with self._catch_all_lock:
            if domain in self._catch_all_cache:
                return self._catch_all_cache[domain]

        # Generate a highly unlikely local-part
        random_local = f"enrichment-verify-{uuid.uuid4().hex[:12]}"
        probe_email = f"{random_local}@{domain}"

        self._rate_wait()
        status = self._smtp_probe(probe_email, mx_host)
        is_catch_all = status == "verified"

        if is_catch_all:
            logger.info("Catch-all domain detected: %s", domain)

        with self._catch_all_lock:
            self._catch_all_cache[domain] = is_catch_all

        return is_catch_all

    @staticmethod
    def _derive_status(smtp_status: str) -> tuple:
        """Map SMTP status to (overall_status, confidence).

        Returns:
            Tuple of (overall_status, confidence).
        """
        mapping = {
            "verified":   ("verified",     "high"),
            "bounced":    ("invalid",      "high"),
            "catch_all":  ("likely_valid",  "low"),
            "timeout":    ("likely_valid",  "medium"),
            "error":      ("likely_valid",  "medium"),
            "unverified": ("likely_valid",  "medium"),
        }
        return mapping.get(smtp_status, ("unverified", "low"))
