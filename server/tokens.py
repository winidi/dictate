"""Bearer tokens, one per person or device, in a JSON file next to the config.

{"dk_...": {"name": "anna", "created": "2026-11-03T17:00:00"}}. The file is
written with mode 600. Tokens are compared in constant time.
"""
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from typing import Dict, Optional

import core

TOKENS_PATH = core.CONFIG_DIR / "server-tokens.json"
PREFIX = "dk_"


class TokenStore:
    def __init__(self, path: Path = TOKENS_PATH):
        self.path = Path(path)
        self._tokens: Dict[str, dict] = {}
        self.reload()

    def reload(self) -> None:
        try:
            self._tokens = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self._tokens = {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._tokens, indent=2))
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)

    def add(self, name: str) -> str:
        """A new token for name; a name may own several (phone, laptop)."""
        name = name.strip()
        if not name:
            raise ValueError("a name is needed")
        token = PREFIX + secrets.token_urlsafe(24)
        self._tokens[token] = {"name": name, "created": time.strftime("%Y-%m-%dT%H:%M:%S")}
        self._save()
        return token

    def remove(self, name: str) -> int:
        """Remove every token of name; returns how many went."""
        before = len(self._tokens)
        self._tokens = {t: v for t, v in self._tokens.items() if v.get("name") != name}
        self._save()
        return before - len(self._tokens)

    def lookup(self, token: Optional[str]) -> Optional[str]:
        """The name a token belongs to, or None."""
        if not token:
            return None
        for t, v in self._tokens.items():
            if hmac.compare_digest(t, token):
                return v.get("name")
        return None

    def names(self):
        out = {}
        for v in self._tokens.values():
            out[v.get("name", "?")] = out.get(v.get("name", "?"), 0) + 1
        return out

    def __len__(self):
        return len(self._tokens)
