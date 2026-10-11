import smtplib

from ql.finder.smtp_check import ACCEPT_ALL, INVALID, UNKNOWN, VALID, classify, verify_domain


def fake_smtp(replies: dict[str, int], default: int = 550, mail_code: int = 250):
    class FakeSMTP:
        sent_data = False

        def __init__(self, host, port, local_hostname=None, timeout=None):
            assert port == 25

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def ehlo_or_helo_if_needed(self):
            pass

        def has_extn(self, name):
            return False

        def mail(self, sender):
            return mail_code, b"ok"

        def rcpt(self, address):
            code = replies.get(address, default)
            return code, b"2.1.5 ok" if code == 250 else b"5.1.1 user unknown"

        def data(self, msg):  # must never be called
            raise AssertionError("verification must not send mail")

    return FakeSMTP


def run(replies, default=550, **kw):
    return verify_domain("acme.com", list(replies), helo_host="h", mail_from="v@h",
                         mx_lookup=lambda d: ["mx.acme.com"],
                         smtp_factory=fake_smtp(replies, default, **kw))


def test_valid_and_invalid():
    report = run({"priya.sharma@acme.com": 250, "priya@acme.com": 550})
    status = {r.address: r.status for r in report.results}
    assert status == {"priya.sharma@acme.com": VALID, "priya@acme.com": INVALID}
    assert report.catch_all is False


def test_catch_all_downgrades_valid():
    report = run({"a@acme.com": 250}, default=250)
    assert report.catch_all is True
    assert report.results[0].status == ACCEPT_ALL


def test_no_mx_means_invalid():
    report = verify_domain("nomail.example", ["a@nomail.example"], helo_host="h",
                           mail_from="v@h", mx_lookup=lambda d: [])
    assert report.results[0].status == INVALID and report.error == "no MX record"


def test_connection_failure_is_unknown():
    def boom(*a, **k):
        raise OSError("timed out")

    report = verify_domain("acme.com", ["a@acme.com"], helo_host="h", mail_from="v@h",
                           mx_lookup=lambda d: ["mx1", "mx2"], smtp_factory=boom)
    assert report.results[0].status == UNKNOWN and "port 25" in report.error


def test_sender_refused_is_unknown():
    report = run({"a@acme.com": 250}, mail_code=550)
    assert report.results[0].status == UNKNOWN


def test_policy_block_is_not_invalid():
    assert classify(550, "5.7.1 Service unavailable; client host blocked using Spamhaus") == UNKNOWN
    assert classify(450, "try later") == UNKNOWN
    assert classify(550, "5.1.1 no such user") == INVALID
    assert classify(550, "5.4.1 Recipient address rejected: Access denied. AS(201806281)") == INVALID
    assert classify(None) == UNKNOWN


def test_disconnect_mid_session():
    class Flaky(fake_smtp({}, 250)):
        calls = 0

        def rcpt(self, address):
            Flaky.calls += 1
            if Flaky.calls > 1:
                raise smtplib.SMTPServerDisconnected("bye")
            return 250, b"ok"

    report = verify_domain("acme.com", ["a@acme.com", "b@acme.com"], helo_host="h",
                           mail_from="v@h", mx_lookup=lambda d: ["mx"], smtp_factory=Flaky)
    assert [r.status for r in report.results] == [VALID, UNKNOWN]


def test_dns_failure_is_unknown_not_invalid():
    from ql.finder.domains import MXLookupError

    def fail(domain):
        raise MXLookupError("DNS lookup for acme.com failed: Timeout")

    report = verify_domain("acme.com", ["a@acme.com"], helo_host="h", mail_from="v@h", mx_lookup=fail)
    assert report.results[0].status == UNKNOWN and "DNS" in report.error


def test_starttls_failure_falls_back_to_plain():
    calls = []

    class TLSBroken(fake_smtp({"a@acme.com": 250})):
        def __init__(self, *a, **k):
            calls.append("connect")

        def has_extn(self, name):
            return name == "starttls"

        def starttls(self, context=None):
            raise OSError("bad certificate")

    report = verify_domain("acme.com", ["a@acme.com"], helo_host="h", mail_from="v@h",
                           mx_lookup=lambda d: ["mx"], smtp_factory=TLSBroken)
    assert report.results[0].status == VALID and calls == ["connect", "connect"]


def test_inconclusive_probe_is_not_false():
    class Grey(fake_smtp({"a@acme.com": 250}, default=451)):
        pass

    report = verify_domain("acme.com", ["a@acme.com"], helo_host="h", mail_from="v@h",
                           mx_lookup=lambda d: ["mx"], smtp_factory=Grey)
    assert report.catch_all is None


def test_blocklisted_ip_stops_probing_and_is_explained():
    from ql import services

    msg = (b"5.7.1 Service unavailable, Client host [103.133.159.191] blocked using Spamhaus. "
           b"To request removal from this list see https://www.spamhaus.org/query/ip/103.133.159.191")
    calls = []

    class Blocked(fake_smtp({})):
        def rcpt(self, address):
            calls.append(address)
            return 550, msg

    report = verify_domain("setoo.co", ["a@setoo.co", "b@setoo.co", "c@setoo.co"], helo_host="h",
                           mail_from="v@h", mx_lookup=lambda d: ["mx"], smtp_factory=Blocked)
    assert [r.status for r in report.results] == [UNKNOWN] * 3 and len(calls) == 1
    assert "blocklist" in services.explain(report.error, "setoo.co")
