#!/usr/bin/env python3
"""Deterministic privacy-gate regressions using invented calls only.

Run: python3 -m unittest discover -s scripts -p 'test_check_real_calls.py' -v
"""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "check_real_calls", Path(__file__).with_name("check-real-calls.py"))
assert SPEC is not None and SPEC.loader is not None
scanner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scanner)


class EnglishInflectionTests(unittest.TestCase):
    def test_comparatives_and_superlatives_use_dictionary_roots(self):
        for root, comparative, superlative in (
            ("early", "earlier", "earliest"),
        ):
            for word in (comparative, superlative):
                with self.subTest(word=word):
                    self.assertTrue(scanner.is_english(word, {root}))
                    self.assertFalse(scanner.is_english(word, set()))

    def test_product_section_is_not_a_person(self):
        names = scanner.name_like(
            ["Move every finished answer to Earlier after reading."], {"early"})
        self.assertNotIn("earlier", names)

    def test_invented_name_still_identifies_a_quote(self):
        names = scanner.name_like(["Ask Marvella about the proposed meeting time."], {"early"})
        self.assertIn("marvella", names)
        self.assertEqual(scanner.identifying(
            "ask marvella about the proposed meeting", [], names), "marvella")

    def test_surname_is_not_treated_as_a_comparative(self):
        self.assertFalse(scanner.is_english("mercier", {"mercy"}))
        self.assertIn("mercier", scanner.name_like(
            ["Ask Mercier about the proposed meeting time."], {"mercy"}))

    def test_name_next_to_home_section_still_blocks(self):
        names = scanner.name_like(
            ["Ask Marvella to move this to Earlier."], {"early"})
        self.assertEqual(scanner.identifying(
            "ask marvella to move this to earlier", [], names), "marvella")

    def test_explicit_private_term_overrides_english(self):
        self.assertEqual(scanner.identifying(
            "move every finished answer to earlier", ["earlier"], set()), "earlier")


class GateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.home = self.root / "hermes"
        (self.home / "speakeasy").mkdir(parents=True)
        self.config = self.root / "config"
        self.config.mkdir()
        (self.config / "private-terms.txt").write_text("")
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)

    def gate(self, call, source, terms=""):
        (self.home / "speakeasy" / "call-log.jsonl").write_text(
            json.dumps({"turns": [{"text": call}]}) + "\n")
        (self.config / "private-terms.txt").write_text(terms)
        (self.repo / "CHANGELOG.md").write_text(source + "\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "CHANGELOG.md"], check=True)
        output = io.StringIO()
        previous = Path.cwd()
        try:
            os.chdir(self.repo)
            with patch.object(scanner, "HOME", self.home), \
                 patch.object(scanner, "CONFIG", self.config), \
                 patch.object(scanner, "common_english", return_value={"early"}), \
                 patch.object(sys, "argv", ["check-real-calls.py"]), \
                 contextlib.redirect_stdout(output):
                status = scanner.main()
        finally:
            os.chdir(previous)
        return status, output.getvalue()

    def test_unchanged_home_release_note_passes(self):
        sentence = "One tap moves every finished answer to Earlier, seen or not."
        self.assertEqual(self.gate(sentence, sentence), (0, ""))

    def test_private_term_still_blocks_home_word(self):
        sentence = "One tap moves every finished answer to Earlier, seen or not."
        status, output = self.gate(sentence, sentence, terms="earlier\n")
        self.assertEqual(status, 1)
        self.assertIn("CHANGELOG.md:1", output)
        self.assertNotIn("('earlier')", output)

    def test_invented_name_blocks(self):
        sentence = "Ask Marvella about the proposed meeting time."
        self.assertEqual(self.gate(sentence, sentence)[0], 1)

    def test_long_number_blocks(self):
        sentence = "The parcel reference is 876543 for collection tomorrow."
        self.assertEqual(self.gate(sentence, sentence)[0], 1)

    def test_email_blocks(self):
        sentence = "Send the meeting agenda to contact@example.invalid tomorrow."
        self.assertEqual(self.gate(sentence, sentence)[0], 1)

    def test_unrelated_text_passes(self):
        self.assertEqual(self.gate(
            "Ask Marvella about the proposed meeting time.",
            "The appearance picker offers several colour palettes."), (0, ""))


if __name__ == "__main__":
    unittest.main()
