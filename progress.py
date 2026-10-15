"""Spaced repetition (SM-2), XP/levels, streaks/freezes and badges."""
from datetime import date, datetime, timedelta

from core import iso, meta_get, meta_set, now, start_of_day, today

GRADES = {"again": 1, "hard": 3, "good": 4, "easy": 5}
AGAIN_MINUTES = 10


# ---------- SM-2 ----------

def sm2(ease: float, interval_d: float, reps: int, rating: str, at: datetime):
    """Classic SM-2 on grade q, returning (ease, interval_d, reps, due_at).

    q < 3 resets repetitions without changing ease and re-shows the card in 10 minutes.
    Otherwise interval is 1, 6, then round(I * EF). Hard halves and Easy doubles that
    interval, so the four buttons show different times. Day intervals fall due at
    midnight so the card is available all of that day."""
    q = GRADES[rating]
    if q < 3:
        return ease, 0.0, 0, at + timedelta(minutes=AGAIN_MINUTES)
    ease = max(1.3, ease + 0.1 - (5 - q) * (0.08 + (5 - q) * 0.02))
    base = 1 if reps == 0 else 6 if reps == 1 else max(1, round(interval_d * ease))
    if q == 3:
        days = max(1, round(base * 0.5))
    elif q == 5:
        days = max(base + 1, round(base * 2))
    else:
        days = base
    return ease, float(days), reps + 1, start_of_day(at, days)


def humanize(due: datetime, at: datetime) -> str:
    mins = round((due - at).total_seconds() / 60)
    if mins < 60:
        return f"{mins} min"
    days = (due.date() - at.date()).days
    if days < 1:
        return f"{round(mins / 60)} h"
    if days < 14:
        return "1 day" if days == 1 else f"{days} days"
    if days < 60:
        return f"{round(days / 7)} weeks"
    return f"{round(days / 30)} months"


def review_state(con, card_id):
    row = con.execute("SELECT ease, interval_d, reps FROM reviews WHERE card_id = ?",
                      (card_id,)).fetchone()
    return (row["ease"], row["interval_d"], row["reps"]) if row else (2.5, 0.0, 0)


def preview_intervals(con, card_id) -> dict:
    at = now()
    state = review_state(con, card_id)
    return {r: humanize(sm2(*state, r, at)[3], at) for r in GRADES}


def apply_review(con, card_id, rating: str):
    at = now()
    ease, interval_d, reps, due = sm2(*review_state(con, card_id), rating, at)
    upsert_review(con, card_id, ease, interval_d, reps, due)
    return due, interval_d


def upsert_review(con, card_id, ease, interval_d, reps, due: datetime):
    con.execute("INSERT INTO reviews (card_id, ease, interval_d, reps, due_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(card_id) DO UPDATE SET ease=excluded.ease, interval_d=excluded.interval_d, "
                "reps=excluded.reps, due_at=excluded.due_at",
                (card_id, ease, interval_d, reps, iso(due)))


def schedule_tomorrow(con, card_id, lapse: bool):
    """Wrong quiz answer (lapse) or newly enrolled card: due at the start of tomorrow."""
    ease, _, reps = review_state(con, card_id)
    if lapse:
        ease, reps = max(1.3, ease - 0.2), 0
    upsert_review(con, card_id, ease, 1.0, reps, start_of_day(now(), 1))


# ---------- XP and levels ----------

def level_cost(n: int) -> int:
    """XP needed to go from level n to level n+1."""
    return 100 + 50 * n


def level_info(total_xp: int) -> dict:
    level, xp = 1, total_xp
    while xp >= level_cost(level):
        xp -= level_cost(level)
        level += 1
    return {"level": level, "level_xp": xp, "level_next": level_cost(level)}


def total_xp(con) -> int:
    return con.execute("SELECT COALESCE(SUM(xp), 0) FROM answers").fetchone()[0]


# ---------- days, streak, freezes ----------

def bump_day(con, field: str):
    con.execute(f"INSERT INTO days (day, {field}) VALUES (?, 1) "
                f"ON CONFLICT(day) DO UPDATE SET {field} = {field} + 1", (today(),))


def streak_info(con) -> dict:
    """Replay history day by day. A day is active with >=1 card seen and >=1 quiz answered.
    Each 7 active days in a row earn a freeze (max 2). A missed day spends a freeze if one
    is held, which keeps the streak alive without adding to it."""
    rows = {r["day"]: r for r in con.execute("SELECT * FROM days")}
    active = {d for d, r in rows.items() if r["cards_seen"] >= 1 and r["quizzes"] >= 1}
    t = now().date()
    today_done = t.isoformat() in active
    if not active:
        return {"streak": 0, "freezes": 0, "today_done": False}
    d = date.fromisoformat(min(active))
    streak = freezes = run = 0
    while d < t:
        key = d.isoformat()
        if key in active:
            streak += 1
            run += 1
            if run % 7 == 0:
                freezes = min(2, freezes + 1)
        elif streak and freezes:
            freezes -= 1
            run = 0
            con.execute("INSERT INTO days (day, freeze_used) VALUES (?, 1) "
                        "ON CONFLICT(day) DO UPDATE SET freeze_used = 1", (key,))
        else:
            streak = run = 0
        d += timedelta(days=1)
    if today_done:
        streak += 1
        run += 1
        if run % 7 == 0:
            freezes = min(2, freezes + 1)
    return {"streak": streak, "freezes": freezes, "today_done": today_done}


# ---------- badges ----------

def award(con, badge_id: str) -> bool:
    cur = con.execute("UPDATE badges SET earned_at = ? WHERE id = ? AND earned_at IS NULL",
                      (iso(now()), badge_id))
    return cur.rowcount == 1


def check_streak_badges(con, streak: int) -> list[str]:
    earned = []
    for n in (7, 30):
        if streak >= n and award(con, f"streak-{n}"):
            earned.append(f"streak-{n}")
    return earned


def check_seen_badges(con) -> list[str]:
    seen = con.execute("SELECT COUNT(*) FROM cards WHERE seen_at IS NOT NULL").fetchone()[0]
    return ["cards-100"] if seen >= 100 and award(con, "cards-100") else []


def note_review_for_sweep(con, rating: str, due_left: int) -> list[str]:
    """'Clean sweep': clear the review queue with no Again since it last emptied."""
    had_again = meta_get(con, "sweep_again", "0") == "1" or rating == "again"
    if due_left > 0:
        meta_set(con, "sweep_again", "1" if had_again else "0")
        return []
    meta_set(con, "sweep_again", "0")
    return ["perfect-review"] if not had_again and award(con, "perfect-review") else []
