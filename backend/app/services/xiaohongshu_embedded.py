"""Read bounded, non-executable data from one already fetched public note page."""
from __future__ import annotations

import json
import math
import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit, urlunsplit

from justhtml import JustHTML

from .public_metadata import safe_author_name


_ASSIGNMENT = re.compile(r"\A\s*window\.__INITIAL_STATE__\s*=\s*")
_NOTE_ID = re.compile(r"[0-9a-f]{24}")
_SCRIPT_LIMIT = 256 * 1024
_MAX_DEPTH = 32
_MAX_NODES = 16384
_COVER_HOST = "sns-webpic-qc.xhscdn.com"
_MEDIA_PATH = re.compile(r"\.(?:mp4|m3u8|mpd|flv|mov|webm|mp3|m4a|aac|wav)(?:$|[/?;])", re.I)


@dataclass(frozen=True)
class EmbeddedXhsMetadata:
    author: str = ""
    cover_url: str = ""


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate state key")
        result[key] = value
    return result


def _finite_float(text):
    number = float(text)
    if not math.isfinite(number):
        raise ValueError("Non-finite state value")
    return number


def _reject_constant(_value):
    raise ValueError("Non-JSON constant")


def _decode_literal(raw: str) -> dict:
    """JSON with only bare undefined values; no JavaScript evaluation/recovery."""
    output = []
    quoted = escaped = False
    depth = index = 0
    previous = ""
    while index < len(raw):
        char = raw[index]
        if quoted:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            index += 1
            continue
        if char == '"':
            quoted = True
        elif char in "{[":
            depth += 1
            if depth > _MAX_DEPTH:
                raise ValueError("State nesting limit")
        elif char in "}]":
            depth -= 1
        elif raw.startswith("undefined", index) and previous in (":", "[", ","):
            end = index + len("undefined")
            suffix = end
            while suffix < len(raw) and raw[suffix] in " \t\r\n":
                suffix += 1
            if suffix == len(raw) or raw[suffix] in ",}]":
                output.append("null")
                index = end
                previous = "l"
                continue
        output.append(char)
        if char not in " \t\r\n":
            previous = char
        index += 1
    state = json.loads("".join(output), object_pairs_hook=_unique_object,
                       parse_constant=_reject_constant, parse_float=_finite_float)
    if not isinstance(state, dict):
        raise ValueError("State must be an object")
    pending = [state]
    count = 0
    while pending:
        value = pending.pop()
        count += 1
        if count > _MAX_NODES:
            raise ValueError("State node limit")
        if isinstance(value, dict):
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    return state


def _safe_cover(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 2048:
        return ""
    if any(char.isspace() or unicodedata.category(char).startswith("C") for char in value):
        return ""
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").casefold()
        if (host != _COVER_HOST or parsed.username is not None or parsed.password is not None
                or parsed.fragment or not parsed.path):
            return ""
        if parsed.scheme == "http" and parsed.port in (None, 80):
            candidate = urlunsplit(("https", host, parsed.path, parsed.query, ""))
        elif parsed.scheme == "https" and parsed.port in (None, 443):
            candidate = value
        else:
            return ""
        if len(candidate) > 2048 or _MEDIA_PATH.search(unquote(unquote(parsed.path))):
            return ""
        return candidate
    except ValueError:
        return ""


def extract_xhs_embedded_metadata(html: str, expected_note_id: str) -> EmbeddedXhsMetadata | None:
    """No I/O. Call only after the source adapter has proved the final note URL."""
    try:
        if (not isinstance(expected_note_id, str) or not _NOTE_ID.fullmatch(expected_note_id)
                or not isinstance(html, str) or len(html.encode("utf-8")) > 4 * 1024 * 1024
                or html.count("<") > 10000):
            return None
        document = JustHTML(html, sanitize=False, scripting_enabled=True, fragment=False)
        root = next((node for node in document.root.children
                     if node.name == "html" and node.namespace == "html"), None)
        candidates = []
        for section in root.children if root is not None else []:
            if section.name not in {"head", "body"} or section.namespace != "html":
                continue
            for node in section.children:
                if node.name != "script" or node.namespace != "html":
                    continue
                attrs = node.attrs or {}
                if ("src" in attrs
                        or attrs.get("type", "").strip().casefold()
                        not in {"", "text/javascript", "application/javascript"}):
                    continue
                text = node.to_text(separator="", strip=False)
                match = _ASSIGNMENT.match(text)
                if not match:
                    continue
                if len(text.encode("utf-8")) > _SCRIPT_LIMIT:
                    return None
                candidates.append(text[match.end():].strip())
        if len(candidates) != 1:
            return None
        raw = candidates[0]
        if raw.endswith(";"):
            raw = raw[:-1].rstrip()
        state = _decode_literal(raw)
        note_state = state.get("note")
        if not isinstance(note_state, dict):
            return None
        if any(note_state.get(key) not in (None, "", expected_note_id)
               for key in ("firstNoteId", "currentNoteId")):
            return None
        detail_map = note_state.get("noteDetailMap")
        entry = detail_map.get(expected_note_id) if isinstance(detail_map, dict) else None
        note = entry.get("note") if isinstance(entry, dict) else None
        if (not isinstance(note, dict) or note.get("noteId") != expected_note_id
                or note.get("type") not in ("normal", "video")):
            return None
        user = note.get("user")
        author = safe_author_name(user.get("nickname")) if isinstance(user, dict) else None
        images = note.get("imageList")
        first = images[0] if isinstance(images, list) and images else None
        cover = _safe_cover(first.get("urlDefault")) if isinstance(first, dict) else ""
        return EmbeddedXhsMetadata(author=author or "", cover_url=cover)
    except (ValueError, TypeError, RecursionError):
        return None
