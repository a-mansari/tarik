"""The team's accounts for the pages, made from a terminal (there is no sign-up page). In the container:

python -m app.users add <email> "<name>"     made, or its name and password changed (the password is asked)
python -m app.users role <email> admin|member  what it may do: an admin changes content, cabinets' settings and
                                         canvases' layout; a member (a new account) reads and uses Discussion
python -m app.users disable <email>          closed: no more logging in, its sessions ended"""

import asyncio
import getpass
import sys

from app import accounts, audit
from app.server import DATABASE
from engine import runs


def asked_password():
    """The new password, typed twice, checked before anything is opened."""
    password = getpass.getpass("Mot de passe (12 caracteres au moins) : ")
    if len(password) < 12:
        raise SystemExit(f"Mot de passe trop court : {len(password)} caracteres, 12 au moins. Rien n'a change.")
    if password != getpass.getpass("Encore : "):
        raise SystemExit("Les deux mots de passe different. Rien n'a change.")
    return password


async def main(command, email, name=""):
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
        raise SystemExit(f"Rien n'a change : {refused}") from None
    finally:
        await runs.pool.close()
    print("ok")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    asyncio.run(main(*sys.argv[1:4]))
