"""Command line: `uv run ql serve`, `uv run ql gmail-auth`, `uv run ql check example.com ...`."""

import argparse
import json

from ql.config import get_settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="ql", description="QL job-search contact finder")
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve", help="run the web tracker and extension API")
    serve.add_argument("--reload", action="store_true")
    sub.add_parser("gmail-auth", help="connect Gmail (drafts only)")
    sub.add_parser("port25", help="test whether outbound port 25 is open here")
    check = sub.add_parser("check", help="check addresses at one domain against its mail server")
    check.add_argument("addresses", nargs="+")
    args = parser.parse_args()

    settings = get_settings()
    if args.cmd == "serve":
        import uvicorn

        print(f"QL running at http://{settings.host}:{settings.port}  (data: {settings.data_dir})")
        uvicorn.run("ql.app:app", host=settings.host, port=settings.port, reload=args.reload)
    elif args.cmd == "gmail-auth":
        from ql.gmail import authorize

        print(f"Saved Gmail token to {authorize()}")
    elif args.cmd == "port25":
        from ql.finder.smtp_check import port25_reachable

        print("open" if port25_reachable() else "blocked (use the verifier VM; see README)")
    elif args.cmd == "check":
        from ql.finder.verifier import verify

        domain = args.addresses[0].rsplit("@", 1)[-1]
        print(json.dumps(verify(domain, args.addresses).to_dict(), indent=2))
