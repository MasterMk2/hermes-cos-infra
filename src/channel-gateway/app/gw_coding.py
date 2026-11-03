#!/usr/bin/env python3
"""Unified Channel Gateway — the coding handoff route (Phase J.1).

Everything in this module is pure: no I/O, no network, no clock beyond the
`now` that is passed in. That is deliberate, because this is where the gateway
decides whether an inbound chat turn is allowed to start real coding work, and
a decision that can be reproduced from its inputs alone is a decision that can
be tested exhaustively.

Why the decision lives here at all
----------------------------------
The Front's model backend cannot do it. The Antigravity bridge delegates tool
requests to its own read-only agent loop and never returns native function
calls, so no Hermes toolset change can give the assistant a working write path.
And it should not be given one: the assistant's context contains retrieved
Discord, Gmail and GitHub text, so a model-issued `job_create` would let text
that merely *travelled through* a personal source start a coding job.

The gateway is the one component holding unforgeable owner identity
(`gw_canonical.build_envelope` accepts nothing derived from message text). So
the authority decision is made here, deterministically, and the Worker is
reached through a typed broker rather than through a language model.

The rules, in one place
-----------------------
1. Only an authenticated owner, in a direct conversation, is ever considered.
2. Naming a project never starts anything. It produces a *proposal*: the exact
   scope, plus a short confirmation token.
3. An authorization is recognised only when a live, unconsumed proposal exists
   for this conversation AND the reply carries an explicit run-authorization
   phrase AND that phrase sits outside quoted or fenced text AND the message is
   short and direct — unless it carries the proposal token, which is itself the
   evidence that the owner read the scope.
4. A generic affirmation is not authority. 「いいね」 in the middle of an
   unrelated discussion must never dispatch a job, so it is answered by asking
   for the explicit phrase instead.
5. Text imitating envelope fields disqualifies the turn from authorizing. It is
   harmless everywhere else in the gateway, but here it is the signature of an
   injection attempt and there is no cost to refusing it.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from gw_canonical import (
    PLATFORM_DISCORD,
    PLATFORM_LINE,
    CanonicalMessage,
    text_contains_envelope_tokens,
)

# What `classify` can conclude. Anything other than PASS_THROUGH means the
# gateway answers the turn itself instead of sending it to Hermes.
PASS_THROUGH = "pass_through"
REQUEST = "request"
AUTHORIZE = "authorize"
STATUS = "status"
NEEDS_EXPLICIT = "needs_explicit"
UNKNOWN_PROJECT = "unknown_project"

#: A proposal the owner never answered stops being an answerable question.
#: Thirty minutes is long enough for a chat exchange and short enough that a
#: message scrolled past this morning cannot authorize anything this evening.
PROPOSAL_TTL_SECONDS = 1800

#: How many acceptance criteria a proposal spells out before summarising the
#: rest. They are part of what the approval digest binds, so the owner needs to
#: see them, but a chat message is not a place for twenty of them.
MAX_CRITERIA_SHOWN = 8

#: Never echo more of an unrecognised project name than this.
MAX_PROJECT_ECHO_CHARS = 64

_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_QUOTE_LINE_RE = re.compile(r"^\s*(?:>+|>>>)\s?.*$", re.MULTILINE)
# Japanese quotation marks and straight/smart double quotes. Not single quotes:
# stripping those would eat the apostrophe in "don't", which is the one word in
# an English refusal that must survive to be seen by the negation check.
_JP_QUOTE_RE = re.compile(r"[「『][^「」『』]{0,300}[」』]")
_DQUOTE_RE = re.compile(r"[\"“”][^\"“”\n]{0,300}[\"“”]")

# ---------------------------------------------------------------- authority
#
# An authorization is not "a message containing an imperative". Substring
# matching reads 実行してはいけません — *do not run it* — as permission to run,
# and that is not a corner case, it is the ordinary way Japanese negates a
# verb: the negation arrives after the verb stem the matcher already accepted.
#
# So the gate is built the other way round. A clause must match a bounded
# positive grammar *in full*, and any clause carrying negation, a condition, a
# question or quoted/reported speech is vetoed before the grammar is consulted.
# The grammar is an allowlist of complete commands; the vetoes are what make it
# safe for that allowlist to be generous enough to be usable.
#
# The token never overrides a veto. Quoting a confirmation code proves the
# owner read the scope; it does not turn "not yet" into "yes".

#: Harmless lead-ins that may precede a command. Bounded on purpose — anything
#: not listed here ends the match, which is the failure direction we want.
_LEAD = (
    r"(?:じゃあ|じゃ|では|それでは|それじゃ|そしたら|それで|よし|ようし|"
    r"この内容で|その内容で|これで|それで|内容ok|"
    r"一回|1回|いっかい|ひとまず|まずは|まず|とりあえず|ぜひ|是非|"
    # A retry is exactly what an owner says after being told the outcome of
    # their instruction is unknown.
    r"もう一度|もういちど|再度|改めて|あらためて|"
    r"さっそく|早速|いますぐ|今すぐ|すぐに|"
    r"(?:完成|最後|終わり|完了)まで|一気に|そのまま|このまま|全部|"
    r"いいね|いいよ|良いね|ok|okay|了解|承知|はい|うん|"
    r"お願いします|よろしく|よろしくお願いします)"
)

#: What is being started.
_OBJECT = r"(?:実行|実装|開発|着手|コーディング|作業|ジョブ(?:の)?作成|ジョブ)"

#: The action, when it follows an object.
_OBJECT_ACTION = (
    r"(?:し(?:て(?:ください|下さい|くれ|ちょうだい)?|ちゃって|よう|ましょう)|"
    r"開始して|始めて|はじめて|進めて|進めよう|進めましょう|"
    r"作成して|作って|投入して|起動して|"
    r"を?お願いします|を?お願い)"
)

#: Commands that stand on their own, with no object.
_BARE_ACTION = (
    r"(?:開始して|開始しよう|始めて|はじめて|始めよう|着手して|着手しよう|"
    r"進めて|進めよう|進めましょう|進めてください|"
    r"走らせて|流して|走ってみよう|走ってみて|回してみよう|回して|"
    r"やって(?:ください|下さい|みて|みよう)?|やろう|やりましょう|"
    r"いこう|行こう|承認(?:する|します)?|ゴー|GO)"
)

_EN_ACTION = (
    r"(?:go\s*ahead|start\s+(?:the\s+)?(?:job|work|implementation)|"
    r"run\s+it|proceed|ship\s+it|let'?s\s+go|approve(?:d)?)"
)

#: One complete command clause. Anchored at both ends: a clause is either a
#: command or it is not, and "contains a command" is not a category here.
_COMMAND_RE = re.compile(
    r"^(?:" + _LEAD + r"[\s、,]*)*"
    r"(?:"
    + _OBJECT + r"(?:を|に|は|も)?[\s]*" + _OBJECT_ACTION
    + r"|" + _BARE_ACTION
    + r"|" + _EN_ACTION +
    r")"
    # Trailing softeners: 〜しようか, 〜してね, 〜してよ. `か` here is the
    # volitional suggestion, not a question — a real question carries ? or ？,
    # which the question veto catches before this point.
    r"(?:よ|ね|な|か|う|っ)*"
    # The confirmation code, captured rather than merely tolerated: the caller
    # checks it against the proposal on the table. Accepting *any* trailing
    # code let a reply quoting a superseded proposal's code run whatever the
    # current proposal happens to be.
    # Hex only, because that is what a confirmation code is
    # (`store.new_proposal_token` is `secrets.token_hex(3).upper()`). Allowing
    # any alphanumeric let the group swallow part of the command itself:
    # `go ahead` matched the bare `GO` action with `ahead` as its "code".
    r"[\s、,]*(?P<token>[0-9A-Fa-f]{4,8})?"
    r"[\s。．\.!！~〜♪、,]*$",
    re.IGNORECASE,
)

#: Vetoes. Each one is a reason a clause that *looks* like a command is not
#: one, and each is checked against the clause before the grammar.
_NEGATION_RE = re.compile(
    r"しないで|しない|されない|ではない|じゃない|ありません|ません|ないで|"
    r"ないよう|いけません|いけない|だめ|駄目|ダメ|禁止|不要|必要ない|"
    r"やめて|止めて|やめとく|中止|取り消|キャンセル|するな|しなくて|"
    r"べきではない|見送|保留|待って|待とう|まだ|一旦(?:止|やめ)|"
    r"\bdo\s*n[o’']?t\b|\bdon’?'?t\b|\bnever\b|\bno\s+need\b|"
    r"\bstop\b|\bcancel\b|\bhold\s+off\b|\bnot\s+yet\b|\bwait\b|\bwithout\b|"
    r"\bdisapprove\b|\brefuse\b",
    re.IGNORECASE,
)

_CONDITION_RE = re.compile(
    r"たら|だったら|とすれば|すれば|れば|ならば|なら|場合|もし|仮に|"
    r"かどうか|次第|してから|した後|したあと|終わってから|"
    r"あとで|後で|のちほど|後ほど|いずれ|そのうち|今度|将来|"
    r"明日|来週|来月|"
    r"\bif\b|\bwhen\b|\bonce\b|\bafter\b|\bunless\b|\buntil\b|\blater\b|"
    r"\btomorrow\b|\bnext\s+week\b",
    re.IGNORECASE,
)

_QUESTION_RE = re.compile(
    r"[?？]|ですか|でしょうか|ますか|んですか|のかな|かなあ|かしら|"
    r"どういう|どうやって|どうすれば|どうなって|なぜ|なんで|何を|何が|"
    r"いつ|どこ|どれ|教えて",
)

#: Talking *about* an instruction rather than giving one.
_REPORTED_RE = re.compile(
    r"という|といった|とのこと|だそう|と言っ|と書い|って(?:いう|言っ|書い)|"
    r"文言|フレーズ|表現|意味|例えば|たとえば|サンプル|例文|"
    r"\bmeans?\b|\bphrase\b|\bquote[ds]?\b|\bsaid\b|\bexample\b",
    re.IGNORECASE,
)

#: A hint that the owner was talking about starting work at all. Used only to
#: choose between "ask them to be explicit" and "this is not a coding turn".
#: Must be a superset of everything the command grammar accepts, or the route
#: declares support for a phrasing it then drops before the classifier ever
#: sees it. `test_every_accepted_phrase_passes_the_plausibility_gate` pins that.
_START_HINT_RE = re.compile(
    r"実行|実装|開発|着手|ジョブ|コーディング|走らせ|走って|回して|"
    r"進めて|進めよう|進めましょう|開始|始め|はじめ|やって|やろう|やりましょう|"
    r"いこう|行こう|承認|ゴー|GO|"
    r"\brun\b|\bstart\b|\bproceed\b|\bgo\s*ahead\b|\bapprove\b|"
    r"\bship\s+it\b|\blet'?s\s+go\b",
    re.IGNORECASE,
)

#: Bound on how much text the authority gate will examine.
MAX_CLAUSES = 40
MAX_CLAUSE_CHARS = 120

#: Marks where quoted or fenced material was removed, so a clause can never be
#: assembled out of the fragments that surrounded somebody else's words.
_TAINT = "\x00"

#: Discord's triple-angle block quote: everything after it is quoted.
_BLOCK_QUOTE_RE = re.compile(r"^[ \t]*>>>", re.MULTILINE)

_CLAUSE_SPLIT_RE = re.compile(r"[。．\.!！?？\n\r]+")

#: Why an apparent authorization was not accepted.
VETO_NEGATED = "negated_instruction"
VETO_CONDITIONAL = "conditional_instruction"
VETO_QUOTED = "question_or_quotation"

#: A different message already spent this proposal.
ALREADY_CONSUMED = "already_consumed"

#: The reply carried a confirmation code that is not the current proposal's.
TOKEN_MISMATCH = "token_mismatch"

#: Re-sending an authorization whose outcome was never learned.
RETRY_PENDING = "retry_pending_authorization"

# Agreement, not instruction. Recognised only so the gateway can explain what
# it needs instead of silently ignoring the owner.
_AFFIRMATION_RE = re.compile(
    r"いいね|いい[よねで]|良いね|良いで|よさそう|良さそう|了解|りょうかい|承知|"
    r"問題ない|それで|そうだね|うん|はい|\bok(?:ay)?\b|\bsure\b|\bnice\b|"
    r"\bsounds?\s+good\b|\blgtm\b",
    re.IGNORECASE,
)

# Asking to begin coding work. Distinct from authorizing: this is what produces
# a proposal.
_REQUEST_RE = re.compile(
    r"実装(?:を)?(?:始|はじ|開始|依頼|お願い)|開発(?:を)?(?:始|はじ|開始)|"
    r"コーディング(?:ジョブ|を)|ジョブ(?:を)?(?:作成|作って|立ち上げ|投入)|"
    r"ワーカー(?:に|で)|worker(?:\s+に|\s+で)?|"
    r"\bimplement\b|\bstart\s+(?:coding|implementation|development)\b|"
    r"\bcreate\s+a?\s*(?:coding\s+)?job\b",
    re.IGNORECASE,
)

# Deliberately broad. Status is a read: the worst a false positive does is
# answer a question about the current job that the owner did not quite ask,
# while a false negative sends "how is it going?" to a Front that cannot read
# jobs at all — which is the failure this phase exists to fix.
_STATUS_RE = re.compile(
    r"進捗|進行状況|状況|ステータス|どうなって|どこまで|"
    r"終わった|終わりました|完了した|動いてる|走ってる|"
    r"\bstatus\b|\bprogress\b|\bhow(?:'s| is) it going\b",
    re.IGNORECASE,
)

_JOB_ID_RE = re.compile(r"\bjob_[0-9a-f]{10}\b")

# These short continuations need the prior owner's project, not an old model
# response about channel permissions. They never count as an authorization.
_FOLLOWUP_STATUS_RE = re.compile(
    r"(?:で|それで|その後|今|いま|今は|いまは)?[\s、,]*"
    r"(?:どう|どうですか|どうでしょう|行けた(?:感じ)?|いけた(?:感じ)?|"
    r"できた(?:感じ)?|出来た(?:感じ)?|進んだ|終わった)[?？!！。\s]*$")
_FOLLOWUP_RETRY_RE = re.compile(
    r"(?:(?:(?:もう一度|もう一回|もっかい|再度|改めて)[\s、,]*)?"
    r"(?:試して(?:みて)?|やってみて|続けて|つづけて)"
    r"(?:ください|下さい|くれる)?|"
    r"(?:(?:いいね|いいよ)[。!！、,\s]*)?(?:一回)?"
    r"(?:完成|最後)まで走ってみようか)[。?？!！\s]*$")


def is_short_followup(text: str) -> bool:
    raw = _normalize(text).strip()
    return bool(_FOLLOWUP_STATUS_RE.fullmatch(raw) or _FOLLOWUP_RETRY_RE.fullmatch(raw))


def project_from_owner_history(projects, texts):
    """Resolve the latest unambiguous unquoted mention; never authorize it."""
    for text in texts:
        own = _strip_quoted(_normalize(text))
        if text_contains_envelope_tokens(own):
            continue
        matches = [p.key for p in projects if p.matches(own)]
        if matches:
            return matches[0] if len(matches) == 1 else None
        if (_REQUEST_RE.search(own) or _STATUS_RE.search(own)) and _candidate_name(own):
            # An explicit unregistered target supersedes any older selection.
            return None
    return None

# A plausible project or repository name, used only to quote back what the
# owner asked for when nothing matched the registry.
_CANDIDATE_RE = re.compile(r"[A-Za-z][A-Za-z0-9._/-]{2,63}")

# Words that look like identifiers but are never project names. Quoting one of
# these back as "unknown project: implement" would be nonsense.
_CANDIDATE_STOPWORDS = frozenset({
    "implement", "implementation", "start", "coding", "job", "jobs", "worker",
    "please", "status", "progress", "proceed", "approve", "approved", "run",
    "the", "and", "for", "with", "github", "com", "https", "http",
})


def _strip_quoted(text: str) -> str:
    """Everything the owner wrote *themselves*, on this turn.

    Quoted lines, fenced blocks, 「」『』 and double-quoted spans are how a chat
    message carries somebody else's words — including the assistant's own
    previous message, which is exactly the text most likely to contain 実行.

    Removed regions leave a taint marker rather than a space. Replacing a quote
    with whitespace silently rejoins whatever surrounded it, and a sentence
    whose middle has been deleted can read as an order when the whole sentence
    was a refusal: 実行して「はいけません」 — *you must not run it* — becomes
    実行して. A clause carrying a taint marker can never authorize.
    """
    text = str(text or "")
    # Discord's triple-angle quote makes the rest of the message quoted, not
    # just the line it appears on.
    block = _BLOCK_QUOTE_RE.search(text)
    if block:
        text = text[:block.start()] + _TAINT
    stripped = _FENCE_RE.sub(_TAINT, text)
    stripped = _INLINE_CODE_RE.sub(_TAINT, stripped)
    stripped = _QUOTE_LINE_RE.sub(_TAINT, stripped)
    stripped = _JP_QUOTE_RE.sub(_TAINT, stripped)
    stripped = _DQUOTE_RE.sub(_TAINT, stripped)
    # An unclosed fence or quotation mark leaves its opener behind. Treat
    # everything from there on as quoted rather than as the owner's own words.
    for opener in ("```", "`", "「", "『", '"', "“", "”"):
        position = stripped.find(opener)
        if position >= 0:
            stripped = stripped[:position] + _TAINT
    return stripped


def _clauses(text: str) -> list:
    """Split into clauses. Nothing is truncated.

    An earlier version cut each clause to a maximum length, which discarded the
    condition in `run it <padding> if approved` and then accepted what was
    left. Over-length is reported to the caller, which refuses.
    """
    parts = []
    for chunk in _CLAUSE_SPLIT_RE.split(text or ""):
        chunk = chunk.strip(" \t　、,")
        if chunk:
            parts.append(chunk)
        if len(parts) >= MAX_CLAUSES:
            break
    return parts


def authorization_verdict(own_text: str, raw_text: str) -> tuple:
    """`(authorized, reason, token)` for one turn's own words.

    The vetoes apply to the **whole** message, not to individual clauses. A
    message is one act: 実行して。まだ実行しないで。 is a retraction, and a gate
    that accepted the first sentence while ignoring the second would act on an
    instruction the owner withdrew in the same breath. The same reasoning
    covers a condition or a question anywhere in the message.

    Only then are clauses examined, and one must match the command grammar in
    full while carrying no quoted material.
    """
    if text_contains_envelope_tokens(raw_text):
        # Harmless for a reasoning turn, refused on the turn that would write.
        return False, "envelope_imitation", ""

    # Sentence terminators do not survive clause splitting, so the question
    # veto is applied here, to the text as the owner wrote it.
    if _QUESTION_RE.search(own_text) or _REPORTED_RE.search(own_text):
        return False, VETO_QUOTED, ""
    if _NEGATION_RE.search(own_text):
        return False, VETO_NEGATED, ""
    if _CONDITION_RE.search(own_text):
        return False, VETO_CONDITIONAL, ""

    clauses = _clauses(own_text)
    if any(len(clause) > MAX_CLAUSE_CHARS for clause in clauses):
        # Never truncate and then accept: what was cut may be what changed the
        # meaning.
        return False, "authorization_not_direct", ""

    for clause in clauses:
        if _TAINT in clause:
            continue
        match = _COMMAND_RE.match(clause)
        if match:
            return True, "explicit_authorization", match.group("token") or ""
    return False, "authorization_not_direct", ""


def _normalize(text: str) -> str:
    """NFKC, so a full-width ＯＫ or ｊｏｂ_… matches the same patterns.

    Without this the guards are trivially bypassed by an input method that
    happens to produce full-width Latin — and on a Japanese phone keyboard that
    is not an exotic case.
    """
    return unicodedata.normalize("NFKC", str(text or ""))


@dataclass(frozen=True)
class ProjectRef:
    """One registry entry, as the gateway needs to see it.

    The gateway deliberately does not hold the registry itself: repository
    ids, agents, test commands and spec files are the operator's, and they live
    with the broker. All the gateway needs is enough to recognise that the
    owner named a project the operator has already approved.
    """

    key: str
    aliases: tuple = ()

    def matches(self, haystack: str) -> bool:
        for name in (self.key, *self.aliases):
            token = str(name or "").strip()
            if not token:
                continue
            if re.search(rf"(?<![A-Za-z0-9]){re.escape(token)}(?![A-Za-z0-9])",
                         haystack, re.IGNORECASE):
                return True
        return False


@dataclass(frozen=True)
class Proposal:
    """A scope the gateway has shown the owner and is willing to act on.

    `scope_digest` is the whole point. It covers the repository, the agent, the
    test command, the write scope, the acceptance criteria, the declared
    toolchains and the exact bytes of the specification revision. The owner
    approves *that*, and the broker refuses an authorization whose digest no
    longer matches what it would run — so an operator editing a spec or
    widening a write scope between the proposal and the answer produces a
    re-review, never a silent substitution.
    """

    conversation_id: str
    project_key: str
    token: str
    created_at: str
    #: The `gateway_request_id` of the turn that asked for it — the audit trail
    #: back to a specific inbound message.
    origin_request_id: str
    scope_digest: str = ""
    spec_revision: int = 0
    spec_sha256: str = ""
    #: Already used by an authorization. The row survives as status context,
    #: but it can never authorize a second write.
    consumed: bool = False
    #: An authorization that was sent and whose outcome never came back. While
    #: this is set, a retry in this conversation reuses *this* id rather than
    #: minting a new one — one approval stays one job across a lost reply —
    #: and a status question resolves that authorization specifically instead
    #: of guessing from the project's history.
    pending_authorization_id: str = ""

    def expired(self, now: datetime, ttl_seconds: int = PROPOSAL_TTL_SECONDS) -> bool:
        try:
            created = datetime.fromisoformat(self.created_at)
        except (TypeError, ValueError):
            # An unparseable timestamp is treated as expired. Failing closed
            # here costs the owner one extra message; failing open would let a
            # corrupt row authorize indefinitely.
            return True
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return now >= created + timedelta(seconds=ttl_seconds)


@dataclass(frozen=True)
class Decision:
    """What the gateway will do with this turn, and why."""

    action: str
    project_key: str | None = None
    job_id: str | None = None
    #: Set when this turn is about one specific authorization rather than a
    #: project or a job id.
    authorization_id: str | None = None
    #: Only ever a bounded, sanitised echo of what the owner typed.
    named: str | None = None
    #: Short machine-readable rationale, written to the audit log.
    reason: str = ""

    @property
    def handled(self) -> bool:
        """True when the gateway answers instead of calling Hermes."""
        return self.action != PASS_THROUGH


def _candidate_name(text: str) -> str | None:
    best = None
    for match in _CANDIDATE_RE.finditer(text):
        token = match.group(0)
        if token.lower() in _CANDIDATE_STOPWORDS:
            continue
        if best is None or len(token) > len(best):
            best = token
    return best[:MAX_PROJECT_ECHO_CHARS] if best else None


def _named_project(projects, haystack: str):
    for project in projects or ():
        if project.matches(haystack):
            return project
    return None


def is_plausible_coding_turn(message: CanonicalMessage, *,
                             has_context: bool = False) -> bool:
    """Cheap, local test for "could this turn possibly concern coding?".

    Used to keep the broker off the path of ordinary conversation. A turn about
    tomorrow's schedule must not pay a broker HTTP timeout to be recognised as
    a turn about tomorrow's schedule, and a broker that is down must not make
    every unrelated message slow.

    Deliberately generous: a false positive costs one loopback request, while a
    false negative would drop a real coding turn into the read-only Front.
    """
    if not message.authenticated:
        return False
    if message.platform not in (PLATFORM_DISCORD, PLATFORM_LINE):
        return False
    raw = _normalize(message.text)
    if is_short_followup(raw):
        return True
    if has_context:
        # With a proposal on the table, agreement and status questions are both
        # in play, so the bar is lower.
        return bool(_START_HINT_RE.search(raw) or _STATUS_RE.search(raw)
                    or _AFFIRMATION_RE.search(raw) or _JOB_ID_RE.search(raw))
    return bool(_REQUEST_RE.search(raw) or _STATUS_RE.search(raw)
                or _JOB_ID_RE.search(raw))


def classify(message: CanonicalMessage, *, enabled: bool, projects=(),
             proposal: Proposal | None = None,
             context_project_key: str | None = None,
             now: datetime | None = None,
             ttl_seconds: int = PROPOSAL_TTL_SECONDS) -> Decision:
    """Decide what this inbound turn is, for the coding route only.

    Returns PASS_THROUGH for everything that is not a coding handoff turn —
    which is the overwhelming majority, and which must keep reaching Hermes
    exactly as it does today. Ordinary planning, GitHub questions, health
    checks and the deployed premium reasoning route are all unaffected: none of
    them match the patterns here, and the route is off entirely unless an
    operator enabled it.

    `proposal` is the durable record of what this conversation last selected.
    Two different things are read from it, and conflating them was a real bug:

      the LIVE proposal  — unexpired and unconsumed. Only this can authorize a
                           write.
      the CONTEXT        — the same row at any age. This is what makes 「進捗」
                           answerable an hour into a job, which is precisely
                           when the owner asks. An approval that has aged out
                           must stop authorizing new work; it must not take the
                           running job's status back to a Front that cannot
                           read jobs at all.
    """
    if not enabled:
        return Decision(PASS_THROUGH, reason="route_disabled")
    # Rule 1. Not the owner, or not a direct conversation: the route does not
    # exist for this turn. Note that this is checked against the envelope's
    # subject, not against anything in the text.
    if not message.authenticated:
        return Decision(PASS_THROUGH, reason="not_owner")
    if message.platform not in (PLATFORM_DISCORD, PLATFORM_LINE):
        return Decision(PASS_THROUGH, reason="unsupported_platform")

    raw = _normalize(message.text)
    own = _strip_quoted(raw)
    now = now or datetime.now(timezone.utc)

    context = proposal
    # Unexpired: the conversation is still about this scope, so agreement,
    # refusal and questions about it are all answerable here. Whether it may
    # still *authorize* is a second question, asked at the AUTHORIZE branch.
    answerable = (proposal if (proposal
                               and (proposal.pending_authorization_id
                                    or not proposal.expired(now, ttl_seconds)))
                  else None)

    # A job id the owner quoted back is the least ambiguous thing in this
    # module, so a status question about one is answered first.
    job_match = _JOB_ID_RE.search(raw)
    if job_match and _STATUS_RE.search(raw):
        return Decision(STATUS, job_id=job_match.group(0), reason="status_by_job_id")

    named = _named_project(projects, raw)
    # A context project is deliberately separate from `proposal`: knowing
    # what the owner is discussing never grants permission to execute it.
    context_key = context.project_key if context else context_project_key
    if (context is None and context_project_key and named is None
            and (_REQUEST_RE.search(own) or _STATUS_RE.search(own))
            and _candidate_name(own)):
        return Decision(UNKNOWN_PROJECT, named=_candidate_name(own),
                        reason="explicit_project_not_registered")

    # Status before authority, always. 「進捗の確認をお願いします」 is a question
    # about work, and a gate that reached the authorization grammar first read
    # its trailing お願いします as an instruction to start.
    if _STATUS_RE.search(raw) or _FOLLOWUP_STATUS_RE.fullmatch(raw.strip()):
        if context is not None and context.pending_authorization_id:
            # An authorization is outstanding. Answering with "the project's
            # most recent job" could show an unrelated older one as though it
            # were the outcome of this approval, so the specific authorization
            # is resolved instead.
            return Decision(STATUS, project_key=context.project_key,
                            authorization_id=context.pending_authorization_id,
                            reason="status_for_pending_authorization")
        if named is not None:
            return Decision(STATUS, project_key=named.key,
                            reason="status_by_project")
        if context is not None:
            return Decision(STATUS, project_key=context.project_key,
                            reason="status_from_context")
        if context_key:
            return Decision(STATUS, project_key=context_key,
                            reason="status_from_owner_history")

    if (_FOLLOWUP_RETRY_RE.fullmatch(own.strip())
            and (answerable is None or not authorization_verdict(own, raw)[0])):
        if context and context.pending_authorization_id:
            return Decision(NEEDS_EXPLICIT, project_key=context.project_key,
                            reason="pending_authorization_unresolved")
        if context_key:
            return Decision(REQUEST, project_key=context_key,
                            reason="fresh_proposal_for_followup")

    if answerable is not None:
        # An authorization must be about the proposal on the table. If the
        # owner names a *different* registered project, they have moved on, and
        # treating that as approval of the old scope would dispatch work they
        # did not ask for.
        if named is not None and named.key != answerable.project_key:
            if answerable.pending_authorization_id:
                return Decision(NEEDS_EXPLICIT, project_key=answerable.project_key,
                                reason="pending_authorization_unresolved")
            return Decision(REQUEST, project_key=named.key,
                            reason="superseded_by_other_project")

        authorized, reason, token_seen = authorization_verdict(own, raw)
        if authorized:
            if token_seen and token_seen.upper() != (answerable.token or "").upper():
                # A code that is not this proposal's. Most likely a reply to a
                # scope that has since been superseded — acting on it would run
                # the current proposal under approval given for a different one.
                return Decision(NEEDS_EXPLICIT,
                                project_key=answerable.project_key,
                                reason=TOKEN_MISMATCH)
            if answerable.pending_authorization_id:
                # The previous attempt's outcome is unknown. Retrying is the
                # right thing to do, under the original id: the broker's ledger
                # collapses it onto the same job if one was created.
                return Decision(AUTHORIZE, project_key=answerable.project_key,
                                reason=RETRY_PENDING)
            if answerable.consumed:
                # A different message already used this proposal. Answering
                # again would be a second job for one approval.
                return Decision(NEEDS_EXPLICIT,
                                project_key=answerable.project_key,
                                reason=ALREADY_CONSUMED)
            return Decision(AUTHORIZE, project_key=answerable.project_key,
                            reason=reason)
        if reason in (VETO_NEGATED, VETO_CONDITIONAL, VETO_QUOTED,
                      "envelope_imitation"):
            return Decision(NEEDS_EXPLICIT, project_key=answerable.project_key,
                            reason=reason)

        token_present = bool(answerable.token) and re.search(
            rf"(?<![A-Za-z0-9]){re.escape(answerable.token)}(?![A-Za-z0-9])",
            own, re.IGNORECASE)
        if token_present:
            # The token alone, with no command, is a reference not an order.
            return Decision(NEEDS_EXPLICIT, project_key=answerable.project_key,
                            reason="token_without_instruction")
        if _AFFIRMATION_RE.search(own):
            # Rule 4. Agreement, answered rather than acted on.
            return Decision(NEEDS_EXPLICIT, project_key=answerable.project_key,
                            reason="generic_affirmation")
        if _START_HINT_RE.search(own):
            return Decision(NEEDS_EXPLICIT, project_key=answerable.project_key,
                            reason="authorization_not_direct")

    if _REQUEST_RE.search(raw):
        if named is not None:
            return Decision(REQUEST, project_key=named.key, reason="named_project")
        return Decision(UNKNOWN_PROJECT, named=_candidate_name(own),
                        reason="project_not_registered")

    return Decision(PASS_THROUGH, reason="not_a_coding_turn")


# --------------------------------------------------------------- rendering
#
# Every reply the route produces is written here, in Japanese, and every one of
# them is derived from a structure the broker returned. There is no template
# that can say "開始しました" without a job id in hand — see `render_authorized`,
# which refuses to claim a start it cannot evidence.

#: Supervisor states, in the words the owner should read. Anything not listed
#: renders through `_UNKNOWN_STATE` with its raw state name, so a state added
#: later shows up honestly instead of being silently mapped to something wrong.
_STATE_JA = {
    "NEW": "作成済み（未着手）",
    "QUEUED": "順番待ち",
    "DISPATCHING": "起動中",
    "RUNNING": "実行中",
    "AGENT_STOPPED": "エージェント停止（再開待ち）",
    "VALIDATING": "受け入れ検証中",
    "CONTINUE_REQUIRED": "継続の承認待ち",
    "WAITING_REVIEW": "独立レビュー待ち",
    "REVIEWING": "独立レビュー中",
    "CHANGES_REQUESTED": "レビュー指摘の修正中",
    "IMPLEMENTATION_ACCEPTED": "実装は受理、レビュー待ち",
    "WAITING_HUMAN": "人手の判断待ち",
    "WAITING_WORKER": "ワーカー待ち",
    "INTERRUPTED": "中断（ワーカー到達不能）",
    "FAILED_TO_START": "起動失敗（再試行可能）",
    "FAILED": "失敗",
    "CANCELLED": "キャンセル済み",
    "COMPLETED": "完了",
}

_UNKNOWN_STATE = "不明な状態"

#: States that prove the supervisor actually took the job. Only these let the
#: route tell the owner that work has started.
DISPATCH_EVIDENCE_STATES = frozenset({"DISPATCHING", "RUNNING", "VALIDATING"})

#: States that are real, durable and not a start — the job exists and is
#: parked. Reported as such, never as "started".
PARKED_STATES = frozenset({
    "NEW", "QUEUED", "WAITING_HUMAN", "WAITING_WORKER", "FAILED_TO_START",
})


def state_label(state) -> str:
    return _STATE_JA.get(str(state or ""), f"{_UNKNOWN_STATE}({state})")


#: Broker readiness codes, in the words the owner should read. A code with no
#: entry falls through to the broker's own detail text, so a reason added on
#: the broker side is never silently dropped from the reply.
_MISSING_JA = {
    "project_disabled": "このプロジェクトは登録簿で有効化されていません",
    "spec_file_missing": "承認できる仕様書のリビジョンがありません",
    "spec_revision_unreadable": "仕様書のリビジョンを読み取れません",
    "repository_not_in_supervisor_allowlist":
        "スーパーバイザのリポジトリ許可リストに未登録です",
    "repository_not_in_bridge_allowlist":
        "ワーカーブリッジのリポジトリ許可リストに未登録です",
    "tests_command_not_allowlisted": "テストコマンドが許可されていません",
    "allowed_paths_not_registered": "変更を許可する範囲が登録内容と一致しません",
    "repository_path_missing": "ワーカー側のチェックアウトがありません",
    "repository_has_no_commits":
        "リポジトリにコミットがありません（最初のコミットが必要です）",
    "worker_offline": "コーディングワーカーに接続できません",
    "worker_busy": "ワーカーが別のジョブを実行中です（同時実行は1件）",
    "agent_not_configured_on_bridge":
        "指定のエージェントがワーカーブリッジに設定されていません",
    "worker_toolchain_missing":
        "ワーカーイメージに必要なツールチェーンがありません（プロビジョニングが必要）",
    "dispatch_disabled": "ハンドオフの実行がまだ有効化されていません",
}


def _missing_line(item) -> str:
    if isinstance(item, dict):
        code = str(item.get("code") or "")
        label = _MISSING_JA.get(code)
        detail = str(item.get("detail") or "")
        if label:
            return f"{label}（{code}）" if not detail else f"{label}: {detail}"
        return detail or code or "理由不明"
    return str(item)


def render_scope(scope: dict, token: str, spec: dict | None = None) -> str:
    """The proposal message: exactly what would run, and how to approve it.

    The specification revision and its hash are shown because they are what the
    owner is actually approving. A reply that said only "FastRawPhotoView を
    実装します" would be an approval of a name.
    """
    criteria = scope.get("acceptance_criteria") or []
    paths = scope.get("allowed_paths") or []
    tools = scope.get("required_toolchains") or []
    spec = spec or {}
    lines = [
        f"コーディングジョブの実行内容を確認してください（プロジェクト: {scope.get('project_key')}）。",
        "",
        f"仕様書: {spec.get('label') or '（未取得）'}"
        + (f" — {spec['title']}" if spec.get("title") else ""),
    ]
    if spec.get("summary"):
        lines.append(f"概要: {spec['summary']}")
    lines += [
        "",
        f"リポジトリ: {scope.get('repository')}",
        f"担当エージェント: {scope.get('agent')}",
        f"テストコマンド: {scope.get('tests_command') or '（登録なし）'}",
        f"変更を許可する範囲: {', '.join(paths) if paths else '（登録なし）'}",
        f"必要なツールチェーン: {', '.join(tools) if tools else '（宣言なし）'}",
        f"最大試行回数: {scope.get('max_attempts', '（未設定）')}",
        f"受け入れ条件: {len(criteria)}件",
    ]
    # The criteria are part of what the digest binds, so the owner has to be
    # able to see them. A count alone makes a swapped condition invisible.
    for criterion in criteria[:MAX_CRITERIA_SHOWN]:
        if isinstance(criterion, dict):
            kind = (criterion.get("verification") or {}).get("kind", "?")
            lines.append(f"　- {criterion.get('id')}: "
                         f"{criterion.get('description')} [{kind}]")
    if len(criteria) > MAX_CRITERIA_SHOWN:
        lines.append(f"　- ほか {len(criteria) - MAX_CRITERIA_SHOWN} 件")
    lines += [
        "",
        "この内容で実行してよければ、次のように明示的に指示してください。",
        f"　　実行して {token}",
        "",
        "「いいね」などの同意だけでは実行しません。承認後に仕様や設定が変わっていた場合は、"
        "実行せずに再確認をお願いします。実行後は独立レビューを経て完了します。",
    ]
    return "\n".join(lines)


def render_scope_changed(project_key: str) -> str:
    """The approved scope no longer describes what would run."""
    return (f"{project_key} の仕様または実行設定が、確認をお願いした時点から変わっています。\n"
            "内容が変わったまま実行はしません。ジョブは作成していません。\n\n"
            "もう一度実行を依頼していただければ、最新の内容で確認をお願いします。")


def render_needs_explicit(project_key: str, token: str, reason: str) -> str:
    head = {
        "generic_affirmation":
            "同意はいただきましたが、コーディングジョブの実行は明示的な指示がないと開始しません。",
        "authorization_not_direct":
            "実行の指示として受け取れませんでした。取り違えを避けるため、短く明示的に指示してください。",
        "token_without_instruction":
            "確認コードだけでは実行しません。実行の指示を添えてください。",
        "envelope_imitation":
            "メッセージに制御用の書式が含まれていたため、この指示では実行しません。",
        VETO_NEGATED:
            "否定・中止の表現が含まれていたため、実行しません。",
        VETO_CONDITIONAL:
            "条件つき・後回しの表現が含まれていたため、いまは実行しません。",
        VETO_QUOTED:
            "質問または引用として書かれていたため、実行の指示とは扱いません。",
        TOKEN_MISMATCH:
            "確認コードが、いまお見せしている内容のものと一致しません。"
            "取り違えを避けるため実行しません。",
    }.get(reason, "この指示ではコーディングジョブを開始しません。")
    return (f"{head}\n\n"
            f"対象プロジェクト: {project_key}\n"
            f"実行する場合は次のように送ってください。\n"
            f"　　実行して {token}")


def render_already_consumed() -> str:
    return ("この確認はすでに処理済みです。"
            "最新の状況は「進捗」と聞いてください。")


def render_in_progress(job_id) -> str:
    """The same authorization is already being processed."""
    tail = f"\nジョブID: {job_id}" if job_id else ""
    return ("この指示はいま処理中です。二重にジョブを作らないため、"
            "完了までお待ちください。" + tail)


def render_conflict(project_key: str) -> str:
    """The same authorization id arrived carrying a different request."""
    return (f"{project_key} の実行指示が、以前に受け付けた内容と一致しません。"
            "取り違えを避けるため実行しませんでした。ジョブは作成していません。\n\n"
            "もう一度実行を依頼して、内容の確認からやり直してください。")


def render_unknown_project(named, requirement: dict | None = None) -> str:
    """What the owner must do before a project can be started at all.

    Precision matters here: "登録されていません" alone sends the owner back to
    the CLI with nothing, which is the failure this whole phase exists to fix.
    """
    who = f"「{named}」" if named else "指定されたプロジェクト"
    lines = [
        f"{who} はコーディングワーカーに登録されていないため、実行できません。",
        "",
        "登録には次のすべてが必要です（オペレーター作業）。",
        "　1. ワーカーブリッジのリポジトリ登録（bridge の config.json）",
        "　2. スーパーバイザのリポジトリ登録（jobs/worker_config.json）",
        "　3. 許可するテストコマンドと変更可能パスの指定",
        "　4. 実行対象となるクリーンなベースコミット",
        "　5. ハンドオフ登録簿（projects.json）のプロジェクト定義と仕様書",
    ]
    for item in (requirement or {}).get("missing") or []:
        lines.append(f"　- 未充足: {_missing_line(item)}")
    lines.append("")
    lines.append("登録が済んでいるプロジェクト名を指定するか、登録を依頼してください。")
    return "\n".join(lines)


def render_not_ready(project_key: str, missing) -> str:
    """A registered project that still cannot run, and precisely why.

    Precision is the deliverable. "実行できません" alone is the answer the owner
    already got once, and it is why this route exists.
    """
    lines = [
        f"{project_key} は登録済みですが、いまは実行できません。",
        "",
        "不足している条件:",
    ]
    for item in missing or ["理由を取得できませんでした"]:
        lines.append(f"　- {_missing_line(item)}")
    lines.append("")
    lines.append("解消後にもう一度指示してください。ジョブは作成していません。")
    return "\n".join(lines)


def render_authorized(result: dict) -> str:
    """The reply to an approved authorization.

    This function is the reason the phase exists, so it is the one place that
    must not lie. It cannot say 開始しました without both a durable job id and a
    state that proves the supervisor claimed the job. A created-but-not-running
    job is reported as created-and-parked, with its reason.
    """
    job_id = str(result.get("job_id") or "")
    state = str(result.get("status") or "")
    if not job_id:
        return ("実行を開始できませんでした。ジョブIDが発行されていません。\n"
                f"理由: {result.get('error') or '不明'}")
    replayed = bool(result.get("replayed"))
    head = "すでに受け付け済みの指示です。" if replayed else "コーディングジョブを登録しました。"
    lines = [head, "", f"ジョブID: {job_id}", f"状態: {state_label(state)}"]
    if state in DISPATCH_EVIDENCE_STATES:
        lines.append("ワーカーへの起動を確認しました。")
    elif state in PARKED_STATES:
        lines.append("まだ実行は始まっていません。理由: "
                     + str(result.get("detail") or result.get("failure_reason")
                           or "起動待ち"))
    else:
        lines.append("状態の詳細: " + str(result.get("detail") or "—"))
    lines.append("")
    lines.append(f"進捗は「{job_id} の進捗」と聞いてください。")
    return "\n".join(lines)


def render_status(status: dict) -> str:
    job_id = str(status.get("job_id") or "")
    if not job_id:
        return ("対象のジョブが見つかりませんでした。ジョブIDを添えて聞いてください。")
    state = str(status.get("status") or "")
    lines = [f"ジョブID: {job_id}", f"状態: {state_label(state)}"]
    if status.get("project_key"):
        lines.insert(0, f"プロジェクト: {status['project_key']}")
    attempt = status.get("attempt")
    max_attempts = status.get("max_attempts")
    if attempt is not None and max_attempts is not None:
        lines.append(f"試行: {attempt}/{max_attempts}")
    passed = status.get("acceptance_pass")
    total = status.get("acceptance_total")
    if passed is not None and total is not None:
        lines.append(f"受け入れ条件: {passed}/{total} 達成")
    if status.get("review_status"):
        lines.append(f"レビュー: {status['review_status']}")
    if status.get("tests_status"):
        lines.append(f"テスト: {status['tests_status']}")
    if state in ("WAITING_HUMAN", "CONTINUE_REQUIRED"):
        lines.append("必要な対応: "
                     + str(status.get("next_action") or "オペレーターの判断が必要です"))
    if state in ("FAILED", "FAILED_TO_START", "INTERRUPTED"):
        lines.append("失敗理由: " + str(status.get("failure_reason") or "不明"))
    if state == "COMPLETED":
        lines.append("独立レビューを通過して完了しています。")
    return "\n".join(lines)


def render_unavailable(detail: str = "") -> str:
    """Connectivity says nothing about whether an earlier job was created."""
    tail = f"\n詳細: {detail}" if detail else ""
    return ("コーディングワーカーへの受付窓口にいま接続できません。"
            "処理状況を確認できません。時間をおいて試してください。" + tail)


def render_no_such_authorization(project_key: str) -> str:
    """No record was visible at lookup time; retain the original retry key."""
    return (f"{project_key} の実行指示は、受付側でまだ確認できません。\n"
            "「実行して」と指示すると、同じ承認IDで再試行します。")


def render_authorization_uncertain(project_key: str, job_id) -> str:
    """The authorization is recorded but its job has not appeared yet."""
    tail = f"\n予定のジョブID: {job_id}" if job_id else ""
    return (f"{project_key} の実行指示は受付側に記録されていますが、"
            "ジョブの作成がまだ確認できていません。" + tail +
            "\n少し待ってからもう一度「進捗」と聞いてください。")


def render_outcome_unknown(project_key: str) -> str:
    """The authorization was sent and the answer never came back.

    This must not say ジョブは作成していません. The broker may have created and
    dispatched the job and then lost the reply, and telling the owner nothing
    happened would be a false statement about production state — the precise
    kind of claim this route exists to stop making. Say what is actually known,
    and point at a readback that genuinely resolves it: the authorization id is
    now held durably, so both the status question and a retry are about *this*
    authorization rather than about the project's history.
    """
    return (f"{project_key} の実行指示は送信しましたが、結果を確認できませんでした。\n"
            "ジョブが作成されたかどうかは、この時点では断定できません。\n\n"
            "この指示は控えてあります。「進捗」と聞けばこの指示の結果を確認し、"
            "もう一度実行を指示すれば同じ指示として送り直します"
            "（二重にジョブは作りません）。")
