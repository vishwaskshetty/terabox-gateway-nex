import pytest
from main import app

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
    assert "Missing required parameter" in data["message"]

def test_api_invalid_mode(client):
    response = client.get("/api?mode=invalid_mode")
    assert response.status_code == 400
    data = response.get_json()
    assert data["error"] == "Invalid mode"

def test_api_invalid_url(client):
    response = client.get("/api?url=https://example.com/s/12345")
    assert response.status_code == 400
    data = response.get_json()
    assert data["status"] == "error"
    assert "Invalid TeraBox share URL" in data["message"]

def test_cors_headers(client):
    response = client.get("/health")
    assert response.headers.get("Access-Control-Allow-Origin") == "*"

