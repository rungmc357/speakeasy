#!/usr/bin/env python3
"""Spacing census: count hand-typed spacing, corner and font-size literals in SwiftUI code,
and check a pull request for new ones (rule S2 of the spacing cleanup).

Two commands, standard library only:

  census  Totals for a tree: every padding / stack-gap / Spacer(minLength:) literal, every
          corner radius literal, every fixed font size, plus how often CleanGrid is used.
            spacing_census.py census                       # working tree, default scope
            spacing_census.py census --tree .@016be7b      # a commit of this repo
            spacing_census.py census --tree .@016be7b --tree ../speakeasy@f36d7157 --json

  check   The ratchet. Compares a base and a head commit and reports
            1. literals on added lines that the removed lines of the same file don't account
               for (a moved or re-indented line is not new), and
            2. any file whose literal count rose.
          --mode report (default) always exits 0; --mode block exits 1 on any finding.
            spacing_census.py check --base origin/b78-ios --head HEAD --mode report

What counts (the rule S1/S7/S10 surfaces):
  spacing  .padding(N), .padding(.edge, N), .padding([.edges], N), EdgeInsets(top: N, ...),
           spacing: N (stacks, grids, GridItem), horizontalSpacing: / verticalSpacing:,
           Spacer(minLength: N)
  radius   cornerRadius: N / .cornerRadius(N) and the four Uneven…Radius: N labels
  font     .system(size: N), UIFont.systemFont(ofSize: N), .custom("…", size: N)

Not counted (the S1 exemptions):
  0 (spacing and radius); 0.5 and 1 pt hairlines; computed arguments (see below); fit code (anything inside enum/struct DockFit,
  CallFit or WidgetLayout, and any argument that mentions tightTyping); font sizes on an
  SF Symbol (`Image(systemName:` or `systemImage:` on the same line or the three before); and any line
  carrying an explicit `// census: ok <reason>` waiver. Comments and string contents are ignored.

Only bare numbers count, including each bare branch of a ternary or `??`: `wide ? 28 : 18`
counts both. Computed values (`CleanGrid.margin - 4`, `size * 0.2`, `max(6, inset)`, the Mac
scale `8 * s`) don't: they are fit or scale code, not a hand-picked spacing step.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import subprocess
import sys

# Rule S1 scope. Paths that don't exist in a repo are simply skipped, so one copy of this
# script serves both speakeasy-apple (iOS/Todd) and speakeasy (the shared cards).
DEFAULT_SCOPE = ["iOS/Todd/", "mac/Sources/SpeakeasyClient/Cards/"]

SCALE = [2, 4, 8, 12, 16, 20, 24, 32, 40, 48]  # CleanGrid.scale (SPK-413)
HAIRLINES = {0.5, 1.0}
FIT_TYPES = ("DockFit", "CallFit", "WidgetLayout")
WAIVER = re.compile(r"//\s*census:\s*ok\b", re.I)

NUM = r"-?\d+(?:\.\d+)?"


# ---------------------------------------------------------------- source scanning

def blank_comments_and_strings(text: str) -> str:
    """Same length and line structure; comments and string contents become spaces."""
    out = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append(re.sub(r"[^\n]", " ", text[i:j]))
            i = j
        elif text.startswith('"""', i):
            j = text.find('"""', i + 3)
            j = n if j < 0 else j + 3
            out.append('"' + re.sub(r"[^\n]", " ", text[i + 1:j - 1]) + '"')
            i = j
        elif c == '"':
            j = i + 1
            while j < n and text[j] not in '"\n':
                j += 2 if text[j] == "\\" else 1
            j = min(j + 1, n)
            out.append('"' + " " * max(0, j - i - 2) + ('"' if j - i >= 2 else ""))
            i = j
        else:
            out.append(c)
            i += 1
    return "".join(out)


def read_arg(code: str, start: int) -> str:
    """The argument expression starting at `start`, up to a ',' or ')' at depth 0."""
    depth = 0
    i = start
    while i < len(code):
        c = code[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            if depth == 0:
                break
            depth -= 1
        elif c == "," and depth == 0:
            break
        i += 1
    return code[start:i]


def read_call(code: str, open_paren: int) -> str:
    """Contents of the parenthesised call whose '(' is at open_paren."""
    depth = 0
    for i in range(open_paren, len(code)):
        if code[i] == "(":
            depth += 1
        elif code[i] == ")":
            depth -= 1
            if depth == 0:
                return code[open_paren + 1:i]
    return code[open_paren + 1:]


def split_args(inner: str) -> list[tuple[int, str]]:
    """Top-level arguments of a call body as (offset, text)."""
    args, depth, last = [], 0, 0
    for i, c in enumerate(inner):
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == "," and depth == 0:
            args.append((last, inner[last:i]))
            last = i + 1
    args.append((last, inner[last:]))
    return args


PURE = re.compile(r"\(*\s*(" + NUM + r")\s*\)*")
DECLARATION = re.compile(r"^\s*[A-Z]\w*[?!]?\s*(=|\{|$)")


def ternary_branches(expr: str) -> list[str]:
    """`a ? b : c` -> [b, c] (recursively); `a ?? b` -> [a, b]; otherwise [expr]."""
    depth, q = 0, -1
    for i, ch in enumerate(expr):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif depth == 0 and expr.startswith("??", i):
            return ternary_branches(expr[:i]) + ternary_branches(expr[i + 2:])
        elif depth == 0 and ch == "?" and q < 0 and not expr.startswith("??", i) and (i == 0 or expr[i - 1] != "?"):
            q = i
        elif depth == 0 and ch == ":" and q >= 0:
            return ternary_branches(expr[q + 1:i]) + ternary_branches(expr[i + 1:])
    return [expr]


def literals_in(expr: str) -> list[float]:
    """Numeric literals an argument hard-codes. Only a bare number (or a ternary / ?? branch
    that is a bare number) counts; anything computed (`margin - 4`, `size * 0.2`, `max(6, x)`,
    the Mac scale `8 * s`) is not a hand-typed spacing value."""
    if "tightTyping" in expr or DECLARATION.match(expr):
        return []
    vals = []
    for branch in ternary_branches(expr):
        m = PURE.fullmatch(branch.strip())
        if m:
            vals.append(float(m.group(1)))
    return vals


PADDING = re.compile(r"\.padding\s*\(")
EDGE_INSETS = re.compile(r"\bEdgeInsets\s*\(")
SPACING_LABEL = re.compile(r"\b(spacing|horizontalSpacing|verticalSpacing|minLength)\s*:")
RADIUS_LABEL = re.compile(r"\b(cornerRadius|topLeadingRadius|topTrailingRadius|bottomLeadingRadius|bottomTrailingRadius)\s*:")
RADIUS_CALL = re.compile(r"\.cornerRadius\s*\(")
FONT_SIZE = re.compile(r"(?:\.system|\bFont\.system|\.custom)\s*\(")
UIFONT_SIZE = re.compile(r"\bsystemFont\s*\(\s*ofSize\s*:")
FIT_DECL = re.compile(r"\b(?:enum|struct|extension)\s+(" + "|".join(FIT_TYPES) + r")\b")
GLYPH = re.compile(r"Image\s*\(\s*systemName\s*:|systemImage\s*:")


def fit_ranges(code: str) -> list[tuple[int, int]]:
    """Character ranges of fit-code type bodies (DockFit, CallFit, WidgetLayout)."""
    ranges = []
    for m in FIT_DECL.finditer(code):
        brace = code.find("{", m.end())
        if brace < 0:
            continue
        depth = 0
        for i in range(brace, len(code)):
            if code[i] == "{":
                depth += 1
            elif code[i] == "}":
                depth -= 1
                if depth == 0:
                    ranges.append((m.start(), i))
                    break
    return ranges


def scan(text: str) -> list[dict]:
    """Every counted literal in one Swift source: {kind, value, line}."""
    code = blank_comments_and_strings(text)
    raw_lines = text.split("\n")
    line_starts = [0]
    for i, c in enumerate(code):
        if c == "\n":
            line_starts.append(i + 1)

    def line_of(pos: int) -> int:
        lo, hi = 0, len(line_starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if line_starts[mid] <= pos:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1

    fits = fit_ranges(code)
    found: list[dict] = []

    def add(kind: str, pos: int, expr: str):
        if any(a <= pos <= b for a, b in fits):
            return
        ln = line_of(pos)
        if WAIVER.search(raw_lines[ln - 1]):
            return
        if kind == "font":
            context = "\n".join(raw_lines[max(0, ln - 4):ln])
            if GLYPH.search(context):
                return
        for v in literals_in(expr):
            if kind in ("spacing", "radius") and v == 0:
                continue
            if kind == "spacing" and abs(v) in HAIRLINES:
                continue
            found.append({"kind": kind, "value": v, "line": ln})

    for m in PADDING.finditer(code):
        inner = read_call(code, m.end() - 1)
        args = split_args(inner)
        if len(args) == 1 and args[0][1].strip() and not args[0][1].strip().startswith((".", "[")) \
                and "EdgeInsets" not in args[0][1]:
            add("spacing", m.end() + args[0][0], args[0][1])
        elif len(args) == 2:
            add("spacing", m.end() + args[1][0], args[1][1])
    for m in EDGE_INSETS.finditer(code):
        inner = read_call(code, m.end() - 1)
        for off, arg in split_args(inner):
            if ":" in arg:
                add("spacing", m.end() + off, arg.split(":", 1)[1])
    for m in SPACING_LABEL.finditer(code):
        line_head = code[code.rfind("\n", 0, m.start()) + 1:m.start()]
        if re.search(r"\b(var|let|case)\s+$", line_head):
            continue  # a declaration named `spacing`, not an argument
        add("spacing", m.end(), read_arg(code, m.end()))
    for m in RADIUS_LABEL.finditer(code):
        add("radius", m.end(), read_arg(code, m.end()))
    for m in RADIUS_CALL.finditer(code):
        add("radius", m.end(), read_arg(code, m.end()))
    for m in FONT_SIZE.finditer(code):
        inner = read_call(code, m.end() - 1)
        for off, arg in split_args(inner):
            if re.match(r"\s*size\s*:", arg):
                add("font", m.end() + off, arg.split(":", 1)[1])
    for m in UIFONT_SIZE.finditer(code):
        add("font", m.end(), read_arg(code, m.end()))
    found.sort(key=lambda f: (f["line"], f["kind"], f["value"]))
    return found


def clean_grid_uses(text: str) -> int:
    return len(re.findall(r"\bCleanGrid\.", blank_comments_and_strings(text)))


# ---------------------------------------------------------------- trees and diffs

def git(repo: str, *args: str) -> str:
    return subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True, text=True).stdout


def in_scope(path: str, scope: list[str]) -> bool:
    return path.endswith(".swift") and any(path.startswith(s) for s in scope)


def tree_files(spec: str, scope: list[str]) -> dict[str, str]:
    """`DIR` (working tree) or `DIR@REV` (a commit) -> {path: source}."""
    repo, _, rev = spec.partition("@")
    repo = repo or "."
    files = {}
    if rev:
        for path in git(repo, "ls-tree", "-r", "--name-only", rev).split("\n"):
            if in_scope(path, scope):
                files[path] = git(repo, "show", f"{rev}:{path}")
    else:
        for top in scope:
            base = os.path.join(repo, top)
            for root, _, names in os.walk(base):
                for name in names:
                    full = os.path.join(root, name)
                    rel = os.path.relpath(full, repo)
                    if in_scope(rel, scope):
                        with open(full, encoding="utf-8") as fh:
                            files[rel] = fh.read()
    return files


def totals(files: dict[str, str]) -> dict:
    by_kind = {k: collections.Counter() for k in ("spacing", "radius", "font")}
    per_file = {}
    lines = 0
    grid = 0
    for path, text in sorted(files.items()):
        lines += text.count("\n") + (0 if text.endswith("\n") or not text else 1)
        grid += clean_grid_uses(text)
        hits = scan(text)
        per_file[path] = len(hits)
        for h in hits:
            by_kind[h["kind"]][h["value"]] += 1
    sp = by_kind["spacing"]

    def fmt(v):
        return int(v) if v == int(v) else v

    return {
        "files": len(files),
        "lines": lines,
        "spacing_literals": sum(sp.values()),
        "spacing_distinct": len(sp),
        "spacing_off_scale": sum(c for v, c in sp.items() if abs(v) not in SCALE),
        "spacing_odd_or_fractional": sum(c for v, c in sp.items() if v != int(v) or int(v) % 2),
        "radius_literals": sum(by_kind["radius"].values()),
        "radius_distinct": len(by_kind["radius"]),
        "font_literals": sum(by_kind["font"].values()),
        "font_distinct": len(by_kind["font"]),
        "clean_grid_uses": grid,
        "spacing_values": {str(fmt(v)): c for v, c in sorted(sp.items())},
        "radius_values": {str(fmt(v)): c for v, c in sorted(by_kind["radius"].items())},
        "font_values": {str(fmt(v)): c for v, c in sorted(by_kind["font"].items())},
        "per_file": per_file,
    }


HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def changed_lines(repo: str, base: str, head: str, scope: list[str]):
    """{path: (removed_line_numbers_in_base, added_line_numbers_in_head)} for scoped files."""
    diff = git(repo, "diff", "--unified=0", "--no-color", "--no-renames", f"{base}...{head}", "--", *scope)
    out: dict[str, tuple[set, set]] = {}
    path = None
    for line in diff.split("\n"):
        if line.startswith("+++ "):
            p = line[4:]
            path = p[2:] if p.startswith("b/") else None
        elif line.startswith("--- "):
            p = line[4:]
            if p.startswith("a/"):
                path = p[2:]
        elif line.startswith("@@") and path and in_scope(path, scope):
            m = HUNK.match(line)
            if not m:
                continue
            a, an, b, bn = int(m[1]), int(m[2] or 1), int(m[3]), int(m[4] or 1)
            rem, add = out.setdefault(path, (set(), set()))
            rem.update(range(a, a + an))
            add.update(range(b, b + bn))
    return out


def show(repo: str, rev: str, path: str) -> str:
    try:
        return git(repo, "show", f"{rev}:{path}")
    except subprocess.CalledProcessError:
        return ""


def check(repo: str, base: str, head: str, scope: list[str]) -> dict:
    merge_base = git(repo, "merge-base", base, head).strip()
    findings, raised = [], []
    for path, (removed, added) in sorted(changed_lines(repo, merge_base, head, scope).items()):
        old_hits = scan(show(repo, merge_base, path))
        new_text = show(repo, head, path)
        new_hits = scan(new_text)
        gone = collections.Counter((h["kind"], h["value"]) for h in old_hits if h["line"] in removed)
        src = new_text.split("\n")
        for h in new_hits:
            if h["line"] not in added:
                continue
            key = (h["kind"], h["value"])
            if gone[key] > 0:
                gone[key] -= 1  # same literal on a removed line: moved or edited, not new
                continue
            findings.append({**h, "path": path, "source": src[h["line"] - 1].strip()[:160]})
        if len(new_hits) > len(old_hits):
            grew = collections.Counter((h["kind"], h["value"]) for h in new_hits)
            grew.subtract((h["kind"], h["value"]) for h in old_hits)
            raised.append({"path": path, "before": len(old_hits), "after": len(new_hits),
                           "grew": [f"{k} {fmt_value(v)}" for (k, v), n in sorted(grew.items()) for _ in range(max(0, n))]})
    return {"base": merge_base, "head": head, "new_literals": findings, "files_raised": raised}


# ---------------------------------------------------------------- output

def fmt_value(v: float) -> str:
    return str(int(v)) if v == int(v) else str(v)


HOW_TO_FIX = {
    "spacing": "use a CleanGrid step (2, 4, 8, 12, 16, 20, 24, 32, 40, 48) or CleanGrid.margin",
    "radius": "use CleanGrid.cardRadius, innerRadius, tileRadius, bubbleRadius or Capsule",
    "font": "use a text style (.body, .callout, .caption…) or @ScaledMetric",
}


def render_check(result: dict, mode: str) -> str:
    lines = ["## Spacing census", ""]
    new, raised = result["new_literals"], result["files_raised"]
    if not new and not raised:
        lines.append("No new spacing, corner or font-size literals. Thanks for using the tokens.")
        return "\n".join(lines) + "\n"
    verdict = "These fail the check." if mode == "block" else "Report only: this check does not block merging yet."
    lines.append(f"{len(new)} new literal(s) on changed lines; {len(raised)} file(s) whose count rose. {verdict}")
    lines.append("")
    if new:
        lines += ["| File | Line | Kind | Value | Code |", "| -- | -- | -- | -- | -- |"]
        for f in new:
            code = f["source"].replace("|", "\\|")
            lines.append(f"| `{f['path']}` | {f['line']} | {f['kind']} | {fmt_value(f['value'])} | `{code}` |")
        lines.append("")
    if raised:
        lines.append("Files whose literal count rose: " + ", ".join(
            f"`{r['path']}` {r['before']} → {r['after']} (+{', '.join(r['grew'])})" for r in raised))
        lines.append("")
    lines.append("How to fix: " + "; ".join(f"{k}: {v}" for k, v in HOW_TO_FIX.items()) + ". "
                 "A value that must stay can carry `// census: ok <reason>` on its line.")
    return "\n".join(lines) + "\n"


def annotate(result: dict, mode: str) -> None:
    level = "error" if mode == "block" else "warning"
    for f in result["new_literals"]:
        msg = f"New {f['kind']} literal {fmt_value(f['value'])}: {HOW_TO_FIX[f['kind']]}"
        print(f"::{level} file={f['path']},line={f['line']},title=Spacing census::{msg}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("census")
    c.add_argument("--tree", action="append", help="DIR or DIR@REV (repeatable; totals are summed)")
    c.add_argument("--scope", action="append", help="path prefix to count (repeatable)")
    c.add_argument("--json", action="store_true")
    c.add_argument("--per-file", action="store_true")
    k = sub.add_parser("check")
    k.add_argument("--repo", default=".")
    k.add_argument("--base", required=True)
    k.add_argument("--head", default="HEAD")
    k.add_argument("--scope", action="append")
    k.add_argument("--mode", choices=["report", "block"], default="report")
    k.add_argument("--summary", help="append the markdown report to this file (e.g. $GITHUB_STEP_SUMMARY)")
    k.add_argument("--annotations", action="store_true", help="print GitHub ::warning / ::error lines")
    k.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    scope = a.scope or DEFAULT_SCOPE

    if a.cmd == "census":
        files = {}
        for spec in a.tree or ["."]:
            for p, t in tree_files(spec, scope).items():
                files[f"{spec.partition('@')[0]}:{p}" if len(a.tree or []) > 1 else p] = t
        t = totals(files)
        if not a.per_file:
            t.pop("per_file")
        if a.json:
            print(json.dumps(t, indent=1))
        else:
            for key, val in t.items():
                if isinstance(val, dict):
                    print(f"{key}: " + ", ".join(f"{v}×{c}" for v, c in val.items()))
                else:
                    print(f"{key}: {val:,}" if isinstance(val, int) else f"{key}: {val}")
        return 0

    result = check(a.repo, a.base, a.head, scope)
    report = render_check(result, a.mode)
    if a.summary:
        with open(a.summary, "a", encoding="utf-8") as fh:
            fh.write(report)
    if a.annotations:
        annotate(result, a.mode)
    print(json.dumps(result, indent=1) if a.json else report)
    failed = bool(result["new_literals"] or result["files_raised"])
    return 1 if (failed and a.mode == "block") else 0


if __name__ == "__main__":
    sys.exit(main())
