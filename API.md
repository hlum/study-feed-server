# Study Feed API contract

The base URL is `http://<tailscale-ip>:8000`. Every body is JSON. There's no auth, because the tailnet is the boundary. All times are in the server's local timezone.

## Card schema

The server stores this object and returns it as-is. Lesson cards are written by Claude in exactly this shape. The server adds `id`, `rare`, `created_at`, `kind`, `lang`, `site` and `seen`. Feed articles become `article` cards with no model call (see below).

```json
{
  "id": 42,
  "card_type": "poster | post | terminal | article",
  "topic": "networking | security | ai | swe | ios",
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
  "created_at": "2026-10-15T03:00:00",
  "kind": "lesson",
  "lang": "en",
  "site": null,
  "seen": false
}
```

An `article` card, made straight from a feed item with no model call:

```json
{
  "id": 230,
  "card_type": "article",
  "topic": "ios",
  "series_id": null, "part": null, "parts_total": null,
  "title": "Swift Default Value in String Interpolations",
  "body": "Swift 6.2 makes it easier to interpolate strings with optional values.",
  "source": { "label": "Use Your Loaf", "url": "https://useyourloaf.com/blog/...", "quote": null },
  "slides": [], "visual": null,
  "cliffhanger": null, "quiz": null, "flashcard": null,
  "deeper": null, "rare": false, "created_at": "2026-10-16T10:40:00",
  "kind": "news", "lang": "en", "site": "Use Your Loaf",
  "published_at": "2025-09-07T17:51:46",
  "seen": false
}
```

Rules:
- **`card_type`:**
  - `poster` is used for the first part of a series and for big ideas. It needs 3-5 `slides`, and the last slide carries the cliffhanger.
  - `post` is used for concepts and news. It should include `source` when there is a real one.
  - `terminal` is used for hands-on cards. It needs `visual.kind = "terminal"` or `"code"`.
  - `article` is a feed item shown as-is. The app should show its title, body, site and `published_at`, and offer the reader view (`/cards/{id}/reader`) and the original link.
- **`article` cards:**
  - `title` is the feed's own title. It can be longer than 80 characters.
  - `body` is the feed's summary, or else the first paragraph of its content, as plain text. It's cut at a sentence boundary at about 280 characters (140 for Japanese).
  - `source` is `{ "label": <feed name>, "url", "quote": null }`.
  - `published_at` comes from the feed, in server-local time, or `null`.
  - `quiz`, `flashcard` and `cliffhanger` are `null`. `quiz` is filled in by `POST /cards/{id}/quiz` when the app asks for one.
  - `topic` comes from the feed's tag in `feeds.txt`.
  - `lang` is `"ja"` when the title or body contains any hiragana, katakana or kanji.
  - Older model-written news cards keep `card_type` `post` or `terminal`.
- **`source`:** every news card has one, linking its article: `{ "label", "quote", "url" }` when the quote appears verbatim in the article, otherwise `{ "label": <feed name>, "url" }` with no `quote` key. Lesson cards have `source: null`. Never invent a quote.
- **`title`** is at most 80 chars. **`body`** is at most 280 chars. **`cliffhanger`** is at most 70 chars.
  - Japanese cards (`lang: "ja"`) use about half: `title` ≤ 40, `body` ≤ 140, `cliffhanger` ≤ 35 characters (slides likewise).
- **`quiz`** is required, with exactly 4 options. **`flashcard`** is required. Both are `null` on `article` cards. An article's `quiz` appears once `/cards/{id}/quiz` has made one.
- **`deeper`** is null until `/cards/{id}/deeper` fills it with markdown.
- **`topic`:** `ios` covers iOS, Swift, SwiftUI, Xcode and the App Store. Cards from feeds tagged `[ios]` in `feeds.txt` always get `ios`.
- **`kind`:** `"news"` for cards made from a feed article (including `article` cards), `"lesson"` for roadmap cards.
- **`lang`:** the language of the card's source article, and the language the card is written in: `"ja"` for Japanese articles, otherwise `"en"`. Lessons are `"en"`. Each card in a batch is judged on its own article. A `ja` card is entirely Japanese (title, body, slides, cliffhanger, quiz, flashcard, source quote), in polite です・ます style, with common technical terms such as API, async/await and SwiftUI kept in English. `/deeper` and `/ask` replies for a `ja` card are in Japanese too. Treat it as optional in the app.
- **`site`:** for news cards, the article's site, with one normalized spelling per site (e.g. `"Qiita"`, `"Zenn"`, `"Swift by Sundell"`; unknown sites use the bare hostname). `null` for lessons.
- `kind`, `lang`, `site` and `seen` appear on every card the API returns: `/feed`, `/review`, `/saved`. `seen` is true once `POST /seen` has been called for the card.

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

### `GET /feed?cursor=<id>&limit=10&unseen_only=false`
Returns `{ "cards": [Card], "next_cursor": 57 }`. By default this is every card, newest first, seen or not. Each card carries `"seen": true | false`.
- **Paging:** pass `next_cursor` back as `cursor` to get the next, older page. `next_cursor` is `null` once the oldest card has been returned, and the app should stop paging then. That last page is topped up with due reviews.
- About 1 in 6 items is a due review card mixed in, flagged `"is_review": true`. Feed cards have `"is_review": false`. A review card can also appear in its own place in the history.
- The first page is never empty while any cards exist.
- **`unseen_only=true`** gives the old behaviour:
  - newest unseen first;
  - when unseen cards run out, it fills with due reviews, then the oldest seen-but-not-mastered cards;
  - it never returns an empty list while any cards exist.

### `POST /seen` `{ "card_id": 42 }`
Marks the card seen. The app calls this when a card has been on screen for 2 s or more. It counts toward the day's activity.

### `POST /react` `{ "card_id": 42, "kind": "got_it | save", "on": true }`
Toggles reactions. Saved cards appear in `/saved`. Saving also enrols the card in review, but only if it has a flashcard or a quiz. An `article` card without a quiz is saved but not enrolled.

### `GET /saved`
Returns `{ "cards": [Card] }`.

### `GET /stories`
Returns `{ "stories": [ { "topic": "networking", "card_id": 42, "preview": "3 packets", "unseen": true } ] }`, one per topic. Each is the newest unseen poster or post; if the topic has none, its newest unseen article; if everything in the topic is seen, its newest card, with `"unseen": false`. The app shows them as thumbnails and hides the row on scroll.

### `POST /answer` `{ "card_id": 42, "choice": 1 }`
Returns:

```json
{ "correct": true, "xp": 20, "rare_bonus": true,
  "level": 7, "level_xp": 360, "level_next": 400, "leveled_up": false,
  "streak": 12, "badges_earned": ["first-rare-card"] }
```

- **XP:** a correct answer is worth 10, doubled when `rare`. A wrong answer is worth 0 and schedules a review for tomorrow.
- **Retries:** the first answer per card counts. Later answers return the same result with `xp: 0`.
- A card with `quiz: null` (an article before `/quiz`) returns `409`.

### `GET /review`
Returns `{ "due": 7, "cards": [Card] }`, which are the cards whose `reviews.due_at <= now`. An `article` card is here only after a wrong answer to its quiz. It has `flashcard: null`, so show its quiz instead.

### `POST /review` `{ "card_id": 42, "rating": "again | hard | good | easy" }`
Runs SM-2 and returns `{ "next_due_at": "...", "interval_days": 6 }`.
- The app shows the next intervals on the buttons, so `GET /review` cards also include `"preview_intervals": { "again": "10 min", "hard": "2 days", "good": "6 days", "easy": "2 weeks" }`.

### `GET /cards/{id}/deeper`
Returns `{ "markdown": "..." }`.
- It's generated on first request with `prompts/lesson_card.md` in deeper mode, then cached.
- Allowed content: at most 250 words, one optional code block with lettered notes, and no images.

### `GET /cards/{id}/reader`
Returns `{ "title": "...", "site": "Zenn", "url": "https://...", "text": "First paragraph.\n\nSecond paragraph." }`: the readable text of the card's article (`source.url`), with no model call.
- The server fetches the page (15 s timeouts, 30 s in total, at most 2 MB, public addresses only) and extracts the main text with trafilatura. Menus, ads and comments are left out.
- Paragraphs, list items and table rows are separated by blank lines. The text is plain, with no HTML or markdown links.
- The first successful extraction is saved, and later requests return it instantly.
- If the page can't be fetched or has no readable text, `text` is `null` (with `title`, `site` and `url` still filled in). Nothing is saved, so a later request tries again. Lesson cards have no URL, so they always return `text: null`.

### `POST /cards/{id}/quiz`
Returns `{ "quiz": { "question", "options": [4], "answer", "explanation" } }`.
- For a card with `quiz: null` (an article), the first request asks Claude for one question about the article, using `prompts/article_quiz.md`. The article is the reader text if available, otherwise the card body.
- The quiz is saved on the card, so `/feed` shows it from then on, `/answer` works, and later requests return it without calling Claude.
- A card that already has a quiz returns it with no call.
- Japanese articles get a Japanese quiz.
- Errors:
  - `503` when Claude is unavailable (for example, a usage limit).
  - `502` when the reply isn't a valid quiz. Try again.

### `GET /cards/{id}/thread`
Returns `{ "messages": [ { "role": "user | assistant", "text": "...", "at": "..." } ] }`.

### `POST /ask` `{ "card_id": 42, "message": "Why not two packets?" }`
Returns an SSE stream:
- `event: token` events with `data: {"text": "..."}`
- then `event: suggestions` with `data: ["What's a SYN flood?", "Make it a flashcard"]`
- then `event: done`.

The server stores both messages in `messages`. Context sent to Claude is the card JSON plus the last 10 messages, using `prompts/ask.md`.
If the user taps "Make it a flashcard", the app sends that text as a message. The server creates a new review card from the conversation and replies with a confirmation. Each thread makes at most one flashcard: if this card's thread already has one, the server repeats its confirmation without calling Claude again.

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
Returns `{ "ok": true, "buffer": 97, "lesson_buffer": 41, "last_ingest": "...", "last_error": null }`. `buffer` counts all unseen cards. `lesson_buffer` counts only unseen lessons, which is what decides whether ingest calls Claude.
