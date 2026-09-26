"""scope_allows agrees with Musubi's own matcher on every pair, for read and write.

Loads the server functions from a Musubi source checkout, so it runs only when
pointed at one: MUSUBI_SOURCE_DIR=~/Projects/musubi pytest tests/test_token_scope_parity.py
"""

from __future__ import annotations

import itertools
import os
import re
from pathlib import Path
from typing import Any

import pytest

from musubi_harness.tokens import scope_allows

SOURCE = Path(os.environ.get("MUSUBI_SOURCE_DIR", "")).expanduser() / "src" / "musubi" / "auth" / "scopes.py"
pytestmark = pytest.mark.skipif(not SOURCE.is_file(), reason="set MUSUBI_SOURCE_DIR to a Musubi checkout to run scope parity")


def server_allows() -> Any:
    text = SOURCE.read_text(encoding="utf-8")
    namespace: dict[str, Any] = {}
    for name in ("_parse_namespace_scope", "_namespace_matches", "_access_allows", "_namespace_scope_allows"):
        match = re.search(rf"^def {name}\(.*?(?=^def |\Z)", text, re.S | re.M)
        assert match, f"{name} not found in {SOURCE}"
        exec(match.group(0).replace("AccessLevel", "str"), namespace)
    return namespace["_namespace_scope_allows"]


@pytest.mark.parametrize("access", ["r", "w"])
def test_scope_allows_matches_the_server_on_every_pair(access: str) -> None:
    allows = server_allows()
    parts = ["aoi", "command-chair", "episodic", "*", "**", "voice", "x"]
    patterns = {"/".join(p) for n in (1, 2, 3, 4) for p in itertools.product(parts, repeat=n)}
    namespaces = ["aoi/command-chair/episodic", "aoi/voice/episodic", "a/b", "aoi/command-chair/curated", "x"]
    mismatches = [
        (f"{pattern}:{granted}", ns)
        for pattern in patterns
        for granted in ("r", "w", "rw", "rwx", "")
        for ns in namespaces
        if allows(f"{pattern}:{granted}", ns, access) != scope_allows(f"{pattern}:{granted}", ns, access)  # type: ignore[arg-type]
    ]
    assert mismatches == []
