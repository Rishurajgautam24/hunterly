# Warmpath: job-search contact finder

[![CI](https://github.com/Rishurajgautam24/warmpath/actions/workflows/ci.yml/badge.svg)](https://github.com/Rishurajgautam24/warmpath/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)

![Dashboard](docs/screenshots/dashboard.png)

A Chrome extension plus a small backend on your Mac. It turns the LinkedIn profile (or company) you're looking at into a checked work email for a hiring manager, a short draft written by a local model, and a tracked follow-up. Built from the "Job Search Contact Finder PRD".

What's in this first build (the PRD's MVP plus the extension):

| Area | What it does |
| --- | --- |
| Capture | Side-panel extension. With the panel open, it reads the LinkedIn profile you're viewing: name, headline, title, company, location, About and up to 6 roles. One click saves it and finds the email. It only reads pages you open (no crawling, no extra requests to LinkedIn); max 30 saves a day. Manual entry works without LinkedIn. |
| Companies | Target list, CSV import, domain suggestions with mail-server (MX) check, "known addresses" that teach QL each company's email pattern. |
| Email finder | Builds up to 30 candidate addresses from ~30 patterns (first.last, flast, numbered ones like ssingh1, middle initials, reversed...), ranked by what's common and by real addresses you've confirmed at that company. Checks the top 10 with the company's mail server without sending anything, and falls back to Hunter (free tier) when your network blocks that. You can edit or type any address yourself. |
| Drafts | Local LLM through Ollama (private, free). Falls back to a fill-in template if Ollama isn't running. Drafts with `[[gaps]]` can't be sent. |
| Sending | "Open in Gmail compose" (no setup), or real Gmail drafts through the Gmail API (drafts only — QL never sends and never reads your mail). |
| Tracker | Stages, follow-ups on day 4 and 9, 3-message limit, bounce handling (switches to the next address), do-not-contact list, reply and bounce rates. |
| Your own emails | Know the address already? Type it: in the extension under **Edit details or type their email**, on a contact's page, or straight into the Email column of the Contacts list. Tick "I know it's right" and QL learns that company's pattern for everyone else there. |
| Export | **Export CSV** on the Contacts page (respects the stage filter) and on the Companies page. Opens in Excel or Google Sheets. |

Not yet built (next phase): Laya classification (contact role, reply triage, draft checks, job fit), Hunter/Snov.io fallback, reading replies from Gmail.

## 1. Start the backend (Mac)

```bash
brew install uv            # or: curl -LsSf https://astral.sh/uv/install.sh | sh
cd backend
uv sync                    # creates .venv with Python 3.12
cp .env.example .env       # optional settings
uv run ql serve            # http://127.0.0.1:8765
```

Open http://127.0.0.1:8765, go to **Settings** and fill in "About me" (name, target role, one-line pitch, links, signature). Every draft uses it.

## 2. Local drafts with Ollama

```bash
brew install ollama        # or the app from ollama.com
ollama serve               # if the app isn't already running
ollama pull llama3.2:3b    # small enough for a MacBook Air; set QL_OLLAMA_MODEL to change
```

The dashboard shows whether Ollama is ready. Without it you still get the template.

## 3. Load the extension

1. Chrome → `chrome://extensions` → turn on **Developer mode**.
2. **Load unpacked** → choose the `extension` folder.
3. Pin "QL Capture" and click it to open the side panel. Open any LinkedIn profile: the panel fills itself in.
4. Click **Save & find email**. If QL doesn't know the company's email domain yet, pick one of the suggested domains (or type it), and the email check runs straight away.

The panel only reads the profile tab you're looking at, while it's open (turn auto-read off under the settings icon to read on demand). It talks only to `127.0.0.1:8765`. If a field comes out wrong, open **Edit details** before saving; LinkedIn changes its page layout from time to time.

## 4. Mail-server checks and port 25

Checking an address means talking to the company's mail server on port 25. Many home ISPs block that port. Test it:

```bash
uv run ql port25           # "open" or "blocked"
uv run ql check someone@company.com other@company.com
```

If it's **open**, you're done; consider setting `QL_SMTP_HELO_HOST` and `QL_SMTP_MAIL_FROM` to a domain you own, since some servers distrust `localhost`.

If it's **blocked**, run the verifier on a small cloud VM whose provider allows outbound port 25 (many big clouds block it too — check first):

```bash
# on the VM
curl -LsSf https://astral.sh/uv/install.sh | sh
# copy the backend folder to the VM, then:
cd backend && uv sync
QL_VERIFIER_KEY=<long random string> QL_SMTP_HELO_HOST=<vm hostname> uv run ql-verifier   # listens on 127.0.0.1:8766

# on the Mac: tunnel to it, then add to backend/.env
ssh -N -L 8766:127.0.0.1:8766 you@your-vm
#   QL_VERIFIER_URL=http://127.0.0.1:8766
#   QL_VERIFIER_KEY=<same string>
```

**Hunter backup (recommended if port 25 is blocked):** sign up free at hunter.io (50 credits a month), copy your API key, and paste it on QL's Settings page. When the mail-server check can't answer, QL asks Hunter instead (1 credit, only charged when Hunter returns an address).

Statuses: **valid** (server confirmed), **accept-all** (server accepts everything, so it's the best guess), **unknown** (no answer: greylisting, blocks, port 25), **invalid** (server said no such mailbox). Without port 25, use **Guess only** — candidates are still ranked by pattern, and adding a few known addresses at the company makes the top guess much better.

## 5. Gmail drafts (optional)

1. Google Cloud console → new project → enable the **Gmail API**.
2. OAuth consent screen: External, add yourself as a test user.
3. Credentials → OAuth client ID → **Desktop app** → download JSON to `~/.ql/credentials.json`.
4. `uv run ql gmail-auth` and approve. Only the `gmail.compose` scope is requested.

Then the contact page shows **Save and create Gmail draft**. You still press send in Gmail, then click **Mark sent** in QL.

## Daily routine

1. Dashboard → follow-ups due today first.
2. Add 3–5 companies; set their domains.
3. Capture 1–2 hiring managers per company (not recruiters).
4. Find email → write draft → add one specific detail → send from Gmail → **Mark sent**.
5. When someone answers: **They replied**. If it bounces: **It bounced** (QL moves to the next address). If they say no: **Do not contact**.

## Guardrails (built in)

- LinkedIn: reads only the profile you're viewing (no crawling, scrolling or extra requests), saves only on click, 30 saves a day.
- Email: at most 20 new first emails a day, 3 messages per person, no open tracking, nothing sent automatically.
- Data stays in `~/.ql/ql.db` on your Mac. The web app only answers on 127.0.0.1, forms carry a per-install token, and the extension API accepts JSON from the extension only.

## Development

```bash
cd backend
uv run pytest              # 51 tests
uv run ruff check src tests
```

Layout: `backend/src/ql/` — `app.py` (pages + API), `services.py` (rules), `finder/` (names, patterns, domains, SMTP check, verifier), `drafts.py` (Ollama + template), `gmail.py`, `verifier_worker.py` (VM service). `extension/` — MV3 side panel.

## Contributing

Contributions are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md), then open an issue or a pull request. Please follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Screenshots

| Contact page | Extension |
| --- | --- |
| ![Contact](docs/screenshots/contact.png) | ![Extension](docs/screenshots/extension.png) |

## License

[MIT](LICENSE) © 2026 Rishu
