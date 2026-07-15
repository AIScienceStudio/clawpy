"""Dynamic account pool — rotates between multiple Claude subscription accounts.

Reads accounts from claude-swap's credential store and picks the one with
the most rate limit headroom. On 429, automatically switches to the next account.

Usage:
    from clawpy.auth.account_pool import AccountPool

    pool = AccountPool()
    token = pool.get_best_token()  # Returns token with most headroom
    pool.mark_rate_limited(token)  # On 429, mark current token as exhausted
"""

from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_SWAP_DATA_DIR = Path.home() / ".local" / "share" / "claude-swap"
_CREDS_DIR = _SWAP_DATA_DIR / "credentials"
_SEQUENCE_FILE = _SWAP_DATA_DIR / "sequence.json"
_USAGE_CACHE = _SWAP_DATA_DIR / "cache" / "usage.json"


@dataclass
class Account:
    slot: int
    email: str
    access_token: str
    refresh_token: str
    expires_at: float
    rate_limited_until: float = 0.0
    five_hour_pct: float = 0.0
    seven_day_pct: float = 0.0

    @property
    def is_rate_limited(self) -> bool:
        return time.time() < self.rate_limited_until

    @property
    def is_expired(self) -> bool:
        return time.time() * 1000 >= self.expires_at - 300_000

    @property
    def headroom_score(self) -> float:
        """Lower is better — accounts with less usage get priority."""
        if self.is_rate_limited:
            return 999.0
        return self.five_hour_pct * 2 + self.seven_day_pct


class AccountPool:
    """Manages multiple Claude subscription accounts for optimal rotation."""

    def __init__(self):
        self._accounts: list[Account] = []
        self._current_idx: int = 0
        self._last_reload: float = 0.0
        self._load_accounts()

    def _maybe_reload(self):
        """Reload credentials from disk every 30 min to pick up refreshed tokens."""
        if time.time() - self._last_reload > 1800:
            logger.info("Reloading account credentials from claude-swap")
            self._accounts.clear()
            self._load_accounts()

    def _load_accounts(self):
        """Load all accounts from claude-swap credential store."""
        self._last_reload = time.time()
        if not _SEQUENCE_FILE.exists():
            logger.warning("claude-swap not configured — no sequence.json found")
            return

        try:
            sequence = json.loads(_SEQUENCE_FILE.read_text())
        except Exception as e:
            logger.error(f"Failed to read sequence.json: {e}")
            return

        accounts_meta = sequence.get("accounts", {})
        active_slot = sequence.get("activeAccountNumber")
        usage_data = self._load_usage_cache()

        # Also read the live credentials (always fresh for active account)
        live_oauth = {}
        live_creds_path = Path.home() / ".claude" / ".credentials.json"
        try:
            if live_creds_path.exists():
                live_data = json.loads(live_creds_path.read_text())
                live_oauth = live_data.get("claudeAiOauth", {})
        except Exception:
            pass

        for slot_str, meta in accounts_meta.items():
            slot = int(slot_str)
            email = meta.get("email", f"account-{slot}")

            # For the active account, prefer live credentials (auto-refreshed)
            if slot == active_slot and live_oauth.get("accessToken"):
                oauth = live_oauth
            else:
                cred_file = _CREDS_DIR / f".creds-{slot}-{email}.enc"
                if not cred_file.exists():
                    logger.warning(f"Credential file missing for slot {slot}: {cred_file}")
                    continue
                try:
                    raw = base64.b64decode(cred_file.read_text())
                    creds = json.loads(raw)
                    oauth = creds.get("claudeAiOauth", {})
                except Exception as e:
                    logger.error(f"Failed to load credentials for slot {slot}: {e}")
                    continue

            account = Account(
                slot=slot,
                email=email,
                access_token=oauth.get("accessToken", ""),
                refresh_token=oauth.get("refreshToken", ""),
                expires_at=oauth.get("expiresAt", 0),
            )

            # Skip expired tokens
            if account.is_expired:
                logger.warning(f"Skipping account {slot} ({email}): token expired")
                continue

            # Load usage stats from cache
            slot_usage = usage_data.get(slot_str, {})
            account.five_hour_pct = slot_usage.get("five_hour", {}).get("pct", 0.0)
            account.seven_day_pct = slot_usage.get("seven_day", {}).get("pct", 0.0)

            if account.access_token:
                self._accounts.append(account)
                logger.info(f"Loaded account {slot}: {email} (5h: {account.five_hour_pct}%, 7d: {account.seven_day_pct}%)")

        if self._accounts:
            self._accounts.sort(key=lambda a: a.headroom_score)
            logger.info(f"Account pool ready: {len(self._accounts)} accounts, best: {self._accounts[0].email}")

    def _load_usage_cache(self) -> dict:
        """Load usage stats from claude-swap's cache."""
        if not _USAGE_CACHE.exists():
            return {}
        try:
            cache = json.loads(_USAGE_CACHE.read_text())
            return cache.get("data", {})
        except Exception:
            return {}

    def refresh_usage(self):
        """Reload usage data from cache (call periodically)."""
        usage_data = self._load_usage_cache()
        for account in self._accounts:
            slot_usage = usage_data.get(str(account.slot), {})
            account.five_hour_pct = slot_usage.get("five_hour", {}).get("pct", 0.0)
            account.seven_day_pct = slot_usage.get("seven_day", {}).get("pct", 0.0)
        self._accounts.sort(key=lambda a: a.headroom_score)

    @property
    def available_accounts(self) -> list[Account]:
        return [a for a in self._accounts if not a.is_rate_limited]

    def get_best_token(self) -> Optional[str]:
        """Get the access token for the account with most headroom."""
        self._maybe_reload()
        available = self.available_accounts
        if not available:
            # All rate limited — return the one that recovers soonest
            if self._accounts:
                soonest = min(self._accounts, key=lambda a: a.rate_limited_until)
                logger.warning(f"All accounts rate-limited, using {soonest.email} (recovers in {int(soonest.rate_limited_until - time.time())}s)")
                return soonest.access_token
            return None

        best = available[0]
        logger.debug(f"Using account: {best.email} (5h: {best.five_hour_pct}%, 7d: {best.seven_day_pct}%)")
        return best.access_token

    def get_next_token(self, current_token: str) -> Optional[str]:
        """Get the next available token (different from current). Used on 429."""
        others = [a for a in self._accounts if a.access_token != current_token and not a.is_rate_limited]
        if others:
            others.sort(key=lambda a: a.headroom_score)
            return others[0].access_token
        return None

    def mark_rate_limited(self, token: str, cooldown_seconds: int = 300):
        """Mark an account as rate-limited after receiving 429."""
        for account in self._accounts:
            if account.access_token == token:
                account.rate_limited_until = time.time() + cooldown_seconds
                logger.warning(f"Account {account.email} rate-limited for {cooldown_seconds}s")
                break

    def mark_unauthorized(self, token: str):
        """Handle 401 — token expired. Force reload credentials from disk."""
        logger.warning("Got 401, forcing credential reload from claude-swap")
        self._last_reload = 0  # Force reload on next get_best_token()
        self._accounts.clear()
        self._load_accounts()

    def get_status(self) -> dict:
        """Get pool status for monitoring."""
        return {
            "total_accounts": len(self._accounts),
            "available": len(self.available_accounts),
            "accounts": [
                {
                    "slot": a.slot,
                    "email": a.email,
                    "five_hour_pct": a.five_hour_pct,
                    "seven_day_pct": a.seven_day_pct,
                    "is_rate_limited": a.is_rate_limited,
                    "headroom_score": a.headroom_score,
                }
                for a in self._accounts
            ],
        }


# Singleton instance
_pool: Optional[AccountPool] = None


def get_pool() -> AccountPool:
    """Get or create the global account pool singleton."""
    global _pool
    if _pool is None:
        _pool = AccountPool()
    return _pool
