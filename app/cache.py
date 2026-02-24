"""SQLite caching module for enrichment results."""

import sqlite3
import json
import logging
import hashlib
from pathlib import Path
from typing import Optional, Dict, Any, Set
from datetime import datetime, timedelta
from contextlib import contextmanager

logger = logging.getLogger(__name__)

# Domain denylist - these domains should never be crawled/enriched
# They are typically social media, shortlinks, or non-business domains
DOMAIN_DENYLIST = {
    # Social media
    'facebook.com', 'instagram.com', 'twitter.com', 'x.com',
    'linkedin.com', 'tiktok.com', 'youtube.com', 'pinterest.com',
    'snapchat.com', 'reddit.com', 'tumblr.com', 'threads.net',
    'xing.com',  # Popular in DACH region
    # URL shorteners
    'bit.ly', 'goo.gl', 't.co', 'tinyurl.com', 'ow.ly', 'is.gd',
    # Review/directory sites
    'yelp.com', 'tripadvisor.com', 'tripadvisor.de', 'tripadvisor.fr',
    'tripadvisor.it', 'tripadvisor.es',
    'trustpilot.com', 'trustpilot.de',
    'google.com', 'google.de', 'google.fr', 'google.it', 'google.es',
    'google.nl', 'google.pl', 'google.pt', 'google.at', 'google.ch',
    'kununu.com',  # DE employer reviews
    'jameda.de',   # DE health reviews
    'golocal.de',  # DE local reviews
    'iens.nl',     # NL restaurant reviews
    'thefork.com', 'lafourchette.com',  # Restaurant booking EU
    # Yellow pages / directories
    'gelbeseiten.de', 'dasoertliche.de', 'meinestadt.de',
    'pagesjaunes.fr', 'paginasmarillas.es', 'paginegialle.it',
    'goudengids.be', 'goldenpages.ie',
    'herold.at',    # AT directory
    'local.ch', 'search.ch',  # CH directories
    'telefoongids.nl', 'detelefoongids.nl',  # NL
    'panoramafirm.pl', 'pkt.pl',  # PL directories
    'pai.pt',       # PT directory
    # E-commerce platforms
    'amazon.com', 'amazon.de', 'amazon.fr', 'amazon.it', 'amazon.es',
    'ebay.com', 'ebay.de', 'ebay.fr', 'ebay.it',
    'etsy.com', 'aliexpress.com', 'alibaba.com',
    'bol.com',      # NL/BE
    'allegro.pl',   # PL
    'cdiscount.fr', # FR
    'idealo.de',    # DE price comparison
    'marktplaats.nl',  # NL marketplace
    # Template/hosting (rarely useful for enrichment)
    'wix.com', 'wixsite.com', 'wordpress.com', 'squarespace.com',
    'weebly.com', 'jimdo.com', 'strato.de', 'ionos.de',
    'webflow.io', 'shopify.com',
    # Maps
    'maps.google.com', 'maps.apple.com', 'openstreetmap.org',
    # Generic
    'example.com', 'test.com', 'localhost',
}


def is_domain_denylisted(domain: str) -> bool:
    """Check if domain is in denylist or is a subdomain of a denylisted domain."""
    if not domain:
        return True
    
    domain_lower = domain.lower().strip()
    
    # Remove protocol if present
    if domain_lower.startswith(('http://', 'https://')):
        domain_lower = domain_lower.split('://', 1)[1]
    
    # Remove www. prefix
    if domain_lower.startswith('www.'):
        domain_lower = domain_lower[4:]
    
    # Remove path if present
    domain_lower = domain_lower.split('/')[0]
    
    # Check exact match
    if domain_lower in DOMAIN_DENYLIST:
        return True
    
    # Check if it's a subdomain of a denylisted domain
    for denylisted in DOMAIN_DENYLIST:
        if domain_lower.endswith('.' + denylisted) or domain_lower == denylisted:
            return True
    
    return False


class EnrichmentCache:
    """SQLite-based cache for enrichment results with failure tracking."""
    
    def __init__(self, cache_path: str = './cache/cache.sqlite'):
        self.cache_path = Path(cache_path)
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
    
    def _init_db(self) -> None:
        """Initialize SQLite database with tables."""
        with self._get_connection() as conn:
            # Check if table exists
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='enrichment_cache'"
            )
            table_exists = cursor.fetchone() is not None
            
            if table_exists:
                # Check if 'status' column exists (old schema migration)
                cursor = conn.execute("PRAGMA table_info(enrichment_cache)")
                columns = [row[1] for row in cursor.fetchall()]
                
                if 'status' not in columns:
                    logger.warning("Old cache schema detected. Migrating to new schema with status tracking...")
                    # Backup old data
                    conn.execute('ALTER TABLE enrichment_cache RENAME TO enrichment_cache_old')
                    
                    # Create new table with status column
                    conn.execute('''
                        CREATE TABLE enrichment_cache (
                            key TEXT PRIMARY KEY,
                            domain TEXT NOT NULL,
                            provider TEXT NOT NULL,
                            status TEXT DEFAULT 'success',
                            result TEXT,
                            error_reason TEXT,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            expires_at TIMESTAMP
                        )
                    ''')
                    
                    # Migrate old data (assume all old entries were successful)
                    conn.execute('''
                        INSERT INTO enrichment_cache (key, domain, provider, status, result, created_at, expires_at)
                        SELECT key, domain, provider, 'success', result, created_at, expires_at
                        FROM enrichment_cache_old
                    ''')
                    
                    # Drop old table
                    conn.execute('DROP TABLE enrichment_cache_old')
                    logger.info("Cache migration complete. Old successful entries preserved.")
            else:
                # Create new table
                conn.execute('''
                    CREATE TABLE enrichment_cache (
                        key TEXT PRIMARY KEY,
                        domain TEXT NOT NULL,
                        provider TEXT NOT NULL,
                        status TEXT DEFAULT 'success',
                        result TEXT,
                        error_reason TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        expires_at TIMESTAMP
                    )
                ''')
            
            # Create indexes
            conn.execute('''
                CREATE INDEX IF NOT EXISTS idx_domain_provider 
                ON enrichment_cache(domain, provider)
            ''')
            
            conn.execute('''
                CREATE INDEX IF NOT EXISTS idx_expires 
                ON enrichment_cache(expires_at)
            ''')
            
            conn.execute('''
                CREATE INDEX IF NOT EXISTS idx_status 
                ON enrichment_cache(status)
            ''')
            
            conn.commit()
    
    @contextmanager
    def _get_connection(self):
        """Get database connection with proper error handling."""
        conn = sqlite3.connect(str(self.cache_path), timeout=30)
        try:
            yield conn
        finally:
            conn.close()
    
    def _generate_key(self, domain: str, provider: str) -> str:
        """Generate cache key from domain and provider."""
        key_string = f"{domain}:{provider}"
        return hashlib.md5(key_string.encode()).hexdigest()
    
    def get(self, domain: str, provider: str, max_age_days: int = 30) -> Optional[Dict[str, Any]]:
        """
        Get cached enrichment result.
        
        Args:
            domain: Website domain
            provider: Provider name
            max_age_days: Maximum age of cached entry
            
        Returns:
            Cached result dict or None if not found/expired
            For failures, returns dict with 'status': 'failed'
        """
        key = self._generate_key(domain, provider)
        
        with self._get_connection() as conn:
            cursor = conn.execute(
                '''
                SELECT result, status, error_reason, created_at 
                FROM enrichment_cache 
                WHERE key = ? AND (expires_at IS NULL OR expires_at > datetime('now'))
                ''',
                (key,)
            )
            
            row = cursor.fetchone()
            if row:
                result_json, status, error_reason, created_at = row
                # Check age
                created = datetime.fromisoformat(created_at.replace('Z', '+00:00') if 'Z' in created_at else created_at)
                # Use naive datetime for comparison (SQLite stores UTC via CURRENT_TIMESTAMP)
                if created.tzinfo is not None:
                    created = created.replace(tzinfo=None)
                if datetime.utcnow() - created < timedelta(days=max_age_days):
                    try:
                        if result_json:
                            data = json.loads(result_json)
                        else:
                            data = {}
                        data['cached_status'] = status
                        data['cached_error_reason'] = error_reason
                        return data
                    except json.JSONDecodeError:
                        logger.warning(f"Failed to decode cached result for {domain}")
        
        return None
    
    def set(
        self, 
        domain: str, 
        provider: str, 
        result: Dict[str, Any],
        status: str = 'success',
        error_reason: Optional[str] = None,
        ttl_days: Optional[int] = None
    ) -> None:
        """
        Cache enrichment result.
        
        Args:
            domain: Website domain
            provider: Provider name
            result: Result dict to cache (can be empty for failures)
            status: 'success', 'failed', or 'denylisted'
            error_reason: Why it failed (e.g., 'ssl_error', 'timeout', 'denylisted')
            ttl_days: Optional TTL in days (defaults based on status)
        """
        key = self._generate_key(domain, provider)
        
        # Default TTL based on status
        if ttl_days is None:
            if status == 'success':
                ttl_days = 30  # Success cached for 30 days
            elif status == 'denylisted':
                ttl_days = 90  # Denylisted cached for 90 days
            else:
                ttl_days = 7   # Failures cached for 7 days
        
        expires_at = (datetime.now() + timedelta(days=ttl_days)).isoformat()
        
        with self._get_connection() as conn:
            conn.execute(
                '''
                INSERT OR REPLACE INTO enrichment_cache 
                (key, domain, provider, status, result, error_reason, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    key,
                    domain,
                    provider,
                    status,
                    json.dumps(result) if result else None,
                    error_reason,
                    expires_at
                )
            )
            conn.commit()
    
    def set_failure(
        self,
        domain: str,
        provider: str,
        error_reason: str,
        ttl_days: int = 7
    ) -> None:
        """
        Cache a failed enrichment attempt.
        
        Args:
            domain: Website domain
            provider: Provider name
            error_reason: Type of failure (ssl_error, timeout, etc.)
            ttl_days: How long to remember this failure (default 7 days)
        """
        self.set(domain, provider, {}, status='failed', error_reason=error_reason, ttl_days=ttl_days)
        logger.debug(f"Cached failure for {domain}: {error_reason}")
    
    def set_denylisted(
        self,
        domain: str,
        provider: str,
        ttl_days: int = 90
    ) -> None:
        """Cache a denylisted domain."""
        self.set(domain, provider, {}, status='denylisted', error_reason='domain_denylisted', ttl_days=ttl_days)
        logger.debug(f"Cached denylist for {domain}")
    
    def is_cached_failure(self, domain: str, provider: str) -> bool:
        """Check if domain is a cached failure (and not expired)."""
        cached = self.get(domain, provider)
        if cached and cached.get('cached_status') in ('failed', 'denylisted'):
            return True
        return False
    
    def delete(self, domain: str, provider: str) -> None:
        """Delete cached entry."""
        key = self._generate_key(domain, provider)
        
        with self._get_connection() as conn:
            conn.execute(
                'DELETE FROM enrichment_cache WHERE key = ?',
                (key,)
            )
            conn.commit()
    
    def clear_expired(self) -> int:
        """Clear expired cache entries. Returns count deleted."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM enrichment_cache WHERE expires_at < datetime('now')"
            )
            conn.commit()
            return cursor.rowcount
    
    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                '''SELECT 
                    COUNT(*), 
                    COUNT(DISTINCT domain), 
                    COUNT(DISTINCT provider),
                    SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN status = 'denylisted' THEN 1 ELSE 0 END)
                FROM enrichment_cache'''
            )
            total, unique_domains, unique_providers, success_count, failed_count, denylist_count = cursor.fetchone()
            
            cursor = conn.execute(
                "SELECT COUNT(*) FROM enrichment_cache WHERE expires_at < datetime('now')"
            )
            expired = cursor.fetchone()[0]
            
            return {
                'total_entries': total or 0,
                'unique_domains': unique_domains or 0,
                'unique_providers': unique_providers or 0,
                'success_count': success_count or 0,
                'failed_count': failed_count or 0,
                'denylist_count': denylist_count or 0,
                'expired_entries': expired or 0,
                'cache_path': str(self.cache_path)
            }
    
    def clear_all(self) -> int:
        """Clear all cache entries. Returns count deleted."""
        with self._get_connection() as conn:
            cursor = conn.execute('DELETE FROM enrichment_cache')
            conn.commit()
            return cursor.rowcount
    
    def retry_failed(self, domain: str, provider: str) -> bool:
        """
        Force retry of a failed domain by deleting its failure cache entry.
        Returns True if an entry was deleted.
        """
        if self.is_cached_failure(domain, provider):
            self.delete(domain, provider)
            logger.info(f"Cleared failure cache for {domain}, will retry on next run")
            return True
        return False


class RateLimiter:
    """Rate limiter for API calls."""
    
    def __init__(self, calls_per_second: float = 3.0):
        """
        Initialize rate limiter.
        
        Args:
            calls_per_second: Maximum calls per second
        """
        import threading
        import time
        
        self.min_interval = 1.0 / calls_per_second if calls_per_second > 0 else 0
        self.last_call_time = 0
        self.lock = threading.Lock()
        self.time_module = time
    
    def wait(self) -> None:
        """Wait if necessary to comply with rate limit."""
        with self.lock:
            current_time = self.time_module.time()
            elapsed = current_time - self.last_call_time
            
            if elapsed < self.min_interval:
                sleep_time = self.min_interval - elapsed
                self.time_module.sleep(sleep_time)
            
            self.last_call_time = self.time_module.time()


# Global cache instance
_cache_instance: Optional[EnrichmentCache] = None


def get_cache(cache_path: str = './cache/cache.sqlite') -> EnrichmentCache:
    """Get or create global cache instance."""
    global _cache_instance
    if _cache_instance is None:
        _cache_instance = EnrichmentCache(cache_path)
    return _cache_instance
