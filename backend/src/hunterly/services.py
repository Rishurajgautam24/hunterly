"""Business logic shared by the web pages and the extension API."""

import csv
import io
import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlmodel import Session, col, func, select

from hunterly import drafts, gmail
from hunterly.config import get_settings
from hunterly.finder import hunter
from hunterly.finder.domains import has_mail_server, normalize_domain, suggest_domains
from hunterly.finder.names import name_parts, split_name
from hunterly.finder.patterns import rank_candidates
from hunterly.finder.smtp_check import ACCEPT_ALL, INVALID, UNKNOWN, VALID, DomainReport
from hunterly.finder.verifier import verify
from hunterly.models import (
    Capture,
    Company,
    Contact,
    EmailCandidate,
    KnownEmail,
    Outreach,
    Setting,
    utcnow,
)

PROFILE_KEYS = ["my_name", "my_pitch", "my_links", "target_role", "signature"]
EMAIL_RE = re.compile(r"^[^@\s]+@[a-z0-9.-]+\.[a-z]{2,}$")
WEBMAIL = {"gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.in", "outlook.com", "hotmail.com",
           "live.com", "icloud.com", "me.com", "proton.me", "protonmail.com", "rediffmail.com",
           "aol.com", "zoho.com"}


class HunterlyError(Exception):
    """A rule was broken (cap reached, do-not-contact, missing data). Shown to the user."""


# ---------- profile ----------

def get_profile(session: Session) -> dict:
    rows = session.exec(select(Setting).where(col(Setting.key).in_(PROFILE_KEYS))).all()
    profile = {k: "" for k in PROFILE_KEYS}
    profile.update({r.key: r.value for r in rows})
    return profile


def save_profile(session: Session, values: dict) -> None:
    for key in PROFILE_KEYS:
        row = session.get(Setting, key) or Setting(key=key)
        row.value = (values.get(key) or "").strip()
        session.add(row)
    session.commit()


def hunter_key(session: Session) -> str | None:
    row = session.get(Setting, "hunter_api_key")
    return (row.value.strip() if row and row.value.strip() else None) or get_settings().hunter_api_key


def save_hunter_key(session: Session, key: str) -> None:
    row = session.get(Setting, "hunter_api_key") or Setting(key="hunter_api_key")
    row.value = key.strip()
    session.add(row)
    session.commit()


def explain(error: str | None, domain: str) -> str | None:
    """Turn a checker error into a plain sentence."""
    if not error:
        return None
    low = error.lower()
    if any(w in low for w in ("spamhaus", "blocked using", "blocklist", "blacklist", "client host",
                              "5.7.1", "5.7.606", "reputation", "rbl")):
        return (f"{domain}'s mail server refused to talk to your internet connection: your public "
                "IP is on a spam blocklist (Spamhaus). That's normal for home and mobile "
                "connections and says nothing about this address. The result stays unknown. "
                "For real checks, add a Hunter key in Settings or use the verifier VM.")
    if "port 25" in low or "timed out" in low or "timeout" in low or "refused" in low:
        return (f"Hunterly couldn't reach {domain}'s mail server: your network most likely blocks port 25, "
                "which these checks need (common on home internet in India). The results are "
                "pattern guesses. Add a Hunter key in Settings, or use the verifier VM.")
    if "dns" in low:
        return "DNS lookups failed on this network, so nothing could be checked."
    if "limit" in low:
        return error
    if "no mx" in low:
        return f"{domain} has no mail server, so no address there can work. Check the domain."
    return error


# ---------- companies ----------

def find_company(session: Session, name: str | None, domain: str | None = None) -> Company | None:
    if domain:
        hit = session.exec(select(Company).where(Company.domain == domain)).first()
        if hit:
            return hit
    if name:
        return session.exec(
            select(Company).where(func.lower(Company.name) == name.strip().lower())
        ).first()
    return None


def upsert_company(session: Session, name: str, domain: str | None = None, **fields) -> Company:
    domain = normalize_domain(domain)
    company = find_company(session, name, domain)
    if company is None:
        company = Company(name=name.strip(), domain=domain)
    elif domain and not company.domain:
        company.domain = domain
    for key, value in fields.items():
        if value not in (None, "") and hasattr(company, key):
            setattr(company, key, value.strip() if isinstance(value, str) else value)
    session.add(company)
    session.commit()
    session.refresh(company)
    return company


def import_companies_csv(session: Session, text: str) -> int:
    """CSV columns (header names, any case): name, domain, size, funding_stage, open_roles,
    website, source, notes. Only name is required. Returns rows imported."""
    reader = csv.DictReader(io.StringIO(text))
    count = 0
    for raw in reader:
        row = {(k or "").strip().lower().replace(" ", "_"): (v or "").strip() for k, v in raw.items()}
        if not row.get("name"):
            continue
        upsert_company(
            session, row["name"], row.get("domain") or row.get("website"),
            size=row.get("size"), funding_stage=row.get("funding_stage") or row.get("stage"),
            open_roles=row.get("open_roles"), website=row.get("website"),
            source=row.get("source") or "csv", notes=row.get("notes"),
        )
        count += 1
    return count


def add_known_email(session: Session, domain: str, full_name: str, address: str,
                    source: str | None = None) -> KnownEmail:
    domain = normalize_domain(domain) or ""
    address = address.strip().lower()
    if not domain or not address.endswith("@" + domain):
        raise HunterlyError(f"{address} is not an address at {domain or 'that domain'}")
    known = KnownEmail(domain=domain, full_name=full_name.strip(), address=address, source=source)
    session.add(known)
    session.commit()
    return known


# ---------- contacts ----------

def normalize_linkedin_url(url: str | None) -> str | None:
    """Canonical profile URL, or None if it isn't a linkedin.com/in/ profile."""
    if not url:
        return None
    m = re.search(r"linkedin\.com/in/([A-Za-z0-9\-_%.]+)", url, re.I)
    return f"https://www.linkedin.com/in/{m.group(1).lower()}/" if m else None


def split_headline(headline: str | None) -> tuple[str | None, str | None]:
    """'Engineering Manager at Acme | Hiring' -> ('Engineering Manager', 'Acme')."""
    if not headline:
        return None, None
    first = re.split(r"\s[|•·]\s", headline)[0]
    m = re.match(r"(.+?)\s+(?:at|@)\s+(.+)", first, re.I)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return first.strip(), None


def captures_last_24h(session: Session) -> int:
    since = utcnow() - timedelta(hours=24)
    return session.exec(select(func.count()).select_from(Capture)
                        .where(Capture.created_at >= since)).one()


def save_contact(
    session: Session,
    *,
    full_name: str,
    company_name: str | None = None,
    domain: str | None = None,
    title: str | None = None,
    headline: str | None = None,
    location: str | None = None,
    linkedin_url: str | None = None,
    notes: str | None = None,
    about: str | None = None,
    experience: list[dict] | None = None,
    company_linkedin_url: str | None = None,
    source: str = "manual",
) -> tuple[Contact, bool]:
    """Create or update a contact. Returns (contact, created)."""
    if not full_name or not full_name.strip():
        raise HunterlyError("A name is required")
    url = normalize_linkedin_url(linkedin_url)
    experience = [e for e in (experience or []) if e.get("title") or e.get("company")][:10]
    current = next((e for e in experience if e.get("current")), experience[0] if experience else None)
    head_title, head_company = split_headline(headline)
    company_name = company_name or (current or {}).get("company") or head_company
    if not title and current and current.get("company") == company_name:
        title = current.get("title")
    title = title or head_title

    company_url = company_linkedin_url if company_linkedin_url and \
        "linkedin.com/company/" in company_linkedin_url else None
    company = upsert_company(session, company_name, domain, linkedin_url=company_url) \
        if company_name else None

    contact = None
    if url:
        contact = session.exec(select(Contact).where(Contact.linkedin_url == url)).first()
    created = contact is None
    if created:
        contact = Contact(full_name=full_name.strip())
    first, last = split_name(full_name)
    contact.full_name, contact.first_name, contact.last_name = full_name.strip(), first, last
    for key, value in {"title": title, "headline": headline, "location": location,
                       "linkedin_url": url, "notes": notes, "about": about}.items():
        if value:
            setattr(contact, key, value.strip())
    if experience:
        contact.experience = json.dumps(experience)
    old_company_id = contact.company_id
    if company:
        contact.company_id = company.id
    contact.source = contact.source or source
    contact.updated_at = utcnow()
    session.add(contact)
    session.commit()
    session.refresh(contact)
    if old_company_id and old_company_id != contact.company_id:
        _drop_if_orphan(session, old_company_id)
    return contact, created


def _drop_if_orphan(session: Session, company_id: int) -> None:
    """Remove a company left with no contacts and no domain (e.g. from a bad earlier read)."""
    company = session.get(Company, company_id)
    if company is None or company.domain:
        return
    if session.exec(select(Contact).where(Contact.company_id == company_id)).first():
        return
    session.delete(company)
    session.commit()


def delete_company(session: Session, company: Company) -> None:
    if session.exec(select(Contact).where(Contact.company_id == company.id)).first():
        raise HunterlyError("Move or delete its contacts first.")
    session.delete(company)
    session.commit()


def capture_from_linkedin(session: Session, **data) -> tuple[Contact, bool]:
    """Save a profile captured by the extension. New people count towards the daily cap;
    updating someone already in Hunterly does not."""
    url = normalize_linkedin_url(data.get("linkedin_url"))
    existing = session.exec(select(Contact).where(Contact.linkedin_url == url)).first() if url else None
    cap = get_settings().daily_capture_cap
    if existing is None and captures_last_24h(session) >= cap:
        raise HunterlyError(f"Daily capture cap reached ({cap} new people in 24 hours). Try again tomorrow.")
    contact, created = save_contact(session, source="linkedin", **data)
    if created:
        session.add(Capture(url=contact.linkedin_url))
        session.commit()
    return contact, created


def experience_of(contact: Contact) -> list[dict]:
    try:
        return json.loads(contact.experience) if contact.experience else []
    except ValueError:
        return []


def domain_suggestions(company: Company, limit: int = 4) -> list[dict]:
    """Likely email domains for a company, checked for mail servers in parallel."""
    guesses: list[str] = []
    if company.website:
        guesses.append(normalize_domain(company.website) or "")
    if company.linkedin_url:
        m = re.search(r"linkedin\.com/company/([^/?#]+)", company.linkedin_url)
        if m:
            slug = re.sub(r"[^a-z0-9]", "", m.group(1).lower())
            guesses += [slug + ".com", slug + ".in", slug + ".io"]
    guesses += suggest_domains(company.name)
    guesses = [g for g in dict.fromkeys(guesses) if g][:8]
    with ThreadPoolExecutor(max_workers=8) as pool:
        checks = list(pool.map(lambda d: has_mail_server(d, timeout=3), guesses))
    ranked = [{"domain": d, "mx": ok} for d, ok in zip(guesses, checks, strict=True) if ok is not False]
    return ranked[:limit]


# ---------- email finding ----------

@dataclass
class FinderOutcome:
    candidates: list[EmailCandidate]
    report: DomainReport | None
    best: EmailCandidate | None
    messages: list[str] = field(default_factory=list)


def choose_best(rows: list[EmailCandidate], catch_all: bool | None) -> EmailCandidate | None:
    ranked = sorted(rows, key=lambda r: r.rank)
    for wanted in (VALID, ACCEPT_ALL, UNKNOWN, "unchecked"):
        for row in ranked:
            if row.status == wanted:
                return row
    return None


_RANK = {VALID: 0, ACCEPT_ALL: 1, UNKNOWN: 2, "unchecked": 3, INVALID: 4}
LOCKED_STAGES = ("contacted", "replied", "closed")


def find_email(session: Session, contact: Contact, check: bool = True) -> FinderOutcome:
    company = session.get(Company, contact.company_id) if contact.company_id else None
    if company is None or not company.domain:
        raise HunterlyError("Add the company's email domain first (for example acme.com).")
    if not contact.first_name:
        raise HunterlyError("The contact needs at least a first name.")

    known = [(k.full_name, k.address) for k in
             session.exec(select(KnownEmail).where(KnownEmail.domain == company.domain))]
    _, middle, _ = name_parts(contact.full_name)
    candidates = rank_candidates(contact.first_name, contact.last_name, company.domain,
                                 known, company.size, middle=middle)
    to_check = [c.address for c in candidates[:get_settings().max_checks]]
    report = verify(company.domain, to_check) if check else None
    by_address = {r.address: r for r in report.results} if report else {}
    now = utcnow()

    old_rows = list(session.exec(select(EmailCandidate)
                                 .where(EmailCandidate.contact_id == contact.id)))
    bounced = {r.address for r in old_rows if r.detail == "bounced"}
    for old in old_rows:
        session.delete(old)
    rows = []
    for rank, cand in enumerate(candidates, start=1):
        result = by_address.get(cand.address)
        status = result.status if result else "unchecked"
        detail = (" ".join(str(x) for x in (result.code, result.message) if x) or None) \
            if result else None
        if cand.address in bounced:  # a real bounce beats any server answer
            status, detail = INVALID, "bounced"
        rows.append(EmailCandidate(
            contact_id=contact.id, address=cand.address, pattern=cand.pattern, rank=rank,
            score=cand.score, status=status, detail=detail, checked_at=now if result else None,
        ))
    messages = []
    key = hunter_key(session) if check else None
    if key and not any(r.status == VALID for r in rows):
        try:
            found = hunter.find(company.domain, contact.first_name, contact.last_name, key)
        except hunter.HunterError as exc:
            found = None
            messages.append(str(exc) + ".")
        if found:
            row = next((r for r in rows if r.address == found.email), None)
            if row is None:
                row = EmailCandidate(contact_id=contact.id, address=found.email, pattern="Hunter",
                                     rank=0, score=1.0)
                rows.append(row)
            if row.detail != "bounced":
                row.rank, row.status = 0, found.status
            row.detail = f"Hunter: {found.status.replace('_', '-')}" + \
                (f", score {found.score}" if found.score is not None else "")
            row.checked_at = now
            messages.append(f"Hunter suggests {found.email} ({found.status.replace('_', '-')}).")
        elif not messages:
            messages.append("Hunter had no address for this person.")
    session.add_all(rows)

    if report is not None:
        if report.mx_host or report.error == "no MX record":
            company.mx_ok = report.mx_host is not None
        session.add(company)
        reason = explain(report.error, company.domain)
        if reason and not any(r.status == VALID for r in rows):
            messages.insert(0, reason)
        if report.catch_all:
            messages.append("This domain accepts every address, so results are accept-all: "
                            "pick by pattern evidence.")
    if known:
        messages.append(f"Pattern learned from {len(known)} known address(es).")

    best = choose_best(rows, report.catch_all if report else None)
    current_new = next((r.status for r in rows if r.address == contact.email), None)
    if contact.stage in LOCKED_STAGES or contact.do_not_contact:
        messages.append("Already emailed (or closed), so the chosen address was not changed.")
        if current_new:
            contact.email_status = current_new
    elif (contact.email and best and current_new != INVALID
          and _RANK.get(best.status, 9) >= _RANK.get(contact.email_status or "unchecked", 9)):
        # The new run found nothing better: keep the address we had (e.g. DNS failed this time).
        if current_new and _RANK[current_new] < _RANK.get(contact.email_status or "unchecked", 9):
            contact.email_status = current_new
        messages.append(f"Kept {contact.email}; nothing better was found.")
    else:
        contact.email = best.address if best else None
        contact.email_status = best.status if best else None
        if contact.stage in ("new", "ready"):
            contact.stage = "ready" if best else "new"
    contact.updated_at = now
    session.add(contact)
    session.commit()
    for row in rows:
        session.refresh(row)
    return FinderOutcome(rows, report, best, messages)


def set_email(session: Session, contact: Contact, address: str, check: bool = True,
              learn: bool = False) -> EmailCandidate:
    """Use an address you typed or edited. Optionally check it, and optionally teach Hunterly the
    company's pattern from it (also done automatically when the check says it's valid)."""
    address = address.strip().lower()
    if not EMAIL_RE.match(address):
        raise HunterlyError(f"'{address}' doesn't look like an email address.")
    domain = address.rsplit("@", 1)[1]
    status, detail = "unchecked", "typed by you"
    if check:
        result = verify(domain, [address]).results[0]
        status = result.status
        detail = " ".join(str(x) for x in (result.code, result.message) if x) or detail
        key = hunter_key(session)
        if status == UNKNOWN and key:
            try:
                hv = hunter.verify(address, key)
                status, detail = hv.status, f"Hunter: {hv.status.replace('_', '-')}"
            except hunter.HunterError as exc:
                detail = str(exc)
    row = session.exec(select(EmailCandidate).where(EmailCandidate.contact_id == contact.id,
                                                    EmailCandidate.address == address)).first()
    if row is None:
        row = EmailCandidate(contact_id=contact.id, address=address, pattern="typed by you",
                             rank=0, score=1.0)
    row.status, row.detail = status, detail
    row.checked_at = utcnow() if check else None
    session.add(row)

    contact.email, contact.email_status = address, status
    if contact.stage == "new":
        contact.stage = "ready"
    contact.updated_at = utcnow()
    session.add(contact)
    for pending in session.exec(select(Outreach).where(Outreach.contact_id == contact.id,
                                                       col(Outreach.sent_at).is_(None))):
        pending.to_address = address
        session.add(pending)

    company = session.get(Company, contact.company_id) if contact.company_id else None
    if company and not company.domain and domain not in WEBMAIL:
        company.domain = domain  # a work address tells us the company's email domain
        session.add(company)
    if company and company.domain == domain and (learn or status == VALID):
        exists = session.exec(select(KnownEmail).where(KnownEmail.address == address)).first()
        if not exists:
            session.add(KnownEmail(domain=domain, full_name=contact.full_name, address=address,
                                   source="confirmed by you" if learn else "checked valid"))
    session.commit()
    session.refresh(row)
    return row


def choose_email(session: Session, contact: Contact, candidate_id: int) -> None:
    cand = session.get(EmailCandidate, candidate_id)
    if not cand or cand.contact_id != contact.id:
        raise HunterlyError("That address doesn't belong to this contact")
    contact.email, contact.email_status = cand.address, cand.status
    if contact.stage == "new":
        contact.stage = "ready"
    session.add(contact)
    session.commit()


# ---------- outreach ----------

def outreach_for(session: Session, contact: Contact) -> list[Outreach]:
    return list(session.exec(select(Outreach).where(Outreach.contact_id == contact.id)
                             .order_by(Outreach.step, Outreach.id)))


def first_emails_last_24h(session: Session) -> int:
    """First emails actually sent in the last 24 hours."""
    since = utcnow() - timedelta(hours=24)
    return session.exec(select(func.count()).select_from(Outreach)
                        .where(Outreach.step == 1, col(Outreach.sent_at).is_not(None),
                               col(Outreach.sent_at) >= since)).one()


def _sent(history: list[Outreach]) -> list[Outreach]:
    """Messages that were sent and did not bounce, oldest first."""
    return sorted((o for o in history if o.sent_at and o.outcome != "bounced"),
                  key=lambda o: o.sent_at)


def next_due(settings, sent: list[Outreach]) -> datetime | None:
    """When the next follow-up is due, or None for the first email."""
    if not sent:
        return None
    days = settings.followup_days
    return sent[0].sent_at + timedelta(days=days[min(len(sent) - 1, len(days) - 1)])


def _check_can_email(contact: Contact) -> None:
    if contact.do_not_contact:
        raise HunterlyError("This contact is on the do-not-contact list.")
    if contact.stage in ("replied", "closed"):
        raise HunterlyError(f"This contact is {contact.stage}; no more emails.")
    if not contact.email:
        raise HunterlyError("Find or choose an email address first.")


def _has_placeholders(outreach: Outreach) -> bool:
    return drafts.PLACEHOLDER in outreach.body or drafts.PLACEHOLDER in outreach.subject


def draft_next(session: Session, contact: Contact) -> tuple[Outreach, drafts.Draft]:
    """Draft the next message (1, 2 or 3). An unsent draft for that step is regenerated."""
    _check_can_email(contact)
    settings = get_settings()
    history = outreach_for(session, contact)
    sent = _sent(history)
    step = len(sent) + 1
    if step > settings.max_messages_per_contact:
        raise HunterlyError(f"Already sent {len(sent)} messages; the limit is "
                      f"{settings.max_messages_per_contact}.")
    due = next_due(settings, sent)
    if due and due > utcnow():
        local = due.astimezone().strftime("%d %b")
        raise HunterlyError(f"Follow-up {step} isn't due until {local}. Give them time to reply.")
    if step == 1 and first_emails_last_24h(session) >= settings.daily_first_email_cap:
        raise HunterlyError(f"Daily cap of {settings.daily_first_email_cap} new first emails reached.")
    pending = next((o for o in history if o.step == step and not o.sent_at), None)

    company = session.get(Company, contact.company_id) if contact.company_id else None
    original = sent[0].subject if sent else None
    ctx = drafts.context(contact, company, get_profile(session), original)
    draft = drafts.make_draft(ctx, step)

    outreach = pending or Outreach(contact_id=contact.id, step=step, to_address=contact.email,
                                   subject="", body="")
    outreach.to_address = contact.email
    outreach.subject, outreach.body, outreach.draft_source = draft.subject, draft.body, draft.source
    outreach.gmail_draft_id = None
    session.add(outreach)
    session.commit()
    session.refresh(outreach)
    return outreach, draft


def save_outreach_text(session: Session, outreach: Outreach, subject: str, body: str) -> None:
    if outreach.sent_at:
        raise HunterlyError("Already sent; it can't be edited.")
    outreach.subject, outreach.body = subject.strip(), body.strip()
    session.add(outreach)
    session.commit()


def push_to_gmail(session: Session, outreach: Outreach) -> str:
    if _has_placeholders(outreach):
        raise HunterlyError("Fill in the [[gaps]] in the draft first.")
    contact = session.get(Contact, outreach.contact_id)
    _check_can_email(contact)
    sent = _sent(outreach_for(session, contact))
    thread_id = sent[0].gmail_thread_id if sent and outreach.step > 1 else None
    draft_id, new_thread = gmail.create_draft(outreach.to_address, outreach.subject,
                                              outreach.body, thread_id)
    outreach.gmail_draft_id, outreach.gmail_thread_id = draft_id, new_thread or thread_id
    session.add(outreach)
    session.commit()
    return draft_id


def mark_sent(session: Session, outreach: Outreach) -> None:
    if outreach.sent_at:
        raise HunterlyError("Already marked sent.")
    if _has_placeholders(outreach):
        raise HunterlyError("This draft still has [[gaps]]; edit it before marking it sent.")
    contact = session.get(Contact, outreach.contact_id)
    _check_can_email(contact)
    cap = get_settings().daily_first_email_cap
    if outreach.step == 1 and first_emails_last_24h(session) >= cap:
        raise HunterlyError(f"You've already sent {cap} first emails in 24 hours. Send this one tomorrow.")
    outreach.sent_at = utcnow()
    contact.stage = "contacted"
    contact.updated_at = utcnow()
    session.add_all([outreach, contact])
    session.commit()


def _last_sent(session: Session, contact: Contact) -> Outreach | None:
    sent = _sent(outreach_for(session, contact))
    return sent[-1] if sent else None


def mark_replied(session: Session, contact: Contact) -> None:
    last = _last_sent(session, contact)
    if last:
        last.outcome = "replied"
        session.add(last)
    contact.stage = "replied"
    session.add(contact)
    session.commit()


def mark_bounced(session: Session, contact: Contact) -> str | None:
    """Record a bounce, mark that address invalid and switch to the next candidate.
    The bounced message no longer counts, so the next draft is a fresh first email."""
    last = _last_sent(session, contact)
    bad = (last.to_address if last else contact.email) or ""
    if last:
        last.outcome = "bounced"
        session.add(last)
    for pending in session.exec(select(Outreach).where(Outreach.contact_id == contact.id,
                                                       col(Outreach.sent_at).is_(None))):
        session.delete(pending)  # drafts written for the bad address
    for cand in session.exec(select(EmailCandidate).where(EmailCandidate.contact_id == contact.id)):
        if cand.address == bad:
            cand.status = INVALID
            cand.detail = "bounced"
            session.add(cand)
    session.flush()
    rows = list(session.exec(select(EmailCandidate).where(EmailCandidate.contact_id == contact.id,
                                                          EmailCandidate.status != INVALID)))
    nxt = choose_best(rows, None)
    contact.email = nxt.address if nxt else None
    contact.email_status = nxt.status if nxt else None
    if contact.stage not in ("replied", "closed") and not contact.do_not_contact:
        contact.stage = derived_stage(session, contact)
    session.add(contact)
    session.commit()
    return contact.email


def derived_stage(session: Session, contact: Contact) -> str:
    """Stage implied by history: contacted if a message went out, else ready/new."""
    if _sent(outreach_for(session, contact)):
        return "contacted"
    return "ready" if contact.email else "new"


def set_do_not_contact(session: Session, contact: Contact, value: bool = True) -> None:
    contact.do_not_contact = value
    if value:
        contact.stage = "closed"
    session.add(contact)
    session.commit()


def set_stage(session: Session, contact: Contact, stage: str) -> None:
    contact.stage = stage
    session.add(contact)
    session.commit()


# ---------- export ----------

def _fmt(dt: datetime | None) -> str:
    return dt.astimezone().strftime("%Y-%m-%d %H:%M") if dt else ""


def _csv(rows: list[list]) -> str:
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    return "\ufeff" + buf.getvalue()  # BOM so Excel opens it as UTF-8


def export_contacts_csv(session: Session, stage: str | None = None) -> str:
    query = select(Contact).order_by(col(Contact.updated_at).desc())
    if stage:
        query = query.where(Contact.stage == stage)
    companies = {c.id: c for c in session.exec(select(Company))}
    rows = [["Name", "First name", "Last name", "Title", "Company", "Company domain", "Email",
             "Email status", "Stage", "Do not contact", "LinkedIn URL", "Location", "Headline",
             "Messages sent", "First sent", "Last sent", "Outcome", "Notes", "Added"]]
    for c in session.exec(query):
        company = companies.get(c.company_id)
        sent = _sent(outreach_for(session, c))
        last = _last_sent(session, c)
        rows.append([c.full_name, c.first_name, c.last_name, c.title or "",
                     company.name if company else "", (company.domain or "") if company else "",
                     c.email or "", (c.email_status or "").replace("_", "-"), c.stage,
                     "yes" if c.do_not_contact else "", c.linkedin_url or "", c.location or "",
                     c.headline or "", len(sent), _fmt(sent[0].sent_at) if sent else "",
                     _fmt(sent[-1].sent_at) if sent else "", (last.outcome or "") if last else "",
                     c.notes or "", _fmt(c.created_at)])
    return _csv(rows)


def export_companies_csv(session: Session) -> str:
    counts: dict[int, int] = {}
    for c in session.exec(select(Contact)):
        if c.company_id:
            counts[c.company_id] = counts.get(c.company_id, 0) + 1
    rows = [["Name", "Domain", "Mail server", "Website", "LinkedIn", "Size", "Funding stage",
             "Open roles", "Contacts", "Notes"]]
    for co in session.exec(select(Company).order_by(Company.name)):
        rows.append([co.name, co.domain or "", {True: "yes", False: "no"}.get(co.mx_ok, ""),
                     co.website or "", co.linkedin_url or "", co.size or "", co.funding_stage or "",
                     co.open_roles or "", counts.get(co.id, 0), co.notes or ""])
    return _csv(rows)


# ---------- follow-ups and stats ----------

@dataclass
class FollowUp:
    contact: Contact
    company: Company | None
    step: int
    due_at: datetime
    overdue_days: int


def due_followups(session: Session, now: datetime | None = None) -> list[FollowUp]:
    settings = get_settings()
    now = now or utcnow()
    out: list[FollowUp] = []
    contacts = session.exec(select(Contact).where(Contact.stage == "contacted",
                                                  Contact.do_not_contact == False)).all()  # noqa: E712
    for contact in contacts:
        sent = _sent(outreach_for(session, contact))
        if not sent or len(sent) >= settings.max_messages_per_contact:
            continue
        due_at = next_due(settings, sent)
        if due_at <= now:
            company = session.get(Company, contact.company_id) if contact.company_id else None
            out.append(FollowUp(contact, company, len(sent) + 1, due_at, (now - due_at).days))
    return sorted(out, key=lambda f: f.due_at)


def stats(session: Session) -> dict:
    sent = session.exec(select(Outreach).where(col(Outreach.sent_at).is_not(None))).all()
    first_sent = {o.contact_id for o in sent if o.step == 1}
    replied = {c.id for c in session.exec(select(Contact).where(Contact.stage == "replied"))}
    bounced = [o for o in sent if o.outcome == "bounced"]
    week_ago = utcnow() - timedelta(days=7)
    return {
        "companies": session.exec(select(func.count()).select_from(Company)).one(),
        "contacts": session.exec(select(func.count()).select_from(Contact)).one(),
        "emails_sent": len(sent),
        "sent_this_week": sum(1 for o in sent if o.sent_at >= week_ago),
        "contacted": len(first_sent),
        "replied": len(replied & first_sent),
        "reply_rate": round(100 * len(replied & first_sent) / len(first_sent), 1) if first_sent else None,
        "bounce_rate": round(100 * len(bounced) / len(sent), 1) if sent else None,
        "captures_24h": captures_last_24h(session),
        "first_emails_24h": first_emails_last_24h(session),
    }
