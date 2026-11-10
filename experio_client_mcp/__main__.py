"""``python -m experio_client_mcp`` serves; ``python -m experio_client_mcp token LOGIN`` makes the token a client gives
its agent (it asks for the key without echoing it, so the key stays out of the shell history)."""

import base64
import getpass
import sys


def token(login):
    key = getpass.getpass("Experio password or API key (not shown): ")
    print(base64.b64encode(f"{login}:{key}".encode()).decode())


if len(sys.argv) == 3 and sys.argv[1] == "token":
    token(sys.argv[2])
elif len(sys.argv) == 1:
    from experio_client_mcp.server import main

    main()
else:
    sys.exit("usage: python -m experio_client_mcp [token LOGIN]")
