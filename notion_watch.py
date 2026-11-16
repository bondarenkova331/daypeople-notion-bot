"""DAY PEOPLE — Notion → Telegram watcher.

Each run: lists every page/database the Notion integration can see, compares
with the previous run's snapshot, and posts one grouped message to Telegram
describing what was created, edited, renamed, deleted or commented.

State between runs is kept in an encrypted file (state.enc) so the repo can be
public without leaking page titles. Nothing sensitive is printed to the log.
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests
from cryptography.fernet import Fernet, InvalidToken

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")  # empty → found automatically
# State is encrypted with a key derived from the Notion token (already secret).
STATE_KEY = base64.urlsafe_b64encode(hashlib.sha256(b"daypeople-state:" + NOTION_TOKEN.encode()).digest())
STATE_PATH = os.environ.get("STATE_PATH", "state.enc")

# An edit is reported only after the page has been quiet this long, so that
# someone typing for 20 minutes produces one message, not four.
SETTLE_MIN = int(os.environ.get("SETTLE_MINUTES", "3"))
# Page-level comments are checked on pages edited within this window.
COMMENT_WINDOW_DAYS = int(os.environ.get("COMMENT_WINDOW_DAYS", "3"))
# More than this many items of one kind by one person in one place → summary.
COLLAPSE_AFTER = 6
DRY_RUN = os.environ.get("DRY_RUN") == "1"

NOTION = "https://api.notion.com/v1"
HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}
SKIP_PROP_TYPES = {
    "formula", "rollup", "created_time", "created_by",
    "last_edited_time", "last_edited_by", "unique_id", "button", "verification",
}


# ---------------------------------------------------------------- Notion API
def notion(method: str, path: str, **kw) -> dict | None:
    for attempt in range(6):
        r = requests.request(method, NOTION + path, headers=HEADERS, timeout=30, **kw)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(float(r.headers.get("Retry-After", 2 ** attempt)))
            continue
        if r.status_code in (403, 404):
            return None
        r.raise_for_status()
        return r.json()
    r.raise_for_status()
    return None


def paginate(method: str, path: str, body: dict | None = None, params: dict | None = None):
    cursor = None
    while True:
        if method == "POST":
            b = dict(body or {}, page_size=100)
            if cursor:
                b["start_cursor"] = cursor
            data = notion("POST", path, json=b)
        else:
            p = dict(params or {}, page_size=100)
            if cursor:
                p["start_cursor"] = cursor
            data = notion("GET", path, params=p)
        if not data:
            return
        yield from data.get("results", [])
        if not data.get("has_more"):
            return
        cursor = data.get("next_cursor")


def plain(rich: list | None) -> str:
    return "".join(x.get("plain_text", "") for x in (rich or [])).strip()


def obj_title(o: dict) -> str:
    if o["object"] == "database":
        return plain(o.get("title")) or "Без названия"
    for p in (o.get("properties") or {}).values():
        if p.get("type") == "title":
            return plain(p.get("title")) or "Без названия"
    return "Без названия"


def prop_value(p: dict, users: dict) -> str | None:
    t = p.get("type")
    v = p.get(t)
    if t in SKIP_PROP_TYPES or t == "title":
        return None
    if t in ("rich_text",):
        return plain(v)
    if t in ("select", "status"):
        return (v or {}).get("name", "")
    if t == "multi_select":
        return ", ".join(x["name"] for x in v or [])
    if t == "people":
        return ", ".join(users.get(x["id"], x.get("name") or "?") for x in v or [])
    if t == "date":
        if not v:
            return ""
        return v["start"] + (f" → {v['end']}" if v.get("end") else "")
    if t == "checkbox":
        return "✓" if v else "✗"
    if t == "number":
        return "" if v is None else str(v)
    if t in ("url", "email", "phone_number"):
        return v or ""
    if t == "relation":
        return f"{len(v or [])} связ."
    if t == "files":
        return ", ".join(f.get("name", "файл") for f in v or [])
    return None


def snapshot(o: dict, users: dict) -> dict:
    parent = o.get("parent") or {}
    props = {}
    if o["object"] == "page" and parent.get("type") == "database_id":
        for name, p in (o.get("properties") or {}).items():
            val = prop_value(p, users)
            if val is not None:
                props[name] = val
    return {
        "k": o["object"],
        "t": obj_title(o),
        "u": o.get("url", ""),
        "let": o.get("last_edited_time"),
        "by": (o.get("last_edited_by") or {}).get("id"),
        "cby": (o.get("created_by") or {}).get("id"),
        "db": parent.get("database_id"),
        "p": props,
    }


# ------------------------------------------------------------------ state
def load_state() -> dict | None:
    if not os.path.exists(STATE_PATH):
        return None
    try:
        with open(STATE_PATH, "rb") as f:
            return json.loads(Fernet(STATE_KEY).decrypt(f.read()))
    except (InvalidToken, ValueError):
        print("state unreadable — starting fresh")
        return None


def save_state(state: dict) -> None:
    with open(STATE_PATH, "wb") as f:
        f.write(Fernet(STATE_KEY).encrypt(json.dumps(state, ensure_ascii=False).encode()))


# --------------------------------------------------------------- Telegram
def find_group_chat() -> str:
    """The group the bot was most recently added to (from getUpdates)."""
    r = requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates", timeout=30)
    r.raise_for_status()
    chat = ""
    for u in r.json().get("result", []):
        for key in ("my_chat_member", "message"):
            c = (u.get(key) or {}).get("chat") or {}
            if c.get("type") in ("group", "supergroup"):
                chat = str(c["id"])
            # a group upgraded to supergroup changes id
            mig = (u.get("message") or {}).get("migrate_to_chat_id")
            if mig:
                chat = str(mig)
    return chat


def tg_send(text: str) -> None:
    if DRY_RUN:
        print("---- TELEGRAM ----\n" + text)
        return
    for attempt in range(5):
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=30,
        )
        if r.status_code == 429:
            time.sleep(r.json().get("parameters", {}).get("retry_after", 5))
            continue
        r.raise_for_status()
        return


def tg_send_long(lines: list[str]) -> None:
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) + 1 > 3900:
            tg_send(chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        tg_send(chunk)


# ------------------------------------------------------------------- main
def esc(s: str) -> str:
    return html.escape(s, quote=False)


def link(title: str, url: str) -> str:
    t = esc(title if len(title) <= 80 else title[:77] + "…")
    return f'<a href="{html.escape(url)}">{t}</a>' if url else f"«{t}»"


def short(v: str) -> str:
    v = v or "—"
    return esc(v if len(v) <= 60 else v[:57] + "…")


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def main() -> None:
    global TG_CHAT_ID
    now = datetime.now(timezone.utc)
    state = load_state()
    if not TG_CHAT_ID:
        TG_CHAT_ID = (state or {}).get("chat") or ("0" if DRY_RUN else find_group_chat())
        if not TG_CHAT_ID:
            print("bot is not in a group yet — add it to the group and write any message there")
            return

    users = {}
    for u in paginate("GET", "/users"):
        users[u["id"]] = u.get("name") or ("бот" if u.get("type") == "bot" else "кто-то")

    current = {}
    for o in paginate("POST", "/search", {"sort": {"direction": "descending",
                                                    "timestamp": "last_edited_time"}}):
        if o.get("archived") or o.get("in_trash"):
            continue
        current[o["id"]] = snapshot(o, users)
    db_titles = {i: s["t"] for i, s in current.items() if s["k"] == "database"}
    print(f"visible objects: {len(current)}")

    if state is None:
        state = {"pages": current, "comments": {}, "chat": TG_CHAT_ID}
        save_state(state)
        tg_send(f"🤖 Бот DAY PEOPLE подключён. Слежу за {len(current)} страницами и базами в Notion "
                f"и буду писать сюда обо всех изменениях.")
        return

    prev = state["pages"]
    seen_comments = state.get("comments", {})
    events = []  # (author_id, action, where_db, line)
    settled = now - timedelta(minutes=SETTLE_MIN)

    def where(s):
        return db_titles.get(s.get("db")) or prev.get(s.get("db") or "", {}).get("t")

    new_prev = {}
    for pid, cur in current.items():
        old = prev.get(pid)
        if old is None:
            if cur["let"] and parse_ts(cur["let"]) > settled:
                continue  # just created — wait until it has a title
            kind = "базу" if cur["k"] == "database" else "страницу"
            events.append((cur["cby"] or cur["by"], "create", where(cur),
                           f"🆕 создал {kind} {link(cur['t'], cur['u'])}"))
            new_prev[pid] = cur
            continue
        if cur["let"] == old["let"]:
            new_prev[pid] = old
            continue
        if cur["let"] and parse_ts(cur["let"]) > settled:
            new_prev[pid] = old  # still being edited — keep old snapshot, report later
            continue
        details = []
        if cur["t"] != old["t"]:
            details.append(f"название: {short(old['t'])} → {short(cur['t'])}")
        for name in sorted(set(old["p"]) | set(cur["p"])):
            a, b = old["p"].get(name), cur["p"].get(name)
            if a != b and not (a in (None, "") and b in (None, "")):
                details.append(f"{esc(name)}: {short(a)} → {short(b)}")
        line = f"✏️ изменил {link(cur['t'], cur['u'])}"
        if details:
            line += "\n      " + "\n      ".join(details[:6])
            if len(details) > 6:
                line += f"\n      …и ещё {len(details) - 6}"
        elif cur["k"] == "page":
            line += " (текст)"
        events.append((cur["by"], "edit", where(cur), line))
        new_prev[pid] = cur

    # Missing from search → confirm deletion directly (search can lag).
    for pid, old in prev.items():
        if pid in current:
            continue
        o = notion("GET", f"/{'databases' if old['k'] == 'database' else 'pages'}/{pid}")
        if o and not (o.get("archived") or o.get("in_trash")):
            new_prev[pid] = old
            continue
        who = (o or {}).get("last_edited_by", {}).get("id")
        kind = "базу" if old["k"] == "database" else "страницу"
        title = esc(old["t"])
        if o is None:
            events.append((None, "delete", where(old), f"🚫 закрыт доступ или удалена {kind} «{title}»"))
        else:
            events.append((who, "delete", where(old), f"🗑 удалил {kind} «{title}»"))

    # Page-level comments on recently active pages.
    window = now - timedelta(days=COMMENT_WINDOW_DAYS)
    for pid, cur in current.items():
        if cur["k"] != "page" or not cur["let"] or parse_ts(cur["let"]) < window:
            continue
        known = set(seen_comments.get(pid, []))
        fresh = []
        for c in paginate("GET", "/comments", params={"block_id": pid}):
            if c["id"] in known:
                continue
            known.add(c["id"])
            fresh.append(c)
        seen_comments[pid] = sorted(known)[-200:]
        if pid not in prev:
            continue  # first time we see this page — just remember existing comments
        for c in fresh:
            text = plain(c.get("rich_text"))
            text = text if len(text) <= 200 else text[:197] + "…"
            events.append(((c.get("created_by") or {}).get("id"), "comment", where(cur),
                           f"💬 комментарий в {link(cur['t'], cur['u'])}:\n      «{esc(text)}»"))
    # forget comment bookkeeping for pages that are gone
    seen_comments = {k: v for k, v in seen_comments.items() if k in current}

    state = {"pages": new_prev, "comments": seen_comments, "chat": TG_CHAT_ID}

    if events:
        by_author = defaultdict(list)
        for author, action, db, line in events:
            by_author[author].append((action, db, line))
        out = []
        for author, items in by_author.items():
            out.append(f"<b>👤 {esc(users.get(author, 'Кто-то'))}</b>")
            groups = defaultdict(list)
            for action, db, line in items:
                groups[(action, db)].append(line)
            for (action, db), lines in groups.items():
                if len(lines) > COLLAPSE_AFTER:
                    verb = {"create": "создал", "edit": "изменил", "delete": "удалил",
                            "comment": "оставил комментариев:"}[action]
                    place = f" в «{esc(db)}»" if db else ""
                    out.append(f"📦 {verb} {len(lines)} записей{place}, например:")
                    out.extend("   " + l for l in lines[:3])
                else:
                    suffix = f"  <i>· {esc(db)}</i>" if db else ""
                    out.extend(l.replace("\n", suffix + "\n", 1) if "\n" in l else l + suffix
                               for l in lines)
            out.append("")
        tg_send_long(out)

    save_state(state)
    print(f"events sent: {len(events)}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # keep secrets/titles out of public logs
        status = getattr(getattr(e, "response", None), "status_code", "")
        print(f"error: {type(e).__name__} {status}", file=sys.stderr)
        raise SystemExit(1)
