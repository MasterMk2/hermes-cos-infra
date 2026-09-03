#!/usr/bin/env python3
"""Unified Channel Gateway — outbound shaping.

Everything that happens to an answer between Hermes producing it and a
platform accepting it: length limits, code-fence-safe chunking, mention
neutralisation, oversized-result summarising, and redaction of anything
secret-shaped that leaked into an error string.

All pure. The platform limits are parameters, not constants baked into the
logic, because LINE and Discord differ and both change.

Redaction reuses the repository's canonical secret patterns rather than
carrying a fourth copy of them. It adds two gateway-specific shapes locally
instead of editing the canonical list, because that list is deployed beside
three coding-subsystem consumers whose visible behaviour a test pins.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# security_patterns.py is deployed next to this app directory, and lives at
# src/ in the repository. Both layouts are searched, because an earlier
# version searched only the first and silently fell back to an EMPTY pattern
# list everywhere else — redaction reported success while removing nothing.
#
# A missing pattern list is now fatal. This is a security control: it fails
# closed and loudly, or a leaked key reaches a chat window with no trace that
# anything was skipped.
_HERE = Path(__file__).resolve().parent
for _candidate in (_HERE, _HERE.parent.parent):
    if (_candidate / "security_patterns.py").is_file():
        sys.path.insert(0, str(_candidate))
        break
try:
    from security_patterns import SECRET_PATTERNS
except ImportError as exc:  # pragma: no cover - deployment error
    raise RuntimeError(
        "security_patterns.py not found next to the gateway app or at src/; "
        "outbound redaction cannot run without the canonical pattern list"
    ) from exc

REDACTED = "[redacted]"

# Shapes the canonical list does not cover, because the coding subsystem never
# handles them: a Discord bot token, and a LINE channel access token.
GATEWAY_EXTRA_PATTERNS = [
    ("discord_bot_token", re.compile(r"\b[A-Za-z0-9_-]{20,30}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{25,}\b")),
    ("line_channel_token", re.compile(r"\b[A-Za-z0-9+/]{100,}={0,2}\b")),
]


def redact(text) -> str:
    """Replace anything secret-shaped with a marker.

    Applied to every error string that can reach a user or a log. It is a
    last line of defence, not a licence to put secrets in messages: the
    gateway's own error paths carry categories, not payloads.
    """
    if not text:
        return ""
    out = str(text)
    for _pid, rx in list(SECRET_PATTERNS) + GATEWAY_EXTRA_PATTERNS:
        out = rx.sub(REDACTED, out)
    return out


def redaction_hits(text) -> set:
    """Pattern ids that matched. Ids only — never the matched value."""
    hits = set()
    if not text:
        return hits
    s = str(text)
    for pid, rx in list(SECRET_PATTERNS) + GATEWAY_EXTRA_PATTERNS:
        if rx.search(s):
            hits.add(pid)
    return hits


# ------------------------------------------------------------- chunking

_FENCE_RE = re.compile(r"^\s*```")


def _fence_state(lines) -> tuple:
    """(open?, info_string) after the given lines."""
    open_fence, info = False, ""
    for line in lines:
        if _FENCE_RE.match(line):
            if open_fence:
                open_fence, info = False, ""
            else:
                open_fence = True
                info = line.strip()[3:].strip()
    return open_fence, info


def chunk_text(text, limit: int, max_chunks: int | None = None) -> list:
    """Split into pieces no longer than `limit`, preferring line boundaries.

    A code fence left open at the end of a chunk is closed, and reopened with
    the same info string at the start of the next — otherwise the first chunk
    renders as an unterminated block and the second as prose (spec section 77).

    When `max_chunks` is set and the text does not fit, the last chunk ends
    with a truncation notice rather than the output being silently cut.
    """
    text = "" if text is None else str(text)
    if limit <= 0:
        return []
    # No text is no chunks, not one empty chunk. The callers treat an empty
    # list as "nothing to deliver"; a [""] would be truthy, and the gateway
    # would try to send a blank message that the platform rejects with 400.
    if not text:
        return []

    lines = text.split("\n")
    chunks, current = [], []

    def flush():
        if not current:
            return
        open_fence, info = _fence_state(current)
        body = "\n".join(current)
        if open_fence:
            body += "\n```"
        chunks.append(body)
        current.clear()
        if open_fence:
            current.append(f"```{info}" if info else "```")

    for line in lines:
        # A single line longer than the limit is hard-split; nothing else can
        # be done without dropping content.
        while len(line) > limit:
            head, line = line[: limit - 1], line[limit - 1 :]
            if current:
                flush()
            chunks.append(head)
        candidate = current + [line]
        if len("\n".join(candidate)) > limit and current:
            flush()
            current.append(line)
        else:
            current.append(line)
    flush()

    # Drop chunks that ended up holding nothing but a reopened fence.
    chunks = [c for c in chunks if c.strip() and c.strip() != "```"]
    if not chunks:
        return []

    if max_chunks is not None and len(chunks) > max_chunks:
        kept = chunks[:max_chunks]
        notice = "…(以降は長いため省略しました)"
        tail = kept[-1]
        if len(tail) + len(notice) + 1 > limit:
            tail = tail[: max(0, limit - len(notice) - 1)]
        kept[-1] = f"{tail}\n{notice}" if tail else notice
        return kept
    return chunks


def summarize_if_oversized(text, *, soft_limit: int, hard_limit: int) -> tuple:
    """(text, was_truncated) for answers too large to send at all.

    Sending tens of thousands of characters to a phone is not a useful answer
    (spec section 78). Past `hard_limit` the gateway keeps the head, says it
    truncated, and leaves the full result where it already lives — inside
    Hermes, retrievable by asking again more narrowly.
    """
    text = "" if text is None else str(text)
    if len(text) <= hard_limit:
        return text, False
    head = text[:soft_limit].rstrip()
    return (
        head
        + "\n\n…結果が長いため、ここまでを送りました"
        + f"(全体は約{len(text)}文字)。範囲を絞って聞き直せます。",
        True,
    )


# ------------------------------------------------------- mention safety

_MENTION_RE = re.compile(r"@(everyone|here)\b")
_ID_MENTION_RE = re.compile(r"<@[!&]?\d{15,25}>")


def neutralize_mentions(text) -> str:
    """Make a reply incapable of pinging anyone, whatever it contains.

    Belt and braces with `allowed_mentions` on the send: the library flag
    stops the ping, and this stops the reply from *looking* like it pinged
    (spec section 51).
    """
    if not text:
        return ""
    out = _MENTION_RE.sub(lambda m: "@​" + m.group(1), str(text))
    out = _ID_MENTION_RE.sub(lambda m: m.group(0).replace("<@", "<@​"), out)
    return out


def prepare_outbound(
    text,
    *,
    limit: int,
    max_chunks: int | None,
    neutralize: bool,
    soft_limit: int | None = None,
    hard_limit: int | None = None,
) -> list:
    """The whole outbound pipeline: summarise, neutralise, redact, chunk."""
    body = "" if text is None else str(text)
    if hard_limit:
        body, _ = summarize_if_oversized(
            body, soft_limit=soft_limit or hard_limit // 2, hard_limit=hard_limit
        )
    if neutralize:
        body = neutralize_mentions(body)
    body = redact(body)
    return chunk_text(body, limit, max_chunks)


# --------------------------------------------------------- user errors

USER_FACING_ERROR = "処理中に内部エラーが発生しました。もう一度お試しください。"
USER_FACING_BUSY = "前のメッセージをまだ処理しています。少し待ってからもう一度どうぞ。"
USER_FACING_TIMEOUT = (
    "応答に時間がかかりすぎたため中断しました。"
    "内容を分けて聞き直すと通ることがあります。"
)
USER_FACING_UNAVAILABLE = "いま応答できません。復旧後にもう一度お試しください。"


def user_facing_error(category: str = "internal") -> str:
    """What a user sees. Never a traceback, never a path, never a value.

    The detail lives in the gateway's own database under a correlation id
    (spec sections 82, 83); the user gets a sentence and the ability to retry.
    """
    return {
        "busy": USER_FACING_BUSY,
        "timeout": USER_FACING_TIMEOUT,
        "unavailable": USER_FACING_UNAVAILABLE,
    }.get(category, USER_FACING_ERROR)
