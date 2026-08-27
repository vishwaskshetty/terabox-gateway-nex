from unittest.mock import AsyncMock, patch
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


def test_api_missing_params(client):
    response = client.get("/api")
    assert response.status_code == 400
    data = response.get_json()
    assert data["status"] == "error"
    assert data["error"] == "missing_parameter"
    assert "Missing required parameter" in data["message"]


def test_api_invalid_mode(client):
    response = client.get("/api?mode=invalid_mode")
    assert response.status_code == 400
    data = response.get_json()
    assert data["error"] == "invalid_mode"


def test_api_invalid_url(client):
    response = client.get("/api?url=https://example.com/s/12345")
    assert response.status_code == 400
    data = response.get_json()
    assert data["status"] == "error"
    assert data["error"] == "invalid_url"
    assert "Invalid TeraBox share URL" in data["message"]


@patch("terabox_gateway.api.fetch_download_link", new_callable=AsyncMock)
@patch("terabox_gateway.api.fetch_direct_links", new_callable=AsyncMock)
def test_api_successful_resolve(mock_direct, mock_download, client):
    mock_files = FileList([
        {
            "server_filename": "video.mp4",
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
            "filename": "video.mp4",
            "size": "7.73 MB",
            "size_bytes": 8108680,
            "link": "https://d.terabox.app/download/link1",
            "direct_link": "https://d.terabox.app/direct/video.mp4",
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
    assert f0["filename"] == "video.mp4"
    assert f0["direct_link"] == "https://d.terabox.app/direct/video.mp4"
    assert f0["download_link"] == "https://d.terabox.app/direct/video.mp4"


@patch("terabox_gateway.api.fetch_download_link", new_callable=AsyncMock)
def test_api_provider_verify_v2(mock_download, client):
    mock_download.return_value = {
        "error": "provider_verification_required",
        "errno": 400210,
        "message": "need verify_v2",
        "surl": "fKvukFFlwMqHt3vbdFoRYQ",
        "requires_verification": True,
        "requires_password": False,
    }

    response = client.get("/api?url=https://1024terabox.com/s/1fKvukFFlwMqHt3vbdFoRYQ&resolve=1&refresh=1")
    assert response.status_code == 409
    data = response.get_json()
    assert data["status"] == "error"
    assert data["error"] == "provider_verification_required"
    assert data["errno"] == 400210
    assert data["requires_verification"] is True
    assert data["requires_password"] is False


@patch("terabox_gateway.api.fetch_download_link", new_callable=AsyncMock)
def test_api_unexpected_exception(mock_download, client):
    mock_download.side_effect = RuntimeError("Unexpected internal crash")

    response = client.get("/api?url=https://1024terabox.com/s/1fKvukFFlwMqHt3vbdFoRYQ&resolve=1&refresh=1")
    assert response.status_code == 500
    data = response.get_json()
    assert data["status"] == "error"
    assert data["error"] == "internal_error"
    assert "Unexpected internal crash" not in data.get("error", "")
    assert data["stage"] == "resolver_execution"


def test_cors_headers(client):
    response = client.get("/health")
    assert response.headers.get("Access-Control-Allow-Origin") == "*"
