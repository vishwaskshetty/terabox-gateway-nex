from unittest.mock import AsyncMock, patch, MagicMock
import pytest
from main import app
from terabox_gateway.terabox_client import FileList


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


def test_health_endpoint(client):
    response = client.get("/health")
    assert response.status_code == 200
    data = response.get_json()
    assert data["status"] == "healthy"
    assert "timestamp" in data


def test_index_endpoint(client):
    response = client.get("/")
    assert response.status_code == 200
    data = response.get_json()
    assert data["name"] == "TeraBox API"
    assert data["status"] == "operational"
    assert "/health" in data["endpoints"]


def test_api_missing_url(client):
    response = client.get("/api")
    assert response.status_code == 400
    data = response.get_json()
    assert data["status"] == "error"
    assert data["error"] == "missing_parameter"
    assert "Missing required parameter" in data["message"]


def test_api_invalid_url(client):
    response = client.get("/api?url=https://example.com/s/12345")
    assert response.status_code == 400
    data = response.get_json()
    assert data["status"] == "error"
    assert data["error"] == "invalid_url"
    assert "Invalid TeraBox share URL" in data["message"]


# Scenario 1: Successful metadata + direct-link resolution
@patch("terabox_gateway.api.fetch_download_link", new_callable=AsyncMock)
@patch("terabox_gateway.api.fetch_direct_links", new_callable=AsyncMock)
def test_api_successful_resolution(mock_direct, mock_download, client):
    mock_files = FileList([
        {
            "server_filename": "2026-04-23-18-55-38(8).mp4",
            "size": 8108680,
            "dlink": "https://d.terabox.app/download/link1",
            "fs_id": "207400602392562",
            "isdir": "0",
            "thumbs": {"url3": "https://d.terabox.app/thumb1.jpg"},
        }
    ])
    mock_download.return_value = mock_files
    
    mock_direct_files = FileList([
        {
            "filename": "2026-04-23-18-55-38(8).mp4",
            "size": "7.73 MB",
            "size_bytes": 8108680,
            "link": "https://d.terabox.app/download/link1",
            "direct_link": "https://d.terabox.app/direct/2026-04-23-18-55-38(8).mp4",
            "thumbnail": "https://d.terabox.app/thumb1.jpg",
            "fs_id": "207400602392562",
        }
    ])
    mock_direct.return_value = mock_direct_files

    response = client.get("/api?url=https://1024terabox.com/s/1fKvukFFlwMqHt3vbdFoRYQ&resolve=1&refresh=1")
    assert response.status_code == 200
    data = response.get_json()
    assert data["status"] == "success"
    assert data["total_files"] == 1
    assert len(data["files"]) == 1
    f0 = data["files"][0]
    assert f0["filename"] == "2026-04-23-18-55-38(8).mp4"
    assert f0["direct_link"] == "https://d.terabox.app/direct/2026-04-23-18-55-38(8).mp4"
    assert f0["download_link"] == "https://d.terabox.app/direct/2026-04-23-18-55-38(8).mp4"


# Scenario 2: Provider errno=400210 (need verify_v2)
@patch("terabox_gateway.api.fetch_download_link", new_callable=AsyncMock)
def test_api_provider_errno_400210(mock_download, client):
    mock_download.return_value = {
        "status": "error",
        "error": "provider_verification_required",
        "errno": 400210,
        "message": "need verify_v2",
        "surl": "fKvukFFlwMqHt3vbdFoRYQ",
        "requires_verification": True,
        "requires_password": False,
        "verification_url": "https://1024terabox.com/s/1fKvukFFlwMqHt3vbdFoRYQ",
        "stage": "provider_resolution",
    }

    response = client.get("/api?url=https://1024terabox.com/s/1fKvukFFlwMqHt3vbdFoRYQ&resolve=1&refresh=1")
    assert response.status_code == 409
    data = response.get_json()
    assert data["status"] == "error"
    assert data["error"] == "provider_verification_required"
    assert data["errno"] == 400210
    assert data["requires_verification"] is True
    assert data["requires_password"] is False
    assert data["verification_url"] == "https://1024terabox.com/s/1fKvukFFlwMqHt3vbdFoRYQ"


# Scenario 3: Provider errno=400310 (access token verification)
@patch("terabox_gateway.api.fetch_download_link", new_callable=AsyncMock)
def test_api_provider_errno_400310(mock_download, client):
    mock_download.return_value = {
        "status": "error",
        "error": "provider_verification_required",
        "errno": 400310,
        "message": "need verify_v2",
        "surl": "abc1234",
        "requires_verification": True,
        "requires_password": False,
        "verification_url": "https://1024terabox.com/s/1abc1234",
        "stage": "provider_resolution",
    }

    response = client.get("/api?url=https://1024terabox.com/s/1abc1234&resolve=1&refresh=1")
    assert response.status_code == 409
    data = response.get_json()
    assert data["status"] == "error"
    assert data["error"] == "provider_verification_required"
    assert data["errno"] == 400310
    assert data["requires_verification"] is True


# Scenario 4: HTML returned from API endpoint (detected as HTML fallback / invalid content)
@patch("terabox_gateway.api.fetch_download_link", new_callable=AsyncMock)
def test_api_html_fallback_detected(mock_download, client):
    mock_download.return_value = {
        "status": "error",
        "error": "provider_verification_required",
        "errno": 400210,
        "message": "API returned HTML page instead of JSON API response",
        "requires_verification": True,
        "requires_password": False,
        "stage": "provider_resolution",
    }

    response = client.get("/api?url=https://1024terabox.com/s/1fKvukFFlwMqHt3vbdFoRYQ&resolve=1&refresh=1")
    assert response.status_code == 409
    data = response.get_json()
    assert data["status"] == "error"
    assert data["error"] == "provider_verification_required"


# Scenario 5: Missing session cookie handling (anonymously resolving with warning)
@patch("terabox_gateway.api.fetch_download_link", new_callable=AsyncMock)
@patch("terabox_gateway.api.fetch_direct_links", new_callable=AsyncMock)
def test_api_missing_session_cookie_anonymous_fallback(mock_direct, mock_download, client):
    mock_files = FileList([
        {
            "server_filename": "public_doc.pdf",
            "size": 102400,
            "dlink": "https://d.terabox.app/download/doc",
            "fs_id": "1001",
        }
    ])
    mock_files.fallback_no_cookie = True
    mock_files.used_cookies = False
    mock_download.return_value = mock_files

    mock_direct_files = FileList([
        {
            "filename": "public_doc.pdf",
            "size_bytes": 102400,
            "download_link": "https://d.terabox.app/download/doc",
            "direct_link": "https://d.terabox.app/direct/doc.pdf",
            "fs_id": "1001",
        }
    ])
    mock_direct_files.fallback_no_cookie = True
    mock_direct_files.used_cookies = False
    mock_direct.return_value = mock_direct_files

    response = client.get("/api?url=https://1024terabox.com/s/1public_share&resolve=1&refresh=1")
    assert response.status_code == 200
    data = response.get_json()
    assert data["status"] == "success"
    assert data["used_cookies"] is False
    assert data["fallback_no_cookie"] is True
    assert "warning" in data


# Scenario 6: Missing signing parameter in direct link resolution
@patch("terabox_gateway.api.fetch_download_link", new_callable=AsyncMock)
@patch("terabox_gateway.api.fetch_direct_links", new_callable=AsyncMock)
def test_api_missing_signing_parameter_returns_error(mock_direct, mock_download, client):
    mock_files = FileList([{"server_filename": "video.mp4", "fs_id": "123"}])
    mock_download.return_value = mock_files
    
    mock_direct.return_value = {
        "status": "error",
        "error": "provider_verification_required",
        "errno": 400210,
        "message": "Missing signing parameters from provider",
        "requires_verification": True,
        "stage": "direct_link_resolution",
    }

    response = client.get("/api?url=https://1024terabox.com/s/1video&resolve=1&refresh=1")
    assert response.status_code == 409
    data = response.get_json()
    assert data["status"] == "error"
    assert data["error"] == "provider_verification_required"
    assert data["requires_verification"] is True


# Scenario 7: Verification-required state with non-500 response
@patch("terabox_gateway.api.fetch_download_link", new_callable=AsyncMock)
def test_api_verification_required_state_status_409(mock_download, client):
    mock_download.return_value = {
        "status": "error",
        "error": "provider_verification_required",
        "errno": 400210,
        "message": "This link requires interactive or captcha verification from the provider",
        "surl": "xyz987",
        "requires_verification": True,
        "requires_password": False,
        "stage": "provider_resolution",
    }

    response = client.get("/api?url=https://1024terabox.com/s/1xyz987&resolve=1&refresh=1")
    assert response.status_code == 409
    data = response.get_json()
    assert data["status"] == "error"
    assert data["error"] == "provider_verification_required"
    assert data["stage"] == "provider_resolution"


# Scenario 8: Successful retry after authorized session verification
@patch("terabox_gateway.api.fetch_download_link", new_callable=AsyncMock)
@patch("terabox_gateway.api.fetch_direct_links", new_callable=AsyncMock)
def test_api_successful_retry_after_verification(mock_direct, mock_download, client):
    mock_files = FileList([
        {
            "server_filename": "verified_video.mp4",
            "size": 5000000,
            "dlink": "https://d.terabox.app/download/verified",
            "fs_id": "999",
        }
    ])
    mock_files.used_cookies = True
    mock_download.return_value = mock_files

    mock_direct_files = FileList([
        {
            "filename": "verified_video.mp4",
            "size_bytes": 5000000,
            "download_link": "https://d.terabox.app/download/verified",
            "direct_link": "https://d.terabox.app/direct/verified_cdn.mp4",
            "fs_id": "999",
        }
    ])
    mock_direct_files.used_cookies = True
    mock_direct.return_value = mock_direct_files

    response = client.get("/api?url=https://1024terabox.com/s/1verified_link&resolve=1&refresh=1")
    assert response.status_code == 200
    data = response.get_json()
    assert data["status"] == "success"
    assert data["used_cookies"] is True
    assert data["files"][0]["direct_link"] == "https://d.terabox.app/direct/verified_cdn.mp4"


# Scenario 9: Gateway unexpected exception (sanitized internal 500)
@patch("terabox_gateway.api.fetch_download_link", new_callable=AsyncMock)
def test_api_unexpected_exception_sanitized_500(mock_download, client):
    mock_download.side_effect = RuntimeError("Fatal DB / process crash with SECRET_KEY=xyz123")

    response = client.get("/api?url=https://1024terabox.com/s/1fKvukFFlwMqHt3vbdFoRYQ&resolve=1&refresh=1")
    assert response.status_code == 500
    data = response.get_json()
    assert data["status"] == "error"
    assert data["error"] == "internal_error"
    assert "SECRET_KEY" not in str(data)
    assert data["stage"] == "resolver_execution"


# Scenario 10: Expired direct link handling
@patch("terabox_gateway.api.fetch_download_link", new_callable=AsyncMock)
@patch("terabox_gateway.api.fetch_direct_links", new_callable=AsyncMock)
def test_api_direct_link_expired_fallback(mock_direct, mock_download, client):
    mock_files = FileList([{"server_filename": "file.mp4", "dlink": "https://expired.dlink.com", "fs_id": "1"}])
    mock_download.return_value = mock_files

    mock_direct_files = FileList([
        {
            "filename": "file.mp4",
            "size_bytes": 1000,
            "download_link": "https://expired.dlink.com",
            "direct_link": "https://expired.dlink.com",
            "fs_id": "1",
        }
    ])
    mock_direct.return_value = mock_direct_files

    response = client.get("/api?url=https://1024terabox.com/s/1test_exp&resolve=1&refresh=1")
    assert response.status_code == 200
    data = response.get_json()
    assert data["status"] == "success"
    assert len(data["files"]) == 1


# Scenario 11: Session isolation between different requests
@patch("terabox_gateway.api.fetch_download_link", new_callable=AsyncMock)
def test_session_isolation_between_requests(mock_download, client):
    # Distinct requests with different URLs should not pollute each other's cache
    mock_download.side_effect = [
        FileList([{"server_filename": "userA.mp4", "fs_id": "1", "dlink": "https://d.terabox.app/1"}]),
        FileList([{"server_filename": "userB.mp4", "fs_id": "2", "dlink": "https://d.terabox.app/2"}]),
    ]
    r1 = client.get("/api?url=https://1024terabox.com/s/1userA_share&refresh=1")
    r2 = client.get("/api?url=https://1024terabox.com/s/1userB_share&refresh=1")
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.get_json()["files"][0]["filename"] == "userA.mp4"
    assert r2.get_json()["files"][0]["filename"] == "userB.mp4" 


# Scenario 12: Dedicated verification session creation endpoint
@patch("terabox_gateway.api.fetch_download_link", new_callable=AsyncMock)
def test_create_verification_session_endpoint(mock_download, client):
    mock_download.return_value = {
        "status": "error",
        "error": "provider_verification_required",
        "errno": 400210,
        "message": "need verify_v2",
        "surl": "fKvukFFlwMqHt3vbdFoRYQ",
        "requires_verification": True,
        "verification_url": "https://1024terabox.com/s/1fKvukFFlwMqHt3vbdFoRYQ",
        "stage": "provider_resolution",
    }

    resp = client.post("/api/verification/session", json={"url": "https://1024terabox.com/s/1fKvukFFlwMqHt3vbdFoRYQ"})
    assert resp.status_code == 409
    data = resp.get_json()
    assert data["status"] == "verification_required"
    assert "session_id" in data
    assert data["verification_url"] == "https://1024terabox.com/s/1fKvukFFlwMqHt3vbdFoRYQ"
    assert data["requires_verification"] is True


# Scenario 13: Verification complete retry - success case
@patch("terabox_gateway.api.fetch_download_link", new_callable=AsyncMock)
@patch("terabox_gateway.api.fetch_direct_links", new_callable=AsyncMock)
def test_verification_complete_success_flow(mock_direct, mock_download, client):
    from terabox_gateway.session_store import session_store
    
    # 1. Create active session
    session = session_store.create_session(
        url="https://1024terabox.com/s/1fKvukFFlwMqHt3vbdFoRYQ",
        surl="fKvukFFlwMqHt3vbdFoRYQ",
        verification_url="https://1024terabox.com/s/1fKvukFFlwMqHt3vbdFoRYQ",
        files=[{"server_filename": "video.mp4", "size": 8108680, "fs_id": "1"}],
    )

    # 2. Mock direct link success
    mock_direct_files = FileList([
        {
            "filename": "video.mp4",
            "size_bytes": 8108680,
            "download_link": "https://d.terabox.app/download/video.mp4",
            "direct_link": "https://d.terabox.app/direct/video.mp4",
            "fs_id": "1",
        }
    ])
    mock_direct.return_value = mock_direct_files

    # 3. Call complete
    resp = client.post("/api/verification/complete", json={"session_id": session.session_id})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "success"
    assert data["session_id"] == session.session_id
    assert len(data["files"]) == 1
    assert data["files"][0]["direct_link"] == "https://d.terabox.app/direct/video.mp4"


# Scenario 14: Verification complete retry - still requires verification
@patch("terabox_gateway.api.fetch_direct_links", new_callable=AsyncMock)
def test_verification_complete_still_requires_verification(mock_direct, client):
    from terabox_gateway.session_store import session_store
    
    session = session_store.create_session(
        url="https://1024terabox.com/s/1fKvukFFlwMqHt3vbdFoRYQ",
        surl="fKvukFFlwMqHt3vbdFoRYQ",
        verification_url="https://1024terabox.com/s/1fKvukFFlwMqHt3vbdFoRYQ",
    )

    mock_direct.return_value = {
        "status": "error",
        "error": "provider_verification_required",
        "errno": 400210,
        "message": "need verify_v2",
        "requires_verification": True,
    }

    resp = client.post("/api/verification/complete", json={"session_id": session.session_id})
    assert resp.status_code == 409
    data = resp.get_json()
    assert data["status"] == "error"
    assert data["error"] == "provider_verification_required"
    assert data["requires_verification"] is True


# Scenario 15: Verification complete on expired session
def test_verification_complete_expired_session(client):
    resp = client.post("/api/verification/complete", json={"session_id": "nonexistent_or_expired_id"})
    assert resp.status_code == 410
    data = resp.get_json()
    assert data["status"] == "error"
    assert data["error"] == "verification_expired"


def test_cors_headers(client):
    response = client.get("/health")
    assert response.headers.get("Access-Control-Allow-Origin") == "*"
