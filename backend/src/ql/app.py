"""FastAPI app: the web tracker (HTML pages) and the JSON API used by the Chrome extension."""

from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Form, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlmodel import Session, col, select
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ql import __version__, drafts, gmail, services
from ql.config import get_settings
from ql.db import get_session
from ql.finder.domains import has_mail_server, normalize_domain
from ql.finder.smtp_check import port25_reachable, port25_status
from ql.models import (
    ROLES,
    STAGE_LABELS,
    STAGES,
    Company,
    Contact,
    EmailCandidate,
    KnownEmail,
    Outreach,
)
from ql.security import check_csrf, install_secret
from ql.services import QLError

WEB = Path(__file__).parent / "web"

app = FastAPI(title="QL", version=__version__, docs_url="/api/docs", redoc_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"chrome-extension://[a-p]{32}",
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)
app.mount("/static", StaticFiles(directory=WEB / "static"), name="static")
templates = Jinja2Templates(directory=WEB / "templates")


def _local(dt: datetime | None, fmt: str = "%d %b %Y, %H:%M") -> str:
    if not dt:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone().strftime(fmt)


def _initials(name: str | None) -> str:
    parts = [p for p in (name or "?").replace(".", " ").split() if p[:1].isalnum()]
    return ((parts[0][:1] + (parts[-1][:1] if len(parts) > 1 else "")) if parts else "?").upper()


def _hue(name: str | None) -> str:
    return f"h{sum(map(ord, name or '')) % 6}"


templates.env.filters["local"] = _local
templates.env.filters["initials"] = _initials
templates.env.filters["hue"] = _hue
templates.env.globals.update(stage_labels=STAGE_LABELS, stages=STAGES, roles=ROLES)

SessionDep = Depends(get_session)


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    from ql.db import get_engine

    if "ollama" not in ctx:
        ctx["ollama"] = drafts.ollama_status()
    with Session(get_engine()) as session:
        nav_due = len(services.due_followups(session))
    ctx.update(csrf=install_secret(), msg=request.query_params.get("msg"),
               err=request.query_params.get("err"), ollama_ok=ctx["ollama"].startswith("ready"),
               gmail_ok=gmail.is_configured(), verifier_remote=bool(get_settings().verifier_url),
               nav_due=nav_due)
    return templates.TemplateResponse(request, name, ctx)


def back(url: str, msg: str | None = None, err: str | None = None) -> RedirectResponse:
    params = {k: v for k, v in {"msg": msg, "err": err}.items() if v}
    return RedirectResponse(url + (("?" + urlencode(params)) if params else ""), status_code=303)


def _get(session: Session, model, obj_id: int):
    obj = session.get(model, obj_id)
    if obj is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail=f"{model.__name__} not found")
    return obj


# ---------------- pages ----------------

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, session: Session = SessionDep):
    settings = get_settings()
    board = {}
    for stage in ("new", "ready", "contacted", "replied"):
        rows = session.exec(select(Contact).where(Contact.stage == stage,
                                                  Contact.do_not_contact == False)  # noqa: E712
                            .order_by(col(Contact.updated_at).desc())).all()
        board[stage] = {"count": len(rows), "people": rows[:8]}
    profile = services.get_profile(session)
    ollama = drafts.ollama_status()
    from ql.finder import smtp_check

    blocked = smtp_check.last_policy_block is not None
    checks_ok = bool(settings.verifier_url or services.hunter_key(session)) or \
        (not blocked and port25_status())
    setup = [
        (bool(profile["my_name"] and profile["my_pitch"]), "Fill in About me", "/settings"),
        (checks_ok, "Email checks can run" if checks_ok else
         ("Mail servers refuse your home IP (spam blocklist): add a Hunter key" if blocked
          else "Port 25 is blocked here: add a Hunter key"), "/settings"),
        (ollama.startswith("ready"), "Start Ollama for written drafts", "/settings"),
        (bool(session.exec(select(Company)).first()), "Add target companies", "/companies"),
        (gmail.is_configured(), "Connect Gmail (optional)", "/settings"),
    ]
    return render(
        request, "dashboard.html", stats=services.stats(session),
        followups=services.due_followups(session), board=board,
        companies=_company_names(session), settings=settings, profile=profile,
        setup=setup, ollama=ollama,
    )


def _company_names(session: Session) -> dict[int, str]:
    return {c.id: c.name for c in session.exec(select(Company))}


@app.get("/companies", response_class=HTMLResponse)
def companies_page(request: Request, session: Session = SessionDep):
    rows = session.exec(select(Company).order_by(col(Company.created_at).desc())).all()
    counts: dict[int, int] = {}
    for c in session.exec(select(Contact)):
        if c.company_id:
            counts[c.company_id] = counts.get(c.company_id, 0) + 1
    return render(request, "companies.html", companies=rows, counts=counts)


@app.post("/companies")
def company_create(name: str = Form(...), domain: str = Form(""), size: str = Form(""),
                   funding_stage: str = Form(""), open_roles: str = Form(""),
                   notes: str = Form(""), csrf: str = Form(""), session: Session = SessionDep):
    check_csrf(csrf)
    company = services.upsert_company(session, name, domain or None, size=size,
                                      funding_stage=funding_stage, open_roles=open_roles,
                                      notes=notes, source="manual")
    return back(f"/companies/{company.id}", msg="Company saved")


@app.post("/companies/import")
async def company_import(file: UploadFile, csrf: str = Form(""), session: Session = SessionDep):
    check_csrf(csrf)
    text = (await file.read()).decode("utf-8-sig", errors="replace")
    count = services.import_companies_csv(session, text)
    return back("/companies", msg=f"Imported {count} companies")


@app.get("/companies/{company_id}", response_class=HTMLResponse)
def company_page(company_id: int, request: Request, session: Session = SessionDep):
    company = _get(session, Company, company_id)
    contacts = session.exec(select(Contact).where(Contact.company_id == company_id)).all()
    known = session.exec(select(KnownEmail).where(KnownEmail.domain == company.domain)).all() \
        if company.domain else []
    suggestions = []
    if request.query_params.get("suggest"):
        suggestions = services.domain_suggestions(company, limit=8)
    return render(request, "company.html", company=company, contacts=contacts, known=known,
                  suggestions=suggestions)


@app.post("/companies/{company_id}")
def company_update(company_id: int, name: str = Form(...), domain: str = Form(""),
                   website: str = Form(""), size: str = Form(""), funding_stage: str = Form(""),
                   open_roles: str = Form(""), notes: str = Form(""), csrf: str = Form(""),
                   session: Session = SessionDep):
    check_csrf(csrf)
    company = _get(session, Company, company_id)
    clean = normalize_domain(domain) if domain else None
    if domain and not clean:
        return back(f"/companies/{company_id}", err=f"'{domain}' doesn't look like a domain")
    company.name, company.domain = name.strip(), clean
    company.website, company.size = website or None, size or None
    company.funding_stage = funding_stage or None
    company.open_roles, company.notes = open_roles or None, notes or None
    company.mx_ok = has_mail_server(clean) if clean else None
    session.add(company)
    session.commit()
    notes = {True: " - mail server found", False: " - warning: no mail server (MX) for this domain",
             None: " - couldn't check the mail server (DNS unreachable)"}
    msg = "Saved" + (notes[company.mx_ok] if clean else "")
    return back(f"/companies/{company_id}", msg=msg)


@app.post("/companies/{company_id}/known")
def company_known(company_id: int, full_name: str = Form(...), address: str = Form(...),
                  source: str = Form(""), csrf: str = Form(""), session: Session = SessionDep):
    check_csrf(csrf)
    company = _get(session, Company, company_id)
    try:
        services.add_known_email(session, company.domain or "", full_name, address, source or None)
    except QLError as exc:
        return back(f"/companies/{company_id}", err=str(exc))
    return back(f"/companies/{company_id}", msg="Known address added; it now steers the pattern")


@app.post("/companies/{company_id}/delete")
def company_delete(company_id: int, csrf: str = Form(""), session: Session = SessionDep):
    check_csrf(csrf)
    company = _get(session, Company, company_id)
    try:
        services.delete_company(session, company)
    except QLError as exc:
        return back(f"/companies/{company_id}", err=str(exc))
    return back("/companies", msg=f"Deleted {company.name}")


@app.get("/contacts", response_class=HTMLResponse)
def contacts_page(request: Request, stage: str = "", session: Session = SessionDep):
    query = select(Contact).order_by(col(Contact.updated_at).desc())
    if stage in STAGES:
        query = query.where(Contact.stage == stage)
    return render(request, "contacts.html", contacts=session.exec(query).all(), stage=stage,
                  companies=_company_names(session))


@app.get("/contacts/new", response_class=HTMLResponse)
def contact_new(request: Request, session: Session = SessionDep):
    names = sorted(_company_names(session).values())
    return render(request, "contact_new.html", company_names=names)


@app.post("/contacts")
def contact_create(full_name: str = Form(...), company_name: str = Form(""), domain: str = Form(""),
                   title: str = Form(""), linkedin_url: str = Form(""), location: str = Form(""),
                   notes: str = Form(""), csrf: str = Form(""), session: Session = SessionDep):
    check_csrf(csrf)
    try:
        contact, created = services.save_contact(
            session, full_name=full_name, company_name=company_name or None,
            domain=domain or None, title=title or None, linkedin_url=linkedin_url or None,
            location=location or None, notes=notes or None)
    except QLError as exc:
        return back("/contacts/new", err=str(exc))
    return back(f"/contacts/{contact.id}", msg="Contact added" if created else "Contact updated")


@app.get("/contacts/{contact_id}", response_class=HTMLResponse)
def contact_page(contact_id: int, request: Request, session: Session = SessionDep):
    contact = _get(session, Contact, contact_id)
    company = session.get(Company, contact.company_id) if contact.company_id else None
    candidates = session.exec(select(EmailCandidate).where(EmailCandidate.contact_id == contact_id)
                              .order_by(EmailCandidate.rank)).all()
    history = services.outreach_for(session, contact)
    current = next((o for o in history if not o.sent_at), None)
    compose = gmail.compose_url(current.to_address, current.subject, current.body) if current else None
    return render(request, "contact.html", contact=contact, company=company,
                  experience=services.experience_of(contact),
                  candidates=candidates, history=history, current=current, compose=compose,
                  gmail_ok=gmail.is_configured(), placeholder=drafts.PLACEHOLDER,
                  followup=next((f for f in services.due_followups(session)
                                 if f.contact.id == contact_id), None))


@app.post("/contacts/{contact_id}")
def contact_update(contact_id: int, full_name: str = Form(...), title: str = Form(""),
                   company_name: str = Form(""), linkedin_url: str = Form(""),
                   location: str = Form(""), role: str = Form(""), notes: str = Form(""),
                   csrf: str = Form(""), session: Session = SessionDep):
    check_csrf(csrf)
    contact = _get(session, Contact, contact_id)
    from ql.finder.names import split_name

    contact.full_name = full_name.strip()
    contact.first_name, contact.last_name = split_name(full_name)
    contact.title, contact.location, contact.notes = title or None, location or None, notes or None
    contact.linkedin_url = services.normalize_linkedin_url(linkedin_url) if linkedin_url else None
    contact.role = role if role in ROLES else None
    if company_name:
        contact.company_id = services.upsert_company(session, company_name).id
    session.add(contact)
    session.commit()
    return back(f"/contacts/{contact_id}", msg="Saved")


@app.post("/contacts/{contact_id}/find")
def contact_find(contact_id: int, check: str = Form("1"), csrf: str = Form(""),
                 session: Session = SessionDep):
    check_csrf(csrf)
    contact = _get(session, Contact, contact_id)
    try:
        outcome = services.find_email(session, contact, check=check == "1")
    except QLError as exc:
        return back(f"/contacts/{contact_id}", err=str(exc))
    best = outcome.best
    msg = (f"Best guess: {best.address} ({best.status.replace('_', '-')})" if best
           else "No usable address found")
    if outcome.messages:
        msg += ". " + " ".join(outcome.messages)
    return back(f"/contacts/{contact_id}", msg=msg)


@app.post("/contacts/{contact_id}/email")
def contact_set_email(contact_id: int, address: str = Form(...), check: str = Form(""),
                      learn: str = Form(""), next: str = Form(""), csrf: str = Form(""),
                      session: Session = SessionDep):
    check_csrf(csrf)
    contact = _get(session, Contact, contact_id)
    url = next if next.startswith("/") and not next.startswith("//") else f"/contacts/{contact_id}"
    try:
        row = services.set_email(session, contact, address, check=bool(check), learn=bool(learn))
    except QLError as exc:
        return back(url, err=str(exc))
    status = row.status.replace("_", "-")
    learned = ". QL will learn the company's pattern from it." if learn or row.status == "valid" else ""
    note = services.explain(row.detail, row.address.rsplit("@", 1)[1]) if row.status == "unknown" else None
    return back(url, msg=f"{contact.full_name}: now using {row.address} ({status}){learned}"
                + (f" {note}" if note and check else ""))


@app.get("/export/contacts.csv")
def export_contacts(stage: str = "", session: Session = SessionDep) -> Response:
    data = services.export_contacts_csv(session, stage if stage in STAGES else None)
    name = f"ql-contacts{'-' + stage if stage in STAGES else ''}-{datetime.now():%Y-%m-%d}.csv"
    return Response(data, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/export/companies.csv")
def export_companies(session: Session = SessionDep) -> Response:
    name = f"ql-companies-{datetime.now():%Y-%m-%d}.csv"
    return Response(services.export_companies_csv(session), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.post("/contacts/{contact_id}/choose")
def contact_choose(contact_id: int, candidate_id: int = Form(...), csrf: str = Form(""),
                   session: Session = SessionDep):
    check_csrf(csrf)
    try:
        services.choose_email(session, _get(session, Contact, contact_id), candidate_id)
    except QLError as exc:
        return back(f"/contacts/{contact_id}", err=str(exc))
    return back(f"/contacts/{contact_id}", msg="Address chosen")


@app.post("/contacts/{contact_id}/draft")
def contact_draft(contact_id: int, csrf: str = Form(""), session: Session = SessionDep):
    check_csrf(csrf)
    try:
        outreach, draft = services.draft_next(session, _get(session, Contact, contact_id))
    except QLError as exc:
        return back(f"/contacts/{contact_id}", err=str(exc))
    note = f"Draft for message {outreach.step} written by {draft.source}"
    return back(f"/contacts/{contact_id}", msg=note + (f". {draft.note}" if draft.note else ""))


@app.post("/contacts/{contact_id}/action")
def contact_action(contact_id: int, action: str = Form(...), csrf: str = Form(""),
                   session: Session = SessionDep):
    check_csrf(csrf)
    contact = _get(session, Contact, contact_id)
    if action == "replied":
        services.mark_replied(session, contact)
        msg = "Marked as replied; follow-ups stopped"
    elif action == "bounced":
        nxt = services.mark_bounced(session, contact)
        msg = f"Bounce recorded. Next address: {nxt}" if nxt else "Bounce recorded; no other address left"
    elif action == "dnc":
        services.set_do_not_contact(session, contact, True)
        msg = "Added to do-not-contact"
    elif action == "undnc":
        services.set_do_not_contact(session, contact, False)
        services.set_stage(session, contact, services.derived_stage(session, contact))
        msg = "Removed from do-not-contact"
    elif action == "close":
        services.set_stage(session, contact, "closed")
        msg = "Closed"
    elif action == "reopen":
        services.set_stage(session, contact, services.derived_stage(session, contact))
        msg = "Reopened"
    else:
        return back(f"/contacts/{contact_id}", err="Unknown action")
    return back(f"/contacts/{contact_id}", msg=msg)


@app.post("/outreach/{outreach_id}")
def outreach_save(outreach_id: int, subject: str = Form(...), body: str = Form(...),
                  then: str = Form("save"), csrf: str = Form(""), session: Session = SessionDep):
    check_csrf(csrf)
    outreach = _get(session, Outreach, outreach_id)
    url = f"/contacts/{outreach.contact_id}"
    try:
        services.save_outreach_text(session, outreach, subject, body)
        if then == "gmail":
            services.push_to_gmail(session, outreach)
            return back(url, msg="Draft created in Gmail. Review it there and press send, "
                                 "then click 'Mark sent' here.")
        if then == "sent":
            services.mark_sent(session, outreach)
            return back(url, msg=f"Message {outreach.step} marked sent")
    except QLError as exc:
        return back(url, err=str(exc))
    except Exception as exc:  # Gmail API errors
        return back(url, err=f"Gmail error: {exc}")
    return back(url, msg="Draft saved")


@app.post("/settings/hunter")
def settings_hunter(hunter_api_key: str = Form(""), csrf: str = Form(""), session: Session = SessionDep):
    check_csrf(csrf)
    services.save_hunter_key(session, hunter_api_key)
    return back("/settings", msg="Hunter key saved" if hunter_api_key.strip() else "Hunter key removed")


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, session: Session = SessionDep):
    port25 = None
    if request.query_params.get("port25"):
        port25 = port25_reachable()
    return render(request, "settings.html", profile=services.get_profile(session),
                  hunter_set=bool(services.hunter_key(session)),
                  settings=get_settings(), ollama=drafts.ollama_status(),
                  gmail_ok=gmail.is_configured(), creds_path=gmail.credentials_path(),
                  port25=port25)


@app.post("/settings")
def settings_save(request: Request, my_name: str = Form(""), my_pitch: str = Form(""),
                  my_links: str = Form(""), target_role: str = Form(""), signature: str = Form(""),
                  csrf: str = Form(""), session: Session = SessionDep):
    check_csrf(csrf)
    services.save_profile(session, {"my_name": my_name, "my_pitch": my_pitch,
                                    "my_links": my_links, "target_role": target_role,
                                    "signature": signature})
    return back("/settings", msg="Profile saved")


# ---------------- JSON API for the extension ----------------

class ExperienceIn(BaseModel):
    title: str = Field(default="", max_length=200)
    company: str = Field(default="", max_length=200)
    dates: str = Field(default="", max_length=100)
    current: bool = False
    company_url: str = Field(default="", max_length=500)


class CaptureIn(BaseModel):
    full_name: str = Field(min_length=1, max_length=200)
    about: str | None = Field(default=None, max_length=3000)
    experience: list[ExperienceIn] = Field(default_factory=list, max_length=10)
    company_linkedin_url: str | None = Field(default=None, max_length=500)
    headline: str | None = Field(default=None, max_length=500)
    title: str | None = Field(default=None, max_length=200)
    company_name: str | None = Field(default=None, max_length=200)
    location: str | None = Field(default=None, max_length=200)
    linkedin_url: str | None = Field(default=None, max_length=500)
    notes: str | None = Field(default=None, max_length=2000)


def _require_json(request: Request) -> None:
    from fastapi import HTTPException

    if not request.headers.get("content-type", "").startswith("application/json"):
        raise HTTPException(status_code=415, detail="JSON only")


@app.get("/api/health")
def api_health(session: Session = SessionDep) -> dict:
    settings = get_settings()
    return {"ok": True, "version": __version__,
            "captures_24h": services.captures_last_24h(session),
            "capture_cap": settings.daily_capture_cap}


@app.get("/api/lookup")
def api_lookup(url: str, session: Session = SessionDep) -> dict:
    norm = services.normalize_linkedin_url(url)
    contact = session.exec(select(Contact).where(Contact.linkedin_url == norm)).first() if norm else None
    if not contact:
        return {"found": False}
    company = session.get(Company, contact.company_id) if contact.company_id else None
    return {"found": True, "contact_id": contact.id, "stage": contact.stage,
            "title": contact.title, "full_name": contact.full_name,
            "email": contact.email, "email_status": contact.email_status,
            "company": company.name if company else None,
            "domain": company.domain if company else None,
            "page": f"/contacts/{contact.id}"}


@app.post("/api/capture", dependencies=[Depends(_require_json)])
def api_capture(data: CaptureIn, session: Session = SessionDep) -> dict:
    from fastapi import HTTPException

    try:
        contact, created = services.capture_from_linkedin(session, **data.model_dump())
    except QLError as exc:
        raise HTTPException(status_code=429 if "cap" in str(exc) else 400, detail=str(exc)) from exc
    settings = get_settings()
    company = session.get(Company, contact.company_id) if contact.company_id else None
    return {"contact_id": contact.id, "created": created, "page": f"/contacts/{contact.id}",
            "title": contact.title,
            "company": company.name if company else None,
            "domain": company.domain if company else None,
            "captures_24h": services.captures_last_24h(session),
            "capture_cap": settings.daily_capture_cap}


@app.post("/api/contacts/{contact_id}/find", dependencies=[Depends(_require_json)])
def api_find(contact_id: int, session: Session = SessionDep) -> dict:
    from fastapi import HTTPException

    contact = _get(session, Contact, contact_id)
    try:
        outcome = services.find_email(session, contact)
    except QLError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"email": contact.email, "status": contact.email_status, "messages": outcome.messages,
            "candidates": [{"address": c.address, "status": c.status, "pattern": c.pattern}
                           for c in outcome.candidates]}


@app.get("/api/contacts/{contact_id}/domain-suggestions")
def api_domain_suggestions(contact_id: int, session: Session = SessionDep) -> dict:
    contact = _get(session, Contact, contact_id)
    company = session.get(Company, contact.company_id) if contact.company_id else None
    if not company:
        return {"suggestions": []}
    return {"company": company.name, "suggestions": services.domain_suggestions(company)}


class EmailIn(BaseModel):
    address: str = Field(max_length=320)
    check: bool = True


@app.post("/api/contacts/{contact_id}/email", dependencies=[Depends(_require_json)])
def api_set_email(contact_id: int, data: EmailIn, session: Session = SessionDep) -> dict:
    from fastapi import HTTPException

    contact = _get(session, Contact, contact_id)
    try:
        row = services.set_email(session, contact, data.address, check=data.check)
    except QLError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    domain = row.address.rsplit("@", 1)[1]
    note = services.explain(row.detail, domain) if row.status == "unknown" else None
    return {"email": row.address, "status": row.status, "candidates": [], "messages": [note] if note else []}


class DomainIn(BaseModel):
    domain: str


@app.post("/api/contacts/{contact_id}/domain", dependencies=[Depends(_require_json)])
def api_set_domain(contact_id: int, data: DomainIn, session: Session = SessionDep) -> dict:
    from fastapi import HTTPException

    contact = _get(session, Contact, contact_id)
    company = session.get(Company, contact.company_id) if contact.company_id else None
    clean = normalize_domain(data.domain)
    if not company or not clean:
        raise HTTPException(status_code=400, detail="Need a company and a valid domain")
    company.domain, company.mx_ok = clean, has_mail_server(clean)
    session.add(company)
    session.commit()
    return {"domain": clean, "mx_ok": company.mx_ok}
