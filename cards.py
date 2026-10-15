"""Card validation against the API.md schema, plus SVG/Mermaid sanitizing."""
import re
import xml.etree.ElementTree as ET

from core import TOPICS

CARD_TYPES = ("poster", "post", "terminal")
VISUAL_KINDS = ("steps", "code", "terminal", "mermaid", "svg")
CARD_KEYS = ("card_type", "topic", "series_id", "part", "parts_total", "title", "body", "source",
             "slides", "visual", "cliffhanger", "quiz", "flashcard")


class Invalid(ValueError):
    pass


def _str(v, name, max_len=None, allow_empty=False):
    if not isinstance(v, str) or (not allow_empty and not v.strip()):
        raise Invalid(f"{name} must be a non-empty string")
    if max_len and len(v) > max_len:
        raise Invalid(f"{name} is {len(v)} chars (max {max_len})")
    return v.strip()


def _str_list(v, name):
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise Invalid(f"{name} must be a list of strings")
    return v


def _int(v, name, nullable=False):
    if v is None and nullable:
        return None
    if isinstance(v, bool) or not isinstance(v, int):
        raise Invalid(f"{name} must be an integer")
    return v


# ---------- SVG ----------
SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
ET.register_namespace("", SVG_NS)
ET.register_namespace("xlink", XLINK_NS)
BANNED_TAGS = {"script", "foreignobject", "iframe", "object", "embed", "image", "audio",
               "video", "use", "animate", "set", "animatemotion", "animatetransform", "handler",
               "listener"}
URL_FN = re.compile(r"url\s*\(", re.I)


def _local(name: str) -> str:
    return name.rsplit("}", 1)[-1].lower()


def sanitize_svg(source: str) -> str:
    """Return a cleaned SVG string or raise Invalid."""
    if not isinstance(source, str) or len(source) > 50_000:
        raise Invalid("svg source missing or too large")
    if re.search(r"<!(DOCTYPE|ENTITY)", source, re.I):
        raise Invalid("svg contains DOCTYPE/ENTITY")
    try:
        root = ET.fromstring(source)
    except ET.ParseError as e:
        raise Invalid(f"svg does not parse: {e}")
    if _local(root.tag) != "svg":
        raise Invalid("svg root element is not <svg>")

    def clean(el):
        for child in list(el):
            tag = _local(child.tag) if isinstance(child.tag, str) else ""
            if not tag or tag in BANNED_TAGS or (tag == "style" and (
                    URL_FN.search(child.text or "") or "@import" in (child.text or "").lower())):
                el.remove(child)
                continue
            clean(child)
        for attr in list(el.attrib):
            name, value = _local(attr), el.attrib[attr]
            low = value.strip().lower()
            if (name.startswith("on")
                    or (name == "href" and not low.startswith("#"))
                    or "javascript:" in low or "data:" in low
                    or (URL_FN.search(value) and not re.fullmatch(r"\s*url\(\s*#[\w-]+\s*\)\s*", value))):
                del el.attrib[attr]

    clean(root)
    return ET.tostring(root, encoding="unicode")


def sanitize_mermaid(source: str) -> str:
    if not isinstance(source, str) or not source.strip() or len(source) > 5000:
        raise Invalid("mermaid source missing or too large")
    lines = [l for l in source.splitlines()
             if not re.match(r"\s*(click\b|%%\{)", l, re.I)]  # no click handlers / init directives
    out = "\n".join(lines)
    if re.search(r"javascript:|<\s*script|<\s*iframe|\bon\w+\s*=", out, re.I):
        raise Invalid("mermaid contains script-like content")
    return out


# ---------- visuals ----------

def validate_visual(v):
    if v is None:
        return None
    if not isinstance(v, dict) or v.get("kind") not in VISUAL_KINDS:
        raise Invalid("visual.kind must be one of " + ", ".join(VISUAL_KINDS))
    kind = v["kind"]
    if kind == "steps":
        actors = _str_list(v.get("actors"), "visual.actors")
        if len(actors) < 2:
            raise Invalid("steps visual needs at least 2 actors")
        steps = v.get("steps")
        if not isinstance(steps, list) or not steps:
            raise Invalid("steps visual needs steps")
        clean = []
        for s in steps:
            if not isinstance(s, dict):
                raise Invalid("step must be an object")
            frm, to = _int(s.get("from"), "step.from"), _int(s.get("to"), "step.to")
            if not (0 <= frm < len(actors) and 0 <= to < len(actors)):
                raise Invalid("step actor index out of range")
            step = {"from": frm, "to": to, "label": _str(s.get("label"), "step.label")}
            if s.get("caption") is not None:
                step["caption"] = _str(s["caption"], "step.caption", allow_empty=True)
            if s.get("state") is not None:
                step["state"] = _str_list(s["state"], "step.state")
            clean.append(step)
        return {"kind": "steps", "actors": actors, "steps": clean}
    if kind == "code":
        out = {"kind": "code", "lang": _str(v.get("lang"), "code.lang"),
               "lines": _str_list(v.get("lines"), "code.lines")}
        notes = v.get("notes") or []
        if not isinstance(notes, list):
            raise Invalid("code.notes must be a list")
        out["notes"] = [{"line": _int(n.get("line"), "note.line"),
                         "mark": _str(n.get("mark"), "note.mark"),
                         "text": _str(n.get("text"), "note.text")}
                        for n in notes if isinstance(n, dict)]
        return out
    if kind == "terminal":
        out = {"kind": "terminal", "command": _str(v.get("command"), "terminal.command"),
               "lines": _str_list(v.get("lines"), "terminal.lines")}
        legend = v.get("legend") or []
        if not isinstance(legend, list):
            raise Invalid("terminal.legend must be a list")
        out["legend"] = [{"token": _str(l.get("token"), "legend.token"),
                          "meaning": _str(l.get("meaning"), "legend.meaning")}
                         for l in legend if isinstance(l, dict)]
        return out
    if kind == "mermaid":
        return {"kind": "mermaid", "source": sanitize_mermaid(v.get("source"))}
    return {"kind": "svg", "source": sanitize_svg(v.get("source"))}


def _soft_visual(v, problems):
    """Optional visuals: drop a bad one instead of the whole card."""
    try:
        return validate_visual(v)
    except Invalid as e:
        problems.append(f"dropped visual: {e}")
        return None


# ---------- card ----------

def validate_card(raw: dict, problems: list | None = None) -> dict:
    """Return a clean card dict (only schema keys) or raise Invalid.
    Non-fatal fixes are appended to `problems` for logging."""
    problems = problems if problems is not None else []
    if not isinstance(raw, dict):
        raise Invalid("card is not an object")
    c = {}
    c["card_type"] = raw.get("card_type")
    if c["card_type"] not in CARD_TYPES:
        raise Invalid(f"bad card_type {c['card_type']!r}")
    c["topic"] = raw.get("topic")
    if c["topic"] not in TOPICS:
        raise Invalid(f"bad topic {c['topic']!r}")
    c["series_id"] = raw.get("series_id") if isinstance(raw.get("series_id"), str) else None
    c["part"] = _int(raw.get("part"), "part", nullable=True)
    c["parts_total"] = _int(raw.get("parts_total"), "parts_total", nullable=True)
    c["title"] = _str(raw.get("title"), "title", 80)
    c["body"] = _str(raw.get("body"), "body", 280)
    c["cliffhanger"] = _str(raw.get("cliffhanger"), "cliffhanger", 70)

    src = raw.get("source")
    if src is None:
        c["source"] = None
    elif isinstance(src, dict):
        c["source"] = {"label": _str(src.get("label"), "source.label", 120),
                       "quote": _str(src.get("quote"), "source.quote", 600),
                       "url": _str(src.get("url"), "source.url", 1000)}
        if not c["source"]["url"].startswith(("http://", "https://")):
            raise Invalid("source.url must be http(s)")
    else:
        raise Invalid("source must be an object or null")

    slides = raw.get("slides") or []
    if not isinstance(slides, list):
        raise Invalid("slides must be a list")
    if c["card_type"] == "poster":
        if not 3 <= len(slides) <= 5:
            raise Invalid(f"poster needs 3-5 slides, got {len(slides)}")
        c["slides"] = [{"title": _str(s.get("title"), "slide.title", 80),
                        "body": _str(s.get("body"), "slide.body", 280),
                        "visual": _soft_visual(s.get("visual"), problems)}
                       for s in slides if isinstance(s, dict)]
        if len(c["slides"]) != len(slides):
            raise Invalid("slide must be an object")
    else:
        if slides:
            problems.append("cleared slides on non-poster card")
        c["slides"] = []

    if c["card_type"] == "terminal":
        c["visual"] = validate_visual(raw.get("visual"))
        if not c["visual"] or c["visual"]["kind"] not in ("terminal", "code"):
            raise Invalid("terminal card needs visual.kind terminal or code")
    else:
        c["visual"] = _soft_visual(raw.get("visual"), problems)

    q = raw.get("quiz")
    if not isinstance(q, dict):
        raise Invalid("quiz is required")
    options = q.get("options")
    if not isinstance(options, list) or len(options) != 4:
        raise Invalid("quiz needs exactly 4 options")
    c["quiz"] = {"question": _str(q.get("question"), "quiz.question", 300),
                 "options": [_str(o, "quiz.option", 200) for o in options],
                 "answer": _int(q.get("answer"), "quiz.answer"),
                 "explanation": _str(q.get("explanation"), "quiz.explanation", 500)}
    if not 0 <= c["quiz"]["answer"] <= 3:
        raise Invalid("quiz.answer must be 0-3")

    f = raw.get("flashcard")
    if not isinstance(f, dict):
        raise Invalid("flashcard is required")
    c["flashcard"] = {"front": _str(f.get("front"), "flashcard.front", 300),
                      "back": _str(f.get("back"), "flashcard.back", 500)}
    return c


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def quote_is_verbatim(quote: str, text: str) -> bool:
    q = _norm(quote).strip(" .\"'…")
    return len(q) >= 20 and q in _norm(text)


def is_rfc_source(card: dict) -> bool:
    src = card.get("source") or {}
    hay = f"{src.get('label', '')} {src.get('url', '')}"
    return bool(re.search(r"\bRFC\s?\d+|rfc-editor\.org|datatracker\.ietf\.org/doc/(html/)?rfc", hay, re.I))
