"""The team's accounts for the pages, made from a terminal (there is no sign-up page). In the container:

python -m server.users add <email> "<name>"     made, or its name and password changed (the password is asked)
python -m server.users role <email> admin|member  what it may do: an admin changes content, cabinets' settings and
                                         canvases' layout; a member (a new account) reads and uses Discussion
python -m server.users disable <email>          closed: no more logging in, its sessions ended"""

import asyncio
import getpass
import sys

from engine import runs
from server import accounts, audit
from server.server import DATABASE


def asked_password():
    """Ask for the new password twice in the terminal and return it.

    Raises SystemExit (nothing changed) when it is shorter than 12 characters or the two entries differ."""
    password = getpass.getpass("Mot de passe (12 caractères au moins) : ")
    if len(password) < 12:
        raise SystemExit(f"Mot de passe trop court : {len(password)} caractères, 12 au moins. Rien n'a changé.")
    if password != getpass.getpass("Encore : "):
        raise SystemExit("Les deux mots de passe diffèrent. Rien n'a changé.")
    return password


async def main(command, email, name=""):
    """Run one users.py ``command`` ("add", "role" or "disable") on account ``email`` and record it in the Journal.

    ``name`` is the account's name for "add" and the role (admin or member) for "role". Exits with the usage text for
    an unknown command, and with « Rien n'a changé » when the account change is refused (ValueError, LookupError)."""
    if command not in ("add", "role", "disable"):
        raise SystemExit(__doc__)
    password = asked_password() if command == "add" else None
    await runs.open_store(DATABASE)
    try:
        if command == "add":
            await accounts.save_user(email, name or email, password)
            await audit.record("terminal", "account_made", email.strip().lower())
        elif command == "role":
            await accounts.set_role(email, name)
            await audit.record("terminal", "account_role", email.strip().lower(), role=name)
        else:
            await accounts.disable(email)
            await audit.record("terminal", "account_closed", email.strip().lower())
    except (ValueError, LookupError) as refused:
        raise SystemExit(f"Rien n'a changé : {refused}") from None
    finally:
        await runs.pool.close()
    print("ok")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    asyncio.run(main(*sys.argv[1:4]))
