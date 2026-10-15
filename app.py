"""Study Feed API. Contract: API.md. Bound to the Tailscale IP only (see run.sh)."""
import asyncio
import json
import logging
import random
import re
import tempfile
import threading
from contextlib import asynccontextmanager, contextmanager
from datetime import timedelta
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import progress as P
from cards import LIMITS, is_rfc_source
from core import (TOPICS, ClaudeError, ask_claude, claude_args, claude_env, connect, defang, fill,
                  init_db, iso, load_prompt, meta_get, now, parse_json_reply, start_of_day)

log = logging.getLogger("studyfeed.app")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
FLASHCARD_MSG = "Make it a flashcard"
# Confirmation prefixes; also used to spot a flashcard already saved from a thread.
FLASHCARD_SAVED = {"en": "Saved as a flashcard (card {id}).", "ja": "フラッシュカードに保存しました（カード {id}）。"}
FLASHCARD_SAVED_RE = re.compile(r"^(?:Saved as a flashcard \(card (\d+)\)\.|フラッシュカードに保存しました（カード (\d+)）。)")


@asynccontextmanager
async def lifespan(_):
    init_db()
    yield


app = FastAPI(title="Study Feed", lifespan=lifespan)


# ---------- helpers ----------

@contextmanager
def db():
    con = connect()
    try:
        yield con
        con.commit()
    finally:
        con.close()


def card_out(row, is_review: bool | None = None) -> dict:
    card = json.loads(row["data"])
    card.update(id=row["id"], rare=bool(row["rare"]), created_at=row["created_at"],
                deeper=row["deeper"])
    # kind/lang/site are stored on the card; defaults only matter for very old rows.
    card.setdefault("kind", "news" if row["source_id"] else "lesson")
    card.setdefault("lang", "en")
    card.setdefault("site", None)
    if is_review is not None:
        card["is_review"] = is_review
    return card


def get_card_row(con, card_id: int):
    row = con.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
    if not row:
        raise HTTPException(404, "card not found")
    return row


def due_rows(con, limit=None, exclude=()):
    q = ("SELECT c.* FROM cards c JOIN reviews r ON r.card_id = c.id WHERE r.due_at <= ? "
         f"AND c.id NOT IN ({','.join('?' * len(exclude))}) ORDER BY r.due_at")
    args = [iso(now()), *exclude]
    if limit:
        q += " LIMIT ?"
        args.append(limit)
    return con.execute(q, args).fetchall()


# ---------- feed ----------

@app.get("/feed")
def feed(cursor: int | None = None, limit: int = Query(10, ge=1, le=50)):
    with db() as con:
        unseen = con.execute(
            "SELECT * FROM cards WHERE seen_at IS NULL AND id < ? ORDER BY id DESC LIMIT ?",
            (cursor if cursor is not None else 2**62, limit)).fetchall()
        next_cursor = unseen[-1]["id"] if unseen else None
        out = [card_out(r, False) for r in unseen]
        used = {r["id"] for r in unseen}

        # About 1 in 6 items is a due review, picked at random so pages don't repeat it.
        due = list(due_rows(con, exclude=tuple(used)))
        random.shuffle(due)
        mixed = []
        for c in out:
            if len(mixed) % 6 == 5 and due:
                r = due.pop()
                mixed.append(card_out(r, True))
                used.add(r["id"])
            mixed.append(c)
        out = mixed[:limit]

        # Unseen ran out: due reviews, then oldest seen-but-not-mastered, then anything.
        if len(out) < limit:
            for r in due:
                if len(out) >= limit:
                    break
                out.append(card_out(r, True))
                used.add(r["id"])
        if len(out) < limit:
            ph = ",".join("?" * len(used))
            rows = con.execute(
                "SELECT c.* FROM cards c LEFT JOIN reviews r ON r.card_id = c.id "
                "LEFT JOIN answers a ON a.card_id = c.id "
                "WHERE c.seen_at IS NOT NULL "
                "AND NOT EXISTS (SELECT 1 FROM reactions x WHERE x.card_id = c.id AND x.kind = 'got_it') "
                "AND NOT (COALESCE(r.interval_d, 0) >= 21) "
                "AND NOT (COALESCE(a.correct, 0) = 1 AND r.card_id IS NULL) "
                f"AND c.id NOT IN ({ph}) ORDER BY c.seen_at LIMIT ?",
                (*used, limit - len(out))).fetchall()
            for r in rows:
                out.append(card_out(r, False))
                used.add(r["id"])
        if not out:
            rows = con.execute("SELECT * FROM cards ORDER BY seen_at LIMIT ?", (limit,)).fetchall()
            out = [card_out(r, False) for r in rows]
        return {"cards": out, "next_cursor": next_cursor}


class CardRef(BaseModel):
    card_id: int


@app.post("/seen")
def seen(body: CardRef):
    with db() as con:
        row = get_card_row(con, body.card_id)
        badges = []
        if row["seen_at"] is None:
            con.execute("UPDATE cards SET seen_at = ? WHERE id = ?", (iso(now()), body.card_id))
            P.bump_day(con, "cards_seen")
            badges += P.check_seen_badges(con)
            badges += P.check_streak_badges(con, P.streak_info(con)["streak"])
        return {"ok": True, "badges_earned": badges}


class React(BaseModel):
    card_id: int
    kind: Literal["got_it", "save"]
    on: bool


@app.post("/react")
def react(body: React):
    with db() as con:
        get_card_row(con, body.card_id)
        if body.on:
            con.execute("INSERT OR IGNORE INTO reactions (card_id, kind, at) VALUES (?, ?, ?)",
                        (body.card_id, body.kind, iso(now())))
            # Saving a card enrols it in spaced repetition.
            if body.kind == "save" and not con.execute(
                    "SELECT 1 FROM reviews WHERE card_id = ?", (body.card_id,)).fetchone():
                P.schedule_tomorrow(con, body.card_id, lapse=False)
        else:
            con.execute("DELETE FROM reactions WHERE card_id = ? AND kind = ?",
                        (body.card_id, body.kind))
        return {"ok": True, "card_id": body.card_id, "kind": body.kind, "on": body.on}


@app.get("/saved")
def saved():
    with db() as con:
        rows = con.execute("SELECT c.* FROM cards c JOIN reactions x ON x.card_id = c.id "
                           "WHERE x.kind = 'save' ORDER BY x.at DESC").fetchall()
        return {"cards": [card_out(r) for r in rows]}


def preview_text(title: str, max_len: int = 24) -> str:
    if len(title) <= max_len:
        return title.rstrip(".")
    cut = title[:max_len].rsplit(" ", 1)[0]
    return cut.rstrip(",.:;") + "…"


@app.get("/stories")
def stories():
    out = []
    with db() as con:
        for topic in TOPICS:
            row = con.execute(
                "SELECT * FROM cards WHERE topic = ? AND card_type IN ('poster','post') "
                "ORDER BY (seen_at IS NULL) DESC, id DESC LIMIT 1", (topic,)).fetchone()
            if row:
                out.append({"topic": topic, "card_id": row["id"],
                            "preview": preview_text(json.loads(row["data"])["title"]),
                            "unseen": row["seen_at"] is None})
    return {"stories": out}


# ---------- quiz ----------

class Answer(BaseModel):
    card_id: int
    choice: int


@app.post("/answer")
def answer(body: Answer):
    with db() as con:
        row = get_card_row(con, body.card_id)
        card = json.loads(row["data"])
        if not 0 <= body.choice < len(card["quiz"]["options"]):
            raise HTTPException(422, "choice out of range")
        rare = bool(row["rare"])
        prev = con.execute("SELECT * FROM answers WHERE card_id = ?", (body.card_id,)).fetchone()
        badges = []
        if prev:
            correct, xp = bool(prev["correct"]), 0
            info = P.level_info(P.total_xp(con))
            leveled_up = False
            streak = P.streak_info(con)["streak"]
        else:
            correct = body.choice == card["quiz"]["answer"]
            xp = (20 if rare else 10) if correct else 0
            before = P.level_info(P.total_xp(con))["level"]
            con.execute("INSERT INTO answers (card_id, choice, correct, xp, answered_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (body.card_id, body.choice, int(correct), xp, iso(now())))
            P.bump_day(con, "quizzes")
            if not correct:
                P.schedule_tomorrow(con, body.card_id, lapse=True)
            info = P.level_info(P.total_xp(con))
            leveled_up = info["level"] > before
            if correct and rare and P.award(con, "first-rare-card"):
                badges.append("first-rare-card")
            streak = P.streak_info(con)["streak"]
            badges += P.check_streak_badges(con, streak)
        return {"correct": correct, "xp": xp, "rare_bonus": rare and correct and xp > 0,
                **info, "leveled_up": leveled_up, "streak": streak, "badges_earned": badges}


# ---------- review ----------

@app.get("/review")
def review_list():
    with db() as con:
        rows = due_rows(con)
        cards = []
        for r in rows:
            c = card_out(r, True)
            c["preview_intervals"] = P.preview_intervals(con, r["id"])
            cards.append(c)
        return {"due": len(rows), "cards": cards}


class ReviewIn(BaseModel):
    card_id: int
    rating: Literal["again", "hard", "good", "easy"]


@app.post("/review")
def review_post(body: ReviewIn):
    with db() as con:
        get_card_row(con, body.card_id)
        due, interval_d = P.apply_review(con, body.card_id, body.rating)
        due_left = len(due_rows(con))
        badges = P.note_review_for_sweep(con, body.rating, due_left)
        interval = int(interval_d) if interval_d == int(interval_d) else interval_d
        return {"next_due_at": iso(due), "interval_days": interval, "badges_earned": badges}


# ---------- deeper ----------

_deeper_locks: dict[int, threading.Lock] = {}
_locks_guard = threading.Lock()


def deeper_size(text: str, lang: str) -> int:
    """English is capped in words (250); Japanese has no spaces, so in characters (600)."""
    return len(re.sub(r"\s", "", text)) if lang == "ja" else len(text.split())


def clean_deeper(md: str, lang: str = "en") -> str:
    cap = 600 if lang == "ja" else 250
    md = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", md)            # no images
    md = re.sub(r"(?is)<\s*(img|script|iframe|svg|object|embed)\b[^>]*>", "", md)
    md = md.strip()
    if md.startswith("```") and md.endswith("```") and md.count("```") == 2:
        md = re.sub(r"^```\w*\n|\n?```$", "", md).strip()   # model wrapped it all in a fence
    # Enforce the cap by dropping whole trailing blocks (never split a code fence).
    blocks, cur, in_code = [], [], False
    for line in md.splitlines():
        if line.strip().startswith("```"):
            in_code = not in_code
        if not line.strip() and not in_code:
            if cur:
                blocks.append("\n".join(cur))
                cur = []
        else:
            cur.append(line)
    if cur:
        blocks.append("\n".join(cur))
    out, words = [], 0
    for b in blocks:
        n = 0 if b.lstrip().startswith("```") else deeper_size(b, lang)  # code doesn't count
        if words + n > cap and out:
            break
        out.append(b)
        words += n
    return "\n\n".join(out)


@app.get("/cards/{card_id}/deeper")
def deeper(card_id: int):
    with _locks_guard:
        lock = _deeper_locks.setdefault(card_id, threading.Lock())
    with lock:
        with db() as con:
            row = get_card_row(con, card_id)
            card = json.loads(row["data"])
            md = row["deeper"]
        if md is None:
            section = load_prompt("lesson_card.md").split("\n---\n", 1)[1]
            section = section.split("\n", 2)[2] if section.lstrip().startswith("DEEPER MODE") else section
            prompt = fill(section.strip(), card_json=defang(json.dumps(card, ensure_ascii=False)))
            try:
                md = clean_deeper(ask_claude(prompt), card.get("lang", "en"))
            except ClaudeError as e:
                log.error("deeper failed for card %d: %s", card_id, e)
                raise HTTPException(503, "Claude is unavailable right now (usage limit?). Try later."
                                    if e.limited else "Could not generate deeper content.")
            with db() as con:
                con.execute("UPDATE cards SET deeper = ? WHERE id = ?", (md, card_id))
        if is_rfc_source(card):
            with db() as con:
                P.award(con, "read-an-rfc")
        return {"markdown": md}


# ---------- ask ----------

@app.get("/cards/{card_id}/thread")
def thread(card_id: int):
    with db() as con:
        get_card_row(con, card_id)
        rows = con.execute("SELECT role, text, at FROM messages WHERE card_id = ? ORDER BY id",
                           (card_id,)).fetchall()
        return {"messages": [dict(r) for r in rows]}


class AskIn(BaseModel):
    card_id: int
    message: str


def sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


SUGGEST_RE = re.compile(r"\n?[ \t]*SUGGESTIONS:\s*(\[.*?\])\s*$", re.S)


def split_suggestions(text: str):
    m = SUGGEST_RE.search(text)
    if not m:
        cut = text.find("SUGGESTIONS:")
        return (text[:cut] if cut >= 0 else text).strip(), []
    try:
        sugg = [s for s in json.loads(m.group(1)) if isinstance(s, str)][:3]
    except json.JSONDecodeError:
        sugg = []
    return text[:m.start()].strip(), sugg


def make_flashcard_card(con, parent_id: int, parent: dict, fc: dict) -> int:
    lim = LIMITS[parent.get("lang", "en")]
    """A review-only card built from the thread: seen already, due tomorrow."""
    front, back = str(fc["front"]).strip(), str(fc["back"]).strip()
    if not front or not back:
        raise ValueError("empty flashcard")
    card = {**parent, "card_type": "post", "part": None, "parts_total": None,
            "title": front[:lim["title"]], "body": back[:lim["body"]], "source": None, "slides": [],
            "visual": None, "flashcard": {"front": front[:lim["front"]], "back": back[:lim["back"]]}}
    for k in ("id", "rare", "created_at", "deeper", "is_review"):
        card.pop(k, None)
    t = iso(now())
    cur = con.execute("INSERT INTO cards (card_type, topic, series_id, part, rare, data, seen_at, "
                      "created_at) VALUES ('post', ?, ?, NULL, 0, ?, ?, ?)",
                      (card["topic"], card["series_id"], json.dumps(card), t, t))
    P.schedule_tomorrow(con, cur.lastrowid, lapse=False)
    return cur.lastrowid


_flashcard_locks: dict[int, asyncio.Lock] = {}


def saved_flashcard_reply(card_id: int) -> str | None:
    """The latest confirmation for a flashcard saved from this card's thread, if that
    flashcard card still exists."""
    with db() as con:
        for m in con.execute("SELECT text FROM messages WHERE card_id = ? AND role = 'assistant' "
                             "ORDER BY id DESC", (card_id,)):
            match = FLASHCARD_SAVED_RE.match(m["text"])
            if match and con.execute("SELECT 1 FROM cards WHERE id = ?",
                                     (int(match.group(1) or match.group(2)),)).fetchone():
                return m["text"]
    return None


@app.post("/ask")
async def ask(body: AskIn):
    message = body.message.strip()
    if not message or len(message) > 2000:
        raise HTTPException(422, "message must be 1-2000 chars")
    with db() as con:
        row = get_card_row(con, body.card_id)
        card = card_out(row)
        history = con.execute("SELECT role, text FROM messages WHERE card_id = ? "
                              "ORDER BY id DESC LIMIT 10", (body.card_id,)).fetchall()[::-1]
        con.execute("INSERT INTO messages (card_id, role, text, at) VALUES (?, 'user', ?, ?)",
                    (body.card_id, message, iso(now())))
    card.pop("deeper", None)
    ja = card.get("lang") == "ja"  # replies to a Japanese card are in Japanese
    thread_txt = "\n".join(f"{m['role']}: {m['text']}" for m in history) or "(none)"
    prompt = fill(load_prompt("ask.md"), card_json=defang(json.dumps(card, ensure_ascii=False)),
                  last_10_messages=defang(thread_txt), message=defang(message))

    def store_reply(text: str):
        with db() as con:
            con.execute("INSERT INTO messages (card_id, role, text, at) VALUES (?, 'assistant', ?, ?)",
                        (body.card_id, text, iso(now())))

    if message == FLASHCARD_MSG:
        async def flashcard_stream():
            # One flashcard per thread: a lock stops a double tap from making two.
            async with _flashcard_locks.setdefault(body.card_id, asyncio.Lock()):
                existing = saved_flashcard_reply(body.card_id)
                if existing:
                    text = existing  # already saved: repeat its confirmation, no Claude call
                else:
                    try:
                        reply = await asyncio.to_thread(ask_claude, prompt)
                        fc = parse_json_reply(reply)
                        with db() as con:
                            new_id = make_flashcard_card(con, body.card_id, card, fc)
                        saved = FLASHCARD_SAVED["ja" if ja else "en"].format(id=new_id)
                        text = (f"{saved}明日の復習に出てきます。\n\n表: {fc['front']}\n裏: {fc['back']}" if ja else
                                f"{saved} It shows up in Review tomorrow.\n\n"
                                f"Front: {fc['front']}\nBack: {fc['back']}")
                    except (ClaudeError, ValueError, KeyError, TypeError) as e:
                        log.error("flashcard failed: %s", e)
                        text = ("すみません、今はフラッシュカードを作れませんでした。しばらくしてからもう一度お試しください。"
                                if ja else "Sorry, I couldn't make a flashcard right now. Try again later.")
                store_reply(text)
            yield sse("token", {"text": text})
            yield sse("suggestions", [])
            yield sse("done", {})
        return StreamingResponse(flashcard_stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    async def stream():
        full, sent = "", 0
        tmp = tempfile.TemporaryDirectory(prefix="studyfeed-ask-")
        proc = await asyncio.create_subprocess_exec(
            *claude_args("stream-json"), cwd=tmp.name, env=claude_env(),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=2**22)
        proc.stdin.write(prompt.encode())
        await proc.stdin.drain()
        proc.stdin.close()
        error = None
        try:
            async for line in proc.stdout:
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("type") == "stream_event":
                    delta = ev["event"].get("delta") or {}
                    if delta.get("type") == "text_delta":
                        full += delta["text"]
                        # Hold back anything that might be the start of the SUGGESTIONS line.
                        cut = full.find("SUGGESTIONS:")
                        if cut < 0:
                            tail_start = full.rfind("\n") + 1
                            tail = full[tail_start:].lstrip()
                            cut = tail_start if tail and "SUGGESTIONS:".startswith(tail) else len(full)
                        if cut > sent:
                            yield sse("token", {"text": full[sent:cut]})
                            sent = cut
                elif ev.get("type") == "result" and ev.get("is_error"):
                    error = str(ev.get("result"))[:300]
            await proc.wait()
            if proc.returncode and not full:
                error = error or (await proc.stderr.read()).decode()[:300] or "claude failed"
        except asyncio.CancelledError:
            proc.kill()
            raise
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            tmp.cleanup()
        answer_text, sugg = split_suggestions(full)
        if len(answer_text) > sent:
            yield sse("token", {"text": answer_text[sent:]})
        if error and not answer_text:
            log.error("ask failed: %s", error)
            answer_text = ("すみません、今は Claude を利用できません（利用上限の可能性があります）。しばらくしてからお試しください。"
                           if ja else
                           "Sorry, Claude is unavailable right now (possibly a usage limit). Try again later.")
            yield sse("token", {"text": answer_text})
        store_reply(answer_text)
        yield sse("suggestions", sugg)
        yield sse("done", {})

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------- stats / health ----------

@app.get("/stats")
def stats():
    with db() as con:
        s = P.streak_info(con)
        P.check_streak_badges(con, s["streak"])
        t = now()
        week_start = start_of_day(t - timedelta(days=t.weekday()))
        last_start = week_start - timedelta(days=7)
        count = lambda a, b: con.execute(
            "SELECT COUNT(*) FROM cards WHERE seen_at >= ? AND seen_at < ?",
            (iso(a), iso(b))).fetchone()[0]
        badges = [{"id": b["id"], "name": b["name"], "earned": b["earned_at"] is not None,
                   "earned_at": b["earned_at"]}
                  for b in con.execute("SELECT * FROM badges ORDER BY rowid")]
        return {**s, **P.level_info(P.total_xp(con)),
                "week_cards": count(week_start, t + timedelta(seconds=1)),
                "last_week_cards": count(last_start, week_start),
                "badges": badges}


@app.get("/health")
def health():
    with db() as con:
        buf = con.execute("SELECT COUNT(*) FROM cards WHERE seen_at IS NULL").fetchone()[0]
        return {"ok": True, "buffer": buf, "last_ingest": meta_get(con, "last_ingest"),
                "last_error": meta_get(con, "last_error")}
