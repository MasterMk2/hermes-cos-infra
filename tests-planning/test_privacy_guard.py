"""Privacy guard for the tracked tree — the standing global rule, in the suite.

Why this exists, stated plainly so it is not weakened by someone who does not
know the history: on 2026-09-08 this repository was pushed to a public GitHub
remote carrying a real person's name and their real appointment (date, weekday,
hour, purpose) in **deployed source**, a real LINE user id, a second real name
with its reading, and a real medical appointment. Two separate sanitisation
passes had already run and both had cleared it.

Both failed the same way, and it was the *method*, not the diligence: each
grepped for the names it happened to know. A list of known-bad names cannot
find an identifier nobody has noticed yet — and the most sensitive string in
the set was a medical appointment, which contains no name at all, so no
name-shaped check would ever have caught it. It is not quoted here either:
repeating a real value in the file that exists to remove it is the same
mistake one layer up.

So this file does two things instead, and neither is a name list:

1. **Pattern classes** (:class:`PatternClassScanTests`). Scan every tracked
   file for the *shapes* personal data takes — a Japanese name with a
   parenthesised reading, a name with an honorific, an external channel user
   id, a personal email domain, a local absolute path, a credential. This is
   the same rule set as the repository's `pre-push` hook, run over the whole
   tree rather than over one push, so a finding is visible at test time
   instead of at push time.

2. **A declared vocabulary** (:class:`DeclaredVocabularyTests`). Every
   `title=` / `counterparty=` / `summary=` literal in `tests-planning/` must be
   a member of `invented_vocabulary`. Pattern classes cannot tell an invented
   errand from a real one, so the fixtures are pinned to an enumerated list
   instead: pasting a production row back in turns the suite red, and adding it
   deliberately means editing a file whose docstring says what that means.

3. **The denylist, read at runtime** (:class:`DenylistTests`). The known real
   strings live in ONE untracked file outside every repository, named by
   ``HERMES_PRIVACY_DENYLIST`` and defaulting to the shared path below. This
   file reads it, greps the tracked tree, and — the assertion that makes the
   allowlist trustworthy — checks that the declared invented vocabulary and
   the denylist do not intersect. An allowlist built by reading contaminated
   fixtures inherits the contamination; this is what catches that.

**No real string appears in this file, and none ever may.** A guard that
carries its own denylist inline is a leak source wearing a safety label, and it
is tracked, so it ships. Categories are described in the abstract here; the
concrete values live only in the untracked denylist. When that file is absent —
a fresh clone, CI — the denylist tests skip with a reason rather than failing,
so nobody is blocked and nobody is tempted to paste the values in to unblock
themselves.

Run with:  python -m pytest tests-planning/test_privacy_guard.py -q
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(HERE, ".."))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from invented_vocabulary import (  # noqa: E402
    DEPLOYED_SOURCE_EXAMPLES,
    INVENTED_COUNTERPARTIES,
    INVENTED_SUMMARIES,
    INVENTED_TITLES,
)

KANJI = r"一-鿿"
HIRAGANA = r"ぁ-ゟ"

#: Placeholders the project has deliberately adopted. Kept SMALL and explicit:
#: this is the only allowlist in the file, and every entry is a value invented
#: for this repository, never a real one that was judged harmless.
DECLARED_PLACEHOLDERS = (
    "山田太郎", "鈴木一郎", "高橋", "ベンダA", "ベンダB", "制作チーム",
    "user@example.com", "archive@example.invalid",
    # Role nouns that legitimately occupy a person-shaped field. 「担当」 also
    # clears the 担当A-D fixtures; a real value such as 「担当の田中さん」 still
    # trips the honorific rule on the remainder.
    "担当", "事務局", "受付", "運営", "委員会", "先生",
)

#: A line carrying one of these is exempt. `privacy-ok` is the explicit,
#: reviewable marker; the rest are the shapes that prove a redactor works or
#: that are reserved-for-documentation by RFC 2606 / RFC 6761.
EXEMPT_LINE_MARKERS = (
    "privacy-ok",
    "re.compile",
    "example.com", "example.invalid", "example.org", "example.net",
    "test.invalid", ".example",
)

#: Directories whose content is not ours to police here. `.git` is not tracked;
#: the vendored hook is checked by its own review.
SKIP_PREFIXES = ()

#: Where the known real strings live. Outside every git tree, on purpose: a
#: denylist committed anywhere is the leak it exists to prevent. The env var
#: makes the path a machine's business rather than this file's.
DENYLIST_ENV = "HERMES_PRIVACY_DENYLIST"
DEFAULT_DENYLIST_PATH = os.path.join(
    os.path.dirname(os.path.dirname(REPO_ROOT)), ".hermes-i2-shared",
    "I2-PRIVACY-DENYLIST.txt",
)


def _load_denylist():
    """The denylist entries, or ``None`` when the file is not on this machine.

    ``None`` means "cannot check", not "clean": the caller skips with a reason.
    Blank lines and `#` comments are dropped; everything else is matched as a
    literal substring, because a real string can appear inside a longer one.
    """
    path = os.environ.get(DENYLIST_ENV) or DEFAULT_DENYLIST_PATH
    try:
        with open(path, encoding="utf-8") as handle:
            raw = handle.read()
    except OSError:
        return None
    return [
        line.strip() for line in raw.split("\n")
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _is_low_entropy_id(token: str) -> bool:
    """True for a hand-written fixture id such as `u0000…0000`.

    A real external id is high-entropy; a fixture is padding. Counting
    distinct hex digits separates the two with no keyword list — the same test
    the `pre-push` hook uses, so the two cannot disagree.
    """
    return len(set(token[1:].lower())) < 8


def _is_obvious_fixture_secret(token: str) -> bool:
    """True for `ghp_AAAA…A` and friends: a credential shape, no credential.

    A real token is high-entropy by construction — that is what makes it a
    secret. A suite that proves a redactor works needs a string of the right
    *shape*, and writes one character repeatedly. Fewer than five distinct
    characters in the body is not something a token generator produces.
    Deliberately narrow: anything richer than that needs an explicit
    `# privacy-ok`, so a real credential can never be waved through by a
    heuristic.
    """
    body = re.sub(r"^(?:gh[pousr]_|github_pat_|sk-|AIza|xox[baprs]-|ya29\.)", "",
                  token)
    return len(body) >= 16 and len(set(body.lower())) < 5


CHECKS = (
    (
        "japanese-name-with-reading",
        re.compile(rf"[{KANJI}]{{2,5}}[(（][{HIRAGANA}]{{2,12}}[)）]"),
        "漢字氏名+ふりがな",
    ),
    (
        # No trailing lookahead. A real title routinely continues into kana
        # straight after the honorific (「<name>さんに…」), so a `(?![ぁ-ん])`
        # guard would swallow exactly the shape that leaked.
        "honorific-name",
        re.compile(rf"[{KANJI}]{{2,4}}(?:さん|氏|様|くん|ちゃん)"),
        "敬称付きの氏名",
    ),
    (
        # Not a fixed length: the leaked value was `u` + 27 hex. A leading
        # u/U is what separates these from a git SHA, since `u` is not hex.
        "external-user-id",
        re.compile(r"(?<![0-9A-Za-z])[Uu][0-9a-f]{20,40}(?![0-9A-Za-z])"),
        "外部サービスのユーザーID",
    ),
    (
        "personal-email",
        re.compile(
            r"[A-Za-z0-9._%+\-]+@(?:gmail|googlemail|yahoo|ymail|outlook|hotmail"
            r"|live|icloud|me|docomo|ezweb|softbank)\.",
            re.I,
        ),
        "個人メールアドレス",
    ),
    (
        "local-path",
        re.compile(r"[Cc]:[\\/]{1,2}Users[\\/]|/Users/[A-Za-z]"),
        "ローカル絶対パス",
    ),
    (
        "secret",
        re.compile(
            r"gh[pousr]_[A-Za-z0-9]{30,}"
            r"|github_pat_[A-Za-z0-9_]{40,}"
            r"|sk-[A-Za-z0-9]{30,}"
            r"|AIza[A-Za-z0-9_\-]{30,}"
            r"|xox[baprs]-[A-Za-z0-9\-]{10,}"
            r"|ya29\.[A-Za-z0-9_\-]{20,}"
            r"|-----BEGIN [A-Z ]*PRIVATE KEY"
        ),
        "認証情報らしき文字列",
    ),
)


def _git(*args):
    """Run git, decoding as UTF-8. The console here is cp932."""
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120,
    )


def _tracked_files():
    proc = _git("ls-files", "-z")
    if proc.returncode != 0:
        return None
    return [name for name in proc.stdout.split("\0") if name.strip()]


class PatternClassScanTests(unittest.TestCase):
    """Shapes, not names. See the module docstring for why."""

    @classmethod
    def setUpClass(cls):
        try:
            cls.files = _tracked_files()
        except (OSError, subprocess.SubprocessError):  # pragma: no cover
            cls.files = None

    def _scan(self, predicate=lambda name: True):
        if self.files is None:  # pragma: no cover - no git in this environment
            self.skipTest("git is unavailable")
        findings = []
        for name in self.files:
            if not predicate(name) or name.startswith(SKIP_PREFIXES):
                continue
            path = os.path.join(REPO_ROOT, name)
            try:
                with open(path, encoding="utf-8") as handle:
                    lines = handle.read().split("\n")
            except (OSError, UnicodeDecodeError):
                continue  # binary or unreadable: nothing to read as text
            for number, line in enumerate(lines, 1):
                stripped = line
                for placeholder in DECLARED_PLACEHOLDERS:
                    stripped = stripped.replace(placeholder, "")
                low = line.lower()
                if any(marker.lower() in low for marker in EXEMPT_LINE_MARKERS):
                    continue
                for label, pattern, why in CHECKS:
                    match = pattern.search(stripped)
                    if not match:
                        continue
                    if label == "external-user-id" and _is_low_entropy_id(match.group(0)):
                        continue
                    if label == "secret" and _is_obvious_fixture_secret(match.group(0)):
                        continue
                    findings.append(
                        f"{name}:{number} [{label} / {why}] {match.group(0)}"
                    )
        return findings

    def test_the_whole_tracked_tree_is_clean(self):
        findings = self._scan()
        self.assertEqual(
            findings, [],
            "personal data shapes found in tracked files:\n  "
            + "\n  ".join(findings),
        )

    def test_the_phase_i2_source_and_fixtures_are_clean(self):
        """The same scan, narrowed, so a failure here names the owner of it."""
        findings = self._scan(
            lambda name: name.startswith(("src/planning/", "tests-planning/",
                                          "src/events/event_mcp.py"))
        )
        self.assertEqual(findings, [], "\n  ".join(findings))

    def test_the_scan_actually_matches_something(self):
        """A guard that cannot fail guards nothing.

        Every pattern is exercised against a synthetic positive, so a regex
        that stops matching — an escaping change, a Python version — is caught
        here rather than by the scan silently passing forever.
        """
        samples = {
            "japanese-name-with-reading": "counterparty=\"田中一郎(たなかいちろう)\"",
            "honorific-name": "田中さんに連絡する",
            "external-user-id": "u1a2b3c4d5e6f7a8b9c0d1e2f3a4b5",
            "personal-email": "someone@gmail.com",
            "local-path": "C:\\Users\\someone\\project",
            "secret": "ghp_" + "a1b2c3d4e5" * 4,
        }
        for label, pattern, _why in CHECKS:
            with self.subTest(check=label):
                self.assertRegex(samples[label], pattern)

    def test_a_padded_fixture_token_is_exempt_but_a_real_shape_is_not(self):
        """The entropy exemption must be narrow enough to still catch a token."""
        self.assertTrue(_is_obvious_fixture_secret("ghp_" + "A" * 36))
        self.assertTrue(_is_obvious_fixture_secret("sk-" + "0" * 32))
        self.assertFalse(_is_obvious_fixture_secret("ghp_1234567890abcdef1234567890abcdef1234"))
        self.assertFalse(_is_obvious_fixture_secret("ghp_" + "A" * 8))

    def test_the_placeholder_allowlist_does_not_swallow_a_real_name(self):
        """Stripping 「高橋」 must not make 「高橋田中さん」 look clean."""
        line = 'counterparty="高橋"  # fine'
        stripped = line
        for placeholder in DECLARED_PLACEHOLDERS:
            stripped = stripped.replace(placeholder, "")
        for _label, pattern, _why in CHECKS:
            self.assertIsNone(pattern.search(stripped))

        hostile = 'counterparty="高橋田中さん"'
        stripped = hostile
        for placeholder in DECLARED_PLACEHOLDERS:
            stripped = stripped.replace(placeholder, "")
        self.assertTrue(
            any(pattern.search(stripped) for _l, pattern, _w in CHECKS),
            "a real name adjacent to a placeholder must still be caught",
        )


class DeclaredVocabularyTests(unittest.TestCase):
    """Fixture content comes from an enumerated list, or the suite goes red."""

    FIELD_RE = re.compile(
        r"""(?P<field>title|counterparty|summary)\s*=\s*f?(?P<q>["'])"""
        r"""(?P<val>(?:\\.|(?!(?P=q))[^\\])*)(?P=q)"""
    )

    DECLARED = {
        "title": INVENTED_TITLES,
        "counterparty": INVENTED_COUNTERPARTIES,
        "summary": INVENTED_SUMMARIES,
    }

    def _fixture_modules(self):
        for name in sorted(os.listdir(HERE)):
            # `invented_vocabulary` is the declaration itself, and this file's
            # own self-tests deliberately carry a hostile sample value.
            if not name.endswith(".py") or name in (
                "invented_vocabulary.py", os.path.basename(__file__)
            ):
                continue
            yield name, open(os.path.join(HERE, name), encoding="utf-8").read()

    def test_every_fixture_string_is_declared_invented(self):
        undeclared = []
        for name, source in self._fixture_modules():
            for match in self.FIELD_RE.finditer(source):
                field, value = match.group("field"), match.group("val")
                if value in self.DECLARED[field]:
                    continue
                line = source[: match.start()].count("\n") + 1
                undeclared.append(f"{name}:{line} {field}={value!r}")
        self.assertEqual(
            undeclared, [],
            "fixture strings not declared in invented_vocabulary.py — if these "
            "are invented, add them there; if they came from production, "
            "replace them:\n  " + "\n  ".join(undeclared),
        )

    def test_the_vocabulary_has_no_unused_entry(self):
        """A stale entry is a real row someone removed and forgot to delete."""
        used = {"title": set(), "counterparty": set(), "summary": set()}
        for _name, source in self._fixture_modules():
            for match in self.FIELD_RE.finditer(source):
                used[match.group("field")].add(match.group("val"))
        for field, declared in self.DECLARED.items():
            with self.subTest(field=field):
                self.assertEqual(sorted(set(declared) - used[field]), [])

    def test_the_vocabulary_itself_passes_the_pattern_classes(self):
        """The enumerated list is content too, and gets no exemption."""
        for field, declared in self.DECLARED.items():
            for value in declared:
                stripped = value
                for placeholder in DECLARED_PLACEHOLDERS:
                    stripped = stripped.replace(placeholder, "")
                if any(marker in value for marker in EXEMPT_LINE_MARKERS):
                    continue
                for label, pattern, why in CHECKS:
                    if label == "external-user-id" :
                        match = pattern.search(stripped)
                        if match and _is_low_entropy_id(match.group(0)):
                            continue
                    with self.subTest(field=field, value=value, check=label):
                        self.assertIsNone(pattern.search(stripped), why)

    def test_deployed_source_quotes_only_invented_titles(self):
        """`renderer.py` is copied onto the Pi and into both MCP trees.

        The worst instance of the 2026-09-08 leak was not a fixture: it was two
        verbatim production commitment titles in this module's docstring, one
        naming a real person and their real appointment.
        """
        path = os.path.join(REPO_ROOT, "src", "planning", "renderer.py")
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        docstring = source.split('"""', 2)[1]
        quoted = set(re.findall(r"「([^」]+)」", docstring))
        appointment_shaped = {
            value for value in quoted
            if re.search(r"\d{1,2}[:/月時]", value) and len(value) >= 8
        }
        self.assertTrue(appointment_shaped, "the illustrative examples vanished")
        self.assertEqual(
            sorted(appointment_shaped - set(DEPLOYED_SOURCE_EXAMPLES)), [],
            "an undeclared title-shaped example is quoted in deployed source",
        )
        # Both properties the examples exist to illustrate must survive: one
        # example carries a date, one carries a clock time.
        self.assertTrue(any(re.search(r"\d{1,2}/\d{1,2}", v)
                            for v in DEPLOYED_SOURCE_EXAMPLES))
        self.assertTrue(any(re.search(r"\d{1,2}:\d{2}", v)
                            for v in DEPLOYED_SOURCE_EXAMPLES))


class DenylistTests(unittest.TestCase):
    """The known real strings, read at runtime from outside every repository.

    This class holds no value of its own. It reads them from the file named by
    ``HERMES_PRIVACY_DENYLIST`` (default :data:`DEFAULT_DENYLIST_PATH`), which
    is deliberately kept outside any git tree — a denylist committed anywhere
    is the leak it was written to prevent.

    Absent file ⇒ skip with a reason. A fresh clone, a CI runner or a
    contributor without the shared directory must not be blocked, and must not
    be tempted to paste the values in to unblock themselves.
    """

    @classmethod
    def setUpClass(cls):
        cls.terms = _load_denylist()

    def _require(self):
        if self.terms is None:
            self.skipTest(
                "the privacy denylist is not available on this machine; set "
                f"{DENYLIST_ENV} to its path to enable this check "
                f"(default: {DEFAULT_DENYLIST_PATH})"
            )
        self.assertTrue(self.terms, "the denylist file is empty")

    def test_no_denylisted_string_is_in_the_planning_tree(self):
        """The part of the tree this task owns. Other files are their owner's."""
        self._require()
        try:
            files = _tracked_files()
        except (OSError, subprocess.SubprocessError):  # pragma: no cover
            self.skipTest("git is unavailable")
        if files is None:  # pragma: no cover
            self.skipTest("git is unavailable")
        hits = []
        for name in files:
            if not name.startswith(("src/planning/", "tests-planning/",
                                    "src/events/")):
                continue
            try:
                with open(os.path.join(REPO_ROOT, name), encoding="utf-8") as handle:
                    lines = handle.read().split("\n")
            except (OSError, UnicodeDecodeError):
                continue
            for number, line in enumerate(lines, 1):
                for term in self.terms:
                    if term in line:
                        # The term itself is NOT put in the message: a failure
                        # report is written to a log somewhere.
                        hits.append(f"{name}:{number} (denylist entry #{self.terms.index(term)})")
        self.assertEqual(
            hits, [],
            "a known real string is present; look the entry number up in the "
            "denylist file rather than pasting the value anywhere:\n  "
            + "\n  ".join(hits),
        )

    def test_the_invented_vocabulary_does_not_intersect_the_denylist(self):
        """The assertion that makes the allowlist worth trusting.

        A vocabulary built by reading the existing fixtures inherits whatever
        those fixtures were seeded from, and these were seeded from production
        rows — so a real title can end up *declared invented*, after which the
        guard certifies exactly the class of string it exists to catch.
        """
        self._require()
        declared = set(INVENTED_TITLES) | set(INVENTED_COUNTERPARTIES) \
            | set(INVENTED_SUMMARIES) | set(DEPLOYED_SOURCE_EXAMPLES)
        offenders = sorted(
            f"entry #{self.terms.index(term)}"
            for term in self.terms
            if any(term in value for value in declared)
        )
        self.assertEqual(
            offenders, [],
            "the invented vocabulary declares a real string as invented:\n  "
            + "\n  ".join(offenders),
        )

    def test_this_file_contains_no_denylisted_string(self):
        """The guard may not become the thing it guards against."""
        self._require()
        for name in ("test_privacy_guard.py", "invented_vocabulary.py"):
            with self.subTest(file=name):
                with open(os.path.join(HERE, name), encoding="utf-8") as handle:
                    text = handle.read()
                found = [f"#{self.terms.index(t)}" for t in self.terms if t in text]
                self.assertEqual(found, [], f"{name} carries denylist {found}")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
