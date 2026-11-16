"""Put the DAY PEOPLE logo on the Telegram group (bot must be admin with 'change group info')."""
import hashlib
import json

import requests

import notion_watch as nw

state = nw.load_state()
chat = (state or {}).get("chat")
data = nw.avatar_jpeg()
if state and chat and data and nw.TG_TOKEN:
    digest = hashlib.sha256(data).hexdigest()
    if state.get("chat_photo") != digest:
        r = requests.post(f"https://api.telegram.org/bot{nw.TG_TOKEN}/setChatPhoto",
                          data={"chat_id": chat},
                          files={"photo": ("logo.jpg", data, "image/jpeg")}, timeout=60)
        ok = r.ok and r.json().get("ok")
        print(f"chat photo update: {r.status_code} {json.dumps(r.json().get('description', ''))}")
        if ok:
            state["chat_photo"] = digest
            nw.save_state(state)
