#!/usr/bin/env python3
"""Tests for spacing_census.py: the counting rules and the pull-request ratchet.

Runs with the standard library only: python3 scripts/test_spacing_census.py
"""
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import spacing_census as sc  # noqa: E402


def kinds(text):
    return [(h["kind"], h["value"]) for h in sc.scan(text)]


class ScanRules(unittest.TestCase):
    def test_counts_padding_gap_spacer_radius_font(self):
        src = """
        VStack(alignment: .leading, spacing: 6) {
            Text("Hi").padding(14)
            Text("Yo").padding(.horizontal, 18).padding([.top, .bottom], 10)
            Spacer(minLength: 12)
        }
        .padding(EdgeInsets(top: 4, leading: 18, bottom: 0, trailing: 18))
        .background(RoundedRectangle(cornerRadius: 19, style: .continuous).fill(.red))
        .cornerRadius(7)
        .font(.system(size: 13, weight: .semibold))
        """
        self.assertEqual(sorted(kinds(src)), sorted([
            ("spacing", 6), ("spacing", 14), ("spacing", 18), ("spacing", 10), ("spacing", 12),
            ("spacing", 4), ("spacing", 18), ("spacing", 18),
            ("radius", 19), ("radius", 7), ("font", 13)]))

    def test_tokens_zero_and_hairlines_are_not_literals(self):
        src = """
        VStack(spacing: CleanGrid.s) { Text("a").padding(CleanGrid.l).padding(.top, 0) }
        .padding(.horizontal, CleanGrid.margin)
        .overlay(Rectangle().padding(.horizontal, 0.5)).padding(1)
        .clipShape(RoundedRectangle(cornerRadius: CleanGrid.cardRadius))
        .font(.callout)
        """
        self.assertEqual(kinds(src), [])

    def test_ternary_branches_count_but_computed_values_do_not(self):
        src = """
        Text("a").padding(.horizontal, wide ? 28 : 18)
        Text("b").padding(.leading, CleanGrid.margin - 4)
        Text("c").padding(.top, max(6, margins.top))
        Text("d").padding(8 * s).font(.system(size: 12 * s))
        Text("e").font(.system(size: base - 0.5))
        """
        self.assertEqual(sorted(kinds(src)), [("spacing", 18), ("spacing", 28)])

    def test_fit_code_and_tight_typing_are_exempt(self):
        src = """
        enum DockFit {
            static let gap: CGFloat = 6
            static func pad() -> some View { Color.clear.padding(.bottom, 7) }
        }
        struct V: View { var body: some View {
            Color.clear.padding(.bottom, tightTyping ? 2 : 8).padding(5)
        } }
        """
        self.assertEqual(kinds(src), [("spacing", 5)])

    def test_glyph_font_sizes_waivers_comments_and_strings_are_exempt(self):
        src = """
        Image(systemName: "mic.fill")
            .font(.system(size: 15, weight: .semibold))
        Label("Filter", systemImage: "line.3.horizontal.decrease")
            .labelStyle(.iconOnly)
            .font(.system(size: 16, weight: .semibold))
        Text("padding(14)").padding(9) // census: ok matches the system keyboard inset
        // .padding(22) in a comment
        /* .cornerRadius(5) */
        Text("x").font(.system(size: 10))
        """
        self.assertEqual(kinds(src), [("font", 10)])

    def test_declarations_named_spacing_are_not_arguments(self):
        src = """
        static var spacing: CGFloat { 12 }
        init(spacing: CGFloat = 8) {}
        let row = HStack(spacing: 3) {}
        """
        self.assertEqual(kinds(src), [("spacing", 3)])

    def test_line_numbers(self):
        hits = sc.scan("a\nb\n  .padding(14)\n")
        self.assertEqual(hits[0]["line"], 3)


class Ratchet(unittest.TestCase):
    """A throwaway git repo per test: base commit on main, PR branch on top."""

    BASE = """import SwiftUI

struct Card: View {
    var body: some View {
        VStack(spacing: CleanGrid.s) {
            Text("Title").padding(.horizontal, 18)
            Text("Body")
        }
        .padding(CleanGrid.cardPadding)
    }
}
"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = self.tmp.name
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "census@example.com")
        self.git("config", "user.name", "Census Test")
        self.write("iOS/Todd/Card.swift", self.BASE)
        self.write("iOS/Todd/Other.swift", "import SwiftUI\nstruct O: View { var body: some View { Text(\"o\").padding(10) } }\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "base")
        self.git("checkout", "-q", "-b", "pr")

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        return subprocess.run(["git", "-C", self.repo, *args], check=True, capture_output=True, text=True).stdout

    def write(self, rel, text):
        path = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(text)

    def commit(self, rel, text):
        self.write(rel, text)
        self.git("commit", "-q", "-am", "change")

    def run_check(self, mode="report"):
        result = sc.check(self.repo, "main", "HEAD", sc.DEFAULT_SCOPE)
        with contextlib.redirect_stdout(io.StringIO()):
            code = sc.main(["check", "--repo", self.repo, "--base", "main", "--mode", mode])
        return result, code

    def test_new_literal_is_reported(self):
        self.commit("iOS/Todd/Card.swift", self.BASE.replace('Text("Body")', 'Text("Body").padding(.top, 14)'))
        result, code = self.run_check()
        self.assertEqual([(f["kind"], f["value"], f["line"]) for f in result["new_literals"]], [("spacing", 14, 7)])
        self.assertEqual(result["files_raised"], [{"path": "iOS/Todd/Card.swift", "before": 1, "after": 2, "grew": ["spacing 14"]}])
        self.assertEqual(code, 0, "report mode never fails the job")
        self.assertEqual(self.run_check("block")[1], 1, "block mode fails on a new literal")

    def test_new_radius_and_font_literals_are_reported(self):
        self.commit("iOS/Todd/Card.swift", self.BASE.replace(
            'Text("Body")', 'Text("Body").font(.system(size: 11.5)).background(RoundedRectangle(cornerRadius: 19).fill(.gray))'))
        result, _ = self.run_check()
        self.assertEqual(sorted((f["kind"], f["value"]) for f in result["new_literals"]), [("font", 11.5), ("radius", 19)])

    def test_token_only_change_passes(self):
        self.commit("iOS/Todd/Card.swift", self.BASE.replace(
            'Text("Body")', 'Text("Body").padding(.top, CleanGrid.m).clipShape(RoundedRectangle(cornerRadius: CleanGrid.innerRadius))'))
        result, code = self.run_check("block")
        self.assertEqual(result["new_literals"], [])
        self.assertEqual(result["files_raised"], [])
        self.assertEqual(code, 0)

    def test_replacing_a_literal_with_a_token_passes(self):
        self.commit("iOS/Todd/Card.swift", self.BASE.replace(".padding(.horizontal, 18)", ".padding(.horizontal, CleanGrid.margin)"))
        result, code = self.run_check("block")
        self.assertEqual((result["new_literals"], result["files_raised"], code), ([], [], 0))

    def test_editing_or_moving_a_line_with_an_old_literal_is_not_new(self):
        moved = self.BASE.replace('            Text("Title").padding(.horizontal, 18)\n', "").replace(
            '            Text("Body")\n', '            Text("Body")\n            Text("Heading").bold().padding(.horizontal, 18)\n')
        self.commit("iOS/Todd/Card.swift", moved)
        result, code = self.run_check("block")
        self.assertEqual((result["new_literals"], result["files_raised"], code), ([], [], 0))

    def test_changing_a_literal_value_is_reported(self):
        self.commit("iOS/Todd/Card.swift", self.BASE.replace(".padding(.horizontal, 18)", ".padding(.horizontal, 15)"))
        result, _ = self.run_check()
        self.assertEqual([(f["kind"], f["value"]) for f in result["new_literals"]], [("spacing", 15)])
        self.assertEqual(result["files_raised"], [], "count unchanged: one literal swapped for another")

    def test_new_file_with_literals_is_reported(self):
        self.write("iOS/Todd/New.swift", "import SwiftUI\nstruct N: View { var body: some View { Text(\"n\").padding(6) } }\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "new file")
        result, _ = self.run_check()
        self.assertEqual([(f["path"], f["value"]) for f in result["new_literals"]], [("iOS/Todd/New.swift", 6)])

    def test_out_of_scope_paths_are_ignored(self):
        self.write("iOS/ToddTests/T.swift", "let x = Text(\"t\").padding(13)\n")
        self.write("iOS/Todd/README.md", ".padding(13)\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "tests")
        result, _ = self.run_check("block")
        self.assertEqual(result["new_literals"], [])

    def test_base_moving_on_does_not_blame_the_pr(self):
        # Another PR lands a literal on main after this branch was cut; only this PR's diff counts.
        self.git("checkout", "-q", "main")
        self.commit("iOS/Todd/Other.swift", "import SwiftUI\nstruct O: View { var body: some View { Text(\"o\").padding(10).padding(3) } }\n")
        self.git("checkout", "-q", "pr")
        self.commit("iOS/Todd/Card.swift", self.BASE.replace('Text("Body")', 'Text("Body").padding(CleanGrid.s)'))
        result, _ = self.run_check("block")
        self.assertEqual(result["new_literals"], [])

    def test_summary_and_annotations(self):
        self.commit("iOS/Todd/Card.swift", self.BASE.replace('Text("Body")', 'Text("Body").padding(.top, 14)'))
        summary = os.path.join(self.repo, "summary.md")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            sc.main(["check", "--repo", self.repo, "--base", "main", "--summary", summary, "--annotations"])
        self.assertIn("::warning file=iOS/Todd/Card.swift,line=7,title=Spacing census::New spacing literal 14", out.getvalue())
        with open(summary) as fh:
            text = fh.read()
        self.assertIn("Report only", text)
        self.assertIn("`iOS/Todd/Card.swift` | 7 | spacing | 14", text)


class Census(unittest.TestCase):
    def test_totals(self):
        files = {
            "iOS/Todd/A.swift": "VStack(spacing: 6) { Text(\"a\").padding(14).padding(.top, 16) }\n.cornerRadius(19)\n",
            "iOS/Todd/B.swift": "Text(\"b\").padding(7).font(.system(size: 11)).padding(CleanGrid.l)\n",
        }
        t = sc.totals(files)
        self.assertEqual(t["files"], 2)
        self.assertEqual(t["spacing_literals"], 4)
        self.assertEqual(t["spacing_distinct"], 4)
        self.assertEqual(t["spacing_off_scale"], 3)  # 6, 14, 7
        self.assertEqual(t["spacing_odd_or_fractional"], 1)  # 7
        self.assertEqual((t["radius_literals"], t["font_literals"], t["clean_grid_uses"]), (1, 1, 1))
        self.assertEqual(t["per_file"], {"iOS/Todd/A.swift": 4, "iOS/Todd/B.swift": 2})


if __name__ == "__main__":
    unittest.main(verbosity=1)
