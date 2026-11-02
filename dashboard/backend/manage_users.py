"""Create or reset dashboard logins. Changes work at once, no restart.

    python manage_users.py set NAME --role admin        # create, or reset the password / role (asks for the password)
    python manage_users.py set NAME --role supervisor
    python manage_users.py list
    python manage_users.py remove NAME

On the server: /srv/dialmind/app/users.sh set NAME --role admin.
The password is typed twice and not shown. Only a salted hash is stored, and it's never printed.
"""

import argparse
import getpass
import re
import sys
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[2] / "shared"), str(Path(__file__).resolve().parent)]

from app import auth  # noqa: E402

MIN_LENGTH = 12
NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def ask_password(from_stdin: bool) -> str:
    if from_stdin:  # for scripts and tests; never take a password as an argument (it would show in the process list)
        pw = sys.stdin.readline().rstrip("\r\n")
    else:
        pw = getpass.getpass(f"Password (at least {MIN_LENGTH} characters, not shown): ")
    if len(pw) < MIN_LENGTH:
        sys.exit(f"Too short: use at least {MIN_LENGTH} characters. Nothing changed.")
    if not from_stdin and getpass.getpass("Same password again: ") != pw:
        sys.exit("The two passwords don't match. Nothing changed.")
    return pw


def cmd_set(name: str, role: str, from_stdin: bool) -> None:
    if not NAME.match(name):
        sys.exit("Use letters, numbers, dots, dashes and underscores for the name. Nothing changed.")
    found = auth.read_users_file()
    action = "Reset" if name in found else "Created"
    found[name] = {"role": role, "hash": auth.hash_password(ask_password(from_stdin))}
    auth.write_users_file(found)
    print(f"{action} '{name}' ({role}). Their old sessions are logged out.")


def cmd_list() -> None:
    found = auth.users()
    if not found:
        print("No users yet.")
    for name, u in sorted(found.items()):
        print(f"{name:<24} {u['role']}{'  (old single login in .env)' if u.get('legacy') else ''}")


def cmd_remove(name: str) -> None:
    found = auth.read_users_file()
    if name not in found:
        if auth.users().get(name, {}).get("legacy"):
            sys.exit(f"'{name}' is the old single login: empty DASHBOARD_PASSWORD_HASH in .env and restart the backend.")
        sys.exit(f"No user '{name}'. Nothing changed.")
    del found[name]
    auth.write_users_file(found)
    print(f"Removed '{name}'. Their sessions are logged out.")
    if not any(u["role"] == "admin" for u in found.values()):
        print("Note: no admin login is left.")


def main() -> None:
    p = argparse.ArgumentParser(description="Create or reset dashboard logins.")
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("set", help="create a user, or reset their password and role")
    s.add_argument("name")
    s.add_argument("--role", required=True, choices=auth.ROLES)
    s.add_argument("--password-stdin", action="store_true", help="read the password from standard input")
    sub.add_parser("list", help="show users and roles")
    r = sub.add_parser("remove", help="remove a user")
    r.add_argument("name")
    a = p.parse_args()
    if a.command == "set":
        cmd_set(a.name, a.role, a.password_stdin)
    elif a.command == "list":
        cmd_list()
    else:
        cmd_remove(a.name)


if __name__ == "__main__":
    main()
