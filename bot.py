"""DAY PEOPLE — Telegram bot.

One group chat, no topics, no Notion digests. What it does:
  • "Claude, сделай …" in the chat        → task for Claude in Notion (status «Делаем»)
  • message with #реф                     → saved to the «Референсы» page in Notion
  • 👍 Ок under a delivery (video/image)  → task «Готово» in Notion
  • ✏️ Правки under a delivery            → asks «что поправить?», the reply goes into
                                            the task, task back to Claude («Делаем»)
Deliveries themselves are sent by Claude from Mr T's Mac (send_to_tg.py) with
callback buttons ok:<page_id> / fix:<page_id>.

Runs in GitHub Actions: one run long-polls Telegram for ~55 min, then the
workflow starts the next run. State (offset, chat id, pending edits) is kept
encrypted between runs.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
from cryptography.fernet import Fernet, InvalidToken

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")  # empty → learned from the group
RUN_SECONDS = int(os.environ.get("RUN_SECONDS", str(55 * 60)))
STATE_PATH = os.environ.get("STATE_PATH", "state.enc")
STATE_KEY = base64.urlsafe_b64encode(hashlib.sha256(b"daypeople-state:" + NOTION_TOKEN.encode()).digest())

TASKS_DB = os.environ.get("TASKS_DB_ID", "41855b35194c81f5b07fec124e05491f")
REFS_PAGE = os.environ.get("REFS_PAGE_ID", "41a55b35194c814b8dedd98980356392")
S_TODO, S_REVIEW, S_DONE = "Делаем", "Посмотреть", "Готово"
DUBAI = timezone(timedelta(hours=4))

NOTION = "https://api.notion.com/v1"
HEADERS = {"Authorization": f"Bearer {NOTION_TOKEN}", "Notion-Version": "2022-06-28",
           "Content-Type": "application/json"}

CLAUDE_RE = re.compile(r"^\s*(claude|клод|клауд|клоуд)\b[\s,:—-]*", re.I)
REF_RE = re.compile(r"#(реф|ref)\b", re.I)
NAMES = {"viktor bondarenko": "Витя", "виктор бондаренко": "Витя", "artemiy vasilchuk": "Витя",
         "дмитрий бабков": "Дима", "dmitry babkov": "Дима"}
HELP = ("<b>Как со мной работать</b>\n\n"
        "• <b>Claude, сделай …</b> — задача для Claude (можно с видео или ссылкой)\n"
        "• <b>#реф</b> в сообщении — референс уйдёт в Notion\n"
        "• Под готовой работой: <b>👍 Ок</b> — принято, <b>✏️ Правки</b> — напиши, что поменять\n\n"
        "Всё остальное — обычная переписка, я её не трогаю.")


# ------------------------------------------------------------------ helpers
def notion(method: str, path: str, **kw) -> dict | None:
    for attempt in range(5):
        r = requests.request(method, NOTION + path, headers=HEADERS, timeout=30, **kw)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(float(r.headers.get("Retry-After", 2 ** attempt)))
            continue
        if not r.ok:
            print(f"notion {method} {r.status_code}")
            return None
        return r.json()
    return None


def tg(method: str, files: dict | None = None, **payload) -> dict:
    url = f"https://api.telegram.org/bot{TG_TOKEN}/{method}"
    for _ in range(3):
        try:
            if files:
                r = requests.post(url, data=payload, files=files, timeout=90)
            else:
                r = requests.post(url, json=payload, timeout=90)
        except requests.RequestException:
            time.sleep(3)
            continue
        if r.status_code == 429:
            time.sleep((r.json().get("parameters") or {}).get("retry_after", 5))
            continue
        return r.json() if r.content else {}
    return {}


def load_state() -> dict:
    try:
        with open(STATE_PATH, "rb") as f:
            return json.loads(Fernet(STATE_KEY).decrypt(f.read()))
    except (FileNotFoundError, InvalidToken, ValueError):
        return {}


def save_state(state: dict) -> None:
    with open(STATE_PATH, "wb") as f:
        f.write(Fernet(STATE_KEY).encrypt(json.dumps(state, ensure_ascii=False).encode()))


def who(m: dict) -> str:
    f = m.get("from") or {}
    full = " ".join(filter(None, [f.get("first_name"), f.get("last_name")]))
    return NAMES.get(full.lower()) or f.get("first_name") or "кто-то"


def msg_link(m: dict) -> str:
    cid = str(m["chat"]["id"])
    return f"https://t.me/c/{cid[4:]}/{m['message_id']}" if cid.startswith("-100") else ""


def media_kind(m: dict) -> str:
    for k, n in (("video", "видео"), ("photo", "фото"), ("animation", "гиф"), ("voice", "голосовое"),
                 ("video_note", "кружок"), ("audio", "аудио"), ("document", "файл")):
        if m.get(k):
            return n
    return ""


def rich(text: str, **ann) -> list:
    out = []
    for i in range(0, len(text), 1900):
        part = {"text": {"content": text[i:i + 1900]}}
        if ann:
            part["annotations"] = ann
        out.append(part)
    return out


def note_block(m: dict, prefix: str, text: str) -> dict:
    """One paragraph: «Дима · 18.11 правки: текст [видео] ↗ Telegram»."""
    stamp = datetime.now(DUBAI).strftime("%d.%m %H:%M")
    parts = rich(f"{who(m)} · {stamp} {prefix}", bold=True) + rich("  " + text if text else "")
    kind = media_kind(m)
    if kind:
        parts += rich(f"  [{kind} в Telegram]", italic=True)
    link = msg_link(m)
    if link:
        parts.append({"text": {"content": "  ↗ открыть", "link": {"url": link}}})
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": parts[:100]}}


def reply(m: dict, text: str, **extra) -> dict:
    return tg("sendMessage", chat_id=m["chat"]["id"], text=text, parse_mode="HTML",
              reply_to_message_id=m["message_id"], disable_web_page_preview=True, **extra)


def react(m: dict, emoji: str = "👍") -> None:
    tg("setMessageReaction", chat_id=m["chat"]["id"], message_id=m["message_id"],
       reaction=[{"type": "emoji", "emoji": emoji}])


# ------------------------------------------------------------------ actions
def task_for_claude(m: dict, text: str) -> None:
    body = CLAUDE_RE.sub("", text, count=1).strip()
    title = (body.split("\n", 1)[0] or f"Задача от {who(m)} ({media_kind(m) or 'сообщение'})")[:150]
    page = notion("POST", "/pages", json={
        "parent": {"database_id": TASKS_DB},
        "properties": {"Задача": {"title": [{"text": {"content": title}}]},
                       "Статус": {"status": {"name": S_TODO}},
                       "Кто": {"select": {"name": "Claude"}}},
        "children": [note_block(m, "в Telegram:", body)],
    })
    if page:
        reply(m, "✅ Записал. Возьму на ближайшей проверке (раз в 3 часа).")
    else:
        reply(m, "⚠️ Не получилось записать задачу — напишу Вите.")


def save_ref(m: dict, text: str) -> None:
    ok = notion("PATCH", f"/blocks/{REFS_PAGE}/children",
                json={"children": [note_block(m, "реф:", REF_RE.sub("", text).strip())]})
    react(m, "👍" if ok else "🤔")


def set_status(page_id: str, status: str, to_claude: bool = False) -> bool:
    props = {"Статус": {"status": {"name": status}}}
    if to_claude:
        props["Кто"] = {"select": {"name": "Claude"}}
    return bool(notion("PATCH", f"/pages/{page_id}", json={"properties": props}))


def on_callback(cq: dict, state: dict) -> None:
    data = cq.get("data") or ""
    m = cq.get("message") or {}
    name = NAMES.get(" ".join(filter(None, [cq["from"].get("first_name"), cq["from"].get("last_name")])).lower()) \
        or cq["from"].get("first_name") or ""
    action, _, page_id = data.partition(":")
    if action == "ok" and page_id:
        ok = set_status(page_id, S_DONE)
        tg("answerCallbackQuery", callback_query_id=cq["id"], text="Принято 👍" if ok else "Не получилось, попробуй ещё")
        if ok and m:
            tg("editMessageReplyMarkup", chat_id=m["chat"]["id"], message_id=m["message_id"],
               reply_markup={"inline_keyboard": [[{"text": f"✅ Принято · {name}", "callback_data": "noop"}]]})
    elif action == "fix" and page_id:
        set_status(page_id, S_TODO, to_claude=True)
        tg("answerCallbackQuery", callback_query_id=cq["id"], text="Напиши, что поправить")
        if m:
            sent = tg("sendMessage", chat_id=m["chat"]["id"], reply_to_message_id=m["message_id"],
                      text=f"✏️ {name}, что поправить? Ответь на это сообщение.",
                      reply_markup={"force_reply": True, "selective": False})
            mid = (sent.get("result") or {}).get("message_id")
            if mid:
                pend = state.setdefault("pending", {})
                pend[str(mid)] = page_id
                pend[str(m["message_id"])] = page_id  # a reply to the delivery itself also counts
                state["pending"] = dict(list(pend.items())[-100:])
    else:
        tg("answerCallbackQuery", callback_query_id=cq["id"])


def on_message(m: dict, state: dict) -> None:
    text = (m.get("text") or m.get("caption") or "").strip()
    if (m.get("from") or {}).get("is_bot"):
        return
    rt = m.get("reply_to_message") or {}
    page_id = state.get("pending", {}).get(str(rt.get("message_id"))) if rt else None
    if page_id:  # an edit request for a delivery
        ok = notion("PATCH", f"/blocks/{page_id}/children",
                    json={"children": [note_block(m, "правки:", text)]})
        set_status(page_id, S_TODO, to_claude=True)
        react(m, "👍" if ok else "🤔")
        if ok:
            reply(m, "Передал Claude, пришлю новую версию сюда.")
        return
    if text.startswith("/"):
        if text.split()[0].split("@")[0].lower() in ("/help", "/start"):
            reply(m, HELP)
        return
    if CLAUDE_RE.match(text):
        task_for_claude(m, text)
    elif REF_RE.search(text):
        save_ref(m, text)


def process(state: dict, timeout: int) -> None:
    global TG_CHAT_ID
    data = tg("getUpdates", offset=state.get("tg_offset", 0), timeout=timeout,
              allowed_updates=["message", "callback_query", "my_chat_member"])
    for u in data.get("result", []):
        state["tg_offset"] = u["update_id"] + 1
        mcm = u.get("my_chat_member") or {}
        if (mcm.get("chat") or {}).get("type") in ("group", "supergroup") and not TG_CHAT_ID:
            TG_CHAT_ID = str(mcm["chat"]["id"])
        m = u.get("message") or {}
        cq = u.get("callback_query")
        chat = str(((cq or {}).get("message") or m).get("chat", {}).get("id", ""))
        if m.get("migrate_to_chat_id") and chat == TG_CHAT_ID:
            TG_CHAT_ID = str(m["migrate_to_chat_id"])
            continue
        if not TG_CHAT_ID and (m.get("chat") or {}).get("type") in ("group", "supergroup"):
            TG_CHAT_ID = chat
        if chat != TG_CHAT_ID:
            continue  # only our group
        try:
            if cq:
                on_callback(cq, state)
            elif m:
                on_message(m, state)
        except Exception as e:  # one bad message must not stop the bot
            print(f"update failed: {type(e).__name__}")
    state["chat"] = TG_CHAT_ID


def avatar_jpeg() -> bytes | None:
    if not os.path.exists("image.png"):
        return None
    from io import BytesIO
    from PIL import Image
    im = Image.open("image.png").convert("RGBA")
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


def sync_branding(state: dict) -> None:
    """Bot avatar + group photo from image.png, once per logo version."""
    data = avatar_jpeg()
    if not data:
        return
    digest = hashlib.sha256(data).hexdigest()
    if state.get("avatar") != digest:
        r = tg("setMyProfilePhoto", files={"logo": ("logo.jpg", data, "image/jpeg")},
               photo=json.dumps({"type": "static", "photo": "attach://logo"}))
        if r.get("ok"):
            state["avatar"] = digest
    if TG_CHAT_ID and state.get("chat_photo") != digest:
        r = tg("setChatPhoto", files={"photo": ("logo.jpg", data, "image/jpeg")}, chat_id=TG_CHAT_ID)
        if r.get("ok"):
            state["chat_photo"] = digest


def ensure_commands(state: dict) -> None:
    if state.get("commands_v") == 2:
        return
    tg("deleteMyCommands")
    if tg("setMyCommands", commands=[{"command": "help", "description": "как со мной работать"}]).get("ok"):
        state["commands_v"] = 2


def main() -> None:
    global TG_CHAT_ID
    state = load_state()
    for k in ("pages", "comments", "own_pages", "own_edits", "topic_notion", "topic_refs", "ref_groups"):
        state.pop(k, None)  # leftovers of the old Notion watcher
    TG_CHAT_ID = TG_CHAT_ID or state.get("chat", "")
    ensure_commands(state)
    sync_branding(state)
    end = time.time() + RUN_SECONDS
    while time.time() < end:
        process(state, timeout=max(1, min(50, int(end - time.time()))))
        save_state(state)


if __name__ == "__main__":
    if not (NOTION_TOKEN and TG_TOKEN):
        print("secrets NOTION_TOKEN / TELEGRAM_BOT_TOKEN are not set — nothing to do")
        raise SystemExit(0)
    try:
        main()
    except Exception as e:  # keep titles/secrets out of public logs
        print(f"error: {type(e).__name__}", file=sys.stderr)
        raise SystemExit(1)
