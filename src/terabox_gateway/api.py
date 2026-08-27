"""TeraBox API Gateway - Main Flask Application.

This module defines the Flask application and all API route handlers.
Flask 3.x is used for native async route support.
Business logic has been separated into dedicated modules:
- config.py: Configuration and constants
- utils.py: Utility functions
- terabox_client.py: TeraBox API client logic
"""

from flask import Flask, request, jsonify, Response, send_from_directory
from datetime import datetime, timezone
import logging
import time

# Import from our modules
from .config import (
    headers,
    load_cookies,
    PROXY_BASE_URL,
    PROXY_MODE_RESOLVE,
    PROXY_MODE_PAGE,
    PROXY_MODE_API,
    PROXY_MODE_STREAM,
    PROXY_MODE_SEGMENT,
    PROXY_MODE_THUMBNAIL,
    PROXY_MODE_LOOKUP,
    PROXY_MODE_HEALTH,
)
from .utils import is_valid_share_url, _proxy_request
from .terabox_client import (
    fetch_download_link,
    fetch_direct_links,
    _normalize_api2_items,
)
from .session_store import session_store, is_valid_direct_download_url
from .rate_limiter import rate_limit
from . import cache



def format_response_time(seconds: float) -> str:
    """Format response time with appropriate unit (s or m).
    
    Args:
        seconds: Time in seconds
        
    Returns:
        Formatted string with 's' or 'm' suffix
    """
    if seconds >= 60:
        minutes = round(seconds / 60, 2)
        return f"{minutes}m"
    else:
        return f"{round(seconds, 3)}s"


def create_app() -> Flask:
    """Create and configure the Flask application.

    This factory keeps a top-level `app` available for Vercel (module import)
    while allowing local development with `python api.py`.
    """

    app = Flask(__name__, static_folder="swagger", static_url_path="/swagger")
    return app


# Create module-level `app` so Vercel/Gunicorn can import it: `from api import app`
app = create_app()


# Basic CORS for browser clients (no extra dependency)
@app.after_request
def add_cors_headers(resp: Response) -> Response:
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return resp




# =============== API ROUTES ===============





@app.route("/health")
def health():
    """Health check endpoint"""
    return jsonify({"status": "healthy", "timestamp": datetime.now(timezone.utc).isoformat()})







@app.route("/")
def index():
    """API information endpoint"""
    return jsonify(
        {
            "name": "TeraBox API",
            "version": "2.0",
            "status": "operational",
            "endpoints": {
                "/": "API information",
                "/docs": "Interactive Swagger UI documentation (API playground)",
                "/swagger.json": "OpenAPI 3.0.0 specification (JSON)",
                "/api": "Unified endpoint - file listing with direct download links, and proxy modes",
                "/health": "Health check",
            },
            "contact": "@Saahiyo",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )


from urllib.parse import urlparse

logger = logging.getLogger("terabox_gateway")


@app.route("/api", methods=["GET"])
@rate_limit
async def api():
    """Unified API endpoint - handles file information and proxy modes."""
    try:
        start_time = time.time()
        mode = request.args.get("mode")
        url = request.args.get("url")
        resolve = request.args.get("resolve", "0") in ("1", "true", "True")

        # Safe request logging (never logs sensitive query/auth data)
        sanitized_host = "none"
        if url:
            try:
                sanitized_host = urlparse(url).netloc or "invalid"
            except Exception:
                sanitized_host = "invalid"

        logger.info(
            f"[API Request] method={request.method} path={request.path} "
            f"hasUrl={'YES' if url else 'NO'} hasResolve={'YES' if resolve else 'NO'} "
            f"targetHost={sanitized_host} mode={mode or 'none'}"
        )
        
        # ===== PROXY MODE LOGIC =====
        if mode:
            # Validate mode parameter
            valid_modes = [
                PROXY_MODE_RESOLVE,
                PROXY_MODE_PAGE,
                PROXY_MODE_API,
                PROXY_MODE_STREAM,
                PROXY_MODE_SEGMENT,
                PROXY_MODE_THUMBNAIL,
                PROXY_MODE_LOOKUP,
                PROXY_MODE_HEALTH,
            ]
            
            if mode not in valid_modes:
                return jsonify({
                    "status": "error",
                    "error": "invalid_mode",
                    "message": f"Invalid mode '{mode}'",
                    "allowed": valid_modes,
                    "stage": "parameter_validation"
                }), 400
            
            # Build proxy request parameters
            params = {"mode": mode}
            
            # Add all query parameters except 'mode' to the proxy request
            for key, value in request.args.items():
                if key != "mode":
                    params[key] = value
            
            # Validate required parameters based on mode
            if mode == PROXY_MODE_RESOLVE:
                if "surl" not in params:
                    return jsonify({"status": "error", "error": "missing_parameter", "message": "Missing required parameter: surl", "stage": "parameter_validation"}), 400
            elif mode == PROXY_MODE_LOOKUP:
                if "surl" not in params and "fid" not in params:
                    return jsonify({"status": "error", "error": "missing_parameter", "message": "Missing required parameter: surl or fid", "stage": "parameter_validation"}), 400
            elif mode == PROXY_MODE_PAGE:
                if "surl" not in params:
                    return jsonify({"status": "error", "error": "missing_parameter", "message": "Missing required parameter: surl", "stage": "parameter_validation"}), 400
            elif mode == PROXY_MODE_API:
                if "jsToken" not in params or "shorturl" not in params:
                    return jsonify({"status": "error", "error": "missing_parameter", "message": "Missing required parameters: jsToken and shorturl", "stage": "parameter_validation"}), 400
            elif mode == PROXY_MODE_STREAM:
                if "surl" not in params:
                    return jsonify({"status": "error", "error": "missing_parameter", "message": "Missing required parameter: surl", "stage": "parameter_validation"}), 400
            elif mode == PROXY_MODE_SEGMENT:
                if "url" not in params:
                    return jsonify({"status": "error", "error": "missing_parameter", "message": "Missing required parameter: url", "stage": "parameter_validation"}), 400
            elif mode == PROXY_MODE_THUMBNAIL:
                if "fid" not in params:
                    return jsonify({"status": "error", "error": "missing_parameter", "message": "Missing required parameter: fid", "stage": "parameter_validation"}), 400
            elif mode == PROXY_MODE_HEALTH:
                pass
            
            # Forward cookies from client request if present
            cookies = {}
            if "Cookie" in request.headers:
                # Parse cookie header
                cookie_header = request.headers.get("Cookie")
                for cookie in cookie_header.split(";"):
                    cookie = cookie.strip()
                    if "=" in cookie:
                        key, value = cookie.split("=", 1)
                        cookies[key.strip()] = value.strip()
            
            # If no cookies from client, try loading from config
            if not cookies:
                cookies = load_cookies()
            
            # Extract client headers to forward
            req_headers = {}
            for k, v in request.headers.items():
                if k.lower() in ["x-admin-key", "authorization"]:
                    req_headers[k] = v
            
            # Make proxy request
            result = await _proxy_request(PROXY_BASE_URL, params, cookies, req_headers=req_headers)
            
            if "error" in result:
                return jsonify(result), result.get("status_code", 502)
            
            # Return response with appropriate content type, filtering out transport/encoding headers
            excluded_headers = {
                "transfer-encoding",
                "content-encoding",
                "content-length",
                "connection",
                "keep-alive",
                "host",
            }
            response_headers = {
                k: v for k, v in result["headers"].items()
                if k.lower() not in excluded_headers
            }
            return Response(
                result["content"],
                status=result["status"],
                headers=response_headers,
                content_type=result["content_type"]
            )
        
        # ===== FILE RESOLVER MODE =====
        if not url:
            return (
                jsonify(
                    {
                        "status": "error",
                        "error": "missing_parameter",
                        "message": "Missing required parameter: url or mode",
                        "stage": "parameter_validation",
                        "examples": {
                            "file_listing": "/api?url=https://1024terabox.com/s/...",
                            "proxy_resolve": "/api?mode=resolve&surl=abc123",
                            "proxy_stream": "/api?mode=stream&surl=abc123"
                        }
                    }
                ),
                400,
            )
        
        if not is_valid_share_url(url):
            return (
                jsonify(
                    {
                        "status": "error",
                        "error": "invalid_url",
                        "message": "Invalid TeraBox share URL",
                        "stage": "parameter_validation",
                        "example": "/api?url=https://1024terabox.com/s/XXXXXXXX",
                    }
                ),
                400,
            )

        password = request.args.get("pwd", "")

        # Check cache first
        refresh = request.args.get("refresh", "").lower() in ("1", "true")
        cached = None if refresh else cache.get(url, password)
        link_data = cached

        if link_data is None:
            link_data = await fetch_download_link(url, password)

        # Check if an error/verification condition occurred
        if isinstance(link_data, dict) and "error" in link_data:
            err_code = link_data.get("errno")
            err_type = link_data.get("error", "provider_error")
            is_verification = (
                link_data.get("requires_verification", False)
                or err_code in (400210, 400310, 400141, 4000020)
                or "verify" in str(link_data.get("message", "")).lower()
                or "verify" in str(err_type).lower()
            )
            v_url = link_data.get("verification_url") or url
            surl = link_data.get("surl", "")
            
            session_id = None
            if is_verification:
                session = session_store.create_session(
                    url=url,
                    surl=surl,
                    verification_url=v_url,
                    password=password,
                    challenge_state={"errno": err_code, "errmsg": link_data.get("message")},
                )
                session_id = session.session_id

            if link_data.get("requires_password"):
                status_code = 400
            elif is_verification:
                status_code = 409
            elif err_code == -1 and "404" in str(link_data.get("details", "")):
                status_code = 404
            else:
                status_code = 502

            err_resp = {
                "status": "error",
                "error": "provider_verification_required" if is_verification else err_type,
                "errno": err_code,
                "message": link_data.get("message") or link_data.get("error") or "Provider resolution failed",
                "requires_verification": is_verification,
                "requires_password": link_data.get("requires_password", False),
                "verification_url": v_url if is_verification else None,
                "stage": "provider_resolution",
            }
            if session_id:
                err_resp["session_id"] = session_id
                err_resp["verification_transfer_supported"] = False  # TeraBox browser CAPTCHAs are bound to browser cookies/IP

            return jsonify(err_resp), status_code

        if not link_data:
            return (
                jsonify(
                    {
                        "status": "error",
                        "error": "no_files_found",
                        "message": "No files found for the supplied share link",
                        "stage": "provider_resolution",
                    }
                ),
                404,
            )

        # Cache raw files
        cache.put(url, link_data, password)

        # Direct link resolution
        if resolve:
            resolved_data = await fetch_direct_links(url, password, files=link_data)
            if isinstance(resolved_data, dict) and "error" in resolved_data:
                err_code = resolved_data.get("errno")
                err_type = resolved_data.get("error", "direct_link_error")
                is_verification = (
                    resolved_data.get("requires_verification", False)
                    or err_code in (400210, 400310, 400141, 4000020)
                    or "verify" in str(resolved_data.get("message", "")).lower()
                    or "verify" in str(err_type).lower()
                )
                v_url = resolved_data.get("verification_url") or url
                session_id = None
                if is_verification:
                    session = session_store.create_session(
                        url=url,
                        surl="",
                        verification_url=v_url,
                        password=password,
                        challenge_state={"errno": err_code, "errmsg": resolved_data.get("message")},
                    )
                    session_id = session.session_id

                status_code = 400 if resolved_data.get("requires_password") else (409 if is_verification else 502)
                err_resp = {
                    "status": "error",
                    "error": "provider_verification_required" if is_verification else err_type,
                    "errno": err_code,
                    "message": resolved_data.get("message") or resolved_data.get("error") or "Direct link resolution failed",
                    "requires_verification": is_verification,
                    "requires_password": resolved_data.get("requires_password", False),
                    "verification_url": v_url if is_verification else None,
                    "stage": "direct_link_resolution",
                }
                if session_id:
                    err_resp["session_id"] = session_id
                    err_resp["verification_transfer_supported"] = False

                return jsonify(err_resp), status_code

            formatted_files = await _normalize_api2_items(resolved_data)
        else:
            formatted_files = await _normalize_api2_items(link_data)

        response_time = format_response_time(time.time() - start_time)
        resp_dict = {
            "status": "success",
            "url": url,
            "files": formatted_files,
            "total_files": len(formatted_files),
            "response_time": response_time,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "used_cookies": getattr(formatted_files, "used_cookies", False),
        }
        if getattr(formatted_files, "fallback_no_cookie", False):
            resp_dict["fallback_no_cookie"] = True
            resp_dict["warning"] = "Cookies were rate-limited or invalid. Resolved anonymously without cookies. Download links may be missing."

        return jsonify(resp_dict)

    except Exception as e:
        logger.exception(f"Unhandled exception in /api resolver: {e}")
        return (
            jsonify(
                {
                    "status": "error",
                    "error": "internal_error",
                    "message": "An unexpected error occurred during resolution",
                    "stage": "resolver_execution",
                }
            ),
            500,
        )


@app.route("/api/verification/session", methods=["GET", "POST"])
@rate_limit
async def create_verification_session_route():
    """Create or initialize a server-side resolver session for a share link."""
    try:
        if request.method == "POST":
            data = request.get_json(silent=True) or {}
            url = data.get("url") or request.args.get("url")
            password = data.get("pwd") or data.get("password") or request.args.get("pwd", "")
        else:
            url = request.args.get("url")
            password = request.args.get("pwd", "")

        if not url:
            return jsonify({
                "status": "error",
                "error": "missing_parameter",
                "message": "Missing required parameter: url",
                "stage": "parameter_validation"
            }), 400

        if not is_valid_share_url(url):
            return jsonify({
                "status": "error",
                "error": "invalid_url",
                "message": "Invalid TeraBox share URL",
                "stage": "parameter_validation"
            }), 400

        link_data = await fetch_download_link(url, password)
        
        if isinstance(link_data, dict) and "error" in link_data:
            err_code = link_data.get("errno")
            err_type = link_data.get("error", "provider_error")
            is_verification = (
                link_data.get("requires_verification", False)
                or err_code in (400210, 400310, 400141, 4000020)
                or "verify" in str(link_data.get("message", "")).lower()
                or "verify" in str(err_type).lower()
            )
            v_url = link_data.get("verification_url") or url
            surl = link_data.get("surl", "")
            
            session = session_store.create_session(
                url=url,
                surl=surl,
                verification_url=v_url,
                password=password,
                challenge_state={"errno": err_code, "errmsg": link_data.get("message")},
            )
            
            return jsonify({
                "status": "verification_required",
                "session_id": session.session_id,
                "verification_url": session.verification_url,
                "requires_verification": True,
                "expires_in_seconds": session.time_remaining_seconds(),
                "stage": "verification_session_created"
            }), 409

        # If metadata is already resolved:
        session = session_store.create_session(
            url=url,
            surl="",
            verification_url=url,
            password=password,
            files=link_data if isinstance(link_data, list) else None,
        )
        return jsonify({
            "status": "ready",
            "session_id": session.session_id,
            "requires_verification": False,
            "files": await _normalize_api2_items(link_data) if isinstance(link_data, list) else [],
            "stage": "share_metadata_success"
        }), 200

    except Exception as e:
        logger.exception(f"Error in /api/verification/session: {e}")
        return jsonify({
            "status": "error",
            "error": "internal_error",
            "message": "An unexpected error occurred creating verification session",
            "stage": "resolver_execution"
        }), 500


@app.route("/api/verification/complete", methods=["POST"])
@rate_limit
async def verification_complete_route():
    """Controlled retry endpoint to resume direct link resolution with an authorized session."""
    try:
        data = request.get_json(silent=True) or {}
        session_id = data.get("session_id") or request.args.get("session_id")
        
        if not session_id:
            return jsonify({
                "status": "error",
                "error": "missing_parameter",
                "message": "Missing required parameter: session_id",
                "stage": "parameter_validation"
            }), 400
            
        session = session_store.get_session(session_id)
        if not session:
            logger.info(f"[TeraBox Diagnostics] stage=verification_expired sessionId={session_id[:8]}***")
            return jsonify({
                "status": "error",
                "error": "verification_expired",
                "message": "Verification session has expired or does not exist",
                "stage": "verification_expired"
            }), 410

        logger.info(f"[TeraBox Diagnostics] stage=direct_link_resolution_started sessionId={session_id[:8]}***")
        
        resolved_data = await fetch_direct_links(session.url, session.password, files=session.files)
        
        if isinstance(resolved_data, dict) and "error" in resolved_data:
            err_code = resolved_data.get("errno")
            err_type = resolved_data.get("error", "direct_link_error")
            is_verification = (
                resolved_data.get("requires_verification", False)
                or err_code in (400210, 400310, 400141, 4000020)
                or "verify" in str(resolved_data.get("message", "")).lower()
                or "verify" in str(err_type).lower()
            )
            if is_verification:
                logger.info(f"[TeraBox Diagnostics] stage=provider_verification_required sessionId={session_id[:8]}***")
                return jsonify({
                    "status": "error",
                    "error": "provider_verification_required",
                    "errno": err_code or 400210,
                    "session_id": session_id,
                    "verification_url": session.verification_url,
                    "requires_verification": True,
                    "message": "TeraBox provider verification is still required or was not transferred",
                    "stage": "provider_verification_required"
                }), 409
                
            return jsonify({
                "status": "error",
                "error": err_type,
                "errno": err_code,
                "session_id": session_id,
                "message": resolved_data.get("message") or resolved_data.get("error"),
                "stage": "direct_link_resolution_failed"
            }), 502

        formatted_files = await _normalize_api2_items(resolved_data)
        
        has_direct_url = False
        if formatted_files and len(formatted_files) > 0:
            first_file = formatted_files[0]
            d_link = first_file.get("direct_link") or first_file.get("download_link")
            if is_valid_direct_download_url(d_link):
                has_direct_url = True

        if has_direct_url:
            session_store.update_session(session_id, verification_state="completed")
            logger.info(f"[TeraBox Diagnostics] stage=direct_link_resolution_success sessionId={session_id[:8]}*** directLinkPresent=YES")
            return jsonify({
                "status": "success",
                "session_id": session_id,
                "url": session.url,
                "files": formatted_files,
                "total_files": len(formatted_files),
                "stage": "direct_link_resolution_success"
            }), 200
        else:
            logger.info(f"[TeraBox Diagnostics] stage=direct_link_resolution_failed sessionId={session_id[:8]}*** directLinkPresent=NO")
            return jsonify({
                "status": "error",
                "error": "direct_link_resolution_failed",
                "session_id": session_id,
                "message": "No direct download link was returned by the provider",
                "stage": "direct_link_resolution_failed"
            }), 422
            
    except Exception as e:
        logger.exception(f"Error in /api/verification/complete: {e}")
        return jsonify({
            "status": "error",
            "error": "internal_error",
            "message": "An unexpected error occurred during verification retry",
            "stage": "resolver_execution"
        }), 500


@app.route("/admin/<path:subpath>", methods=["GET"])
@rate_limit
async def admin_proxy(subpath):
    """Proxy admin requests to the upstream worker."""
    try:
        base_url = PROXY_BASE_URL.rstrip("/")
        upstream_url = f"{base_url}/admin/{subpath}"
        
        # Forward query parameters
        params = dict(request.args)
        
        # Forward cookies
        cookies = {}
        if "Cookie" in request.headers:
            cookie_header = request.headers.get("Cookie")
            for cookie in cookie_header.split(";"):
                cookie = cookie.strip()
                if "=" in cookie:
                    key, value = cookie.split("=", 1)
                    cookies[key.strip()] = value.strip()
        if not cookies:
            cookies = load_cookies()
            
        # Extract forwardable headers
        req_headers = {}
        for k, v in request.headers.items():
            if k.lower() in ["x-admin-key", "authorization"]:
                req_headers[k] = v
                
        # Make proxy request
        result = await _proxy_request(upstream_url, params, cookies, req_headers=req_headers)
        
        if "error" in result:
            return jsonify(result), result.get("status_code", 500)
            
        excluded_headers = {
            "transfer-encoding",
            "content-encoding",
            "content-length",
            "connection",
            "keep-alive",
            "host",
        }
        response_headers = {
            k: v for k, v in result["headers"].items()
            if k.lower() not in excluded_headers
        }
        return Response(
            result["content"],
            status=result["status"],
            headers=response_headers,
            content_type=result["content_type"]
        )
    except Exception as e:
        logging.error(f"Admin proxy error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500



@app.route("/swagger.json", methods=["GET"])
def swagger_spec():
    """Serve the OpenAPI 3.0.0 JSON specification."""
    import os
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return send_from_directory(os.path.join(base_dir, "swagger"), "swagger.json")



@app.route("/docs", methods=["GET"])
def swagger_ui():
    """Serve the Swagger UI documentation playground."""
    return """
    <!DOCTYPE html>
    <html lang="en">
    <head>
      <meta charset="utf-8" />
      <meta name="viewport" content="width=device-width, initial-scale=1" />
      <title>TeraBox Gateway API - Swagger UI</title>
      <link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css" />
      <link rel="icon" type="image/png" href="https://unpkg.com/swagger-ui-dist@5/favicon-32x32.png" sizes="32x32" />
      <style>
        html { box-sizing: border-box; overflow: -margin-y; }
        *, *:before, *:after { box-sizing: inherit; }
        body { margin: 0; background: #fafafa; }
        .swagger-ui .topbar { display: none; }
      </style>
    </head>
    <body>
      <div id="swagger-ui"></div>
      <script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js" charset="UTF-8"></script>
      <script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-standalone-preset.js" charset="UTF-8"></script>
      <script>
        window.onload = () => {
          window.ui = SwaggerUIBundle({
            url: '/swagger.json',
            dom_id: '#swagger-ui',
            presets: [
              SwaggerUIBundle.presets.apis,
              SwaggerUIBundle.SwaggerUIStandalonePreset
            ],
            layout: "BaseLayout",
            deepLinking: true,
            showExtensions: true,
            showCommonExtensions: true
          });
        };
      </script>
    </body>
    </html>
    """



if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)