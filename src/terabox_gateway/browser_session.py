"""Server-side interactive Playwright browser session manager for TeraBox verification.

Manages isolated Chromium browser contexts on the server, enabling manual or interactive
completion of TeraBox anti-bot challenges while preserving the exact session context.
"""

import asyncio
import base64
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger("terabox_gateway")

MAX_CONCURRENT_BROWSERS = int(os.getenv("MAX_CONCURRENT_BROWSERS", "5"))
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "600"))  # 10 minutes default
VIEWPORT_WIDTH = 1280
VIEWPORT_HEIGHT = 800


@dataclass
class BrowserVerificationSession:
    """Represents an isolated server-side Playwright browser verification session."""
    session_id: str
    url: str
    surl: str
    password: str = ""
    verification_url: str = ""
    state: str = "pending"  # "pending", "verifying", "completed", "failed", "expired"
    created_at: float = field(default_factory=time.time)
    expires_at: float = field(default_factory=lambda: time.time() + SESSION_TTL_SECONDS)
    playwright_instance: Any = None
    browser: Any = None
    context: Any = None
    page: Any = None
    files: Optional[List[Dict[str, Any]]] = None
    resolved_links: Optional[List[Dict[str, Any]]] = None
    verified: bool = False
    last_screenshot: Optional[bytes] = None
    last_activity: float = field(default_factory=time.time)
    intercepted_download_urls: List[str] = field(default_factory=list)

    def is_expired(self) -> bool:
        return time.time() >= self.expires_at

    def time_remaining_seconds(self) -> int:
        remaining = int(self.expires_at - time.time())
        return max(0, remaining)


class BrowserSessionManager:
    """Thread-safe manager for Playwright browser verification sessions."""
    def __init__(self):
        self._sessions: Dict[str, BrowserVerificationSession] = {}
        self._lock = asyncio.Lock()
        self._sync_lock = threading.Lock()
        self._global_playwright = None
        self._global_browser = None

    async def get_or_create_browser(self):
        """Get or initialize global Playwright Chromium browser."""
        if self._global_browser and self._global_browser.is_connected():
            return self._global_playwright, self._global_browser
            
        from playwright.async_api import async_playwright
        self._global_playwright = await async_playwright().start()
        self._global_browser = await self._global_playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-gpu",
                "--single-process",
            ],
        )
        logger.info("[Verification] browser_started")
        return self._global_playwright, self._global_browser

    async def create_session(
        self,
        url: str,
        surl: str,
        password: str = "",
        verification_url: str = "",
        files: Optional[List[Dict[str, Any]]] = None,
    ) -> BrowserVerificationSession:
        """Create a new isolated browser context and session."""
        session_id = secrets.token_urlsafe(24)
        clean_surl = surl[1:] if surl.startswith("1") else surl
        v_url = verification_url or f"https://1024terabox.com/s/1{clean_surl}"

        async with self._lock:
            # Purge expired sessions
            await self._purge_expired()

            # Enforce concurrency limit
            if len(self._sessions) >= MAX_CONCURRENT_BROWSERS:
                oldest_id = min(self._sessions, key=lambda k: self._sessions[k].created_at)
                await self._close_session(oldest_id)

            session = BrowserVerificationSession(
                session_id=session_id,
                url=url,
                surl=clean_surl,
                password=password,
                verification_url=v_url,
                state="pending",
                files=files,
            )
            self._sessions[session_id] = session

        logger.info(f"[Verification] session_created sessionId={session_id[:8]}***")
        return session

    async def ensure_page_loaded(self, session_id: str) -> Optional[BrowserVerificationSession]:
        """Ensure the Playwright browser context and page are open and navigating."""
        session = self.get_session(session_id)
        if not session or session.is_expired():
            return None

        if session.page is not None and not session.page.is_closed():
            return session

        try:
            playwright_inst, browser = await self.get_or_create_browser()
            context = await browser.new_context(
                viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                locale="en-US",
                timezone_id="America/New_York",
            )
            page = await context.new_page()

            # Intercept download requests or direct CDN links
            async def handle_response(response):
                try:
                    res_url = response.url
                    if any(cdn in res_url for cdn in ("d.terabox.app", "terabox.com/download", "data.terabox.app", "dlink")):
                        if res_url not in session.intercepted_download_urls:
                            session.intercepted_download_urls.append(res_url)
                except Exception:
                    pass

            page.on("response", handle_response)

            target_url = session.verification_url or session.url
            logger.info(f"[Verification] verification_pending sessionId={session_id[:8]}*** navigating=YES")
            session.state = "verifying"
            
            await page.goto(target_url, timeout=30000, wait_until="domcontentloaded")
            await asyncio.sleep(1.5)  # Allow dynamic scripts to mount

            session.context = context
            session.page = page
            session.last_activity = time.time()
            return session
        except Exception as e:
            logger.exception(f"Error launching page for session {session_id[:8]}***: {e}")
            session.state = "failed"
            return session

    def get_session(self, session_id: str) -> Optional[BrowserVerificationSession]:
        """Synchronously lookup a session by ID if not expired."""
        with self._sync_lock:
            session = self._sessions.get(session_id)
            if not session:
                return None
            if session.is_expired():
                session.state = "expired"
                return None
            return session

    async def get_screenshot(self, session_id: str) -> Optional[bytes]:
        """Capture live screenshot of the verification page."""
        session = await self.ensure_page_loaded(session_id)
        if not session or not session.page or session.page.is_closed():
            return None
        try:
            screenshot = await session.page.screenshot(type="jpeg", quality=75)
            session.last_screenshot = screenshot
            session.last_activity = time.time()
            return screenshot
        except Exception as e:
            logger.debug(f"Screenshot capture error for {session_id[:8]}***: {e}")
            return session.last_screenshot

    async def forward_click(self, session_id: str, x: int, y: int) -> bool:
        """Forward mouse click event to Playwright page."""
        session = await self.ensure_page_loaded(session_id)
        if not session or not session.page or session.page.is_closed():
            return False
        try:
            await session.page.mouse.click(x, y)
            session.last_activity = time.time()
            await asyncio.sleep(0.5)
            return True
        except Exception as e:
            logger.debug(f"Click forward error for {session_id[:8]}***: {e}")
            return False

    async def forward_drag(self, session_id: str, from_x: int, from_y: int, to_x: int, to_y: int, steps: int = 15) -> bool:
        """Forward mouse drag/slide event to Playwright page (for slider puzzle CAPTCHAs)."""
        session = await self.ensure_page_loaded(session_id)
        if not session or not session.page or session.page.is_closed():
            return False
        try:
            await session.page.mouse.move(from_x, from_y)
            await session.page.mouse.down()
            await asyncio.sleep(0.05)
            await session.page.mouse.move(to_x, to_y, steps=steps)
            await asyncio.sleep(0.05)
            await session.page.mouse.up()
            session.last_activity = time.time()
            await asyncio.sleep(0.5)
            return True
        except Exception as e:
            logger.debug(f"Drag forward error for {session_id[:8]}***: {e}")
            return False

    async def forward_type(self, session_id: str, text: str) -> bool:
        """Forward text typing to active element on Playwright page."""
        session = await self.ensure_page_loaded(session_id)
        if not session or not session.page or session.page.is_closed():
            return False
        try:
            await session.page.keyboard.type(text)
            session.last_activity = time.time()
            return True
        except Exception as e:
            logger.debug(f"Type forward error for {session_id[:8]}***: {e}")
            return False

    async def attempt_direct_resolution(self, session_id: str) -> Tuple[bool, Optional[List[Dict[str, Any]]], Optional[str]]:
        """Retry direct-link resolution in the SAME browser context after manual verification."""
        session = self.get_session(session_id)
        if not session:
            return False, None, "Session expired or not found"
        
        logger.info(f"[Verification] direct_resolution_started sessionId={session_id[:8]}***")

        # 1. Check if intercepted URLs contain a valid direct download link
        for direct_url in session.intercepted_download_urls:
            if is_valid_direct_download_url(direct_url):
                logger.info(f"[Verification] direct_resolution_success sessionId={session_id[:8]}*** source=intercepted")
                session.state = "completed"
                session.verified = True
                files = [{
                    "filename": (session.files[0].get("server_filename") or session.files[0].get("filename") if session.files else "download_file"),
                    "size_bytes": (session.files[0].get("size") or session.files[0].get("size_bytes") if session.files else 0),
                    "direct_link": direct_url,
                    "download_link": direct_url,
                }]
                return True, files, None

        # 2. Evaluate in-page state and trigger download if needed
        if session.page and not session.page.is_closed():
            try:
                # Check for in-page download URLs
                found_links = await session.page.evaluate("""() => {
                    const links = [];
                    // Look for dlink / download attributes
                    document.querySelectorAll('a[href*="download"], a[href*="d.terabox"], a[href*="dlink"]').forEach(el => {
                        if (el.href) links.push(el.href);
                    });
                    // Look in window globals
                    if (window.yunData && window.yunData.file_list) {
                        window.yunData.file_list.forEach(f => {
                            if (f.dlink) links.push(f.dlink);
                        });
                    }
                    return links;
                }""")
                for link in found_links or []:
                    if is_valid_direct_download_url(link):
                        logger.info(f"[Verification] direct_resolution_success sessionId={session_id[:8]}*** source=page_eval")
                        session.state = "completed"
                        session.verified = True
                        files = [{
                            "filename": (session.files[0].get("server_filename") or session.files[0].get("filename") if session.files else "download_file"),
                            "size_bytes": (session.files[0].get("size") or session.files[0].get("size_bytes") if session.files else 0),
                            "direct_link": link,
                            "download_link": link,
                        }]
                        return True, files, None
            except Exception as e:
                logger.debug(f"In-page evaluation exception: {e}")

        # 3. If no direct link yet, check if verification is still required on the page
        logger.info(f"[Verification] provider_verification_still_required sessionId={session_id[:8]}***")
        return False, None, "provider_verification_required"

    async def _close_session(self, session_id: str):
        """Close context and page for a specific session."""
        session = self._sessions.pop(session_id, None)
        if session:
            try:
                if session.context:
                    await session.context.close()
            except Exception:
                pass

    async def _purge_expired(self):
        """Purge and clean up expired sessions."""
        now = time.time()
        expired_ids = [sid for sid, s in self._sessions.items() if s.expires_at <= now]
        for sid in expired_ids:
            logger.info(f"[Verification] verification_expired sessionId={sid[:8]}***")
            await self._close_session(sid)


# Global browser session manager instance
browser_session_manager = BrowserSessionManager()


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
        # Must not be share page, verify challenge page, or login portal
        if "/s/" in parsed.path or "surl=" in (parsed.query or ""):
            return False
        if "/sharing/" in parsed.path or "/verify" in parsed.path or "/captcha" in parsed.path or "/login" in parsed.path:
            return False
        return True
    except Exception:
        return False
