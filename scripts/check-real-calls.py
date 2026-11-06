#!/usr/bin/env python3
"""Fail if the repo quotes a real voice call in a way that identifies the person who made it.

Ordinary phrasing is fine: the voice repeats its own scripted lines, and people say common things
("turn off the lights", "what's the weather"). A run of 6+ consecutive words from a real call only
fails the check when that run also contains something identifying:

  - a term from ~/.config/speakeasy/private-terms.txt (people, devices, machines, places, trips),
  - a name-like word from the calls: capitalized mid-sentence, not ordinary English, not a common
    product/brand name (see GENERIC below),
  - an email address from the calls, or a number of 3+ digits (phone, address, account-like).

Reads local Speakeasy state on this machine (never committed). Skips quietly on machines without it.
Prints only file:line by default; --show adds which identifying word matched (local use only).
"""
import json, os, re, sqlite3, subprocess, sys
from pathlib import Path

HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
CONFIG = Path.home() / ".config" / "speakeasy"
N = 6

# Names that show up in calls but say nothing about who you are.
GENERIC = set("""
speakeasy hermes tailscale codex chatgpt openai claude anthropic gemini google apple mac macbook
imac iphone ipad vision pro watch airpods siri safari chrome discord telegram slack whatsapp imessage
gmail outlook spotify youtube netflix plex github amazon uber lyft venice coingecko coinbase nasdaq
bitcoin ethereum solana tesla nvidia microsoft meta instagram twitter tiktok reddit linkedin
monday tuesday wednesday thursday friday saturday sunday january february march april may june
july august september october november december gpt live jev typesafe openrouter wifi bluetooth
app apps jan feb mar apr jun jul aug sep sept oct nov dec mon tue tues wed thu thurs fri sat sun
okay ok yeah yep nope hey hi hello thanks api url ssh usb pdf
plugin plugins xcode testflight kanban
""".split())


def words(t):
    return re.findall(r"[a-z0-9']+", t.lower())


def common_english():
    d = Path("/usr/share/dict/words")
    if not d.exists():
        return set()
    return {w for w in d.read_text(errors="ignore").split() if w.islower()}


def call_texts():
    """Only words from real calls (requests, answers, transcripts), not coding sessions."""
    texts = []
    db = HOME / "speakeasy" / "state.sqlite3"
    if db.exists():
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        texts += [t or "" for (t,) in c.execute("select title from runs")]
        texts += [t or "" for (t,) in c.execute(
            "select text from work_events where kind in ('request','result','milestone')")]
    log = HOME / "speakeasy" / "call-log.jsonl"
    if log.exists():
        for line in log.read_text(errors="ignore").splitlines():
            try:
                call = json.loads(line)
            except ValueError:
                continue
            for turn in call.get("turns", []) or []:
                texts.append(turn.get("text", "") if isinstance(turn, dict) else str(turn))
    return texts


def private_terms():
    f = CONFIG / "private-terms.txt"
    if not f.exists():
        return []
    return [l.strip().lower() for l in f.read_text().splitlines() if l.strip() and not l.startswith("#")]


def is_english(w, english):
    """In the dictionary directly or as a plain inflection (bills, users, sharing, booked)."""
    if w in english:
        return True
    for suffix, back in (("ies", "y"), ("es", ""), ("s", ""), ("ing", ""), ("ing", "e"), ("ed", ""),
                         ("ed", "e"), ("ly", ""), ("er", ""), ("ers", "")):
        if w.endswith(suffix) and w[: -len(suffix)] + back in english:
            return True
    return False


def name_like(texts, english):
    """Capitalized words used mid-sentence in calls that aren't ordinary English or generic names."""
    names = set()
    for t in texts:
        t = " ".join(tok for tok in t.split() if len(tok) <= 25)   # drop pasted keys / encoded blobs
        for sentence in re.split(r"(?<=[.!?])\s+", t):
            for w in re.findall(r"\b[A-Z][a-z']{2,19}\b", sentence)[1:]:   # skip the first word
                low = re.sub(r"'s$", "", w.lower())
                if "'" in low:
                    continue                                      # contractions (I'll, I've)
                if not re.search(r"[aeiouy]", low) or re.search(r"[^aeiouy']{5}", low):
                    continue                                      # not a pronounceable word
                if not is_english(low, english) and low not in GENERIC:
                    names.add(low)
    return names


def identifying(gram, terms, names):
    """The identifying part of a matched run, or None if it's ordinary phrasing."""
    for t in terms:
        if re.search(rf"(?<![a-z0-9']){re.escape(t)}(?![a-z0-9'])", gram):
            return t
    for w in gram.split():
        if w in names or re.fullmatch(r"\d{3,}", w):
            return w
    return None


def main():
    show = "--show" in sys.argv
    if not (HOME / "speakeasy").exists():
        return 0
    texts = call_texts()
    if not texts:
        return 0
    terms, names = private_terms(), name_like(texts, common_english())
    grams = set()
    for t in texts:
        terms += [" ".join(words(e)) for e in re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", t)]
        w = words(t)
        for i in range(len(w) - N + 1):
            grams.add(" ".join(w[i:i + N]))
    files = subprocess.run(["git", "ls-files"], capture_output=True, text=True).stdout.split("\n")
    bad = 0
    for f in files:
        if not f or f.startswith("scripts/"):
            continue
        try:
            text = Path(f).read_text(errors="ignore")
        except (OSError, IsADirectoryError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            w = words(line)
            for i in range(len(w) - N + 1):
                g = " ".join(w[i:i + N])
                if g in grams:
                    why = identifying(g, terms, names)
                    if why:
                        print(f"FOUND identifying words from a real call: {f}:{n}"
                              + (f"  ({why!r})" if show else ""))
                        bad += 1
                        break
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
