# Verification — 2026-09-14

- 36 automated tests pass: private-IP/DNS restrictions, video rotation/sample-aspect handling, MP4 faststart, process cancellation, durable update/job deduplication, upload checkpoints, 429 backoff, ambiguous upload handling, rich HTML paragraphs, caption metadata, and orphan cleanup.
- Created and configured the bot, description, about text, avatar and settings through Telegram MCP, using the verified owner account and BotFather.
- Instagram Reel: 22,472,748-byte original. Telegram preview is 720×1280, 158 seconds, with `supports_streaming=True`; original delivered separately. Success reaction verified.
- YouTube: “Me at the zoo” downloaded and delivered as preview/original; separate ZIP mode passed.
- TikTok short link: downloaded and delivered as document.
- Instagram photo post: photo preview, original, and separate caption delivered. Downloaded returned ZIP via Telegram MCP and verified it contains the photo and `caption.txt` (558 characters including source URL); ZIP CRC test passes.
- Native document upload: 64,657,225-byte ZIP, original filename and application/zip MIME preserved.
- Large document upload: 928,670,970-byte ZIP successfully delivered through the local Bot API.
- Cancellation: pressed the inline cancel button during a large download; job entered `cancelled`, temporary directory removed, later jobs completed.
- Restart recovery: an interrupted pending Instagram task resumed after a service restart and completed.
- Live rich-message readback confirmed native `RichMessage` paragraphs and the Instagram caption heading, using a read-only Telegram MCP inspection tool.
- Test media directories removed from VPS. Worker and API remain running, poll health fresh, no queued/running jobs at final audit. `/opt/instasaver` approximately 52 MiB including its venv; Telegram API image approximately 46 MB. No retained media in the downloads directory.

Historical test failures are retained briefly in the job database: an obsolete Google sample URL returned HTTP 403; a large archive initially timed out connecting to its source. DNS address failover and bounded HTTP retries were added, and the large-archive test subsequently succeeded. These are not represented as permanently reliable source URLs.

No guarantee is made that every website, private post, DRM stream, or future expired cookie remains downloadable. The tested workflows above passed on Frankfurt2.

# Verification — 2026-10-14 (clean delivery update)

- 46 automated tests pass (new: creator/title naming for Instagram, X, YouTube and gallery-dl metadata, unsafe-name sanitizing, numbered carousels, URL cache keys, card escaping/splitting, private cache isolation).
- Live on Frankfurt2: Instagram Reel delivered as captionless video + original `atlanta.night — HORRORLAND ATLANTA x BUSHIDO ZHO🎃.mp4` + separate card; expired owner cookies fell back to anonymous automatically.
- Same reel via a different share link (`/reels/…?igsh=`) replayed instantly from cache with no download.
- Audio mode delivered `atlanta.night — HORRORLAND ATLANTA x BUSHIDO ZHO🎃.mp3` with card.
- Local Bot API accepted a mixed photo/video `sendMediaGroup` with `file://` paths (test messages deleted).
