"""Supply Codex HTTP authentication from the ignored project .env file.

Invoke only as an MCP HTTP headers helper: stdout contains a credential and
must be consumed by the MCP client, never displayed or written to logs.
"""

import json
import sys
from pathlib import Path

from dotenv import dotenv_values


def main() -> int:
    values = dotenv_values(Path(__file__).resolve().parents[1] / ".env")
    token = values.get("MCP_AUTH_TOKEN") or ""
    if len(token) < 32 or any(not 33 <= ord(char) <= 126 for char in token):
        print("MCP HTTP credential is unavailable or invalid.", file=sys.stderr)
        return 1
    print(json.dumps({"Authorization": "Bearer " + token}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
