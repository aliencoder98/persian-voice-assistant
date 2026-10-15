"""Operational commands: python -m app.cli <command>."""

import argparse
import sys

from app.config import get_settings


def send_test_email() -> int:
    from app.tools.resend_client import EmailError, send_email

    owner = get_settings().owner_email
    if not owner:
        print("OWNER_EMAIL is not set.", file=sys.stderr)
        return 1
    try:
        message_id = send_email(
            to=owner,
            subject="ایمیل آزمایشی دستیار صوتی",
            body="سلام! این یک ایمیل آزمایشی است. اگر آن را می‌بینید، کلید Resend درست کار می‌کند.",
        )
    except EmailError as exc:
        print(f"Failed: {exc}", file=sys.stderr)
        return 1
    print(f"Sent test email to {owner} (id {message_id}).")
    return 0


def seed() -> int:
    from app.db.seed import main

    main()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("send-test-email", help="Send one test email to OWNER_EMAIL via Resend")
    sub.add_parser("seed", help="Create the owner and two sample contacts")
    args = parser.parse_args()
    return {"send-test-email": send_test_email, "seed": seed}[args.command]()


if __name__ == "__main__":
    raise SystemExit(main())
