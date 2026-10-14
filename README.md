# Instasaver

Russian Telegram downloader, deployed at [@instasaver_kirniy_bot](https://t.me/instasaver_kirniy_bot).

Send a link. Media arrives **clean, with no caption**, so it forwards without junk:

- **Video** (default): streamable MP4, then the unchanged original as a document. Carousels arrive as native Telegram albums (up to 10 per album; oversized items as documents).
- **Оригинал**: original file(s) as documents; several files are zipped.
- **ZIP**: everything in one archive plus `caption.txt`.
- **Звук**: mp3 extracted from the video, with title/performer tags.

Files are named after the creator and post: `atlanta.night — HORRORLAND ATLANTA x BUSHIDO ZHO🎃.mp4` (handle for Instagram/TikTok/X, channel for YouTube; first caption line when the site title is generic; Unicode-safe, ≤160 bytes).

After the media comes a separate **post card**: creator (linked to their profile), display name, title, the full caption in an expandable quote (long captions continue in follow-ups), views/likes/comments/duration/date, plus «Открыть пост» and «Звук mp3» buttons.

Repeat links are **instant**: successful deliveries are cached by normalized URL (share junk like `igsh`, `vrfl`, `si`, `utm_*` stripped; youtu.be ≡ youtube.com) for 7 days and resent by Telegram `file_id`. Results fetched with the owner's cookies are cached privately for the owner only.

If owner cookies are expired, the worker retries anonymously (and vice versa), so a dead session never blocks public posts. `/cancel` and the cancel button terminate the current subprocess group. `/status` shows your queue.

Inspired by [Pablo Stanley's Yoinks](https://github.com/pablostanley/yoinks), using its yt-dlp + FFmpeg architecture and format preference approach. This is a separate Python Telegram service, not a wrapper around Yoinks' interactive terminal UI. Yoinks' MIT notice is retained in `LICENSE.yoinks`.

## Running deployment

- VPS: SSH alias `frankfurt2`, `/opt/instasaver`.
- Worker: `instasaver.service`, unprivileged `instasaver` user.
- Telegram: `instasaver-api` Docker container, Bot API **10.3**, pinned image digest.
- API is bound only to `127.0.0.1:8091`; no public webhook or inbound bot port.
- SQLite queue, update offsets and per-file delivery checkpoints: `/opt/instasaver/data/state.sqlite3`.
- Bot token: `/opt/instasaver/secrets/bot.env` (mode 600).
- Telegram application ID/hash: `/opt/instasaver/secrets/api.env` (mode 600).
- Site-specific cookies: `/opt/instasaver/secrets/cookies/` (mode 700 directory, 600 files).

```sh
ssh frankfurt2 'systemctl status instasaver --no-pager'
ssh frankfurt2 'journalctl -u instasaver -n 30 --no-pager'
ssh frankfurt2 'python3 /opt/instasaver/scripts/healthcheck.py'
ssh frankfurt2 'systemctl restart instasaver'
```

Both worker and API restart automatically on failure/reboot. A worker restart resumes queued/running jobs and skips files whose successful Telegram delivery was checkpointed. An upload with an ambiguous network result is not blindly retried: the bot asks the user to check the chat first. There is still a small unavoidable crash window between Telegram accepting a send and SQLite recording it; exactly-once delivery across the two services is not claimed.

## Local development

Requires Python 3.12+, FFmpeg/ffprobe, Node 20+ (YouTube JS challenge solver), and uv.

```sh
uv sync --frozen
uv run pytest -q
```

The production worker requires a **local-mode** Bot API server with the same absolute data path mounted. It passes `file://` paths for uploads; the hosted cloud Bot API is not a drop-in substitute.

Environment variables:

| Variable | Purpose |
|---|---|
| `BOT_TOKEN` | BotFather token, never committed |
| `OWNER_ID` | Telegram user ID allowed to use personal cookies |
| `BOT_API_URL` | Local API URL (production: `http://127.0.0.1:8091`) |
| `DATA_DIR` | Writable persistent queue and job directory |
| `SECRETS_DIR` | Read-only site cookie directory |
| `ALLOWED_USER_IDS` | Optional comma-separated allowlist; empty permits public downloads for everyone |

Only the owner can use the imported Chrome cookies. Other users always download anonymously. Instagram owner requests use the authenticated session immediately; other sites first try anonymously, then use the owner's site-specific cookies if needed.

## Cookie refresh

The owner authorized importing media-platform cookies from this Mac's Chrome session. Cookie values are never printed. To refresh locally:

```sh
uv run scripts/export_cookies.py work/cookies Default
scp work/cookies/*.txt frankfurt2:/opt/instasaver/secrets/cookies/
ssh frankfurt2 'chown instasaver:instasaver /opt/instasaver/secrets/cookies/*.txt; chmod 600 /opt/instasaver/secrets/cookies/*.txt'
```

No restart is required. Cookies expire and sites may require a fresh login or challenge; no downloader can guarantee permanent access to every link. The bot reports those failures in Russian rather than hanging indefinitely. Private/unavailable/DRM content and site/IP blocks are not guaranteed to download.

## Deployment and updates

Use system Python on the server; do not create a service venv pointing into `/root/.local/share/uv`, because the service cannot traverse root's home.

1. Create the `instasaver` system user and `/opt/instasaver/{data,api,api/tmp,secrets,secrets/cookies}` with service ownership; secrets directories mode 700.
2. Sync source, `pyproject.toml`, `uv.lock`, scripts, and deployment files. Exclude `work`, `.venv`, `.git`, `data`, and `secrets`.
3. Install secrets out of band. `bot.env` needs `BOT_TOKEN` and `OWNER_ID`; `api.env` needs `TELEGRAM_API_ID` and `TELEGRAM_API_HASH`.
4. Run `uv sync --frozen --no-dev --python /usr/bin/python3` inside `/opt/instasaver`.
5. Run `sh deploy/start-api.sh`. This works without Docker Compose; `deploy/compose.yaml` is an alternative for systems with the Compose plugin.
6. Install `deploy/instasaver.service` to `/etc/systemd/system/`, then `systemctl daemon-reload && systemctl enable --now instasaver`.
7. Run the health check and send a real link through the bot.

For extractor updates, update yt-dlp/gallery-dl in a checkout, run tests and real YouTube/Instagram smoke downloads, then deploy the updated lockfile. Production does not self-install untested updates.

## Boundaries and resource limits

- One active download per VPS; up to 5 queued links per user, 30 globally, 30 links/user/hour.
- Maximum delivered file/ZIP: 1.95 GB, below Telegram local API's 2 GB limit.
- Up to 20 files in an Instagram post. Full profiles, live streams, and entire YouTube playlists are intentionally not bulk-downloaded.
- Six GB free-disk reserve before new work; periodic per-process disk guard and bounded execution time. With one worker, normal temporary usage is at most about 4 GB for a maximum-size original plus a preview/ZIP (a write burst can briefly exceed a polling guard). Files are not kept as a permanent archive.
- Requests pass through a public-address-only proxy that resolves, validates, and pins destination IPs; private addresses and non-web ports are blocked, including redirect targets.
- FFmpeg only probes local media through an allowlist of demuxers/protocols. It respects rotation and sample aspect ratio, outputs square pixels when needed, and puts MP4 metadata before media for streaming.
- Originals are never transcoded. Preview conversion is separate.
- Temporary downloads are deleted after success/failure/cancel, including terminal paths where notification delivery fails. A janitor runs at startup and every five minutes: it deletes terminal job artifacts and hour-old orphan folders, preserves active/resumable work, and expires abandoned jobs after 24 hours. SQLite WAL is checkpointed. Queue history is retained for 24 hours; processed incoming message bodies are discarded.
- Shared VPS services are untouched. Worker/API have CPU and memory limits.

## API references

[Official Bot API](https://core.telegram.org/bots/api): `sendVideo`, `sendDocument`, `setMessageReaction`, `sendRichMessage`, `sendRichMessageDraft`, `getUpdates`. Rich HTML uses explicit paragraphs/line breaks. Draft IDs are stable and stop-generation updates cancel their corresponding job. Durable status messages remain available while transient drafts expire. HTTP 429 honors `retry_after`; HTML fallback is available if rich messages are unsupported.

The avatar was generated with the built-in imagegen tool. Its prompt is saved in `assets/avatar-prompt.txt`; it and the Russian profile text were installed through Telegram MCP/BotFather.
