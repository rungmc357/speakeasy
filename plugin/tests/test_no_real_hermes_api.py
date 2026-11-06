"""The conftest guard: no test can reach a real Hermes API server on its default port."""
from __future__ import annotations

import socket
import urllib.request

import pytest

import conftest
from speakeasy.hermes_api import HermesAPI
from speakeasy.settings import DEFAULT_HERMES_API_PORT


def _forget_hits(start: int) -> None:
    # This test reaches the port on purpose; drop its hits so the autouse guard doesn't fail it.
    del conftest.REAL_HERMES_API_HITS[start:]


def test_a_connect_to_the_hermes_api_port_is_refused_and_recorded():
    start = len(conftest.REAL_HERMES_API_HITS)
    try:
        with pytest.raises(ConnectionRefusedError):
            socket.create_connection(("127.0.0.1", DEFAULT_HERMES_API_PORT), timeout=1)
        with socket.socket() as s:
            assert s.connect_ex(("127.0.0.1", DEFAULT_HERMES_API_PORT)) != 0
        assert conftest.REAL_HERMES_API_HITS[start:] == [("127.0.0.1", DEFAULT_HERMES_API_PORT)] * 2
    finally:
        _forget_hits(start)


def test_a_real_hermes_api_client_on_the_default_port_never_gets_through():
    start = len(conftest.REAL_HERMES_API_HITS)
    try:
        api = HermesAPI(f"http://127.0.0.1:{DEFAULT_HERMES_API_PORT}", lambda: "",
                        opener=urllib.request.urlopen)
        with pytest.raises(Exception):
            api.model_options(timeout=1)
        assert conftest.REAL_HERMES_API_HITS[start:], "the lookup should have been stopped at the socket"
    finally:
        _forget_hits(start)


def test_other_local_ports_still_connect(hermes):
    with socket.create_connection(hermes.httpd.server_address[:2], timeout=2):
        pass
