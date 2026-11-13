"""What config and the page check about a slideshow without importing its network client.

`listing_url` itself is tested in `test_reddit_source.py`, beside the source that reads it.
"""

import base64
import json

import pytest

from arrt_player.sources import fetch
from arrt_player.sources.reddit_settings import REDDIT_HOST, clean_session, session_expiry, session_problem


def jwt(claims: object) -> str:
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJIUzI1NiJ9.{body}.signature"


def test_the_host_the_cookie_goes_to_is_the_clients_own():
    assert REDDIT_HOST == fetch.REDDIT_HOST


def test_the_expiry_is_read_from_the_jwts_exp():
    # Unpadded base64, as Reddit's is: the reader pads it.
    assert session_expiry(jwt({"exp": 1_809_734_400, "sub": "t2_x"})) == 1_809_734_400.0


@pytest.mark.parametrize(
    "value",
    [
        "not-a-jwt",
        "a.b",
        "a.!!!.c",
        jwt(["exp"]),
        jwt({"sub": "no exp"}),
        jwt({"exp": "2027-05-08"}),
        jwt({"exp": True}),
        "a." + base64.urlsafe_b64encode(b"not json").decode() + ".c",
    ],
)
def test_a_cookie_that_does_not_say_when_it_expires_has_no_expiry(value: str):
    assert session_expiry(value) is None


@pytest.mark.parametrize("pasted", ["abc", "  abc\n", "reddit_session=abc", " reddit_session=abc "])
def test_a_pasted_cookie_is_its_value_alone(pasted: str):
    assert clean_session(pasted) == "abc"


@pytest.mark.parametrize("value", ["", "a b", "a;b", "a,b", 'a"b', "a\\b", "a\tb", "x" * 8193])
def test_a_value_that_cannot_ride_in_a_cookie_header_is_refused(value: str):
    assert session_problem(value) is not None


def test_a_real_shaped_value_is_taken():
    assert session_problem(jwt({"exp": 1})) is None


@pytest.mark.parametrize("exp", [10**400, 1e300, float("inf"), -1, 0, 32_503_680_000])
def test_an_expiry_no_date_can_be_written_for_is_none(exp):
    """`json.dumps` writes 10**400 as an integer and inf as `Infinity`; either crashed the page's date before."""
    assert session_expiry(jwt({"exp": exp})) is None
