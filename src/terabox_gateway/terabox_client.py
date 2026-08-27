"""TeraBox API client module.

This module handles all interactions with the TeraBox API,
including fetching file information, direct download links, and formatting responses.
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional, Union
from urllib.parse import parse_qs, urlparse

import aiohttp

from .config import headers, load_cookies
from .utils import extract_thumbnail_dimensions, get_formatted_size, request_with_retry


logger = logging.getLogger("terabox_gateway")


class FileList(list):
    """Subclass of list to carry additional metadata like fallback_no_cookie."""
    fallback_no_cookie: bool = False
    used_cookies: bool = False


def log_sanitized_stage_diagnostics(
    stage: str,
    endpoint_path: str,
    http_status: Union[int, str],
    errno: Union[int, str, None] = None,
    errmsg: Optional[str] = None,
    cookies: Optional[Dict[str, str]] = None,
    js_token: Optional[str] = None,
    dp_log_id: Optional[str] = None,
    signing_params: Optional[Dict[str, Any]] = None,
    verification_url: Optional[str] = None,
    interactive_verification: bool = False,
):
    """Log sanitized diagnostics without exposing sensitive secrets, cookies, or full query parameters."""
    ndus_present = "YES" if (cookies and ("ndus" in cookies or "NDUS" in cookies)) else "NO"
    csrf_present = "YES" if (cookies and ("csrfToken" in cookies or "csrf" in cookies)) else "NO"
    session_cookies_present = "YES" if (cookies and len(cookies) > 0) else "NO"
    js_token_present = "YES" if bool(js_token) else "NO"
    dp_log_id_present = "YES" if bool(dp_log_id) else "NO"
    signing_params_present = "YES" if bool(signing_params and all(k in signing_params for k in ("sign1", "sign3"))) else "NO"
    verification_url_present = "YES" if bool(verification_url) else "NO"
    interactive_req = "YES" if interactive_verification else "NO"
    
    clean_errmsg = str(errmsg)[:60].replace("\n", " ") if errmsg else "NONE"

    logger.info(
        f"[TeraBox Diagnostics] stage={stage} endpoint={endpoint_path} httpStatus={http_status} "
        f"errno={errno if errno is not None else 'NONE'} errmsg=\"{clean_errmsg}\" "
        f"ndusPresent={ndus_present} csrfPresent={csrf_present} cookiesPresent={session_cookies_present} "
        f"jsTokenPresent={js_token_present} dpLogIdPresent={dp_log_id_present} "
        f"signingParamsPresent={signing_params_present} verificationUrlPresent={verification_url_present} "
        f"interactiveVerificationRequired={interactive_req}"
    )


async def fetch_download_link(
    url: str, password: str = ""
) -> Union[List[Dict[str, Any]], Dict[str, Any]]:
    """Fetch file information from TeraBox share link using unified proxy API.
    
    Args:
        url: TeraBox share URL
        password: Optional password for protected links
        
    Returns:
        Union[List[Dict[str, Any]], Dict[str, Any]]: List of files or error dict
    """
    try:
        from .config import PROXY_BASE_URL, PROXY_MODE_RESOLVE
        
        # Extract surl from URL
        parsed_url = urlparse(url)
        if "surl=" in parsed_url.query:
            surl = parse_qs(parsed_url.query)["surl"][0]
        elif "/s/" in parsed_url.path:
            surl = parsed_url.path.split("/s/")[1].split("/")[0].split("?")[0]
        else:
            logger.error("Could not extract surl from URL")
            return {
                "status": "error",
                "error": "invalid_url_format",
                "errno": -1,
                "message": "Could not extract share code from URL",
                "stage": "parameter_validation"
            }
        
        # Normalize surl
        clean_code = surl[1:] if surl.startswith("1") else surl
        verification_link = f"https://1024terabox.com/s/1{clean_code}"
        
        initial_cookies = load_cookies()
        attempts = [initial_cookies]
        if initial_cookies:
            attempts.append({})
            
        for idx, cookies_to_send in enumerate(attempts):
            is_last_attempt = (idx == len(attempts) - 1)
            try:
                connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())
                async with aiohttp.ClientSession(connector=connector, cookies=cookies_to_send, headers=headers) as session:
                    params = {
                        "mode": PROXY_MODE_RESOLVE,
                        "surl": clean_code,
                        "raw": "1",
                    }
                    if password:
                        params["pwd"] = password
                    
                    log_sanitized_stage_diagnostics(
                        stage="share_session_creation",
                        endpoint_path=PROXY_BASE_URL,
                        http_status="PENDING",
                        cookies=cookies_to_send,
                        verification_url=verification_link,
                    )
                    
                    async with request_with_retry(session, "GET", PROXY_BASE_URL, params=params) as response:
                        if response.status != 200:
                            error_text = await response.text()
                            
                            should_retry = False
                            try:
                                import json
                                err_json = json.loads(error_text)
                                upstream_err = err_json.get("details", {})
                                if isinstance(upstream_err, dict):
                                    upstream_errno = upstream_err.get("errno")
                                    upstream_msg = str(upstream_err.get("errmsg", "") or upstream_err.get("reason", ""))
                                    if (
                                        upstream_errno in (4000020, 400141, 400210, 400310)
                                        or "need verify" in upstream_msg.lower()
                                        or "verify_v2" in upstream_msg.lower()
                                        or "verification" in upstream_msg.lower()
                                    ):
                                        if not is_last_attempt:
                                            logger.warning("Upstream returned verification requirement with cookies. Retrying without cookies.")
                                            should_retry = True
                                        else:
                                            log_sanitized_stage_diagnostics(
                                                stage="provider_resolution",
                                                endpoint_path=PROXY_BASE_URL,
                                                http_status=response.status,
                                                errno=upstream_errno or 400210,
                                                errmsg=upstream_msg,
                                                cookies=cookies_to_send,
                                                verification_url=verification_link,
                                                interactive_verification=True,
                                            )
                                            return {
                                                "status": "error",
                                                "error": "provider_verification_required",
                                                "errno": upstream_errno or 400210,
                                                "message": upstream_msg or "This link requires interactive or captcha verification from the provider",
                                                "surl": clean_code,
                                                "requires_verification": True,
                                                "requires_password": bool(upstream_errno in (4000020, 400141)),
                                                "verification_url": verification_link,
                                                "stage": "provider_resolution",
                                            }
                            except Exception as e:
                                logger.debug(f"Failed to parse error response JSON: {e}")
                            
                            if should_retry:
                                continue
                            
                            if "verification" in error_text.lower() or "verify" in error_text.lower():
                                log_sanitized_stage_diagnostics(
                                    stage="provider_resolution",
                                    endpoint_path=PROXY_BASE_URL,
                                    http_status=response.status,
                                    errno=400210,
                                    errmsg="Verification challenge encountered",
                                    cookies=cookies_to_send,
                                    verification_url=verification_link,
                                    interactive_verification=True,
                                )
                                return {
                                    "status": "error",
                                    "error": "provider_verification_required",
                                    "errno": 400210,
                                    "message": "Provider verification challenge encountered",
                                    "surl": clean_code,
                                    "requires_verification": True,
                                    "requires_password": False,
                                    "verification_url": verification_link,
                                    "stage": "provider_resolution",
                                }
                            
                            log_sanitized_stage_diagnostics(
                                stage="provider_resolution",
                                endpoint_path=PROXY_BASE_URL,
                                http_status=response.status,
                                errno=-1,
                                errmsg=f"Proxy returned {response.status}",
                                cookies=cookies_to_send,
                            )
                            return {
                                "status": "error",
                                "error": f"proxy_error_{response.status}",
                                "errno": -1,
                                "message": f"Upstream proxy returned status {response.status}",
                                "details": error_text[:200],
                                "stage": "provider_resolution",
                            }
                        
                        response_data = await response.json()
                        
                        if "error" in response_data:
                            error_msg = str(response_data.get("error", "Unknown error"))
                            
                            if "verify" in error_msg.lower() or "verification" in error_msg.lower():
                                log_sanitized_stage_diagnostics(
                                    stage="provider_resolution",
                                    endpoint_path=PROXY_BASE_URL,
                                    http_status=200,
                                    errno=400210,
                                    errmsg=error_msg,
                                    cookies=cookies_to_send,
                                    verification_url=verification_link,
                                    interactive_verification=True,
                                )
                                return {
                                    "status": "error",
                                    "error": "provider_verification_required",
                                    "errno": 400210,
                                    "message": "Provider verification required",
                                    "surl": clean_code,
                                    "requires_verification": True,
                                    "requires_password": False,
                                    "verification_url": verification_link,
                                    "stage": "provider_resolution",
                                }
                            
                            if "jsToken" in error_msg or "cookie" in error_msg.lower():
                                return {
                                    "status": "error",
                                    "error": "token_extraction_failed",
                                    "errno": -1,
                                    "message": "Failed to extract authentication tokens. Provider may require verification or cookies.",
                                    "stage": "provider_resolution",
                                }
                            
                            return {
                                "status": "error",
                                "error": error_msg,
                                "errno": -1,
                                "message": error_msg,
                                "stage": "provider_resolution",
                            }
                        
                        if "upstream" in response_data:
                            api_response = response_data["upstream"]
                        else:
                            api_response = response_data.get("data", response_data)
                        
                        errno = api_response.get("errno", -1)
                        errmsg = str(api_response.get("errmsg", ""))
                        
                        if (
                            errno in (400141, 4000020, 400210, 400310)
                            or "need verify" in errmsg.lower()
                            or "verify_v2" in errmsg.lower()
                        ):
                            if not is_last_attempt:
                                logger.warning(f"Upstream returned errno {errno} with cookies. Retrying without cookies.")
                                continue
                            
                            log_sanitized_stage_diagnostics(
                                stage="provider_resolution",
                                endpoint_path=PROXY_BASE_URL,
                                http_status=200,
                                errno=errno,
                                errmsg=errmsg,
                                cookies=cookies_to_send,
                                verification_url=verification_link,
                                interactive_verification=True,
                            )
                            return {
                                "status": "error",
                                "error": "provider_verification_required",
                                "errno": errno,
                                "message": errmsg or "This link requires password or captcha verification",
                                "surl": clean_code,
                                "requires_verification": True,
                                "requires_password": bool(errno in (400141, 4000020)),
                                "verification_url": verification_link,
                                "stage": "provider_resolution",
                            }
                        
                        if errno != 0:
                            error_msg = errmsg or "Unknown error"
                            log_sanitized_stage_diagnostics(
                                stage="provider_resolution",
                                endpoint_path=PROXY_BASE_URL,
                                http_status=200,
                                errno=errno,
                                errmsg=error_msg,
                                cookies=cookies_to_send,
                            )
                            return {
                                "status": "error",
                                "error": "provider_error",
                                "errno": errno,
                                "message": error_msg,
                                "stage": "provider_resolution",
                            }
                        
                        if "list" not in api_response:
                            return {
                                "status": "error",
                                "error": "no_files_found",
                                "errno": -1,
                                "message": "No file list returned by provider",
                                "stage": "provider_resolution",
                            }
                        
                        files = api_response["list"]
                        log_sanitized_stage_diagnostics(
                            stage="metadata_resolution",
                            endpoint_path=PROXY_BASE_URL,
                            http_status=200,
                            errno=0,
                            errmsg="SUCCESS",
                            cookies=cookies_to_send,
                            js_token=api_response.get("jsToken"),
                            dp_log_id=api_response.get("dplogid"),
                        )
                        
                        result_files = FileList(files)
                        result_files.fallback_no_cookie = (idx > 0)
                        result_files.used_cookies = (idx == 0 and bool(cookies_to_send))
                        return result_files
                        
            except Exception as e:
                logger.error(f"Error on attempt {idx+1}: {e}", exc_info=True)
                if is_last_attempt:
                    return {
                        "status": "error",
                        "error": "connection_error",
                        "errno": -1,
                        "message": str(e),
                        "stage": "provider_resolution",
                    }
                    
    except Exception as e:
        logger.error(f"Unexpected error in fetch_download_link: {e}", exc_info=True)
        return {
            "status": "error",
            "error": "internal_error",
            "errno": -1,
            "message": str(e),
            "stage": "provider_resolution",
        }


async def format_file_info(file_data: Dict[str, Any]) -> Dict[str, Any]:
    """Format file information for API response."""
    thumbnails = {}
    if "thumbs" in file_data:
        for key, thumb_url in file_data["thumbs"].items():
            if thumb_url:
                dimensions = extract_thumbnail_dimensions(thumb_url)
                thumbnails[dimensions] = thumb_url

    return {
        "filename": file_data.get("server_filename", "Unknown"),
        "size": get_formatted_size(file_data.get("size", 0)),
        "size_bytes": file_data.get("size", 0),
        "download_link": file_data.get("dlink", ""),
        "is_directory": file_data.get("isdir") == "1",
        "thumbnails": thumbnails,
        "path": file_data.get("path", ""),
        "fs_id": file_data.get("fs_id", ""),
    }


async def fetch_direct_links(
    url: str, password: str = "", files: Optional[List[Dict[str, Any]]] = None
) -> Union[List[Dict[str, Any]], Dict[str, Any]]:
    """Fetch files with direct download links.
    
    Args:
        url: TeraBox share URL
        password: Optional password for protected links
        files: Optional list of files already fetched
        
    Returns:
        Union[List[Dict[str, Any]], Dict[str, Any]]: List of files with direct links or error dict
    """
    try:
        if files is None:
            files = await fetch_download_link(url, password)

        if isinstance(files, dict) and "error" in files:
            return files

        session_cookies = load_cookies()
        connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())
        async with aiohttp.ClientSession(
            connector=connector,
            cookies=session_cookies,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=30, connect=10),
        ) as session:
            results = FileList()
            if hasattr(files, "fallback_no_cookie"):
                results.fallback_no_cookie = files.fallback_no_cookie
            if hasattr(files, "used_cookies"):
                results.used_cookies = files.used_cookies

            for item in files or []:
                if not isinstance(item, dict):
                    continue

                dlink = item.get("dlink") or item.get("download_link") or item.get("link") or ""
                direct_link = item.get("direct_link")

                if dlink and not direct_link:
                    try:
                        async with request_with_retry(
                            session, "HEAD", dlink, allow_redirects=False
                        ) as response:
                            direct_link = response.headers.get("Location") or str(response.url)
                    except Exception as e:
                        logger.error(f"Error resolving direct CDN link: {e}")
                        direct_link = dlink

                final_direct = direct_link or dlink or None

                log_sanitized_stage_diagnostics(
                    stage="direct_link_resolution",
                    endpoint_path=dlink[:30] if dlink else "none",
                    http_status=200 if final_direct else "NO_DIRECT_LINK",
                    errno=0 if final_direct else -1,
                    errmsg="SUCCESS" if final_direct else "No direct link returned",
                    cookies=session_cookies,
                )

                results.append(
                    {
                        "filename": item.get("server_filename") or item.get("filename", "Unknown"),
                        "size": get_formatted_size(item.get("size", item.get("size_bytes", 0))),
                        "size_bytes": item.get("size_bytes", item.get("size", 0)),
                        "link": dlink,
                        "download_link": final_direct or dlink,
                        "direct_link": final_direct,
                        "thumbnail": (item.get("thumbs") or {}).get("url3", "") or item.get("thumbnail", ""),
                        "fs_id": item.get("fs_id", ""),
                    }
                )

            return results

    except Exception as e:
        logger.exception(f"Error in fetch_direct_links: {e}")
        return {
            "status": "error",
            "error": "direct_link_error",
            "message": str(e),
            "errno": -1,
            "stage": "direct_link_resolution",
        }


async def _gather_format_file_info(files: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Helper to run format_file_info concurrently."""
    tasks = [format_file_info(item) for item in files if isinstance(item, dict)]
    if not tasks:
        return []
    results = await asyncio.gather(*tasks)
    return results


async def _normalize_api2_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Normalize items returned by fetch_direct_links to the /api response shape."""
    out = FileList()
    if hasattr(items, "fallback_no_cookie"):
        out.fallback_no_cookie = items.fallback_no_cookie
    if hasattr(items, "used_cookies"):
        out.used_cookies = items.used_cookies
    for item in items or []:
        try:
            if not isinstance(item, dict):
                continue
            filenamestr = item.get("filename") or item.get("server_filename", "Unknown")
            size_h = (
                item.get("size")
                if isinstance(item.get("size"), str)
                else get_formatted_size(item.get("size_bytes", item.get("size", 0)))
            )
            size_b = item.get("size_bytes", item.get("size", 0))
            download = (
                item.get("direct_link")
                or item.get("download_link")
                or item.get("link")
                or item.get("dlink")
                or ""
            )
            thumbs: Dict[str, str] = {}
            thumb_single = item.get("thumbnail") or (item.get("thumbs") or {}).get("url3")
            if thumb_single:
                thumbs["original"] = thumb_single
            formatted = {
                "filename": filenamestr,
                "size": size_h,
                "size_bytes": size_b,
                "download_link": download,
                "is_directory": item.get("is_directory", False),
                "thumbnails": thumbs,
                "path": item.get("path", ""),
                "fs_id": item.get("fs_id", ""),
            }
            if item.get("direct_link"):
                formatted["direct_link"] = item["direct_link"]
            out.append(formatted)
        except Exception:
            continue
    return out
