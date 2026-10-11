"""Email drafts: a local LLM through Ollama, with a fill-in template as the fallback."""

import json
from dataclasses import dataclass

import httpx

from hunterly.config import get_settings

PLACEHOLDER = "[["  # template gaps look like [[add ...]]; a draft containing one can't be sent

_RULES = (
    "You write short, personal emails from a job seeker to someone at a company they want to "
    "work for. Rules: plain text; no flattery, buzzwords, emojis or placeholders; never invent "
    "facts that are not in the input; write as the sender in first person; end with the "
    "sender's signature exactly as given. Reply with JSON only: {\"subject\": ..., \"body\": ...}."
)
_STEP_BRIEF = {
    1: "First email, under 120 words, in three short parts: (1) common ground - one specific "
       "detail about the person or company taken from the input; (2) proof of the sender's "
       "work from their pitch and links; (3) a small ask - a 15-minute chat, or a pointer to "
       "the right person for the role. Subject: 3 to 6 words.",
    2: "Follow-up to an earlier email that got no reply, under 60 words. Mention the earlier "
       "note in one line, add one new useful fact about the sender, repeat the small ask. "
       "Subject: 'Re: ' plus the original subject.",
    3: "Final, polite close-the-loop note under 50 words. No pressure; say this is the last "
       "message and leave the door open. Subject: 'Re: ' plus the original subject.",
}


@dataclass
class Draft:
    subject: str
    body: str
    source: str  # "ollama" or "template"
    note: str = ""

    @property
    def has_placeholders(self) -> bool:
        return PLACEHOLDER in self.body or PLACEHOLDER in self.subject


def _roles(contact) -> list[str]:
    try:
        roles = json.loads(contact.experience) if contact.experience else []
    except ValueError:
        return []
    return [f"{r.get('title', '')} at {r.get('company', '')} ({r.get('dates', '')})".strip()
            for r in roles[:3]]


def context(contact, company, profile: dict, original_subject: str | None = None) -> dict:
    return {
        "recipient": {
            "first_name": contact.first_name or contact.full_name,
            "full_name": contact.full_name,
            "title": contact.title or contact.headline or "",
            "headline": contact.headline or "",
            "location": contact.location or "",
            "notes": contact.notes or "",
            "about": (contact.about or "")[:600],
            "recent_roles": _roles(contact),
        },
        "company": {
            "name": company.name if company else "",
            "open_roles": (company.open_roles or "") if company else "",
            "notes": (company.notes or "") if company else "",
        },
        "sender": {
            "name": profile.get("my_name", ""),
            "pitch": profile.get("my_pitch", ""),
            "links": profile.get("my_links", ""),
            "target_role": profile.get("target_role", ""),
            "signature": profile.get("signature", "") or profile.get("my_name", ""),
        },
        "original_subject": original_subject or "",
    }


def template_draft(ctx: dict, step: int) -> Draft:
    r, c, s = ctx["recipient"], ctx["company"], ctx["sender"]
    first = r["first_name"].split()[0] if r["first_name"] else "there"
    role = s["target_role"] or "[[target role]]"
    sig = s["signature"] or "[[your name]]"
    original = ctx["original_subject"] or f"{role} at {c['name']}"
    if step == 1:
        subject = f"{role} at {c['name']}".strip()
        body = (
            f"Hi {first},\n\n"
            f"[[one specific detail about {first}'s work or {c['name'] or 'the company'} - "
            "a launch, a post, a talk, the team's product"
            + (f". From their About: \"{r['about'][:90]}...\"" if r.get("about") else "")
            + "]]\n\n"
            f"I'm {s['name'] or '[[your name]]'}, {s['pitch'] or '[[one line on what you do]]'}."
            + (f" Some of my work: {s['links']}." if s["links"] else "")
            + f"\n\nWould you be open to a 15-minute chat about {role} roles on your team, or "
            "could you point me to the right person?\n\n"
            f"Thanks,\n{sig}"
        )
    elif step == 2:
        subject = f"Re: {original}"
        body = (
            f"Hi {first},\n\nFollowing up on my note from last week. "
            "[[one new, useful fact - a recent project, a result, a relevant link]]\n\n"
            "Would a short chat in the next couple of weeks work?\n\n"
            f"Thanks,\n{sig}"
        )
    else:
        subject = f"Re: {original}"
        body = (
            f"Hi {first},\n\nI know inboxes get busy, so this is my last note. If {role} "
            "hiring opens up later, I'd be glad to hear from you.\n\n"
            f"Best,\n{sig}"
        )
    return Draft(subject, body, "template")


def ollama_draft(ctx: dict, step: int) -> Draft:
    settings = get_settings()
    payload = {
        "model": settings.ollama_model,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.4},
        "messages": [
            {"role": "system", "content": _RULES},
            {"role": "user", "content": _STEP_BRIEF[step] + "\n\nInput:\n" + json.dumps(ctx)},
        ],
    }
    resp = httpx.post(f"{settings.ollama_url.rstrip('/')}/api/chat", json=payload,
                      timeout=settings.ollama_timeout)
    resp.raise_for_status()
    data = json.loads(resp.json()["message"]["content"])
    if not isinstance(data, dict):
        raise ValueError("model did not return a JSON object")
    subject, body = str(data.get("subject", "")).strip(), str(data.get("body", "")).strip()
    if not subject or not body:
        raise ValueError("model returned an empty subject or body")
    signature = ctx["sender"]["signature"]
    if signature and signature.splitlines()[0] not in body:
        body += f"\n\n{signature}"
    words = len(body.split())
    note = f"{words} words" + (" - longer than the target, trim it" if words > 130 else "")
    return Draft(subject, body, "ollama", note)


def make_draft(ctx: dict, step: int) -> Draft:
    try:
        return ollama_draft(ctx, step)
    except (httpx.HTTPError, ValueError, KeyError, json.JSONDecodeError) as exc:
        draft = template_draft(ctx, step)
        draft.note = f"Ollama unavailable ({type(exc).__name__}); used the template - fill the [[gaps]]."
        return draft


def ollama_status() -> str:
    settings = get_settings()
    try:
        resp = httpx.get(f"{settings.ollama_url.rstrip('/')}/api/tags", timeout=3)
        resp.raise_for_status()
        names = [m.get("name", "") for m in resp.json().get("models", [])]
    except (httpx.HTTPError, ValueError, AttributeError):
        return "not running"
    model = settings.ollama_model
    if any(n == model or n.split(":")[0] == model for n in names):
        return f"ready ({model})"
    return f"running, but model {model} is not pulled (run: ollama pull {model})"
