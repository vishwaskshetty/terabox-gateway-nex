"""Server-side Resolver Session Store for TeraBox Gateway.

Manages temporary resolver sessions when TeraBox requires interactive or token verification.
Session cookies and headers are maintained server-side and never exposed to clients.
"""

import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse


logger = logging.getLogger("terabox_gateway")

SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "600"))  # 10 minutes default
MAX_ACTIVE_SESSIONS = int(os.getenv("MAX_ACTIVE_SESSIONS", "1000"))


@dataclass
class ResolverSession:
    """Represents an active server-side resolution session."""
    session_id: str
    url: str
    surl: str
    verification_url: str
    password: str = ""
    cookies: Dict[str, str] = field(default_factory=dict)
    user_agent: str = ""
    referer: str = ""
    target_domain: str = ""
    challenge_state: Dict[str, Any] = field(default_factory=dict)
    verification_state: str = "pending"  # "pending", "completed", "expired", "failed"
    created_at: float = field(default_factory=time.time)
    expires_at: float = field(default_factory=lambda: time.time() + SESSION_TTL_SECONDS)
    files: Optional[List[Dict[str, Any]]] = None

    def is_expired(self) -> bool:
        return time.time() >= self.expires_at

    def time_remaining_seconds(self) -> int:
        remaining = int(self.expires_at - time.time())
        return max(0, remaining)


class SessionStore:
    """Thread-safe in-memory session store with TTL eviction."""
    def __init__(self):
        self._sessions: Dict[str, ResolverSession] = {}
        self._lock = threading.Lock()

    def create_session(
        self,
        url: str,
        surl: str,
        verification_url: str,
        password: str = "",
        cookies: Optional[Dict[str, str]] = None,
        user_agent: str = "",
        referer: str = "",
        challenge_state: Optional[Dict[str, Any]] = None,
        files: Optional[List[Dict[str, Any]]] = None,
    ) -> ResolverSession:
        """Create and store a new ResolverSession."""
        session_id = secrets.token_urlsafe(24)
        target_domain = urlparse(url).netloc or "terabox.app"
        
        session = ResolverSession(
            session_id=session_id,
            url=url,
            surl=surl,
            verification_url=verification_url,
            password=password,
            cookies=cookies or {},
            user_agent=user_agent,
            referer=referer or f"https://{target_domain}/",
            target_domain=target_domain,
            challenge_state=challenge_state or {},
            files=files,
        )

        with self._lock:
            # Clean up expired sessions periodically
            self._purge_expired_locked()
            
            if len(self._sessions) >= MAX_ACTIVE_SESSIONS:
                # Evict oldest
                oldest_id = min(self._sessions, key=lambda k: self._sessions[k].created_at)
                del self._sessions[oldest_id]
                
            self._sessions[session_id] = session

        logger.info(
            f"[TeraBox SessionStore] stage=verification_session_created "
            f"sessionId={session_id[:8]}*** targetDomain={target_domain} "
            f"ttlSeconds={SESSION_TTL_SECONDS}"
        )
        return session

    def get_session(self, session_id: str) -> Optional[ResolverSession]:
        """Retrieve a session by ID if it has not expired."""
        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                return None
            if session.is_expired():
                del self._sessions[session_id]
                logger.info(f"[TeraBox SessionStore] stage=verification_expired sessionId={session_id[:8]}***")
                return None
            return session

    def update_session(self, session_id: str, **kwargs) -> Optional[ResolverSession]:
        """Update fields of an active session."""
        with self._lock:
            session = self._sessions.get(session_id)
            if not session or session.is_expired():
                return None
            for key, val in kwargs.items():
                if hasattr(session, key):
                    setattr(session, key, val)
            return session

    def delete_session(self, session_id: str) -> bool:
        """Explicitly delete a session."""
        with self._lock:
            if session_id in self._sessions:
                del self._sessions[session_id]
                return True
            return False

    def _purge_expired_locked(self) -> None:
        """Purge expired sessions (caller must hold _lock)."""
        now = time.time()
        expired_keys = [k for k, v in self._sessions.items() if v.expires_at <= now]
        for k in expired_keys:
            del self._sessions[k]


# Global session store instance
session_store = SessionStore()


def is_valid_direct_download_url(url: Optional[str]) -> bool:
    """Validate that a candidate direct download URL is usable and not a webpage or challenge."""
    if not url or not isinstance(url, str):
        return False
    url = url.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        return False
    try:
        parsed = urlparse(url)
        if not parsed.netloc:
            return False
        # Must not be share page, verify challenge page, or generic portal
        if "/s/" in parsed.path or "surl=" in (parsed.query or ""):
            return False
        if "/sharing/" in parsed.path or "/verify" in parsed.path or "/captcha" in parsed.path:
            return False
        return True
    except Exception:
        return False
