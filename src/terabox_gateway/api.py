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
            if link_data.get("requires_password"):
                status_code = 400
            elif is_verification:
                status_code = 409
            elif err_code == -1 and "404" in str(link_data.get("details", "")):
                status_code = 404
            else:
                status_code = 502

            return (
                jsonify(
                    {
                        "status": "error",
                        "error": "provider_verification_required" if is_verification else err_type,
                        "errno": err_code,
                        "message": link_data.get("message") or link_data.get("error") or "Provider resolution failed",
                        "requires_verification": is_verification,
                        "requires_password": link_data.get("requires_password", False),
                        "stage": "provider_resolution",
                    }
                ),
                status_code,
            )

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
                status_code = 400 if resolved_data.get("requires_password") else (409 if is_verification else 502)
                return (
                    jsonify(
                        {
                            "status": "error",
                            "error": "provider_verification_required" if is_verification else err_type,
                            "errno": err_code,
                            "message": resolved_data.get("message") or resolved_data.get("error") or "Direct link resolution failed",
                            "requires_verification": is_verification,
                            "requires_password": resolved_data.get("requires_password", False),
                            "stage": "direct_link_resolution",
                        }
                    ),
                    status_code,
                )
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