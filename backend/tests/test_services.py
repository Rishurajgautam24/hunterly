from datetime import timedelta

import pytest

from hunterly import services
from hunterly.finder.smtp_check import AddressResult, DomainReport
from hunterly.models import Outreach, utcnow
from hunterly.services import HunterlyError


@pytest.fixture
def fake_verify(monkeypatch):
    """Mail server that only knows priya.sharma@acme.com."""
    def verify(domain, addresses):
        results = [AddressResult(a, "valid" if a == "priya.sharma@acme.com" else "invalid", 250)
                   for a in addresses]
        return DomainReport(domain, "mx.acme.com", False, results)

    monkeypatch.setattr(services, "verify", verify)


def make_contact(session, **kw):
    data = dict(full_name="Priya Sharma", company_name="Acme", domain="acme.com",
                title="Engineering Manager")
    data.update(kw)
    contact, _ = services.save_contact(session, **data)
    return contact


def test_headline_split_and_dedupe(session):
    c1, created = services.capture_from_linkedin(
        session, full_name="Priya Sharma", headline="Engineering Manager at Acme | Hiring",
        linkedin_url="https://www.linkedin.com/in/Priya-Sharma/?trk=x")
    assert created and c1.title == "Engineering Manager"
    c2, created = services.capture_from_linkedin(
        session, full_name="Priya Sharma", linkedin_url="linkedin.com/in/priya-sharma")
    assert not created and c2.id == c1.id


def test_capture_cap(session, monkeypatch):
    monkeypatch.setenv("HUNTERLY_DAILY_CAPTURE_CAP", "2")
    from hunterly.config import get_settings
    get_settings.cache_clear()
    for i in range(2):
        services.capture_from_linkedin(session, full_name=f"P {i}",
                                       linkedin_url=f"https://linkedin.com/in/p{i}")
    with pytest.raises(HunterlyError, match="cap"):
        services.capture_from_linkedin(session, full_name="P 3")


def test_find_email_picks_valid(session, fake_verify):
    contact = make_contact(session)
    outcome = services.find_email(session, contact)
    assert outcome.best.address == "priya.sharma@acme.com"
    assert contact.stage == "ready" and contact.email_status == "valid"


def test_find_needs_domain(session):
    contact = make_contact(session, company_name="NoDomain Co", domain=None)
    with pytest.raises(HunterlyError, match="domain"):
        services.find_email(session, contact)


def test_full_outreach_flow(session, fake_verify):
    contact = make_contact(session)
    services.find_email(session, contact)

    outreach, draft = services.draft_next(session, contact)
    assert draft.source == "template" and outreach.step == 1  # Ollama is not running in tests
    with pytest.raises(HunterlyError, match="gaps"):
        services.mark_sent(session, outreach)

    services.save_outreach_text(session, outreach, "Backend role at Acme", "Hi Priya, ...")
    services.mark_sent(session, outreach)
    assert contact.stage == "contacted"

    # not due yet
    assert services.due_followups(session) == []
    with pytest.raises(HunterlyError, match="isn't due"):
        services.draft_next(session, contact)
    # day 4: follow-up 2 is due
    due = services.due_followups(session, now=utcnow() + timedelta(days=4, minutes=1))
    assert [f.step for f in due] == [2]
    backdate(session, outreach, days=5)

    o2, _ = services.draft_next(session, contact)
    assert o2.step == 2 and o2.subject.startswith("Re: Backend role at Acme")

    services.mark_replied(session, contact)
    assert services.due_followups(session, now=utcnow() + timedelta(days=30)) == []
    with pytest.raises(HunterlyError):
        services.draft_next(session, contact)
    stats = services.stats(session)
    assert stats["reply_rate"] == 100.0 and stats["contacted"] == 1


def backdate(session, outreach, days):
    outreach.sent_at = outreach.sent_at - timedelta(days=days)
    session.add(outreach)
    session.commit()


def test_three_message_limit(session, fake_verify):
    contact = make_contact(session)
    services.find_email(session, contact)
    for _ in range(3):
        o, _ = services.draft_next(session, contact)
        services.save_outreach_text(session, o, "s", "b")
        services.mark_sent(session, o)
        backdate(session, o, days=10)
    with pytest.raises(HunterlyError, match="limit"):
        services.draft_next(session, contact)


def test_bounce_switches_address(session, monkeypatch):
    def verify(domain, addresses):  # catch-all domain: everything accepted
        return DomainReport(domain, "mx", True, [AddressResult(a, "accept_all", 250)
                                                 for a in addresses])
    monkeypatch.setattr(services, "verify", verify)
    contact = make_contact(session)
    services.find_email(session, contact)
    first = contact.email
    o, _ = services.draft_next(session, contact)
    services.save_outreach_text(session, o, "s", "b")
    services.mark_sent(session, o)
    nxt = services.mark_bounced(session, contact)
    assert nxt and nxt != first and contact.stage == "ready"
    assert session.get(Outreach, o.id).outcome == "bounced"


def test_do_not_contact_blocks_drafts(session, fake_verify):
    contact = make_contact(session)
    services.find_email(session, contact)
    services.set_do_not_contact(session, contact)
    with pytest.raises(HunterlyError, match="do-not-contact"):
        services.draft_next(session, contact)


def test_csv_import(session):
    text = "Name,Domain,Size,Open Roles\nAcme,https://acme.com,51-200,Backend\nBeta,,,\n,x.com,,\n"
    assert services.import_companies_csv(session, text) == 2
    assert services.find_company(session, "acme").domain == "acme.com"


def test_known_email_must_match_domain(session):
    with pytest.raises(HunterlyError):
        services.add_known_email(session, "acme.com", "Rohit", "rohit@other.com")


def test_bounce_restarts_at_first_email_and_find_remembers(session, monkeypatch):
    def verify(domain, addresses):  # catch-all domain: everything accepted
        return DomainReport(domain, "mx", True, [AddressResult(a, "accept_all", 250)
                                                 for a in addresses])
    monkeypatch.setattr(services, "verify", verify)
    contact = make_contact(session)
    services.find_email(session, contact)
    bad = contact.email
    o, _ = services.draft_next(session, contact)
    services.save_outreach_text(session, o, "s", "b")
    services.mark_sent(session, o)
    services.mark_bounced(session, contact)
    o2, _ = services.draft_next(session, contact)
    assert o2.step == 1 and o2.to_address != bad
    services.find_email(session, contact)  # re-running must not bring the bounced one back
    assert contact.email != bad
    assert services.due_followups(session, now=utcnow() + timedelta(days=30)) == []


def test_failed_recheck_keeps_valid_address(session, fake_verify, monkeypatch):
    contact = make_contact(session)
    services.find_email(session, contact)
    assert contact.email_status == "valid"

    def broken(domain, addresses):
        return DomainReport(domain, None, None, [AddressResult(a, "unknown") for a in addresses],
                            error="DNS lookup failed")
    monkeypatch.setattr(services, "verify", broken)
    services.find_email(session, contact)
    assert contact.email == "priya.sharma@acme.com" and contact.email_status == "valid"


def test_mark_sent_guards(session, fake_verify):
    contact = make_contact(session)
    services.find_email(session, contact)
    o, _ = services.draft_next(session, contact)
    services.save_outreach_text(session, o, "[[target role]] at Acme", "Hi")
    with pytest.raises(HunterlyError, match="gaps"):
        services.mark_sent(session, o)
    services.save_outreach_text(session, o, "Role at Acme", "Hi")
    services.mark_replied(session, contact)
    with pytest.raises(HunterlyError):
        services.mark_sent(session, o)
    assert contact.stage == "replied"


def test_cap_counts_sends(session, fake_verify, monkeypatch):
    monkeypatch.setenv("HUNTERLY_DAILY_FIRST_EMAIL_CAP", "1")
    from hunterly.config import get_settings
    get_settings.cache_clear()
    a = make_contact(session)
    b = make_contact(session, full_name="Rohit Gupta", linkedin_url="https://linkedin.com/in/rg")
    services.find_email(session, a)
    b.email = "rohit.gupta@acme.com"
    session.add(b)
    session.commit()
    oa, _ = services.draft_next(session, a)
    ob, _ = services.draft_next(session, b)  # drafting doesn't use up the cap
    for o in (oa, ob):
        services.save_outreach_text(session, o, "s", "b")
    services.mark_sent(session, oa)
    with pytest.raises(HunterlyError, match="first emails"):
        services.mark_sent(session, ob)


def test_non_linkedin_url_is_dropped():
    assert services.normalize_linkedin_url("javascript:alert(1)") is None
    assert services.normalize_linkedin_url("https://www.linkedin.com/in/Ab-C/x") == \
        "https://www.linkedin.com/in/ab-c/"


def test_capture_uses_experience_and_about(session):
    contact, _ = services.capture_from_linkedin(
        session, full_name="Priya Sharma", headline="Payments | Hiring",
        about="I lead the payments platform team.",
        experience=[{"title": "Engineering Manager", "company": "Razorpay", "dates": "2022 - Present",
                     "current": True, "company_url": "https://www.linkedin.com/company/razorpay/"},
                    {"title": "SDE 2", "company": "PhonePe", "dates": "2019 - 2022"}],
        company_linkedin_url="https://www.linkedin.com/company/razorpay/",
        linkedin_url="https://www.linkedin.com/in/priya/")
    company = session.get(services.Company, contact.company_id)
    assert company.name == "Razorpay" and company.linkedin_url.endswith("/company/razorpay/")
    assert contact.title == "Engineering Manager" and contact.about.startswith("I lead")
    assert [e["company"] for e in services.experience_of(contact)] == ["Razorpay", "PhonePe"]


def test_domain_suggestions_use_linkedin_slug(session, monkeypatch):
    monkeypatch.setattr(services, "has_mail_server", lambda d, timeout=3: d in {"razorpay.com", "rzp.io"})
    company = services.upsert_company(session, "Razorpay Software Pvt Ltd",
                                      linkedin_url="https://www.linkedin.com/company/razorpay/")
    got = [s["domain"] for s in services.domain_suggestions(company)]
    assert got == ["razorpay.com"]


def test_old_database_gets_new_columns(tmp_path, monkeypatch):
    import sqlite3

    from hunterly import db
    from hunterly.config import get_settings

    data = tmp_path / "old"
    data.mkdir()
    con = sqlite3.connect(data / "hunterly.db")
    con.execute("CREATE TABLE contact (id INTEGER PRIMARY KEY, full_name VARCHAR NOT NULL)")
    con.commit()
    con.close()
    monkeypatch.setenv("HUNTERLY_DATA_DIR", str(data))
    get_settings.cache_clear()
    db.reset_engine()
    with db.get_engine().connect() as conn:
        cols = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(contact)")}
    assert {"about", "experience", "stage", "email"} <= cols


def test_recapture_updates_without_using_cap_and_drops_bad_company(session, monkeypatch):
    monkeypatch.setenv("HUNTERLY_DAILY_CAPTURE_CAP", "1")
    from hunterly.config import get_settings
    get_settings.cache_clear()
    url = "https://www.linkedin.com/in/deepak/"
    bad, _ = services.capture_from_linkedin(
        session, full_name="Deepak Gopinath", title="Jul 2021 - Feb 2024",
        company_name="VP Consulting Services", linkedin_url=url)
    bad_company = bad.company_id
    fixed, created = services.capture_from_linkedin(
        session, full_name="Deepak Gopinath", linkedin_url=url,
        experience=[{"title": "Partner", "company": "Blue Matter", "dates": "Apr 2024 - Present",
                     "current": True}])
    assert not created and fixed.title == "Partner"
    assert session.get(services.Company, fixed.company_id).name == "Blue Matter"
    assert session.get(services.Company, bad_company) is None
    assert services.captures_last_24h(session) == 1


def test_set_email_by_hand_learns_and_updates_drafts(session, fake_verify):
    contact = make_contact(session)
    services.find_email(session, contact)
    o, _ = services.draft_next(session, contact)
    row = services.set_email(session, contact, "PSharma1@acme.com", check=False, learn=True)
    assert contact.email == "psharma1@acme.com" and row.pattern == "{f}{last}1"  # was a guess
    typed = services.set_email(session, contact, "ps.work@acme.com", check=False)
    assert typed.pattern == "typed by you" and contact.email == "ps.work@acme.com"
    assert session.get(Outreach, o.id).to_address == "ps.work@acme.com"
    other, _ = services.save_contact(session, full_name="Rohit Gupta", company_name="Acme",
                                     linkedin_url="https://linkedin.com/in/rohit")
    services.find_email(session, other, check=False)
    assert other.email == "rgupta1@acme.com"  # learned {f}{last}{n} from the address above
    with pytest.raises(HunterlyError):
        services.set_email(session, contact, "not an email")


def test_hunter_fallback_when_checks_fail(session, monkeypatch):
    from hunterly.finder import hunter

    def unknown(domain, addresses):
        return DomainReport(domain, None, None, [AddressResult(a, "unknown") for a in addresses],
                            error="mx.acme.com: TimeoutError (outbound port 25 may be blocked)")
    monkeypatch.setattr(services, "verify", unknown)
    contact = make_contact(session)
    outcome = services.find_email(session, contact)
    assert contact.email_status == "unknown" and "port 25" in outcome.messages[0]

    services.save_hunter_key(session, "test-key")
    monkeypatch.setattr(hunter, "find",
                        lambda d, first, last, k: hunter.HunterResult("priya@acme.com", "valid", 96))
    outcome = services.find_email(session, contact)
    assert contact.email == "priya@acme.com" and contact.email_status == "valid"


def test_data_dir_expands_home(monkeypatch, tmp_path):
    from pathlib import Path

    from hunterly.config import get_settings

    monkeypatch.setenv("HUNTERLY_DATA_DIR", "~/hunterly-test-data")
    get_settings.cache_clear()
    assert get_settings().data_dir == Path.home() / "hunterly-test-data"
