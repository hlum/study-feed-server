"""Card validation against the API.md schema, plus SVG/Mermaid sanitizing."""
import re
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher

from core import TOPICS, is_japanese

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

# Text limits per card language. Japanese limits are about half the English ones,
# because each Japanese character carries more meaning.
LIMITS = {
    "en": {"title": 80, "body": 280, "cliffhanger": 70, "question": 300, "option": 200,
           "explanation": 500, "front": 300, "back": 500},
    "ja": {"title": 40, "body": 140, "cliffhanger": 35, "question": 150, "option": 100,
           "explanation": 250, "front": 150, "back": 250},
}


def validate_card(raw: dict, problems: list | None = None, lang: str = "en") -> dict:
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
    lim = LIMITS[lang]
    c["title"] = _str(raw.get("title"), "title", lim["title"])
    c["body"] = _str(raw.get("body"), "body", lim["body"])
    c["cliffhanger"] = _str(raw.get("cliffhanger"), "cliffhanger", lim["cliffhanger"])

    src = raw.get("source")
    if src is None:
        c["source"] = None
    elif isinstance(src, dict):
        # quote is optional: news cards keep {label, url} when no verbatim quote exists.
        c["source"] = {"label": _str(src.get("label"), "source.label", 120)}
        if src.get("quote") is not None:
            c["source"]["quote"] = _str(src.get("quote"), "source.quote", 600)
        c["source"]["url"] = _str(src.get("url"), "source.url", 1000)
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
        c["slides"] = [{"title": _str(s.get("title"), "slide.title", lim["title"]),
                        "body": _str(s.get("body"), "slide.body", lim["body"]),
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

    c["quiz"] = validate_quiz(raw.get("quiz"), lang)

    f = raw.get("flashcard")
    if not isinstance(f, dict):
        raise Invalid("flashcard is required")
    c["flashcard"] = {"front": _str(f.get("front"), "flashcard.front", lim["front"]),
                      "back": _str(f.get("back"), "flashcard.back", lim["back"])}
    if lang == "ja":
        # A Japanese article's card must really be in Japanese, every reader-facing part.
        parts = {"title/body": c["title"] + c["body"], "quiz": c["quiz"]["question"] + c["quiz"]["explanation"],
                 "flashcard": c["flashcard"]["front"] + c["flashcard"]["back"]}
        not_ja = [k for k, v in parts.items() if not is_japanese(v)]
        if not_ja:
            raise Invalid("card for a Japanese article is not in Japanese: " + ", ".join(not_ja))
    return c


def validate_quiz(q, lang: str = "en") -> dict:
    lim = LIMITS[lang]
    if not isinstance(q, dict):
        raise Invalid("quiz is required")
    options = q.get("options")
    if not isinstance(options, list) or len(options) != 4:
        raise Invalid("quiz needs exactly 4 options")
    quiz = {"question": _str(q.get("question"), "quiz.question", lim["question"]),
            "options": [_str(o, "quiz.option", lim["option"]) for o in options],
            "answer": _answer_index(q.get("answer"), options),
            "explanation": _str(q.get("explanation"), "quiz.explanation", lim["explanation"])}
    if not 0 <= quiz["answer"] <= 3:
        raise Invalid("quiz.answer must be 0-3")
    return quiz


def _answer_index(answer, options) -> int:
    """The answer as an option index. Accepts the option's own text when it matches exactly
    one option; anything else that isn't among the options is rejected."""
    if isinstance(answer, str):
        hits = [i for i, o in enumerate(options) if isinstance(o, str) and _norm(o) == _norm(answer)]
        if len(hits) == 1:
            return hits[0]
        if answer.strip().isdigit():
            return int(answer.strip())
        raise Invalid("quiz.answer is not among the options")
    return _int(answer, "quiz.answer")


# ---------- limits: report, then shorten ----------

def _limited_fields(raw: dict, lang: str):
    """(name, path, text, max) for every length-limited text field in a raw card;
    path walks from the card to the field, e.g. ("quiz", "options", 2)."""
    lim = LIMITS[lang]
    paths = [(("title",), "title"), (("body",), "body"), (("cliffhanger",), "cliffhanger"),
             (("quiz", "question"), "question"), (("quiz", "explanation"), "explanation"),
             (("flashcard", "front"), "front"), (("flashcard", "back"), "back")]
    if raw.get("card_type") == "poster" and isinstance(raw.get("slides"), list):
        for i in range(len(raw["slides"])):
            paths += [(("slides", i, "title"), "title"), (("slides", i, "body"), "body")]
    q = raw.get("quiz")
    if isinstance(q, dict) and isinstance(q.get("options"), list):
        paths += [(("quiz", "options", i), "option") for i in range(len(q["options"]))]
    out = []
    for path, limit_key in paths:
        node = raw
        for step in path:
            ok = (isinstance(node, dict) and step in node) or (isinstance(node, list) and isinstance(step, int))
            node = node[step] if ok else None
            if node is None:
                break
        if isinstance(node, str):
            name = ".".join(f"[{p}]" if isinstance(p, int) else p for p in path).replace(".[", "[")
            out.append((name, path, node, lim[limit_key]))
    return out


def length_problems(raw, lang: str) -> list[str]:
    """Every over-limit field, worded for the retry prompt, e.g.
    'body was 286 chars, max 280. Rewrite it shorter.'"""
    if not isinstance(raw, dict):
        return []
    return [f"{n} was {len(t.strip())} chars, max {m}. Rewrite it shorter."
            for n, _, t, m in _limited_fields(raw, lang) if len(t.strip()) > m]


_SENTENCE_END = re.compile(r"(?<=[.!?])\s+|(?<=[。！？])")


def shorten_text(text: str, max_len: int) -> str | None:
    """Keep whole sentences up to max_len. None if not even the first sentence fits."""
    out = ""
    for sentence in (p.strip() for p in _SENTENCE_END.split(text.strip()) if p.strip()):
        candidate = out + (" " if out and not out.endswith(("。", "！", "？")) else "") + sentence
        if len(candidate) > max_len:
            break
        out = candidate
    return out or None


def shorten_card(raw: dict, lang: str) -> dict | None:
    """Shorten every over-limit field at a sentence boundary. None if any field can't be
    shortened that way (e.g. a one-sentence title), which makes the card unusable."""
    import copy
    card = copy.deepcopy(raw)
    for _, path, text, max_len in _limited_fields(card, lang):
        if len(text.strip()) <= max_len:
            continue
        short = shorten_text(text, max_len)
        if short is None:
            return None
        node = card
        for step in path[:-1]:
            node = node[step]
        node[path[-1]] = short
    return card


# ---------- quality guard (small models) ----------

def _plain(s: str) -> str:
    return re.sub(r"[\W_]+", "", s.lower())


def quality_problems(card: dict) -> list[str]:
    """Problems that pass the schema but make a bad card."""
    out = []
    t, b = _plain(card["title"]), _plain(card["body"])
    if (b == t or b in t or (b.startswith(t) and len(b) - len(t) < 12)
            or SequenceMatcher(None, t, b).ratio() >= 0.85):
        out.append("body only repeats the title")
    opts = [_plain(o) for o in card["quiz"]["options"]]
    if len(set(opts)) < len(opts):
        out.append("quiz has duplicate options")
    if not 0 <= card["quiz"]["answer"] < len(opts):
        out.append("quiz answer is not among the options")
    return out


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def quote_is_verbatim(quote: str, text: str) -> bool:
    q = _norm(quote).strip(" .\"'…")
    return len(q) >= 20 and q in _norm(text)


def is_rfc_source(card: dict) -> bool:
    src = card.get("source") or {}
    hay = f"{src.get('label', '')} {src.get('url', '')}"
    return bool(re.search(r"\bRFC\s?\d+|rfc-editor\.org|datatracker\.ietf\.org/doc/(html/)?rfc", hay, re.I))
