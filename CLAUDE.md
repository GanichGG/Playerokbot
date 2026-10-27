# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository layout

The whole application lives in `ARR/`. The repo root only holds git metadata and this file. All commands below assume `cd ARR`.

## Commands

- Install: `pip install -r requirements.txt` (`PlayerokAPI>=0.2.0`, `httpx`). Needs Python 3.10+ (the code uses `dict | None` and `list[str]` annotations).
- Run: `python bot.py`. Requires `PLAYEROK_TOKEN`, `TG_BOT_TOKEN`, `TG_OWNER_ID` (see Configuration).
- Tests, linter, formatter, and build: none configured. Check syntax with `python -c "import ast; ast.parse(open('bot.py', encoding='utf-8').read())"`.

## Architecture

The app is a single file, `bot.py`, built on asyncio. It acts as a Telegram remote control for a Playerok seller account, plus semi-automatic purchasing of the matching key on Plati.

`main()` runs four coroutines concurrently via `asyncio.gather`:
- `listener.run()`: PlayerokAPI event stream (`NEW_DEAL`, `DEAL_UPDATED`, `NEW_MESSAGE`). Dispatches to `handle_deal` and `on_message`.
- `telegram_loop()`: long-polls `getUpdates` with raw httpx calls to the Bot API. Only messages and callbacks from `TG_OWNER_ID` are processed.
- `mail_loop()`: polls IMAP for Plati emails. It is a no-op unless `MAIL_USER` is set.
- `background_loop()`: checks for new reviews every 2 minutes and sends the daily summary at 23:59 local time.

Telegram is the only UI. Owner-facing messages go through `say()`, and `tg()` handles Telegram's `retry_after` and ignores harmless errors.

### Purchase flow

1. `handle_deal` sees a deal reach `PAID` and calls `on_paid`. It looks up the lot in `lots.json` through `find_lot`: an exact name match wins, otherwise a substring match.
2. It builds a Plati payment link (`PAY_URL`) and records `state["awaiting"][tag]`. The tag is `make_tag(deal_id)`, which is also the plus-address in the order email when `MAIL_USER` is set.
3. The owner gets the message with `tg_manual`, which stores `state["pending"][msg_id]`.
4. Key delivery has two paths:
   - Mail: `mail_loop` finds the `oplata.info` email whose recipient or body contains the tag, extracts `id_i` and `uid` with `BUY_LINK_RE`, and calls `fetch_goods`.
   - Manual: the owner replies to the pending message with the buy link or the key. A buy link goes through `fetch_goods`; anything else is delivered as-is.
5. `fetch_goods` polls Plati's `purchase.asp` (up to 10 tries, 5 s apart). `retval` 66 means payment is not confirmed yet, and 0 means success.
6. `deliver` sends the text to the buyer and marks the deal in `state["delivered"]`.

Status changes are detected by comparing against `state["deal_status"]`, so notifications fire only on transitions.

### Owner interaction state

- `state["pending"]`: owner message id mapped to a deal. A reply with a key or link triggers delivery.
- `state["chat_links"]`: owner message id mapped to a Playerok chat. A reply is sent back to the buyer.
- `state["input"]`: the multi-step edit started by an inline button (`e:<field>:<item_id>`). The next plain text message is consumed by `apply_edit`. `/cancel` clears it.

### Lots

`my_items` pages through statuses `None`, `DISCONTINUED`, `EXPIRED`, and `SOLD`, capped at 20 pages per status. `cached_items` keeps a 30-second cache. `LOT_GROUPS` maps Playerok statuses to the categories shown in `/lots`.

Renaming a lot moves its `lots.json` key to the new name. Linking Plati writes `lots.json` and drops the example placeholder key.

### State

`state.json` is loaded at import and rewritten in full by `save_state()` after each mutation. `save_state()` also trims each list or dict to a fixed size, so the dedup lists (`seen`, `delivered`, `used_purchases`, `mail_seen`, `reviews_seen`) do not grow without bound. There is no locking, so run exactly one process.

`stat()` keeps daily counters. `stats_text` prefers live sales from Playerok history (`today_sales_from_playerok`) and falls back to the counters only when that history is unavailable.

### Remote updates

The owner can send a file over Telegram. `on_document` accepts only the names in `UPDATABLE`. It checks `.py` files with `compile` and `.json` files with `json.loads` before replacing anything. The previous version is kept as `<name>.bak`. Replacing `bot.py` or `env.txt` restarts the process with `os.execv`.

## Configuration

`load_env()` runs at import and reads the first existing file from `.env`, `env.txt`, `.env.txt`, `env`, in that order. Real environment variables take precedence. A missing required key raises `KeyError` at import time.

Other settings: `MAIL_USER`, `MAIL_APP_PASSWORD`, `IMAP_HOST` (default `imap.gmail.com`), `MAIL_POLL_SECONDS` (default 15), `TZ_OFFSET` (fixed hours, default 3; not a named time zone).

`env.txt` contains live credentials. Keep it out of git.

## Gotchas

- Runtime files (`env.txt`, `lots.json`, `replies.json`, `state.json`, `*.bak`) sit next to `bot.py` and are read via `BASE = Path(__file__).parent`. Run the bot from any directory, but the files must stay in `ARR/`.
- Playerok data comes through the third-party `PlayerokAPI` package. Its attribute names (`raw_price`, `views_counter`, and so on) are used directly, and some are read with `getattr` defaults. Check the installed version before assuming a field exists.
- Telegram messages are user-facing Russian strings. Keep new owner-facing text in Russian to match.
- The bot token is logged by httpx at INFO level, so `httpx` and `httpcore` are set to WARNING in `main()`. Keep that setting.
