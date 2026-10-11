# Contributing to QL

Thanks for your interest in QL. Issues and pull requests are welcome.

## Getting started

1. Fork the repo and clone your fork.
2. Set up the backend (needs [uv](https://docs.astral.sh/uv/) and Python 3.12):

   ```bash
   cd backend
   uv sync
   uv run pytest
   uv run ruff check src tests
   ```
3. To work on the extension, load the `extension/` folder as an unpacked extension in `chrome://extensions` (Developer mode).

## Making a change

- Create a branch from `main`: `git checkout -b fix/short-description`.
- Keep pull requests focused: one fix or feature each.
- Add or update tests for behaviour changes. `uv run pytest` and `uv run ruff check src tests` must pass; CI runs both.
- Use clear commit messages in the imperative mood ("Add MX fallback", not "added").
- Open a PR against `main` and fill in the template.

## Guardrails

QL is a personal, respectful-outreach tool. PRs that add LinkedIn crawling or scraping beyond the page the user is viewing, bulk or automatic email sending, open tracking, or sending data to third-party services by default will not be accepted.

## Reporting bugs and ideas

Use the issue templates. For security problems, see [SECURITY.md](SECURITY.md) and don't open a public issue.

By contributing you agree your work is released under the [MIT License](LICENSE) and that you'll follow the [Code of Conduct](CODE_OF_CONDUCT.md).
