"""The tracker files as text: the frontmatter (a flat YAML subset), `## ` sections, bullets and link lines. Pure
functions of a file's text: reading and writing the files is model's."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------- frontmatter

LIST_ITEM = re.compile(r'\s*(?:"(?:[^"\\]|\\.)*"|[^,]+)')


def parse_value(raw: str):
    """One frontmatter value; raises ValueError for a quoted value that is not valid JSON."""
    raw = raw.strip()
    if raw.startswith('"'):
        return json.loads(raw)
    if raw.startswith("[") and raw.endswith("]"):
        items = [x.strip() for x in LIST_ITEM.findall(raw[1:-1]) if x.strip()]
        return [json.loads(x) if x.startswith('"') else x for x in items]
    return raw


def needs_quotes(s: str, extra: str = "") -> bool:
    return (s != s.strip() or s[0] in "[{\"'#&*!|>%@`" or ": " in s or " #" in s
            or any(c in s for c in extra) or any(ord(c) < 32 for c in s))


def format_value(value) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(json.dumps(str(x), ensure_ascii=False) if x and needs_quotes(str(x), ',"[]')
                               else str(x) for x in value) + "]"
    s = str(value)
    if s == "":
        return ""
    return json.dumps(s, ensure_ascii=False) if needs_quotes(s) else s


def split_frontmatter(text: str) -> tuple[list[str], str]:
    if not text.startswith("---\n"):
        return [], text
    end = text.find("\n---\n", 3)
    if end == -1:
        return [], text
    return text[4:end].splitlines(), text[end + 5:]


def parse_meta(lines: list[str]) -> tuple[dict, list[str]]:
    """Frontmatter keys, and the lines that are not valid. A bad value is kept as its raw text, so one bad line
    never stops the rest of the tracker from loading; `check` reports it."""
    meta, problems = {}, []
    for n, line in enumerate(lines, 2):  # line 1 is the opening ---
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            problems.append(f"frontmatter line {n} is not `key: value`: {line.strip()[:60]}")
            continue
        key, _, raw = line.partition(":")
        key = key.strip()
        if key in meta:
            problems.append(f"frontmatter line {n}: key '{key}' appears twice")
        try:
            meta[key] = parse_value(raw)
        except ValueError:
            meta[key] = raw.strip()
            problems.append(f"frontmatter line {n}: {key}: a value that starts with a quote must be one JSON string "
                            f"— quote all of it, or remove the quotes")
    return meta, problems


def frontmatter_problems(text: str) -> list[str]:
    if not text.startswith("---\n"):
        return ["no frontmatter: the file must start with a `---` line"]
    return [] if text.find("\n---\n", 3) != -1 else ["frontmatter has no closing `---` line"]


def render_frontmatter(lines: list[str], updates: dict) -> list[str]:
    """Replace keys in place, keeping order and untouched lines; append new keys; a None value removes the key."""
    out, seen = [], set()
    for line in lines:
        key = line.partition(":")[0].strip() if ":" in line else None
        if key in updates:
            if updates[key] is not None:
                out.append(f"{key}: {format_value(updates[key])}")
            seen.add(key)
        else:
            out.append(line)
    out += [f"{k}: {format_value(v)}" for k, v in updates.items() if k not in seen and v is not None]
    return out


# ---------------------------------------------------------------- sections

BULLET = re.compile(r"\s*[-*] ")


def strip_comments(text: str) -> str:
    """The text without its `<!-- -->` comments: the templates' guidance."""
    return re.sub(r"<!--.*?-->", "", text, flags=re.S)


def section_re(heading: str) -> re.Pattern:
    """A `## ` section, from its heading to the next one; group 1 is its text. Case does not count, so reads and
    writes find the same section."""
    return re.compile(rf"^## {re.escape(heading)}\s*$(.*?)(?=^## |\Z)", re.M | re.S | re.I)


def section_span(text: str, heading: str) -> re.Match | None:
    return section_re(heading).search(text)


def section(body: str, heading: str) -> str:
    m = section_span(body, heading)
    return m[1].strip() if m else ""


def bullets(text: str) -> list[str]:
    return [ln.strip()[2:].strip() for ln in text.splitlines() if ln.strip().startswith(("- ", "* "))]


def headings(body: str) -> list[str]:
    return [m.group(1).strip() for m in re.finditer(r"^## (.+)$", body, re.M)]


def section_block(body: str, heading: str) -> str:
    """A section with its own heading, for showing on its own."""
    m = section_span(body, heading)
    return m[0].strip() + "\n\n" if m else ""


def without_section(body: str, heading: str) -> str:
    return section_re(heading).sub("", body)


# ---------------------------------------------------------------- link lines

LINK_LINE = re.compile(r"^- (?:\*\*)?([A-Z][A-Za-z]*(?: [A-Za-z]+)?)(?:\*\*)?:(?:\*\*)?\s+(\S.*)$")


@dataclass
class Link:
    """One `- Label: text` line of README ## Context or a ticket's ## Links; nested bullets are its `sub` lines."""
    label: str
    text: str
    sub: list[str] = field(default_factory=list)


def parse_links(text: str) -> tuple[list[Link], list[str]]:
    """The link lines of a Context/Links section, and any lines that do not follow the format."""
    items, bad = [], []
    for ln in strip_comments(text).splitlines():
        if not ln.strip():
            continue
        if ln.startswith("- "):
            m = LINK_LINE.match(ln)
            items.append(Link(m[1], m[2])) if m else bad.append(ln)
        elif items:
            items[-1].sub.append(ln.strip().removeprefix("- "))
        else:
            bad.append(ln)
    return items, bad


def link_ident(link: Link) -> str:
    """The id a link line names: the first word of its link title, or of the line; a bare URL gives its last path
    segment."""
    m = re.match(r"\[([^\]]+)\]\(", link.text)
    token = (m[1] if m else link.text).split()[0]
    if re.match(r"https?://", token):
        token = re.split(r"[?#]", token)[0].rstrip("/").rsplit("/", 1)[-1]
    return token.rstrip(":;,.")
