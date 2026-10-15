"""Generate cards into the buffer. Run by cron; see README.md.

Each `claude -p` call makes BATCH_SIZE cards (5 articles, or 5 parts of one series).

  python ingest.py --limit 5   # insert N new cards now (ignores the buffer)
  python ingest.py --nightly   # fill toward BUFFER_TARGET, at most NIGHTLY_CALLS calls
  python ingest.py --topup     # only if the buffer is under 60, at most TOPUP_MAX_CALLS calls
"""
import argparse
import fcntl
import hashlib
import html
import json
import logging
import random
import re
import sys
import urllib.request
from collections import Counter
from urllib.parse import urlparse

import feedparser
import trafilatura

from cards import (Invalid, length_problems, quality_problems, quote_is_verbatim, shorten_card,
                   validate_card)
from core import (BATCH_SIZE, BUFFER_TARGET, CLAUDE_MODEL, LOG_DIR, NIGHTLY_CALLS, RARE_RATE, ROOT,
                  TOPUP_MAX_CALLS, TOPUP_THRESHOLD,
                  ClaudeError, ask_claude, connect, defang, fill, init_db, is_japanese, iso,
                  load_feeds, load_prompt, site_name,
                  meta_get, meta_set, now, parse_json_reply)

log = logging.getLogger("studyfeed.ingest")
UA = "StudyFeed/1.0 (personal RSS reader)"
MAX_BYTES = 2_000_000
ARTICLE_CHARS = 4000  # per article; 5 articles share one prompt

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


# ---------- feeds ----------

def http_get(url: str, timeout: int = 15) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(MAX_BYTES)


def html_to_text(s: str) -> str:
    s = re.sub(r"(?is)<(script|style|noscript|svg|nav|header|footer)\b.*?</\1>", " ", s or "")
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def url_hash(url: str) -> str:
    return hashlib.sha256(url.strip().encode()).hexdigest()


def candidate_items(con) -> list[dict]:
    """New feed entries, round-robin across feeds so one feed can't dominate."""
    per_feed = []
    for feed, tags in load_feeds():
        try:
            parsed = feedparser.parse(http_get(feed))
            if parsed.bozo and not parsed.entries:
                raise ValueError(str(parsed.bozo_exception))
        except Exception as e:
            log.warning("feed failed, skipping: %s (%s)", feed, e)
            continue
        feed_name = html_to_text(parsed.feed.get("title", "")) or urlparse(feed).hostname or feed
        fresh = []
        for e in parsed.entries[:15]:
            link = e.get("link")
            if not link or con.execute("SELECT 1 FROM sources WHERE url_hash = ?",
                                       (url_hash(link),)).fetchone():
                continue
            summary = e.get("summary") or ""
            if e.get("content"):
                summary = max((c.get("value", "") for c in e.content), key=len, default=summary)
            fresh.append({"url": link, "feed": feed, "feed_name": feed_name[:120], "ios": "ios" in tags,
                          "title": html_to_text(e.get("title", "")),
                          "text": html_to_text(summary)})
        per_feed.append(fresh)
    # A news call takes only BATCH_SIZE items, so a fixed order would always favour the
    # feeds at the top of feeds.txt. Shuffle so every feed gets a turn.
    random.shuffle(per_feed)
    out = []
    while any(per_feed):
        for lst in per_feed:
            if lst:
                out.append(lst.pop(0))
    return out


def extract_article(page: bytes, url: str) -> str | None:
    """Main article body only (no menus, ads or comments), via trafilatura."""
    body = trafilatura.extract(page, url=url, include_comments=False, include_images=False,
                               include_links=False, deduplicate=True)
    return re.sub(r"\s+", " ", body).strip() if body else None


def article_text(item: dict) -> str:
    """Feed summary, or the article body itself when the summary is too thin (e.g. HN links)."""
    text = item["text"]
    if len(text) < 400:
        try:
            body = extract_article(http_get(item["url"]), item["url"])
            if body:
                text = (text + " " + body).strip()
            else:
                log.info("no article body found at %s; using the feed summary", item["url"])
        except Exception as e:
            log.info("could not fetch article %s (%s)", item["url"], e)
    return text[:ARTICLE_CHARS]


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


def article_block(n: int, item: dict) -> str:
    url = defang(item["url"]).replace('"', "%22")
    feed = defang(item["feed"]).replace('"', "%22")
    return (f'<article n="{n}" url="{url}" feed="{feed}" lang="{item["lang"]}">\n{defang(item["title"])}\n\n'
            f'{defang(item["text"])}\n</article>')


def news_source(model_src, item: dict, problems: list) -> dict | None:
    """Every news card links its article. The model's label and quote are kept only when
    the quote appears verbatim in the article; otherwise the source is {feed name, url}."""
    url = item["url"]
    if not url.startswith(("http://", "https://")):
        problems.append("no source: article url is not http(s)")
        return None
    src = model_src if isinstance(model_src, dict) else {}
    label, quote = src.get("label"), src.get("quote")
    if (isinstance(label, str) and 0 < len(label.strip()) <= 120
            and isinstance(quote, str) and len(quote.strip()) <= 600
            and quote_is_verbatim(quote, item["title"] + " " + item["text"])):
        return {"label": label.strip(), "quote": quote.strip(), "url": url}
    if quote:
        problems.append("dropped quote: not found verbatim in article")
    return {"label": item["feed_name"], "url": url}


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


def make_news_cards(con, run: Run, items: list[dict]) -> list[int]:
    """One call for up to BATCH_SIZE articles. Returns new card ids. Raises ClaudeError."""
    for it in items:
        it["text"] = article_text(it)
        # Each article's own language; a card is never judged by the rest of its batch.
        it["lang"] = "ja" if is_japanese(it["title"] + " " + it["text"]) else "en"
    template = load_prompt("news_card.md")
    reply = run.call(fill(template, articles="\n\n".join(article_block(i + 1, it)
                                                          for i, it in enumerate(items))) + visual_shapes())

    # The call succeeded: mark every article used whatever the outcome, so none loops forever.
    source_ids = []
    for it in items:
        cur = con.execute("INSERT OR IGNORE INTO sources (url_hash, url, feed, fetched_at) "
                          "VALUES (?, ?, ?, ?)", (url_hash(it["url"]), it["url"], it["feed"], iso(now())))
        source_ids.append(cur.lastrowid if cur.rowcount else None)
    con.commit()
    try:
        entries = parse_json_reply(reply, "[")
        if not isinstance(entries, list):
            raise ValueError("reply is not a JSON array")
    except ValueError as e:
        run.drop("news-batch", f"bad reply: {e}", "whole batch", reply)
        return []
    if len(entries) != len(items):
        log.warning("news reply has %d entries for %d articles; matching by source url",
                    len(entries), len(items))
    by_url = {it["url"]: i for i, it in enumerate(items)}

    def prepare(raw: dict) -> dict:
        # The server builds the source itself (below), so a bad one never drops the card.
        raw = dict(raw)
        raw["_source"] = raw.pop("source", None)
        raw.update(source=None, series_id=None, part=None, parts_total=None)
        return raw

    ids = []
    for pos, raw in enumerate(entries):
        if isinstance(raw, dict) and raw.get("skip") is True:
            run.stats["skipped"] += 1
            if len(entries) == len(items):
                log.info("skipped (not IT / too thin): %s", items[pos]["url"])
            continue
        if not isinstance(raw, dict):
            run.drop("news", "entry is not an object", "news", raw)
            continue
        i = pos if len(entries) == len(items) else by_url.get((raw.get("source") or {}).get("url"))
        if i is None:
            run.drop("news", "cannot match card to an article", "news", raw)
            continue
        item = items[i]

        def retry(bad: dict, over: list[str], item=item) -> dict | None:
            new = first_card(run.call(fill(template, articles=article_block(1, item))
                                      + visual_shapes() + retry_note(bad, over, "ARTICLE")))
            return prepare(new) if isinstance(new, dict) and new.get("skip") is not True else new

        result, used = finish_card(run, prepare(raw), item["lang"], "news",
                                   f"{item['url']} [{item['lang']}]", retry)
        if result is None:
            continue
        card, problems = result
        card["source"] = news_source(used.get("_source"), item, problems)
        if item["ios"]:
            card["topic"] = "ios"  # cards from [ios] feeds are always iOS
        # Language comes from this card's own article, never from the rest of the batch.
        card.update(kind="news", lang=item["lang"], site=site_name(item["url"]))
        for p in problems:
            log.info("news card fix: %s", p)
        ids.append(insert_card(con, card, source_ids[i]))
        con.commit()
    return ids


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

def buffer_size(con) -> int:
    return con.execute("SELECT COUNT(*) FROM cards WHERE seen_at IS NULL").fetchone()[0]


def run(target: int, max_calls: int, exact: bool) -> int:
    """Alternate news and lesson calls until `target` cards are in or `max_calls` are spent
    (retries count too). With `exact`, each call asks only for the cards still needed."""
    global LAST_RUN
    con = connect()
    r = LAST_RUN = Run(max_calls)
    inserted = 0
    news_queue = None
    turn = int(meta_get(con, "lesson_rotation", "0"))
    roadmaps = set(LESSON_ROTATION)
    want_news = meta_get(con, "next_is_news", "1") == "1"
    done: set[str] = set()
    news_empty = False
    try:
        while inserted < target and r.can_call():
            if news_empty and done == roadmaps:
                log.warning("no new feed items and all roadmaps finished; stopping")
                break
            count = min(BATCH_SIZE, target - inserted) if exact else BATCH_SIZE
            if want_news and not news_empty:
                if news_queue is None:
                    news_queue = candidate_items(con)
                    log.info("%d new feed items available", len(news_queue))
                if not news_queue:
                    news_empty = True
                    want_news = False
                    continue
                batch, news_queue = news_queue[:count], news_queue[count:]
                ids = make_news_cards(con, r, batch)
                kind = "news"
            else:
                if done == roadmaps:
                    want_news = True  # every roadmap finished: news only
                    continue
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
                kind = f"lesson/{roadmap}"
            inserted += len(ids)
            log.info("after call %d (%s): inserted %d cards %s", r.calls, kind, len(ids), ids)
            want_news = not want_news
            meta_set(con, "next_is_news", "1" if want_news else "0")
            con.commit()
        if r.error:
            raise r.error  # a retry call failed: handled like any failed call below
        meta_set(con, "last_ingest", iso(now()))
        meta_set(con, "last_error", None)
    except ClaudeError as e:
        # Pro usage limit or any CLI failure: stop the batch, the next cron run retries.
        log.error("stopping after %d cards: %s%s", inserted, e, " (usage limit)" if e.limited else "")
        meta_set(con, "last_ingest", iso(now()))
        meta_set(con, "last_error", f"{iso(now())} {e}")
    finally:
        con.commit()
        con.close()
    log.info("summary: %s", r.summary(inserted))
    return inserted


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--limit", type=int, help="insert exactly N cards")
    g.add_argument("--nightly", action="store_true")
    g.add_argument("--topup", action="store_true")
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
    buf = buffer_size(con)
    con.close()
    if args.limit is not None:
        target, max_calls = args.limit, args.limit + 2  # bounded even if many cards drop
    elif args.topup and buf >= TOPUP_THRESHOLD:
        log.info("buffer %d >= %d, no top-up needed", buf, TOPUP_THRESHOLD)
        return 0
    else:
        target = max(0, BUFFER_TARGET - buf)
        max_calls = TOPUP_MAX_CALLS if args.topup else NIGHTLY_CALLS
    log.info("buffer %d, want %d cards, at most %d calls (model %s, %d per call)",
             buf, target, max_calls, CLAUDE_MODEL, BATCH_SIZE)
    n = run(target, max_calls, exact=args.limit is not None) if target > 0 else 0
    log.info("done: inserted %d", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
