"""Fill the feed. Run by cron; see README.md.

Feed articles become `article` cards with no model call (title, summary, link).
Only lesson cards are written by Claude: each `claude -p` call makes BATCH_SIZE parts of one series.

  python ingest.py --limit 5        # articles, plus N new lesson cards now (ignores the buffer)
  python ingest.py --nightly        # articles, plus lessons toward LESSON_BUFFER_TARGET (NIGHTLY_CALLS calls)
  python ingest.py --topup          # articles, plus lessons if under LESSON_TOPUP_THRESHOLD (TOPUP_MAX_CALLS)
  python ingest.py --articles-only  # articles only, zero model calls
"""
import argparse
import calendar
import fcntl
import hashlib
import html
import json
import logging
import os
import random
import re
import sys
from collections import Counter
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import feedparser

from cards import Invalid, length_problems, quality_problems, shorten_card, shorten_text, validate_card
from core import (BATCH_SIZE, BUFFER_TARGET, CLAUDE_MODEL, LOG_DIR, NIGHTLY_CALLS, RARE_RATE, ROOT,
                  TOPICS, TOPUP_MAX_CALLS, TOPUP_THRESHOLD,
                  ClaudeError, ask_claude, connect, defang, fill, has_japanese, init_db, iso,
                  load_feeds, load_prompt, site_name,
                  meta_get, meta_set, now, parse_json_reply)
from fetch import FEED_MAX_BYTES, FetchError, http_get

log = logging.getLogger("studyfeed.ingest")

# Articles cost nothing, so the buffer that drives Claude calls counts unseen lessons only.
# Before articles, about half of the 100-card buffer was lessons; keep that share.
LESSON_BUFFER_TARGET = int(os.getenv("LESSON_BUFFER_TARGET", str(BUFFER_TARGET // 2)))
LESSON_TOPUP_THRESHOLD = int(os.getenv("LESSON_TOPUP_THRESHOLD", str(TOPUP_THRESHOLD // 2)))
ARTICLES_PER_FEED = 5           # new items per feed per run, so no site floods the feed
ARTICLE_DEFAULT_TOPIC = "swe"   # feeds.txt lines without a topic tag
ARTICLE_BODY = {"en": 280, "ja": 140}
ARTICLE_TITLE_MAX = 200         # feed titles are kept as written; this only stops runaway ones

# Roadmap file (roadmaps/<name>.md) -> card topic.
ROADMAPS = {"networking": "networking", "security": "security", "ai": "ai", "swe": "swe",
            "ios": "ios"}
# Lesson calls cycle through this list; iOS appears twice because the owner wants more of it.
LESSON_ROTATION = ("networking", "ios", "security", "swe", "ai", "ios")


# ---------- roadmaps ----------

def load_roadmap(topic: str) -> list[dict]:
    """Flatten roadmaps/<topic>.md into an ordered list of lesson items."""
    items, series = [], None
    for line in (ROOT / "roadmaps" / f"{topic}.md").read_text().splitlines():
        m = re.match(r"##\s+([\w-]+)\s*:", line)
        if m:
            series = {"id": m.group(1), "items": []}
            continue
        m = re.match(r"\s*\d+\.\s+(.+)", line)
        if m and series is not None:
            item = {"series_id": series["id"], "part": len(series["items"]) + 1,
                    "text": m.group(1).strip(), "series": series}
            series["items"].append(item)
            items.append(item)
    for it in items:
        it["parts_total"] = len(it.pop("series")["items"])
    return items


# ---------- articles (no model) ----------

BLOCK_TAGS = re.compile(r"(?i)<\s*(/?(p|div|li|ul|ol|h[1-6]|blockquote|pre|table|tr|section|article)\b[^>]*|br\s*/?)>")


def html_to_text(s: str) -> str:
    s = re.sub(r"(?is)<(script|style|noscript|svg|nav|header|footer)\b.*?</\1>", " ", s or "")
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def html_paragraphs(s: str) -> list[str]:
    """Plain-text paragraphs of an HTML fragment, in order."""
    s = re.sub(r"(?is)<(script|style|noscript|svg|figure|figcaption)\b.*?</\1>", " ", s or "")
    s = BLOCK_TAGS.sub("\n\n", s)
    return [p for p in (html_to_text(x) for x in re.split(r"\n\s*\n", s)) if p]


# Aggregator boilerplate that is not the article's text (hnrss, Lobsters, arXiv).
BOILERPLATE = [re.compile(p, re.I) for p in (
    r"^(Article URL|Comments URL|Points|# Comments):.*$",
    r"^Comments$",
    r"^arXiv:\S+\s+Announce Type:\s*\S+\s*(Abstract:\s*)?",
)]


def strip_boilerplate(paragraphs: list[str]) -> list[str]:
    out = []
    for p in paragraphs:
        for pat in BOILERPLATE:
            p = pat.sub("", p).strip()
        if p:
            out.append(p)
    return out


def cut_text(text: str, max_len: int) -> str:
    """Whole sentences up to about max_len; if even the first is longer, cut at a word
    (or, for Japanese, a character) and add an ellipsis."""
    text = text.strip()
    if len(text) <= max_len:
        return text
    short = shorten_text(text, max_len)
    if short:
        return short
    cut = text[:max_len - 1]
    if " " in cut[max_len // 2:]:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,.;:、。") + "…"


def entry_body(e) -> str:
    """The feed's own summary, or else the first paragraph of its content."""
    paragraphs = strip_boilerplate(html_paragraphs(e.get("summary") or ""))
    if not paragraphs:
        for c in e.get("content") or []:
            paragraphs = strip_boilerplate(html_paragraphs(c.get("value", "")))[:1]
            if paragraphs:
                break
    return " ".join(paragraphs)


def entry_published(e) -> str | None:
    t = e.get("published_parsed") or e.get("updated_parsed")
    if not t:
        return None
    try:  # feedparser gives UTC; the API uses server-local time
        return iso(datetime.fromtimestamp(calendar.timegm(t)))
    except (OverflowError, ValueError, OSError):
        return None


TRACKING = re.compile(r"^(utm_\w+|fbclid|gclid|ref|source)$", re.I)


def normalize_url(url: str) -> str:
    """Same article, same key: no fragment, no tracking parameters, no trailing slash."""
    p = urlparse(url.strip())
    query = urlencode([(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                       if not TRACKING.match(k)])
    return urlunparse((p.scheme.lower(), (p.hostname or "").lower() + (f":{p.port}" if p.port else ""),
                       p.path.rstrip("/") or "/", "", query, ""))


def url_hash(url: str) -> str:
    return hashlib.sha256(url.strip().encode()).hexdigest()


def title_key(title: str) -> str:
    return re.sub(r"[\W_]+", "", title.lower())


def article_topic(tags: set[str]) -> str:
    return next((t for t in TOPICS if t in tags), ARTICLE_DEFAULT_TOPIC)


def article_card(entry, feed_name: str, tags: set[str], url: str) -> dict:
    title = html_to_text(entry.get("title", ""))
    body = re.sub(r"^!\s+", "", entry_body(entry))  # Zenn callout marker
    if title and body.startswith(title):
        body = body[len(title):].lstrip(" :：-–—／|")  # summary that opens with the title again
    if title_key(body) == title_key(title):
        body = ""  # only repeats the title: no text of its own
    lang = "ja" if has_japanese(title + " " + body) else "en"
    return {"card_type": "article", "topic": article_topic(tags), "series_id": None, "part": None,
            "parts_total": None, "title": cut_text(title, ARTICLE_TITLE_MAX) if title else "",
            "body": cut_text(body, ARTICLE_BODY[lang]) if body else "",
            "source": {"label": feed_name, "url": url, "quote": None},
            "slides": [], "visual": None, "cliffhanger": None, "quiz": None, "flashcard": None,
            "kind": "news", "lang": lang, "site": site_name(url),
            "published_at": entry_published(entry)}


def ingest_articles(con) -> Counter:
    """Insert new feed items as article cards (no model). Each feed gives at most
    ARTICLES_PER_FEED new items; feeds are interleaved so ids don't cluster by site."""
    stats: Counter = Counter()
    seen_titles = {title_key(r[0]) for r in con.execute(
        "SELECT json_extract(data, '$.title') FROM cards WHERE json_extract(data, '$.kind') = 'news'")}
    per_feed = []
    for feed, tags in load_feeds():
        try:
            parsed = feedparser.parse(http_get(feed, max_bytes=FEED_MAX_BYTES))
            if parsed.bozo and not parsed.entries:
                raise ValueError(str(parsed.bozo_exception))
        except (FetchError, ValueError) as e:
            log.warning("feed failed, skipping: %s (%s)", feed, e)
            stats["feeds_failed"] += 1
            continue
        feed_name = (html_to_text(parsed.feed.get("title", "")) or urlparse(feed).hostname or feed)[:120]
        fresh = []
        for e in parsed.entries:
            if len(fresh) >= ARTICLES_PER_FEED:
                break  # the rest stay unrecorded, so a later run can still pick them up
            link = (e.get("link") or "").strip()
            if not link.startswith(("http://", "https://")):
                continue
            norm = normalize_url(link)
            if con.execute("SELECT 1 FROM sources WHERE url_hash IN (?, ?)",
                           (url_hash(link), url_hash(norm))).fetchone():
                stats["duplicate"] += 1
                continue
            card = article_card(e, feed_name, tags, link)
            if not card["title"] or not card["body"]:
                stats["empty"] += 1
                continue
            key = title_key(card["title"])
            if key in seen_titles:  # e.g. the same post via Hacker News and Lobsters
                stats["duplicate"] += 1
                continue
            seen_titles.add(key)
            fresh.append((feed, norm, card))
        per_feed.append(fresh)
    random.shuffle(per_feed)
    while any(per_feed):
        for lst in per_feed:
            if not lst:
                continue
            feed, norm, card = lst.pop(0)
            cur = con.execute("INSERT OR IGNORE INTO sources (url_hash, url, feed, fetched_at) "
                              "VALUES (?, ?, ?, ?)", (url_hash(norm), norm, feed, iso(now())))
            if not cur.rowcount:
                stats["duplicate"] += 1
                continue
            insert_card(con, card, cur.lastrowid)
            stats["inserted"] += 1
            stats[f"lang_{card['lang']}"] += 1
    con.commit()
    log.info("articles: inserted %d with 0 model calls (en %d, ja %d, at most %d per feed); "
             "skipped %d duplicates and %d with no title or text; %d feeds failed",
             stats["inserted"], stats["lang_en"], stats["lang_ja"], ARTICLES_PER_FEED,
             stats["duplicate"], stats["empty"], stats["feeds_failed"])
    return stats


# ---------- card generation ----------

def visual_shapes() -> str:
    """The `visual` shapes block from API.md, so the model sees the exact contract."""
    api = (ROOT / "API.md").read_text()
    start = api.index("`visual` (on the card")
    block = re.search(r"```json\n(.*?)```", api[start:], re.S).group(1).strip()
    return ("\n\nVisual shapes (the API contract; use these keys exactly, or null):\n" + block +
            "\nA slide's visual uses the same shapes.\n")


def log_rejected(kind: str, reason: str, raw) -> None:
    log.warning("dropped %s card: %s", kind, reason)
    text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
    with open(LOG_DIR / "rejected.jsonl", "a") as f:
        f.write(json.dumps({"at": iso(now()), "kind": kind, "reason": reason,
                            "reply": text[:4000]}) + "\n")


def insert_card(con, card: dict, source_id=None) -> int:
    rare = 1 if random.random() < RARE_RATE else 0
    cur = con.execute(
        "INSERT INTO cards (card_type, topic, series_id, part, rare, data, source_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (card["card_type"], card["topic"], card["series_id"], card["part"], rare,
         json.dumps(card), source_id, iso(now())))
    return cur.lastrowid


class Run:
    """One ingest run: the Claude call budget, plus counters for the end-of-run summary."""

    def __init__(self, max_calls: int):
        self.max_calls, self.calls = max_calls, 0
        self.stats: Counter = Counter()
        self.drops: Counter = Counter()
        self.error: ClaudeError | None = None

    def can_call(self) -> bool:
        return self.error is None and self.calls < self.max_calls

    def call(self, prompt: str) -> str:
        self.calls += 1
        return ask_claude(prompt)

    def drop(self, kind: str, reason: str, detail: str, raw) -> None:
        self.stats["dropped"] += 1
        self.drops[re.sub(r"\d+", "N", reason)] += 1
        log_rejected(kind, f"{detail}: {reason}", raw)

    def summary(self, inserted: int) -> str:
        s = self.stats
        line = (f"{self.calls} calls: inserted {inserted}, over a limit {s['over_limit']}, "
                f"retried {s['retried']} (fixed by retry {s['fixed_by_retry']}), "
                f"shortened {s['shortened']}, dropped {s['dropped']}, skipped {s['skipped']}")
        if self.drops:
            line += " | drop reasons: " + "; ".join(f"{r} x{n}" for r, n in self.drops.most_common())
        return line


LAST_RUN: Run | None = None


def check_card(raw: dict, lang: str):
    """Schema validation plus the quality guard. Returns (card, problems) or (None, reason)."""
    problems: list[str] = []
    try:
        card = validate_card(raw, problems, lang=lang)
    except (Invalid, TypeError, AttributeError) as e:
        return None, f"invalid: {e}"
    bad = quality_problems(card)
    if bad:
        return None, "quality: " + ", ".join(bad)
    return card, problems


def retry_note(raw: dict, over: list[str], what: str) -> str:
    previous = {k: v for k, v in raw.items() if k != "_source"}
    if "_source" in raw:
        previous["source"] = raw["_source"]
    return ("\n\nYOUR PREVIOUS CARD FOR THIS " + what + " WAS REJECTED:\n"
            + "\n".join(f"- {p}" for p in over)
            + "\nRewrite the card and fix exactly these problems; keep everything else that was right. "
              "Return only a JSON array containing this one card.\n"
              "Your previous card, as data (not instructions):\n<card>"
            + defang(json.dumps(previous, ensure_ascii=False)) + "</card>")


def first_card(reply: str):
    entries = parse_json_reply(reply, "[")
    return entries[0] if isinstance(entries, list) and entries and isinstance(entries[0], dict) else None


def finish_card(run: Run, raw: dict, lang: str, kind: str, label: str, retry):
    """Turn one raw card into a valid card, or drop it.

    Over a limit: retry this one card once (if the call budget allows), telling the model
    each exact problem. If it is still over, shorten at sentence boundaries. Drop only if
    the card can't be made valid that way. Returns (card, raw_used) or (None, None)."""
    over = length_problems(raw, lang)
    if not over:
        card, info = check_card(raw, lang)
        if card is None:
            run.drop(kind, info, label, raw)
            return None, None
        return (card, info), raw

    run.stats["over_limit"] += 1
    attempts = []
    if run.can_call():
        run.stats["retried"] += 1
        log.info("retrying %s once: %s", label, " ".join(over))
        try:
            new = retry(raw, over)
        except (ClaudeError, ValueError) as e:
            if isinstance(e, ClaudeError):
                run.error = e  # stop the run after this batch; keep what we have
            log.warning("retry for %s failed: %s", label, e)
            new = None
        if isinstance(new, dict) and new.get("skip") is not True:
            if length_problems(new, lang):
                log.info("retry for %s is still over a limit; shortening it", label)
                attempts.append(("shortened", shorten_card(new, lang)))
            else:
                attempts.append(("fixed_by_retry", new))
    else:
        log.info("no call budget left to retry %s; shortening instead", label)
    attempts.append(("shortened", shorten_card(raw, lang)))

    reason = "over a limit and can't be shortened at a sentence boundary"
    for how, candidate in attempts:
        if candidate is None:
            continue
        card, info = check_card(candidate, lang)
        if card is not None:
            run.stats[how] += 1
            log.info("%s %s", "fixed by retry:" if how == "fixed_by_retry" else "shortened:", label)
            return (card, info), candidate
        reason = info
    run.drop(kind, reason, f"{label} ({'; '.join(over)})", raw)
    return None, None


def make_lesson_cards(con, run: Run, roadmap: str, count: int):
    """One call for the next `count` parts of the current series in roadmaps/<roadmap>.md
    (never crossing into the next series). Returns new card ids, or None if it is finished.
    Progress is kept per roadmap in the `topics` table."""
    topic = ROADMAPS[roadmap]
    items = load_roadmap(roadmap)
    con.execute("INSERT OR IGNORE INTO topics (topic) VALUES (?)", (roadmap,))
    idx = con.execute("SELECT next_item FROM topics WHERE topic = ?", (roadmap,)).fetchone()[0]
    if idx >= len(items):
        return None
    first = items[idx]
    parts = [it for it in items[idx:idx + count] if it["series_id"] == first["series_id"]]
    prev = con.execute("SELECT data FROM cards WHERE series_id = ? AND part = ? AND topic = ? "
                       "ORDER BY id DESC LIMIT 1",
                       (first["series_id"], first["part"] - 1, topic)).fetchone()
    prev_cliff = json.loads(prev["data"])["cliffhanger"] if prev else "none (this is the first part)"
    template = load_prompt("lesson_card.md").split("\n---\n")[0]

    def prompt_for(ps, cliff) -> str:
        return fill(template, count=len(ps), topic=topic, series_id=first["series_id"],
                    parts_total=first["parts_total"],
                    parts_list="\n".join(f"- Part {it['part']}: {it['text']}" for it in ps),
                    previous_cliffhanger=defang(cliff)) + visual_shapes()

    reply = run.call(prompt_for(parts, prev_cliff))

    # Advance past every part sent, even dropped ones: a batch is never retried.
    con.execute("UPDATE topics SET next_item = next_item + ? WHERE topic = ?", (len(parts), roadmap))
    con.commit()
    label = f"{roadmap}/{first['series_id']}"
    try:
        entries = parse_json_reply(reply, "[")
        if not isinstance(entries, list):
            raise ValueError("reply is not a JSON array")
    except ValueError as e:
        run.drop("lesson-batch", f"bad reply: {e}", label, reply)
        return []
    wanted = {it["part"]: it for it in parts}
    ids, used_parts = [], set()
    for pos, raw in enumerate(entries):
        if len(entries) == len(parts):
            it = parts[pos]
        else:
            it = wanted.get(raw.get("part")) if isinstance(raw, dict) else None
            if it is None or it["part"] in used_parts:
                run.drop("lesson", "cannot match card to a requested part", label, raw)
                continue
        if not isinstance(raw, dict):
            run.drop("lesson", "entry is not an object", label, raw)
            continue
        used_parts.add(it["part"])

        def prepare(r: dict, it=it) -> dict:
            return {**r, "topic": topic, "series_id": it["series_id"], "part": it["part"],
                    "parts_total": it["parts_total"]}

        before = entries[pos - 1] if pos > 0 and isinstance(entries[pos - 1], dict) else None
        cliff = before.get("cliffhanger") if before and isinstance(before.get("cliffhanger"), str) else prev_cliff

        def retry(bad: dict, over: list[str], it=it, cliff=cliff) -> dict | None:
            new = first_card(run.call(prompt_for([it], cliff) + retry_note(bad, over, "PART")))
            return prepare(new) if isinstance(new, dict) else None

        result, _ = finish_card(run, prepare(raw), "en", "lesson", f"{label}#{it['part']}", retry)
        if result is None:
            continue
        card, problems = result
        if card["source"]:
            # No article to check a lesson quote against, so don't risk an invented one.
            problems.append("dropped unverifiable source on lesson card")
            card["source"] = None
        for p in problems:
            log.info("lesson card fix: %s", p)
        card.update(kind="lesson", lang="en", site=None)
        ids.append(insert_card(con, card))
        con.commit()
    return ids


# ---------- batch ----------

def lesson_buffer(con) -> int:
    return con.execute("SELECT COUNT(*) FROM cards WHERE seen_at IS NULL "
                       "AND json_extract(data, '$.kind') = 'lesson'").fetchone()[0]


def run(target: int, max_calls: int, exact: bool) -> int:
    """Lesson calls, rotating roadmaps, until `target` cards are in or `max_calls` are spent
    (retries count too). With `exact`, each call asks only for the cards still needed."""
    global LAST_RUN
    con = connect()
    r = LAST_RUN = Run(max_calls)
    inserted = 0
    turn = int(meta_get(con, "lesson_rotation", "0"))
    roadmaps = set(LESSON_ROTATION)
    done: set[str] = set()
    try:
        while inserted < target and r.can_call():
            if done == roadmaps:
                log.warning("all roadmaps finished; add items to roadmaps/*.md")
                break
            count = min(BATCH_SIZE, target - inserted) if exact else BATCH_SIZE
            roadmap = LESSON_ROTATION[turn % len(LESSON_ROTATION)]
            turn += 1
            meta_set(con, "lesson_rotation", str(turn % len(LESSON_ROTATION)))
            if roadmap in done:
                continue
            ids = make_lesson_cards(con, r, roadmap, count)
            if ids is None:
                log.info("roadmap finished: add items to roadmaps/%s.md", roadmap)
                done.add(roadmap)
                continue
            inserted += len(ids)
            log.info("after call %d (lesson/%s): inserted %d cards %s", r.calls, roadmap, len(ids), ids)
            con.commit()
        if r.error:
            raise r.error  # a retry call failed: handled like any failed call below
        meta_set(con, "last_error", None)
    except ClaudeError as e:
        # Pro usage limit or any CLI failure: stop the batch, the next cron run retries.
        log.error("stopping after %d cards: %s%s", inserted, e, " (usage limit)" if e.limited else "")
        meta_set(con, "last_error", f"{iso(now())} {e}")
    finally:
        con.commit()
        con.close()
    log.info("lesson summary: %s", r.summary(inserted))
    return inserted


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--limit", type=int, help="insert exactly N lesson cards (plus new articles)")
    g.add_argument("--nightly", action="store_true")
    g.add_argument("--topup", action="store_true")
    g.add_argument("--articles-only", action="store_true", help="feed articles only, no model calls")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(message)s")

    lock = open(LOG_DIR / "ingest.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.info("another ingest is running; exiting")
        return 0

    init_db()
    con = connect()
    try:
        ingest_articles(con)
        buf = lesson_buffer(con)
        meta_set(con, "last_ingest", iso(now()))
        con.commit()
    finally:
        con.close()
    if args.articles_only:
        return 0
    if args.limit is not None:
        target, max_calls = args.limit, args.limit + 2  # bounded even if many cards drop
    elif args.topup and buf >= LESSON_TOPUP_THRESHOLD:
        log.info("lesson buffer %d >= %d, no lesson top-up needed", buf, LESSON_TOPUP_THRESHOLD)
        return 0
    else:
        target = max(0, LESSON_BUFFER_TARGET - buf)
        max_calls = TOPUP_MAX_CALLS if args.topup else NIGHTLY_CALLS
    log.info("lesson buffer %d, want %d lessons, at most %d calls (model %s, %d per call)",
             buf, target, max_calls, CLAUDE_MODEL, BATCH_SIZE)
    n = run(target, max_calls, exact=args.limit is not None) if target > 0 else 0
    log.info("done: inserted %d lesson cards", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
