# Study Feed server

Backend for the Study Feed iPhone app. The contract is in `API.md`.

**App URL (type this into the app's settings): `http://100.65.4.50:8001`**

It is reachable only over Tailscale. The API binds to this machine's Tailscale IP, never `0.0.0.0`. It uses port 8001 because `camsite.service` already holds 8000.

## Layout

| File | What it does |
|---|---|
| `app.py` | FastAPI app with every endpoint in `API.md` |
| `ingest.py` | Cron job that generates cards with `claude -p` and keeps about 100 unseen |
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

Each `claude -p` call makes `BATCH_SIZE` (default 5) cards. A news call sends 5 articles, and a lesson call sends the next 5 parts of one series. Calls alternate between news and lessons, and lessons rotate through the 4 topics.

The cron jobs are installed (`crontab -l`; a copy is in `deploy/crontab.txt`):

- `0 3 * * *` runs `--nightly`: fills toward `BUFFER_TARGET`, at most `NIGHTLY_CALLS` (5) calls.
- `0 */4 * * *` runs `--topup`: only runs if the buffer is under 60, at most `TOPUP_MAX_CALLS` (3) calls.

To run ingest manually:

```sh
cd ~/server-setup
.venv/bin/python ingest.py --limit 5    # insert 5 cards now, whatever the buffer
.venv/bin/python ingest.py --nightly
.venv/bin/python ingest.py --topup
```

If a call fails or hits a Pro usage limit, the run stops and records the error in `/health`. The next cron run retries.

Each card in a reply is validated on its own: bad cards are logged and dropped, and good ones are kept. Lesson parts that get dropped are not retried. When a roadmap file is finished, ingest logs it, and you can add more items to `roadmaps/<topic>.md`.

## Configuration (`.env`)

| Key | Meaning |
|---|---|
| `CLAUDE_MODEL` | Model for every call, via `--model`. Code default is `sonnet`; set to `haiku` here. |
| `BATCH_SIZE`, `NIGHTLY_CALLS`, `TOPUP_MAX_CALLS` | Cards per call and call caps (defaults 5 / 5 / 3) |
| `BUFFER_TARGET`, `RARE_RATE` | 100 unseen cards; 8% rare |
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

Article text sits inside `<article>` tags. Any tag-like text inside it is escaped, so an article can't close the tag early.

Validation and sanitizing:

- **News sources:** quotes are kept only if they appear verbatim in the article. Lesson cards never carry a source.
- **SVG:** the sanitizer strips scripts, `foreignObject`, `on*` attributes, external hrefs, and `url()` in styles.
- **Mermaid:** `click` and `%%{init}` lines are removed.

## Testing with a fake date

`FAKE_NOW` overrides the clock. To check that a wrongly answered card shows up in review the next day, run a second server on loopback against a copy of the database:

```sh
cp study.db /tmp/t.db
DB_PATH=/tmp/t.db FAKE_NOW=2026-10-16T09:00:00 .venv/bin/uvicorn app:app --host 127.0.0.1 --port 18002
curl -s http://127.0.0.1:18002/review
```

## Design notes

- **Review queue:** cards enter it from a wrong quiz answer (due tomorrow), from saving a card, or from "Make it a flashcard" in Ask. Correct answers don't enrol a card.
- **SM-2 ratings:** Again re-shows the card in 10 min. Hard halves the SM-2 interval and Easy doubles it, so the four buttons show different times.
- **Levels:** you start at level 1. Going from level n to n+1 costs `100 + 50*n` XP.
