"""The team's accounts for the pages, made from a terminal (there is no sign-up page). In the container:

python users.py add <email> "<name>"     made, or its name and password changed (the password is asked)
python users.py disable <email>          closed: no more logging in, its sessions ended"""

import asyncio
import getpass
import sys

from engine import accounts, runs
from server import DATABASE


def asked_password():
    """The new password, typed twice, checked before anything is opened."""
    password = getpass.getpass("Mot de passe (12 caracteres au moins) : ")
    if len(password) < 12:
        raise SystemExit(f"Mot de passe trop court : {len(password)} caracteres, 12 au moins. Rien n'a change.")
    if password != getpass.getpass("Encore : "):
        raise SystemExit("Les deux mots de passe different. Rien n'a change.")
    return password


async def main(command, email, name=""):
    if command not in ("add", "disable"):
        raise SystemExit(__doc__)
    password = asked_password() if command == "add" else None
    await runs.open_store(DATABASE)
    try:
        if command == "add":
            await accounts.save_user(email, name or email, password)
        else:
            await accounts.disable(email)
    finally:
        await runs.pool.close()
    print("ok")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    asyncio.run(main(*sys.argv[1:4]))
