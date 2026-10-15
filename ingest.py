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

import feedparser

from cards import Invalid, quote_is_verbatim, validate_card
from core import (BATCH_SIZE, BUFFER_TARGET, CLAUDE_MODEL, LOG_DIR, NIGHTLY_CALLS, RARE_RATE, ROOT,
                  TOPICS, TOPUP_MAX_CALLS, TOPUP_THRESHOLD,
                  ClaudeError, ask_claude, connect, defang, fill, init_db, iso, load_prompt,
                  meta_get, meta_set, now, parse_json_reply)

log = logging.getLogger("studyfeed.ingest")
UA = "StudyFeed/1.0 (personal RSS reader)"
MAX_BYTES = 2_000_000
ARTICLE_CHARS = 4000  # per article; 5 articles share one prompt


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
    feeds = [l.strip() for l in (ROOT / "feeds.txt").read_text().splitlines()
             if l.strip() and not l.strip().startswith("#")]
    per_feed = []
    for feed in feeds:
        try:
            parsed = feedparser.parse(http_get(feed))
            if parsed.bozo and not parsed.entries:
                raise ValueError(str(parsed.bozo_exception))
        except Exception as e:
            log.warning("feed failed, skipping: %s (%s)", feed, e)
            continue
        fresh = []
        for e in parsed.entries[:15]:
            link = e.get("link")
            if not link or con.execute("SELECT 1 FROM sources WHERE url_hash = ?",
                                       (url_hash(link),)).fetchone():
                continue
            summary = e.get("summary") or ""
            if e.get("content"):
                summary = max((c.get("value", "") for c in e.content), key=len, default=summary)
            fresh.append({"url": link, "feed": feed, "title": html_to_text(e.get("title", "")),
                          "text": html_to_text(summary)})
        per_feed.append(fresh)
    out = []
    while any(per_feed):
        for lst in per_feed:
            if lst:
                out.append(lst.pop(0))
    return out


def article_text(item: dict) -> str:
    """Feed summary, or the page itself when the summary is too thin (e.g. HN links)."""
    text = item["text"]
    if len(text) < 400:
        try:
            page = http_get(item["url"]).decode("utf-8", "replace")
            text = (text + " " + html_to_text(page)).strip()
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
    return (f'<article n="{n}" url="{url}" feed="{feed}">\n{defang(item["title"])}\n\n'
            f'{defang(item["text"])}\n</article>')


def make_news_cards(con, items: list[dict]) -> list[int]:
    """One call for up to BATCH_SIZE articles. Returns new card ids. Raises ClaudeError."""
    for it in items:
        it["text"] = article_text(it)
    prompt = fill(load_prompt("news_card.md"),
                  articles="\n\n".join(article_block(i + 1, it) for i, it in enumerate(items)))
    reply = ask_claude(prompt + visual_shapes())

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
        log_rejected("news-batch", str(e), reply)
        return []
    if len(entries) != len(items):
        log.warning("news reply has %d entries for %d articles; matching by source url",
                    len(entries), len(items))
    by_url = {it["url"]: i for i, it in enumerate(items)}

    ids = []
    for pos, raw in enumerate(entries):
        if isinstance(raw, dict) and raw.get("skip") is True:
            if len(entries) == len(items):
                log.info("skipped (not IT / too thin): %s", items[pos]["url"])
            continue
        if len(entries) == len(items):
            i = pos
        else:
            i = by_url.get(((raw or {}).get("source") or {}).get("url") if isinstance(raw, dict) else None)
            if i is None:
                log_rejected("news", "cannot match card to an article", raw)
                continue
        item = items[i]
        try:
            raw["series_id"] = raw["part"] = raw["parts_total"] = None
            problems = []
            card = validate_card(raw, problems)
        except (TypeError, Invalid) as e:
            log_rejected("news", f"{item['url']}: {e}", raw)
            continue
        if card["source"]:
            # Never serve an invented quote, and pin the URL to the real article.
            if quote_is_verbatim(card["source"]["quote"], item["title"] + " " + item["text"]):
                card["source"]["url"] = item["url"]
            else:
                problems.append("dropped source: quote not found verbatim in article")
                card["source"] = None
        for p in problems:
            log.info("news card fix: %s", p)
        ids.append(insert_card(con, card, source_ids[i]))
    con.commit()
    return ids


def make_lesson_cards(con, topic: str, count: int):
    """One call for the next `count` parts of the current series (never crossing into the
    next series). Returns new card ids, or None if the roadmap is finished."""
    items = load_roadmap(topic)
    idx = con.execute("SELECT next_item FROM topics WHERE topic = ?", (topic,)).fetchone()[0]
    if idx >= len(items):
        return None
    first = items[idx]
    parts = [it for it in items[idx:idx + count] if it["series_id"] == first["series_id"]]
    prev = con.execute("SELECT data FROM cards WHERE series_id = ? AND part = ? AND topic = ? "
                       "ORDER BY id DESC LIMIT 1",
                       (first["series_id"], first["part"] - 1, topic)).fetchone()
    prev_cliff = json.loads(prev["data"])["cliffhanger"] if prev else "none (this is the first part)"
    template = load_prompt("lesson_card.md").split("\n---\n")[0]
    prompt = fill(template, count=len(parts), topic=topic, series_id=first["series_id"],
                  parts_total=first["parts_total"],
                  parts_list="\n".join(f"- Part {it['part']}: {it['text']}" for it in parts),
                  previous_cliffhanger=defang(prev_cliff))
    reply = ask_claude(prompt + visual_shapes())

    # Advance past every part sent, even dropped ones: a batch is never retried.
    con.execute("UPDATE topics SET next_item = next_item + ? WHERE topic = ?", (len(parts), topic))
    con.commit()
    label = f"{topic}/{first['series_id']}"
    try:
        entries = parse_json_reply(reply, "[")
        if not isinstance(entries, list):
            raise ValueError("reply is not a JSON array")
    except ValueError as e:
        log_rejected("lesson-batch", f"{label}: {e}", reply)
        return []
    wanted = {it["part"]: it for it in parts}
    ids, used = [], set()
    for pos, raw in enumerate(entries):
        if len(entries) == len(parts):
            it = parts[pos]
        else:
            it = wanted.get(raw.get("part")) if isinstance(raw, dict) else None
            if it is None or it["part"] in used:
                log_rejected("lesson", f"{label}: cannot match card to a requested part", raw)
                continue
        used.add(it["part"])
        try:
            raw.update(topic=topic, series_id=it["series_id"], part=it["part"],
                       parts_total=it["parts_total"])
            problems = []
            card = validate_card(raw, problems)
        except (AttributeError, Invalid) as e:
            log_rejected("lesson", f"{label}#{it['part']}: {e}", raw)
            continue
        if card["source"]:
            # No article to check a lesson quote against, so don't risk an invented one.
            problems.append("dropped unverifiable source on lesson card")
            card["source"] = None
        for p in problems:
            log.info("lesson card fix: %s", p)
        ids.append(insert_card(con, card))
    con.commit()
    return ids


# ---------- batch ----------

def buffer_size(con) -> int:
    return con.execute("SELECT COUNT(*) FROM cards WHERE seen_at IS NULL").fetchone()[0]


def run(target: int, max_calls: int, exact: bool) -> int:
    """Alternate news and lesson calls until `target` cards are in or `max_calls` are spent.
    With `exact`, each call asks only for the cards still needed, so it never overshoots."""
    con = connect()
    inserted = calls = 0
    news_queue = None
    topic_turn = int(meta_get(con, "lesson_rotation", "0"))
    want_news = meta_get(con, "next_is_news", "1") == "1"
    done_topics: set[str] = set()
    news_empty = False
    try:
        while inserted < target and calls < max_calls:
            if news_empty and len(done_topics) == len(TOPICS):
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
                calls += 1
                ids = make_news_cards(con, batch)
                kind = "news"
            else:
                if len(done_topics) == len(TOPICS):
                    want_news = True  # every roadmap finished: news only
                    continue
                topic = TOPICS[topic_turn % len(TOPICS)]
                topic_turn += 1
                meta_set(con, "lesson_rotation", str(topic_turn % len(TOPICS)))
                if topic in done_topics:
                    continue
                calls += 1
                ids = make_lesson_cards(con, topic, count)
                if ids is None:
                    calls -= 1  # no call was made
                    log.info("roadmap finished for %s; add items to roadmaps/%s.md", topic, topic)
                    done_topics.add(topic)
                    continue
                kind = f"lesson/{topic}"
            inserted += len(ids)
            log.info("call %d (%s): inserted %d cards %s", calls, kind, len(ids), ids)
            want_news = not want_news
            meta_set(con, "next_is_news", "1" if want_news else "0")
            con.commit()
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
