# Study Feed server

Backend for the Study Feed iPhone app. The contract is in `API.md`.

**App URL (type this into the app's settings): `http://100.65.4.50:8001`**

It is reachable only over Tailscale. The API binds to this machine's Tailscale IP, never `0.0.0.0`. It uses port 8001 because `camsite.service` already holds 8000.

## Layout

| File | What it does |
|---|---|
| `app.py` | FastAPI app with every endpoint in `API.md` |
| `ingest.py` | Cron job: turns feed items into article cards (no model), and has `claude -p` write lesson cards |
| `fetch.py` | Safe HTTP fetches (timeouts, size cap, public addresses only) and the reader view (trafilatura) |
| `core.py` | Config, SQLite, clock, and the sandboxed `claude -p` wrapper |
| `cards.py` | Card schema validation and SVG/Mermaid sanitizing |
| `progress.py` | SM-2, XP/levels, streak/freezes, badges |
| `run.sh` | Starts uvicorn on the Tailscale IP (container: `$TAILSCALE_IP`; host: `tailscale ip -4`) |
| `deploy/` | The Docker stack (`deploy/stack/`) and a copy of the crontab |
| `study.db` | SQLite database |
| `logs/` | `ingest.log`, plus `rejected.jsonl` for malformed cards that were dropped |

## Service

Study Feed runs as the Docker stack **`study-feed`** in `/opt/stacks/study-feed`, like your other services. You'll see it in Dockge (`:5001`), and as a tile in the "Learning" group on Homepage (`:3000`). The stack's source files are in `deploy/stack/`.

- **Code:** the container live-mounts this folder at `/app`, so edits here take effect after a restart.
- **Network:** it uses host networking, and `run.sh` binds only `100.65.4.50:8001`.
- **Reboot:** `restart: unless-stopped` brings it back after a reboot or power loss. At boot it waits for the Tailscale IP before binding.
- **Claude CLI:** the host's own `claude` install is mounted read-only, so it follows host updates.

Restart it in Dockge (open `study-feed`, then Restart), or from a shell:

```sh
docker restart study-feed       # after editing .env or code
docker ps --filter name=study-feed
```

After editing `deploy/stack/compose.yaml` or `Dockerfile`:

```sh
cp deploy/stack/* /opt/stacks/study-feed/
cd /opt/stacks/study-feed && docker compose up -d --build
```

## Logs

```sh
docker logs -f study-feed              # API (also in Dockge)
tail -f logs/ingest.log                # cron ingest runs
tail -n 5 logs/rejected.jsonl          # dropped cards and why
curl -s http://100.65.4.50:8001/health # buffer size, last ingest, last error
```

## Ingest

Every run does two things:

1. **Articles, with no model call.** Each new item in `feeds.txt` becomes an `article` card:
   - the feed's title, its summary (or first paragraph) cut to about 280 characters (140 for Japanese), the link, the site, and the publish date;
   - at most 5 new items per feed per run, so no single site floods the feed;
   - items already seen are skipped (same URL ignoring tracking parameters, or same title);
   - items with no title or no text are dropped. That affects Hacker News, Lobsters and Hugging Face, whose feeds carry no summary.
2. **Lessons, written by Claude.** Each `claude -p` call writes the next `BATCH_SIZE` (5) parts of one series. Calls rotate through the roadmaps. Whether ingest calls Claude at all depends on the **lesson buffer** (unseen lessons only), because articles cost nothing:
   - `0 3 * * *` runs `--nightly`: fills toward `LESSON_BUFFER_TARGET` (default 50, half of `BUFFER_TARGET`), at most `NIGHTLY_CALLS` (5) calls.
   - `0 */4 * * *` runs `--topup`: calls Claude only if the lesson buffer is under `LESSON_TOPUP_THRESHOLD` (default 30), at most `TOPUP_MAX_CALLS` (3) calls.

The cron jobs are installed (`crontab -l`; a copy is in `deploy/crontab.txt`).

To run ingest manually:

```sh
cd ~/server-setup
.venv/bin/python ingest.py --articles-only  # new feed articles only, zero model calls
.venv/bin/python ingest.py --limit 5        # articles, plus 5 lesson cards now, whatever the buffer
.venv/bin/python ingest.py --nightly
.venv/bin/python ingest.py --topup
```

The log line `articles: inserted N with 0 model calls` and the `lesson summary: N calls: ...` line show what each run did.

If a call fails or hits a Pro usage limit, the run stops and records the error in `/health`. The next cron run retries. Articles are inserted before any call, so they never depend on Claude.

Each lesson card in a reply is validated on its own: bad cards are logged and dropped, and good ones are kept. Lesson parts that get dropped are not retried. When a roadmap file is finished, ingest logs it, and you can add more items to `roadmaps/<topic>.md`.

## Content sources

- **News:** `feeds.txt` holds one RSS/Atom URL per line. It covers general tech, security, AI, iOS/Swift, Qiita, Zenn, and Japanese company tech blogs. Each run interleaves the feeds in a shuffled order, so card ids don't cluster by site.
- **Topics:** put a tag after each URL in `feeds.txt`: `[ios]`, `[security]`, `[ai]`, `[swe]` or `[networking]`. That feed's article cards get that topic. Untagged feeds get `swe` (`ARTICLE_DEFAULT_TOPIC` in `ingest.py`).
- **Card fields:** each card gets `kind` (`news`/`lesson`), `lang` and, for news, `site` (one spelling per site; see `SITE_NAMES` in `core.py`). An article is `ja` if its title or body contains any hiragana, katakana or kanji. Its "go deeper", Ask replies and on-demand quiz are in Japanese too.
- **Older news cards:** the model-written news cards from before articles keep their content, quiz and flashcard. `prompts/news_card.md` is no longer used.
- **Reader view:** `GET /cards/{id}/reader` fetches the article page once (2 MB cap, timeouts), extracts the main text with trafilatura, and stores it in the `readers` table.
- **On-demand quiz:** `POST /cards/{id}/quiz` makes one quiz for an article with one Claude call (`prompts/article_quiz.md`) and saves it on the card.
- **Lessons:** `roadmaps/*.md`. Lesson calls rotate `networking → ios → security → swe → ai → ios` (`LESSON_ROTATION` in `ingest.py`), so iOS gets a third of all lessons. `roadmaps/ios.md` covers Swift concurrency, SwiftUI, memory, lifecycle, networking, security, tooling, Xcode and the App Store. Progress per roadmap is kept in the `topics` table.
- **Schema upgrades:** `init_db()` migrates older databases automatically. It rebuilds `cards` for new `CHECK` values (the `ios` topic, the `article` card type) without losing rows, saving a copy of the database in `logs/study-before-migration-*.db` first. It also fills in `kind`/`lang`/`site`.
- **X/Twitter:** not supported. There's no free feed, the API is paid, and scraping breaks X's terms. To follow particular people, add their blog, newsletter, or a Bluesky profile feed (`https://bsky.app/profile/<handle>/rss`) to `feeds.txt`.

## Configuration (`.env`)

| Key | Meaning |
|---|---|
| `CLAUDE_MODEL` | Model for every call, via `--model`. Code default is `sonnet`; set to `haiku` here. |
| `BATCH_SIZE`, `NIGHTLY_CALLS`, `TOPUP_MAX_CALLS` | Cards per call and call caps (defaults 5 / 5 / 3) |
| `BUFFER_TARGET`, `RARE_RATE` | 100 unseen cards; 8% rare |
| `LESSON_BUFFER_TARGET`, `LESSON_TOPUP_THRESHOLD` | Unseen lessons that ingest aims for (default 50), and the level under which a top-up calls Claude (default 30) |
| `PORT` | 8001 |

`NIGHTLY_BATCH` in `.env` is no longer used and can be removed.

Restart the container after changing `.env`. Cron picks up changes on its next run.

## Rotating the Claude token

1. Run `claude setup-token` on this server and copy the new token.
2. In `.env`, replace the value of `CLAUDE_CODE_OAUTH_TOKEN=`. Keep it on **one line**: the first paste wrapped onto two lines and broke auth.
3. Run `docker restart study-feed`.
4. Check it: `.venv/bin/python ingest.py --limit 1`, then `curl -s http://100.65.4.50:8001/health`.

**Fallback:** if Pro limits get in the way, put an API key in `ANTHROPIC_API_KEY=` in `.env`. The same `claude -p` call then works with the key, and no code changes are needed.

## How Claude is called safely

Every call runs in an empty temp directory with these flags:

- `--tools ""` (no tools)
- `--max-turns 1`
- `--strict-mcp-config`
- `--setting-sources ""`
- `--no-session-persistence`
- a short system prompt saying that tagged text is data only

The environment is reduced to auth plus `PATH` and `HOME`.

Article text (for the on-demand quiz) sits inside `<article>` tags. Any tag-like text inside it is escaped, so an article can't close the tag early.

Validation and sanitizing:

- **News sources:** every news card links its article URL. Article cards use `{label: feed name, url, quote: null}`. Older model-written news cards kept a quote only if it appeared verbatim in the article. Lesson cards never carry a source.
- **Fetching pages:** feed and article URLs are untrusted. `fetch.py` allows only http(s) to public addresses, checking every redirect too, so a feed can't point the server at the LAN, the tailnet or localhost. It also enforces timeouts and a size cap (2 MB for article pages, 10 MB for feeds).
- **SVG:** the sanitizer strips scripts, `foreignObject`, `on*` attributes, external hrefs, and `url()` in styles.
- **Mermaid:** `click` and `%%{init}` lines are removed.

## Testing with a fake date

`FAKE_NOW` overrides the clock. To check that a wrongly answered card shows up in review the next day, run a second server on loopback against a copy of the database:

```sh
cp study.db /tmp/t.db
DB_PATH=/tmp/t.db FAKE_NOW=2026-10-16T09:00:00 .venv/bin/uvicorn app:app --host 127.0.0.1 --port 18002
curl -s http://127.0.0.1:18002/review
```

## Tests

```sh
.venv/bin/python -m unittest discover -s tests -v
```

`tests/test_quiz.py` covers `POST /cards/{id}/quiz`:
- Japanese articles get a Japanese quiz, and an English reply for one is rejected.
- Duplicate options are rejected.
- A saved quiz is returned without a call.

Claude and the page fetch are stubbed, and the tests use a throwaway database, so they make no calls and never touch `study.db`.

## Design notes

- **Feed:** `/feed` returns every card, newest first, seen or not, each with `seen`. Due reviews are mixed in. `?unseen_only=true` gives the old unseen-only feed.
- **Review queue:** cards enter it from a wrong quiz answer (due tomorrow), from saving a card, or from "Make it a flashcard" in Ask. Correct answers don't enrol a card.
- **SM-2 ratings:** Again re-shows the card in 10 min. Hard halves the SM-2 interval and Easy doubles it, so the four buttons show different times.
- **Levels:** you start at level 1. Going from level n to n+1 costs `100 + 50*n` XP.
