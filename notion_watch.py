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

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
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
TASKS_DB = os.environ.get("TASKS_DB_ID", "41855b35194c81f5b07fec124e05491f")
COMMANDS = {
    # command: (status, assignee, description for /help)
    "task":   ("К выполнению", None,     "новая задача"),
    "idea":   ("Идеи",         None,     "идея"),
    "claude": ("К выполнению", "Claude", "задача для Claude"),
    "gpt":    ("К выполнению", "GPT",    "задача для GPT"),
    "vitya":  ("К выполнению", "Витя",   "задача для Вити"),
    "dima":   ("К выполнению", "Дима",   "задача для Димы"),
}
HELP = ("<b>Команды DAY PEOPLE</b>\n\n"
        "/task — задача\n"
        "/idea — идея\n"
        "/claude — задача для Claude\n"
        "/gpt — задача для GPT\n"
        "/vitya · /dima — задача для Вити или Димы\n\n"
        "<i>Первая строка — название, дальше — описание. Пример:</i>\n"
        "<code>/claude Пост про DJ Run\nЛёгкий тон, до 600 знаков</code>")


def tg_api(method: str, **payload) -> dict:
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/{method}", json=payload, timeout=60)
    if r.status_code == 429:
        time.sleep(r.json().get("parameters", {}).get("retry_after", 5))
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/{method}", json=payload, timeout=60)
    return r.json()


def create_task(cmd: str, text: str, author: str) -> dict | None:
    status, assignee, _ = COMMANDS[cmd]
    lines = text.strip().split("\n", 1)
    title = lines[0][:200]
    body = lines[1].strip() if len(lines) > 1 else ""
    stamp = datetime.now(timezone(timedelta(hours=4))).strftime("%d.%m")
    to = assignee or "команда"
    props = {
        "Задача": {"title": [{"text": {"content": title}}]},
        "Статус": {"status": {"name": status}},
        "Передача": {"rich_text": [{"text": {"content": f"[{stamp} · {author} → {to}] из Telegram"}}]},
    }
    if assignee:
        props["Исполнитель"] = {"select": {"name": assignee}}
    payload = {"parent": {"database_id": TASKS_DB}, "properties": props}
    if body:
        payload["children"] = [{"object": "block", "type": "paragraph", "paragraph": {
            "rich_text": [{"text": {"content": chunk}} for chunk in
                          (body[i:i + 1900] for i in range(0, len(body), 1900))]}}]
    return notion("POST", "/pages", json=payload)


def process_updates(state: dict) -> None:
    """Read new Telegram updates: learn the group chat id and run /commands."""
    global TG_CHAT_ID
    if DRY_RUN:
        return
    data = tg_api("getUpdates", offset=state.get("tg_offset", 0), timeout=0,
                  allowed_updates=["message", "edited_message", "my_chat_member"])
    for u in data.get("result", []):
        state["tg_offset"] = u["update_id"] + 1
        mcm = u.get("my_chat_member") or {}
        if (mcm.get("chat") or {}).get("type") in ("group", "supergroup") and \
                (mcm.get("new_chat_member") or {}).get("status") in ("member", "administrator"):
            if not os.environ.get("TELEGRAM_CHAT_ID"):
                TG_CHAT_ID = str(mcm["chat"]["id"])
        edited = "edited_message" in u
        m = u.get("message") or u.get("edited_message") or {}
        if m.get("migrate_to_chat_id") and str(m["chat"]["id"]) == TG_CHAT_ID:
            TG_CHAT_ID = str(m["migrate_to_chat_id"])
        if m.get("migrate_from_chat_id") and str(m["migrate_from_chat_id"]) == TG_CHAT_ID:
            TG_CHAT_ID = str(m["chat"]["id"])
        if str((m.get("chat") or {}).get("id")) == TG_CHAT_ID and m.get("is_topic_message"):
            created = m.get("forum_topic_created") or (m.get("reply_to_message") or {}).get("forum_topic_created") or {}
            if "notion" in created.get("name", "").lower():
                state["topic_notion"] = m.get("message_thread_id")
        if edited:
            continue
        if not TG_CHAT_ID and (m.get("chat") or {}).get("type") in ("group", "supergroup"):
            TG_CHAT_ID = str(m["chat"]["id"])
        if str((m.get("chat") or {}).get("id")) != TG_CHAT_ID:
            continue  # only our group may use commands
        text = m.get("text") or m.get("caption") or ""
        if not text.startswith("/"):
            continue
        head, _, rest = text.partition(" ")
        if "\n" in head:
            head, rest = head.split("\n", 1)[0], text.split("\n", 1)[1]
        cmd = head[1:].split("@")[0].lower()
        reply = {"chat_id": TG_CHAT_ID, "reply_to_message_id": m["message_id"],
                 "parse_mode": "HTML", "disable_web_page_preview": True}
        if m.get("is_topic_message"):
            reply["message_thread_id"] = m.get("message_thread_id")
        if cmd in ("help", "start"):
            tg_api("sendMessage", text=HELP, **reply)
        elif cmd in COMMANDS:
            if not rest.strip():
                tg_api("sendMessage", text=f"Напиши текст после команды, например:\n<code>/{cmd} Что сделать</code>", **reply)
                continue
            who = " ".join(filter(None, [m["from"].get("first_name"), m["from"].get("last_name")])) or "Telegram"
            page = create_task(cmd, rest, who)
            if page:
                state.setdefault("own_pages", []).append(page["id"])
                state["own_pages"] = state["own_pages"][-300:]
                title = rest.strip().split("\n", 1)[0]
                status, assignee, _ = COMMANDS[cmd]
                meta_line = " · ".join(filter(None, [assignee, status]))
                tg_api("sendMessage", text=f"✅ Добавил в Notion\n<b>{link(title, page.get('url', ''))}</b>\n"
                                           f"<i>{esc(meta_line)}</i>", **reply)
            else:
                tg_api("sendMessage", text="⚠️ Не получилось записать в Notion", **reply)


def ensure_commands(state: dict) -> None:
    if DRY_RUN or state.get("commands_v") == 1:
        return
    cmds = [{"command": c, "description": d[2]} for c, d in COMMANDS.items()]
    cmds.append({"command": "help", "description": "список команд"})
    if tg_api("setMyCommands", commands=cmds).get("ok"):
        state["commands_v"] = 1


def avatar_jpeg() -> bytes | None:
    """Logo file from the repo → square JPG on white (Telegram crops avatars to a circle)."""
    path = next((p for p in ("logo.jpg", "logo.png", "image.png") if os.path.exists(p)), None)
    if not path:
        return None
    from io import BytesIO
    from PIL import Image
    im = Image.open(path).convert("RGBA")
    box = im.getchannel("A").point(lambda a: 255 if a > 20 else 0).getbbox()
    if box:
        im = im.crop(box)
    w, h = im.size
    side = int(max(w, h) / 0.68)
    canvas = Image.new("RGBA", (side, side), (255, 255, 255, 255))
    canvas.alpha_composite(im, ((side - w) // 2, (side - h) // 2))
    out = BytesIO()
    canvas.convert("RGB").resize((640, 640), Image.LANCZOS).save(out, "JPEG", quality=95)
    return out.getvalue()


def sync_avatar(state: dict) -> None:
    """Set the bot's profile photo from the logo (once per logo version)."""
    if DRY_RUN:
        return
    data = avatar_jpeg()
    if not data:
        return
    digest = hashlib.sha256(data).hexdigest()
    if state.get("avatar") == digest:
        return
    r = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/setMyProfilePhoto",
        data={"photo": json.dumps({"type": "static", "photo": "attach://logo"})},
        files={"logo": ("logo.jpg", data, "image/jpeg")},
        timeout=60,
    )
    print(f"avatar update: {r.status_code}")
    if r.ok:
        state["avatar"] = digest


NOTION_THREAD = None  # forum topic "Notion" (message_thread_id), learned from updates


def tg_send(text: str) -> None:
    """Post to the group — into the "Notion" topic when the group has topics."""
    global TG_CHAT_ID
    if DRY_RUN:
        print("---- TELEGRAM ----\n" + text)
        return
    for attempt in range(5):
        payload = {"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML",
                   "disable_web_page_preview": True}
        if NOTION_THREAD:
            payload["message_thread_id"] = NOTION_THREAD
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          json=payload, timeout=30)
        params = (r.json() if r.content else {}).get("parameters", {})
        if r.status_code == 429:
            time.sleep(params.get("retry_after", 5))
            continue
        if params.get("migrate_to_chat_id"):  # group became a supergroup
            TG_CHAT_ID = str(params["migrate_to_chat_id"])
            continue
        r.raise_for_status()
        return


# ------------------------------------------------------------------- main
def esc(s: str) -> str:
    return html.escape(s, quote=False)


def link(title: str, url: str) -> str:
    t = esc(title if len(title) <= 80 else title[:77] + "…")
    return f'<a href="{html.escape(url)}">{t}</a>' if url else f"«{t}»"


def short(v) -> str:
    return esc(clip(v or "—", 50))


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


NAMES = {  # how people are called in the chat
    "viktor bondarenko": "Витя", "виктор бондаренко": "Витя", "artemiy vasilchuk": "Витя",
    "дмитрий бабков": "Дима", "dmitry babkov": "Дима",
}
COMMENT_MAX = 280
MAX_PAGES_PER_DIGEST = 12


def who_name(uid, users: dict) -> str:
    full = users.get(uid) or ""
    if not full or "telegram bot" in full.lower():
        return ""  # unknown actor or our own integration — show the action without a name
    if full.lower() in NAMES:
        return NAMES[full.lower()]
    return full.split()[0]


def clip(t: str, n: int) -> str:
    t = " ".join((t or "").split())
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def render(events: list, users: dict) -> list[str]:
    """One block per Notion page; blank line between blocks; quotes for comments."""
    # collapse bulk creations: same author, same database, many new pages
    bulk = defaultdict(list)
    for e in events:
        if e["kind"] == "create":
            bulk[(e["who"], e["db"])].append(e)
    collapsed = {k: v for k, v in bulk.items() if len(v) > COLLAPSE_AFTER}
    skip = {id(e) for v in collapsed.values() for e in v}

    pages, order = defaultdict(list), []
    for e in events:
        if id(e) in skip:
            continue
        if e["page"] not in pages:
            order.append(e["page"])
        pages[e["page"]].append(e)

    blocks = []
    for (who, db), items in collapsed.items():
        w = who_name(who, users)
        lines = [(f"📦 <b>{esc(w)}</b> добавил {len(items)} записей" if w else f"📦 Добавлено {len(items)} записей")
                 + (f" в «{esc(db)}»" if db else "")]
        for e in items[:3]:
            lines.append(f"   • {link(e['title'], e['url'])}")
        if len(items) > 3:
            lines.append(f"   <i>и ещё {len(items) - 3}</i>")
        blocks.append("\n".join(lines))

    for pid in order[:MAX_PAGES_PER_DIGEST]:
        items = pages[pid]
        head = items[-1]
        title = (f"<b>{link(head['title'], head['url'])}</b>" if head["url"]
                 else f"<b>{esc(clip(head['title'], 80))}</b>")
        lines = [f"📄 {title}"]
        if head["db"]:
            lines.append(f"<i>{esc(head['db'])}</i>")
        lines.append("")
        for e in items:
            who = who_name(e["who"], users)
            name = f"<b>{esc(who)}</b>"
            if e["kind"] == "create":
                lines.append(f"🆕 {name} создал страницу" if who else "🆕 Новая страница")
            elif e["kind"] == "delete":
                lines.append(f"🗑 {name} удалил страницу" if who else "🗑 Страница удалена")
            elif e["kind"] == "comment":
                lines.append(f"💬 {name or 'Комментарий'}")
                lines.append(f"<blockquote>{esc(clip(e['text'], COMMENT_MAX))}</blockquote>")
            elif e["details"]:
                lines.append(f"✏️ {name} изменил" if who else "✏️ Изменено")
                for field, a_, b_ in e["details"][:5]:
                    lines.append(f"      {esc(field)}:  {short(a_)}  →  <b>{short(b_)}</b>")
                if len(e["details"]) > 5:
                    lines.append(f"      <i>и ещё {len(e['details']) - 5} полей</i>")
            else:
                lines.append(f"✏️ {name} обновил текст" if who else "✏️ Обновлён текст")
        blocks.append("\n".join(lines).rstrip())
    if len(order) > MAX_PAGES_PER_DIGEST:
        blocks.append(f"<i>…и ещё {len(order) - MAX_PAGES_PER_DIGEST} страниц обновлено</i>")
    return blocks


def tg_send_blocks(blocks: list[str]) -> None:
    sep = "\n\n· · ·\n\n"
    chunk = ""
    for b in blocks:
        if chunk and len(chunk) + len(sep) + len(b) > 3800:
            tg_send(chunk)
            chunk = ""
        chunk = b if not chunk else chunk + sep + b
    if chunk:
        tg_send(chunk)


def main() -> None:
    global TG_CHAT_ID
    now = datetime.now(timezone.utc)
    state = load_state()
    meta = {k: v for k, v in (state or {}).items() if k not in ("pages", "comments")}
    if not TG_CHAT_ID:
        TG_CHAT_ID = meta.get("chat") or ("0" if DRY_RUN else "")
    process_updates(meta)
    sync_avatar(meta)
    if not TG_CHAT_ID:
        save_state({**(state or {}), **meta}) if state else None
        print("bot is not in a group yet — add it to the group")
        return
    meta["chat"] = TG_CHAT_ID
    global NOTION_THREAD
    NOTION_THREAD = meta.get("topic_notion")
    ensure_commands(meta)

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
        state = {"pages": current, "comments": {}, **meta}
        save_state(state)
        tg_send(f"🤖 Бот DAY PEOPLE подключён. Слежу за {len(current)} страницами и базами в Notion "
                f"и буду писать сюда обо всех изменениях.\n\nЧтобы сразу добавить задачу или идею в Notion — /help")
        return

    prev = state["pages"]
    seen_comments = state.get("comments", {})
    events = []  # dicts: page, title, url, db, kind, who, details, text
    settled = now - timedelta(minutes=SETTLE_MIN)

    def where(s):
        return db_titles.get(s.get("db")) or prev.get(s.get("db") or "", {}).get("t")

    def ev(pid, snap, kind, who, details=None, text=None):
        events.append({"page": pid, "title": snap["t"], "url": snap.get("u", ""),
                       "db": where(snap), "kind": kind, "who": who,
                       "details": details or [], "text": text})

    new_prev = {}
    for pid, cur in current.items():
        old = prev.get(pid)
        if old is None:
            if pid in meta.get("own_pages", []):
                new_prev[pid] = cur
                continue  # created by a bot command — already confirmed in the chat
            if cur["let"] and parse_ts(cur["let"]) > settled:
                continue  # just created — wait until it has a title
            ev(pid, cur, "create", cur["cby"] or cur["by"])
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
            details.append(("Название", old["t"], cur["t"]))
        for name in sorted(set(old["p"]) | set(cur["p"])):
            a_, b_ = old["p"].get(name), cur["p"].get(name)
            if a_ != b_ and not (a_ in (None, "") and b_ in (None, "")):
                details.append((name, a_, b_))
        ev(pid, cur, "edit", cur["by"], details)
        new_prev[pid] = cur

    # Missing from search → confirm deletion directly (search can lag).
    for pid, old in prev.items():
        if pid in current:
            continue
        o = notion("GET", f"/{'databases' if old['k'] == 'database' else 'pages'}/{pid}")
        if o and not (o.get("archived") or o.get("in_trash")):
            new_prev[pid] = old
            continue
        ev(pid, {**old, "u": ""}, "delete", (o or {}).get("last_edited_by", {}).get("id"))

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
            ev(pid, cur, "comment", (c.get("created_by") or {}).get("id"),
               text=plain(c.get("rich_text")))
    seen_comments = {k: v for k, v in seen_comments.items() if k in current}

    state = {"pages": new_prev, "comments": seen_comments, **meta}

    if events:
        tg_send_blocks(render(events, users))

    save_state(state)
    print(f"events sent: {len(events)}")


if __name__ == "__main__":
    if not (NOTION_TOKEN and TG_TOKEN):
        print("secrets NOTION_TOKEN / TELEGRAM_BOT_TOKEN are not set yet — nothing to do")
        raise SystemExit(0)
    try:
        main()
    except Exception as e:  # keep secrets/titles out of public logs
        status = getattr(getattr(e, "response", None), "status_code", "")
        print(f"error: {type(e).__name__} {status}", file=sys.stderr)
        raise SystemExit(1)
