# Study Feed API contract

The base URL is `http://<tailscale-ip>:8000`. Every body is JSON. There's no auth, because the tailnet is the boundary. All times are in the server's local timezone.

## Card schema

The server stores this object and returns it as-is. Claude must produce exactly this shape. The server adds `id`, `rare`, `created_at`.

```json
{
  "id": 42,
  "card_type": "poster | post | terminal",
  "topic": "networking | security | ai | swe",
  "series_id": "tcp-basics",
  "part": 4,
  "parts_total": 12,
  "title": "Opening a TCP connection takes 3 packets.",
  "body": "Two prove one direction. The third proves the reply got back.",
  "source": { "label": "RFC 793, 1981", "quote": "The three-way handshake is ...", "url": "https://www.rfc-editor.org/rfc/rfc793" },
  "slides": [ { "title": "...", "body": "...", "visual": null } ],
  "visual": null,
  "cliffhanger": "What if packet 3 is lost?",
  "quiz": {
    "question": "The server's SYN-ACK arrives, but the final ACK is lost. What happens?",
    "options": ["...", "...", "...", "..."],
    "answer": 1,
    "explanation": "The server holds the half-open connection and resends SYN-ACK."
  },
  "flashcard": { "front": "...", "back": "..." },
  "deeper": null,
  "rare": false,
  "created_at": "2026-10-15T03:00:00"
}
```

Rules:
- **`card_type`:**
  - `poster` is used for the first part of a series and for big ideas. It needs 3-5 `slides`, and the last slide carries the cliffhanger.
  - `post` is used for concepts and news. It should include `source` when there is a real one.
  - `terminal` is used for hands-on cards. It needs `visual.kind = "terminal"` or `"code"`.
- **`source`** is optional. Never invent a quote. Omit `source` rather than fabricate one.
- **`title`** is at most 80 chars. **`body`** is at most 280 chars. **`cliffhanger`** is at most 70 chars.
- **`quiz`** is required, with exactly 4 options. **`flashcard`** is required.
- **`deeper`** is null until `/cards/{id}/deeper` fills it with markdown.

`visual` (on the card or on a slide) is one of these shapes:

```json
{ "kind": "steps", "actors": ["client", "server"],
  "steps": [ { "from": 0, "to": 1, "label": "SYN seq=1000", "caption": "...", "state": ["SYN_SENT", "LISTEN"] } ] }

{ "kind": "code", "lang": "python", "lines": ["srv.listen(128)"],
  "notes": [ { "line": 1, "mark": "a", "text": "The backlog..." } ] }

{ "kind": "terminal", "command": "sudo tcpdump -n port 443",
  "lines": ["10.0.0.5 > 93.184.216.34  [S]  seq 1000"],
  "legend": [ { "token": "[S]", "meaning": "SYN, client asks" } ] }

{ "kind": "mermaid", "source": "sequenceDiagram\n  client->>server: SYN" }

{ "kind": "svg", "source": "<svg ...>...</svg>" }
```

The server sanitizes SVG before storing it (see CLAUDE.md).

## Endpoints

### `GET /feed?cursor=<id>&limit=10`
Returns `{ "cards": [Card], "next_cursor": 57 }`, newest unseen first.
- About 1 in 6 items is a due review card mixed in, flagged `"is_review": true`.
- If unseen cards run out, fill with due reviews, then the oldest seen-but-not-mastered cards.
- Never return an empty list while any cards exist.

### `POST /seen` `{ "card_id": 42 }`
Marks the card seen. The app calls this when a card has been on screen for 2 s or more. It counts toward the day's activity.

### `POST /react` `{ "card_id": 42, "kind": "got_it | save", "on": true }`
Toggles reactions. Saved cards appear in `/saved`.

### `GET /saved`
Returns `{ "cards": [Card] }`.

### `GET /stories`
Returns `{ "stories": [ { "topic": "networking", "card_id": 42, "preview": "3 packets", "unseen": true } ] }`, one per topic: the newest unseen poster or post. The app shows them as thumbnails and hides the row on scroll.

### `POST /answer` `{ "card_id": 42, "choice": 1 }`
Returns:

```json
{ "correct": true, "xp": 20, "rare_bonus": true,
  "level": 7, "level_xp": 360, "level_next": 400, "leveled_up": false,
  "streak": 12, "badges_earned": ["first-rare-card"] }
```

- **XP:** a correct answer is worth 10, doubled when `rare`. A wrong answer is worth 0 and schedules a review for tomorrow.
- **Retries:** the first answer per card counts. Later answers return the same result with `xp: 0`.

### `GET /review`
Returns `{ "due": 7, "cards": [Card] }`, which are the cards whose `reviews.due_at <= now`.

### `POST /review` `{ "card_id": 42, "rating": "again | hard | good | easy" }`
Runs SM-2 and returns `{ "next_due_at": "...", "interval_days": 6 }`.
- The app shows the next intervals on the buttons, so `GET /review` cards also include `"preview_intervals": { "again": "10 min", "hard": "2 days", "good": "6 days", "easy": "2 weeks" }`.

### `GET /cards/{id}/deeper`
Returns `{ "markdown": "..." }`.
- It's generated on first request with `prompts/lesson_card.md` in deeper mode, then cached.
- Allowed content: at most 250 words, one optional code block with lettered notes, and no images.

### `GET /cards/{id}/thread`
Returns `{ "messages": [ { "role": "user | assistant", "text": "...", "at": "..." } ] }`.

### `POST /ask` `{ "card_id": 42, "message": "Why not two packets?" }`
Returns an SSE stream:
- `event: token` events with `data: {"text": "..."}`
- then `event: suggestions` with `data: ["What's a SYN flood?", "Make it a flashcard"]`
- then `event: done`.

The server stores both messages in `messages`. Context sent to Claude is the card JSON plus the last 10 messages, using `prompts/ask.md`.
If the user taps "Make it a flashcard", the app sends that text as a message. The server creates a new review card from the conversation and replies with a confirmation.

### `GET /stats`
Returns:

```json
{ "streak": 12, "freezes": 1, "today_done": false,
  "level": 7, "level_xp": 360, "level_next": 400,
  "week_cards": 142, "last_week_cards": 118,
  "badges": [ { "id": "read-an-rfc", "name": "Read an RFC", "earned": true } ] }
```

- **Active day:** a day with at least 1 card seen *and* at least 1 quiz answered.
- **Streak:** the count of consecutive active days ending today, or ending yesterday if today isn't active yet.
- **Freezes:** earn 1 per 7-day streak, holding at most 2. A missed day spends a freeze automatically.
- **Levels:** reaching level n+1 costs `100 + 50*n` XP.
- **Badges** come from the seed list in `schema.sql`.
- The evening "save your streak" nudge is a local notification scheduled by the app, using `today_done`. There's no push server.

### `GET /health`
Returns `{ "ok": true, "buffer": 97, "last_ingest": "...", "last_error": null }`.
