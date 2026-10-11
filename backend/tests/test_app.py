import re

import pytest
from fastapi.testclient import TestClient

from ql.app import app


@pytest.fixture
def client():
    return TestClient(app)


def csrf(client, path="/"):
    return re.search(r'name="csrf" value="([^"]+)"', client.get("/contacts/new").text).group(1)


def test_pages_render(client):
    for path in ["/", "/companies", "/contacts", "/contacts/new", "/settings"]:
        assert client.get(path).status_code == 200, path


def test_form_needs_csrf(client):
    r = client.post("/companies", data={"name": "Acme"}, follow_redirects=False)
    assert r.status_code == 403
    r = client.post("/companies", data={"name": "Acme", "domain": "acme.com",
                                        "csrf": csrf(client)}, follow_redirects=False)
    assert r.status_code == 303


def test_manual_contact_and_pages(client):
    token = csrf(client)
    r = client.post("/contacts", data={"full_name": "Priya Sharma", "company_name": "Acme",
                                       "title": "EM", "csrf": token})
    assert r.status_code == 200 and "Priya Sharma" in r.text
    page = client.get("/contacts/1")
    assert "Find and check email" in page.text
    # guess-only finder works offline
    r = client.post("/contacts/1/find", data={"check": "0", "csrf": token})
    assert "domain" in r.text  # no domain yet -> helpful error


def test_capture_api(client):
    origin = "chrome-extension://" + "a" * 32
    body = {"full_name": "Priya Sharma", "headline": "EM at Acme",
            "linkedin_url": "https://www.linkedin.com/in/priya/"}
    r = client.post("/api/capture", json=body, headers={"Origin": origin})
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == origin
    data = r.json()
    assert data["created"] and data["company"] == "Acme" and data["captures_24h"] == 1
    look = client.get("/api/lookup", params={"url": "https://linkedin.com/in/PRIYA"}).json()
    assert look["found"] and look["contact_id"] == data["contact_id"]


def test_capture_api_rejects_forms_and_other_sites(client):
    r = client.post("/api/capture", data={"full_name": "x"})
    assert r.status_code == 415
    r = client.post("/api/capture", json={"full_name": "x"}, headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers


def test_bad_host_rejected():
    r = TestClient(app, base_url="http://evil.example").get("/")
    assert r.status_code == 400


def test_export_and_inline_email_edit(client):
    token = csrf(client)
    client.post("/contacts", data={"full_name": "Sandeep Singh", "company_name": "Blue Matter",
                                   "domain": "bluematterconsulting.com", "csrf": token})
    r = client.post("/contacts/1/email", data={"address": "ssingh1@bluematterconsulting.com",
                                               "next": "/contacts", "learn": "1", "csrf": token},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/contacts?")
    csv_text = client.get("/export/contacts.csv").text
    assert "ssingh1@bluematterconsulting.com" in csv_text and csv_text.startswith("﻿Name,")
    assert "attachment" in client.get("/export/contacts.csv").headers["content-disposition"]
    assert "bluematterconsulting.com" in client.get("/export/companies.csv").text
    assert 'name="address"' in client.get("/contacts").text  # inline edit box in the list
    page = client.get("/contacts/1").text
    assert 'value="ssingh1@bluematterconsulting.com"' in page


def test_typed_work_email_sets_company_domain(client):
    token = csrf(client)
    client.post("/contacts", data={"full_name": "Asha Rao", "company_name": "NoDomain Labs", "csrf": token})
    client.post("/contacts/1/email", data={"address": "asha@nodomainlabs.io", "csrf": token})
    assert "nodomainlabs.io" in client.get("/export/companies.csv").text
