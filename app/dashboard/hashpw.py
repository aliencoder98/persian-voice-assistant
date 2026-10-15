"""Create DASHBOARD_PASSWORD_HASH: python -m app.dashboard.hashpw"""

import getpass
import sys

from app.dashboard.auth import hash_password


def main() -> int:
    password = getpass.getpass("Dashboard password: ")
    if len(password) < 10:
        print("Use at least 10 characters.", file=sys.stderr)
        return 1
    if getpass.getpass("Repeat: ") != password:
        print("Passwords differ.", file=sys.stderr)
        return 1
    print("\nAdd to .env:\n")
    print(f"DASHBOARD_PASSWORD_HASH={hash_password(password)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
