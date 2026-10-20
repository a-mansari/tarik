"""The team's accounts for the pages, made from a terminal (there is no sign-up page). In the container:

python users.py add <email> "<name>"     made, or its name and password changed (the password is asked)
python users.py disable <email>          closed: no more logging in, its sessions ended"""

import asyncio
import getpass
import sys

from engine import accounts, runs
from server import DATABASE


async def main(command, email, name=""):
    await runs.open_store(DATABASE)
    await accounts.setup()
    if command == "add":
        password = getpass.getpass("Mot de passe : ")
        if len(password) < 12:
            raise SystemExit("12 caracteres au moins")
        if password != getpass.getpass("Encore : "):
            raise SystemExit("les deux mots de passe different")
        await accounts.save_user(email, name or email, password)
    elif command == "disable":
        await accounts.disable(email)
    else:
        raise SystemExit(__doc__)
    await runs.pool.close()
    print("ok")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    asyncio.run(main(*sys.argv[1:4]))
