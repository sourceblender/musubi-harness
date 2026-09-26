"""validity_refusal agrees with the PyJWT that Musubi's server runs, claim for claim.

Musubi decodes with ``jwt.decode(..., audience="musubi", issuer=...)`` and no
leeway. This compares against PyJWT's own ``_validate_exp`` and
``_validate_aud`` when PyJWT is importable (it is in a Musubi checkout's venv):

    ~/Projects/musubi/.venv/bin/python -m pytest tests/test_token_validity_parity.py
"""

from __future__ import annotations

from typing import Any

import pytest

from musubi_harness.tokens import validity_refusal

jwt = pytest.importorskip("jwt")
NOW = 1_800_000_000.0
BASE = {"iss": "https://oauth.example"}


def server(payload: dict[str, Any], check: str) -> str | None:
    api = jwt.api_jwt.PyJWT()
    try:
        if check == "exp":
            api._validate_exp(payload, NOW, 0)
        else:
            api._validate_aud(payload, "musubi")
    except jwt.ExpiredSignatureError:
        return "token has expired"
    except jwt.DecodeError:
        return "token exp claim must be an integer"
    except jwt.MissingRequiredClaimError:
        return "token missing aud claim"
    except jwt.InvalidAudienceError:
        return "token audience is not musubi"
    except TypeError:
        # PyJWT crashes on a non-numeric-typed exp; the server cannot accept it either.
        return "token exp claim must be an integer"
    return None


EXPS = [True, False, 0, 1, NOW, NOW - 1, NOW + 1, NOW + 0.5, NOW - 0.5, str(int(NOW) + 5), str(int(NOW) - 5)]
EXPS += ["soon", "1.5", 1e300, None, [], {}]
AUDS = ["musubi", "other", ["musubi"], ["other", "musubi"], ["other"], [], ["musubi", 7], [7], 7, "", "musubix"]
AUDS += [["Musubi"], ("musubi",), {"musubi": 1}]


@pytest.mark.parametrize("exp", EXPS, ids=repr)
def test_exp_matches_pyjwt(exp: Any) -> None:
    assert validity_refusal({**BASE, "aud": "musubi", "exp": exp}, now=NOW) == server({"exp": exp}, "exp")


@pytest.mark.parametrize("aud", AUDS, ids=repr)
def test_aud_matches_pyjwt(aud: Any) -> None:
    assert validity_refusal({**BASE, "aud": aud}, now=NOW) == server({"aud": aud}, "aud")
