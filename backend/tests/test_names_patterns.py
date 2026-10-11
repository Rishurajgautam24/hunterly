from ql.finder.domains import normalize_domain, suggest_domains
from ql.finder.names import clean_full_name, local_part, split_name
from ql.finder.patterns import infer_patterns, parse_size, rank_candidates, render


def test_split_name_cleans_linkedin_noise():
    assert split_name("Priya Sharma, PhD") == ("Priya", "Sharma")
    assert split_name("Dr. Rahul K. Verma (He/Him)") == ("Rahul", "Verma")
    assert split_name("Ananya Iyer | Hiring Backend Engineers") == ("Ananya", "Iyer")
    assert split_name("Madonna") == ("Madonna", "")
    assert clean_full_name("Arjun 🚀 Mehta") == "Arjun Mehta"


def test_local_part_folds_accents():
    assert local_part("José-Luis") == "joseluis"


def test_render_patterns():
    assert render("{first}.{last}", "Priya", "Sharma") == "priya.sharma"
    assert render("{f}{last}", "Priya", "Sharma") == "psharma"
    assert render("{first}.{last}", "Madonna", "") is None


def test_known_emails_beat_priors():
    known = [("Rohit Gupta", "rgupta@acme.in"), ("Neha Rao", "nrao@acme.in")]
    assert infer_patterns(known, "acme.in")["{f}{last}"] == 2
    ranked = rank_candidates("Priya", "Sharma", "acme.in", known)
    assert ranked[0].address == "psharma@acme.in"
    assert len({c.address for c in ranked}) == len(ranked) <= 30


def test_small_company_prefers_first_name():
    ranked = rank_candidates("Priya", "Sharma", "tiny.io", company_size="11-50")
    assert ranked[0].address == "priya@tiny.io"


def test_parse_size():
    assert parse_size("1,001-5,000") == 5000
    assert parse_size("10000+") == 10000
    assert parse_size("unknown") is None


def test_domains():
    assert normalize_domain("https://www.Acme.com/about") == "acme.com"
    assert normalize_domain("jane@acme.co.in") == "acme.co.in"
    assert normalize_domain("not a domain") is None
    assert suggest_domains("Acme Technologies Pvt Ltd")[0] == "acme.com"


def test_broken_resolver_is_reported(monkeypatch):
    import pytest

    from ql.finder import domains

    monkeypatch.setattr(domains, "_resolve_mx", lambda d, t: [])
    monkeypatch.setattr(domains, "_canary", None)
    with pytest.raises(domains.MXLookupError):
        domains.mx_hosts("acme.com")
    assert domains.has_mail_server("acme.com") is None


def test_real_no_mx_when_resolver_is_fine(monkeypatch):
    from ql.finder import domains

    monkeypatch.setattr(domains, "_resolve_mx", lambda d, t: ["mx.google.com"] if d == "gmail.com" else [])
    monkeypatch.setattr(domains, "_canary", None)
    assert domains.mx_hosts("nomail.example") == []
    assert domains.has_mail_server("nomail.example") is False


def test_numbered_pattern_is_learned():
    known = [("Sandeep Singh", "ssingh1@bluematterconsulting.com"),
             ("Rahul Mehta", "rmehta1@bluematterconsulting.com")]
    ranked = rank_candidates("Priya", "Sharma", "bluematterconsulting.com", known)
    assert ranked[0].address == "psharma1@bluematterconsulting.com"
    assert ranked[0].pattern == "{f}{last}1"


def test_numbered_and_middle_candidates_exist():
    ranked = rank_candidates("Sandeep", "Singh", "acme.com", middle="Kumar")
    addresses = {c.address for c in ranked}
    assert {"ssingh1@acme.com", "sandeep.singh@acme.com", "sksingh@acme.com",
            "sandeep.k.singh@acme.com", "singh.sandeep@acme.com"} <= addresses
    assert len(ranked) == len(addresses) <= 30


def test_name_parts():
    from ql.finder.names import name_parts
    assert name_parts("Sandeep Kumar Singh") == ("Sandeep", "Kumar", "Singh")
    assert name_parts("Priya Sharma") == ("Priya", "", "Sharma")
