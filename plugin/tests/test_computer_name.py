"""The Hermes computer's own name for "paired with Hermes on Studio Mac" (SPK-139): never an address
or a host name, "" when there's no person-facing name so the apps say "your Hermes computer"."""
from __future__ import annotations

import subprocess

import pytest

from speakeasy import computer_name as C


@pytest.mark.parametrize("raw, want", [
    ("Studio Mac", "Studio Mac"),
    ("Sam's MacBook Air", "Sam's MacBook Air"),
    ("  Office   iMac ", "Office iMac"),
    ("homeserver", "homeserver"),
    ("127.0.0.1", ""),
    ("::1", ""),
    ("[fe80::1]", ""),
    ("192.168.1.20", ""),
    ("localhost", ""),
    ("hermes.example-tailnet.ts.net", ""),
    ("calm-river-sound-demo.trycloudflare.com", ""),
    ("ip-10-0-0-12", ""),
    ("3f9a2c1b7d", ""),
    ("", ""),
])
def test_only_person_facing_names_pass(raw, want):
    assert C.plain(raw) == want


def test_mac_reads_the_sharing_name(monkeypatch):
    seen = []

    def run(args, **kw):
        seen.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="Studio Mac\n", stderr="")

    monkeypatch.setattr(C.subprocess, "run", run)
    monkeypatch.setattr(C.sys, "platform", "darwin")
    assert C.read() == "Studio Mac"
    assert seen == [["/usr/sbin/scutil", "--get", "ComputerName"]]


def test_mac_failure_answers_nothing(monkeypatch):
    def run(args, **kw):
        raise subprocess.TimeoutExpired(args, 2)

    monkeypatch.setattr(C.subprocess, "run", run)
    monkeypatch.setattr(C.sys, "platform", "darwin")
    assert C.read() == ""


def test_linux_reads_the_pretty_name(tmp_path):
    info = tmp_path / "machine-info"
    info.write_text('CHASSIS=desktop\nPRETTY_HOSTNAME="Basement Server"\n')
    assert C._linux(info) == "Basement Server"
    assert C._linux(tmp_path / "missing") == ""


def test_cached_between_status_polls(monkeypatch):
    calls = []
    monkeypatch.setattr(C, "_cache", None)
    monkeypatch.setattr(C, "read", lambda: calls.append(1) or "Studio Mac")
    assert C.computer_name(now=1000.0) == "Studio Mac"
    assert C.computer_name(now=1300.0) == "Studio Mac"
    assert len(calls) == 1
    assert C.computer_name(now=1000.0 + C._TTL + 1) == "Studio Mac"
    assert len(calls) == 2


def test_status_carries_the_name(service, monkeypatch):
    from speakeasy import service as S
    monkeypatch.setattr(S, "computer_name", lambda: "Studio Mac")
    assert service.status()["computer_name"] == "Studio Mac"
    monkeypatch.setattr(S, "computer_name", lambda: "")
    assert service.status()["computer_name"] == ""
