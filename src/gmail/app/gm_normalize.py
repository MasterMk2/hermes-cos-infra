#!/usr/bin/env python3
"""Safe Gmail MIME normalisation.

The parser treats every header, body, attachment name, and URL-shaped string
as untrusted data.  It never renders HTML, fetches remote content, opens a
link, or decodes an attachment into the canonical store.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import re
from datetime import datetime, timezone
from email.message import Message
from email.parser import HeaderParser
from html.parser import HTMLParser


MAX_BODY_CHARS = 200_000
MAX_HEADER_CHARS = 4_000
MAX_ATTACHMENT_COUNT = 100
MAX_ATTACHMENT_NAME_CHARS = 300


class _TextExtractor(HTMLParser):
    BREAK_TAGS = {"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, value):
        self.parts.append(value)

    def handle_starttag(self, tag, attrs):
        if tag.casefold() in self.BREAK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag.casefold() in self.BREAK_TAGS:
            self.parts.append("\n")

    def text(self) -> str:
        return "".join(self.parts)


def source_id_for(message_id: str) -> str:
    return f"gmail:{message_id}"


def _headers(payload: dict) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in payload.get("headers") or []:
        name = str(item.get("name") or "").casefold()
        if name and name not in result:
            result[name] = str(item.get("value") or "")[:MAX_HEADER_CHARS]
    return result


def _charset(headers: dict[str, str]) -> str | None:
    content_type = headers.get("content-type")
    if not content_type:
        return None
    try:
        parsed: Message = HeaderParser().parsestr(f"Content-Type: {content_type}\n\n")
        value = parsed.get_content_charset()
        return str(value) if value else None
    except Exception:
        return None


def _decode_data(value, charset: str | None) -> str:
    if not value:
        return ""
    try:
        raw = base64.urlsafe_b64decode(str(value) + "=" * (-len(str(value)) % 4))
    except (ValueError, TypeError):
        return ""
    encodings = [charset, "utf-8", "iso-2022-jp", "cp932", "latin-1"]
    for encoding in encodings:
        if not encoding:
            continue
        try:
            return raw.decode(encoding, "replace")
        except (LookupError, UnicodeError):
            continue
    return ""


def _plain_text(value: str) -> str:
    value = html.unescape(str(value or ""))
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", value)
    value = re.sub(r"\n{3,}", "\n\n", value).strip()
    return value[:MAX_BODY_CHARS]


def html_to_text(value: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(value)
        parser.close()
        return _plain_text(parser.text())
    except Exception:
        return _plain_text(re.sub(r"<[^>]*>", " ", value))


def strip_quoted_history(value: str) -> str:
    """Keep current prose for extraction without hiding full search evidence."""
    lines = str(value or "").splitlines()
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(">"):
            continue
        if re.match(r"^On .+wrote:$", stripped, re.IGNORECASE):
            break
        if re.match(r"^-{2,}\s*(Original Message|Forwarded message)", stripped, re.IGNORECASE):
            break
        if re.match(r"^[-─]{3,}\s*(元のメッセージ|転送メッセージ)", stripped):
            break
        out.append(line)
    return _plain_text("\n".join(out))


def _walk_part(part: dict, inherited_headers: dict[str, str] | None,
               plain: list[str], rich: list[str], attachments: list[dict], mime: list[str]) -> None:
    headers = _headers(part) or dict(inherited_headers or {})
    mime_type = str(part.get("mimeType") or headers.get("content-type") or "").split(";", 1)[0].casefold()
    if mime_type:
        mime.append(mime_type[:120])
    filename = str(part.get("filename") or "")
    body = part.get("body") or {}
    if filename or body.get("attachmentId"):
        if len(attachments) < MAX_ATTACHMENT_COUNT:
            attachments.append({
                "filename": filename[:MAX_ATTACHMENT_NAME_CHARS],
                "mime_type": mime_type,
                "size": int(body.get("size") or 0),
                "attachment_present": bool(body.get("attachmentId")),
            })
    data = body.get("data")
    if data and not filename:
        text = _decode_data(data, _charset(headers))
        if mime_type == "text/plain":
            plain.append(_plain_text(text))
        elif mime_type == "text/html":
            rich.append(html_to_text(text))
    for child in part.get("parts") or []:
        if isinstance(child, dict):
            _walk_part(child, headers, plain, rich, attachments, mime)


def _iso_from_ms(value) -> str | None:
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def normalize_message(payload: dict, *, ingested_at: str) -> dict:
    message_id = str(payload.get("id") or "")
    thread_id = str(payload.get("threadId") or "")
    if not message_id or not thread_id:
        raise ValueError("Gmail message is missing id or threadId")
    root = payload.get("payload") or {}
    headers = _headers(root)
    plain: list[str] = []
    rich: list[str] = []
    attachments: list[dict] = []
    mime: list[str] = []
    _walk_part(root, {}, plain, rich, attachments, mime)
    body = "\n\n".join(item for item in plain if item.strip()) or "\n\n".join(item for item in rich if item.strip())
    body = _plain_text(body)
    commitment_text = strip_quoted_history(body)
    labels = sorted({str(label) for label in (payload.get("labelIds") or []) if str(label)})
    material = "\n".join((
        headers.get("from", ""), headers.get("to", ""), headers.get("cc", ""),
        headers.get("subject", ""), body, ",".join(labels),
    ))
    return {
        "source_id": source_id_for(message_id),
        "message_id": message_id,
        "thread_id": thread_id,
        "history_id": str(payload.get("historyId") or ""),
        "internal_date_ms": int(payload.get("internalDate") or 0),
        "received_at": _iso_from_ms(payload.get("internalDate")),
        "from_header": headers.get("from", "")[:MAX_HEADER_CHARS],
        "to_header": headers.get("to", "")[:MAX_HEADER_CHARS],
        "cc_header": headers.get("cc", "")[:MAX_HEADER_CHARS],
        "subject": headers.get("subject", "")[:MAX_HEADER_CHARS],
        "labels_json": json.dumps(labels, ensure_ascii=False, separators=(",", ":")),
        "snippet": _plain_text(payload.get("snippet") or "")[:2000],
        "body_text": body,
        "commitment_text": commitment_text,
        "content_state": "available",
        "mime_json": json.dumps({"types": sorted(set(mime))[:30]}, ensure_ascii=False, separators=(",", ":")),
        "attachments_json": json.dumps(attachments, ensure_ascii=False, separators=(",", ":")),
        "is_sent": int("SENT" in labels),
        "deleted_at": None,
        "ingested_at": ingested_at,
        "updated_at": ingested_at,
        "revision": 0,
        "baseline": 0,
        "content_hash": hashlib.sha256(material.encode("utf-8")).hexdigest(),
    }


def unavailable_message(message_id: str, thread_id: str, *, ingested_at: str) -> dict:
    """Represent a message which Gmail lists but will not disclose to this grant.

    This holds only opaque Gmail identifiers and an availability state.  It
    deliberately contains no guessed header/body information and is excluded
    from search, commitment extraction, and proactive attention.
    """
    message_id = str(message_id or "")
    thread_id = str(thread_id or message_id)
    if not message_id or not thread_id:
        raise ValueError("Gmail unavailable message is missing id")
    return {
        "source_id": source_id_for(message_id), "message_id": message_id, "thread_id": thread_id,
        "history_id": "", "internal_date_ms": 0, "received_at": None,
        "from_header": "", "to_header": "", "cc_header": "", "subject": "", "labels_json": "[]",
        "snippet": "", "body_text": "", "commitment_text": "",
        "content_state": "body_unavailable", "mime_json": "{}", "attachments_json": "[]",
        "is_sent": 0, "deleted_at": None, "ingested_at": ingested_at, "updated_at": ingested_at,
        "revision": 0, "baseline": 0,
        "content_hash": hashlib.sha256(f"body_unavailable:{message_id}".encode("utf-8")).hexdigest(),
    }
