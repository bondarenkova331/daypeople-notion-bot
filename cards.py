"""Branded image cards for Telegram: one card per Notion page update.

Dark background, DAY PEOPLE orange, logo in the header, one row per person/action.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
FONT = {
    "title": os.path.join(HERE, "fonts", "InterDisplay-ExtraBold.otf"),
    "bold": os.path.join(HERE, "fonts", "Inter-SemiBold.otf"),
    "med": os.path.join(HERE, "fonts", "Inter-Medium.otf"),
    "reg": os.path.join(HERE, "fonts", "Inter-Regular.otf"),
}
LOGO = next((os.path.join(HERE, p) for p in ("logo.png", "image.png", "logo.jpg")
             if os.path.exists(os.path.join(HERE, p))), None)

W = 1080
PAD = 72
BG = (14, 14, 14)
PANEL = (26, 26, 26)
LINE = (44, 44, 44)
WHITE = (245, 242, 236)  # warm cream, like the brand
GREY = (140, 136, 130)
DIM = (96, 93, 89)
ORANGE = (242, 92, 5)
PEOPLE = {  # avatar colours
    "Витя": (242, 92, 5), "Дима": (64, 132, 255), "Claude": (204, 120, 92), "GPT": (16, 163, 127),
}
DUBAI = timezone(timedelta(hours=4))


def font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT[kind], size)


def wrap(draw: ImageDraw.ImageDraw, text: str, f, width: int, max_lines: int = 0) -> list[str]:
    words, lines, cur = (text or "").split(), [], ""
    for w in words:
        test = (cur + " " + w).strip()
        if draw.textlength(test, font=f) <= width:
            cur = test
            continue
        if cur:
            lines.append(cur)
        cur = w
        while draw.textlength(cur, font=f) > width:  # very long word
            cut = len(cur)
            while cut > 1 and draw.textlength(cur[:cut], font=f) > width:
                cut -= 1
            lines.append(cur[:cut])
            cur = cur[cut:]
    if cur:
        lines.append(cur)
    if max_lines and len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and draw.textlength(last + "…", font=f) > width:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
    return lines


class Canvas:
    """Draws top-to-bottom on a tall scratch image, then crops to content."""

    def __init__(self):
        self.img = Image.new("RGB", (W, 6000), BG)
        self.d = ImageDraw.Draw(self.img)
        self.y = 0

    def text(self, x, s, f, fill, lh=None):
        self.d.text((x, self.y), s, font=f, fill=fill)
        self.y += lh or int(f.size * 1.32)

    def done(self) -> bytes:
        self.y += PAD
        img = self.img.crop((0, 0, W, max(self.y, 540)))
        out = BytesIO()
        img.save(out, "PNG", optimize=True)
        return out.getvalue()


def header(c: Canvas, label: str):
    c.d.rectangle((0, 0, W, 10), fill=ORANGE)
    c.y = 54
    x = PAD
    if LOGO:
        try:
            logo = Image.open(LOGO).convert("RGBA")
            box = logo.getchannel("A").point(lambda a: 255 if a > 20 else 0).getbbox()
            if box:
                logo = logo.crop(box)
            logo.thumbnail((64, 64))
            # cream badge behind the black/orange mark
            badge = Image.new("RGBA", (84, 84), (0, 0, 0, 0))
            ImageDraw.Draw(badge).ellipse((0, 0, 83, 83), fill=WHITE)
            badge.alpha_composite(logo, ((84 - logo.width) // 2, (84 - logo.height) // 2))
            c.img.paste(badge, (x, c.y - 6), badge)
            x += 108
        except Exception:
            pass
    c.d.text((x, c.y), "DAY PEOPLE", font=font("bold", 30), fill=WHITE)
    c.d.text((x, c.y + 40), label, font=font("med", 24), fill=GREY)
    now = datetime.now(DUBAI).strftime("%H:%M")
    tw = c.d.textlength(now, font=font("med", 26))
    c.d.text((W - PAD - tw, c.y + 10), now, font=font("med", 26), fill=GREY)
    c.y += 120


def avatar(c: Canvas, x: int, y: int, name: str, size: int = 64):
    col = PEOPLE.get(name, (90, 88, 84))
    c.d.ellipse((x, y, x + size, y + size), fill=col)
    ini = (name or "•")[0].upper()
    f = font("bold", int(size * 0.46))
    tw = c.d.textlength(ini, font=f)
    c.d.text((x + (size - tw) / 2, y + size * 0.2), ini, font=f, fill=(255, 255, 255))


def pill(c: Canvas, x: int, y: int, text: str, f, fg, bg, outline=None) -> int:
    tw = c.d.textlength(text, font=f)
    w = int(tw + 36)
    c.d.rounded_rectangle((x, y, x + w, y + f.size + 24), radius=(f.size + 24) // 2, fill=bg, outline=outline,
                          width=2 if outline else 0)
    c.d.text((x + 18, y + 10), text, font=f, fill=fg)
    return w


def action_row(c: Canvas, who: str, action: str):
    """Avatar + 'Name  action'."""
    y0 = c.y
    if who:
        avatar(c, PAD, y0, who)
        c.d.text((PAD + 88, y0 + 2), who, font=font("bold", 32), fill=WHITE)
        c.d.text((PAD + 88, y0 + 40), action, font=font("reg", 26), fill=GREY)
    else:
        c.d.ellipse((PAD, y0, PAD + 64, y0 + 64), outline=LINE, width=3)
        c.d.text((PAD + 88, y0 + 16), action, font=font("med", 30), fill=WHITE)
    c.y = y0 + 88


def comment_box(c: Canvas, text: str):
    f = font("reg", 34)
    lines = wrap(c.d, text, f, W - 2 * PAD - 120, max_lines=7)
    lh = int(f.size * 1.42)
    h = len(lines) * lh + 52
    x0 = PAD + 88
    c.d.rounded_rectangle((x0, c.y, W - PAD, c.y + h), radius=22, fill=PANEL)
    c.d.rounded_rectangle((x0, c.y, x0 + 8, c.y + h), radius=4, fill=ORANGE)
    yy = c.y + 26
    for ln in lines:
        c.d.text((x0 + 38, yy), ln, font=f, fill=WHITE)
        yy += lh
    c.y += h + 28


def change_rows(c: Canvas, details):
    x0 = PAD + 88
    for field, old, new in details[:5]:
        c.d.text((x0, c.y), field.upper(), font=font("med", 22), fill=DIM)
        c.y += 38
        f = font("med", 28)
        old_s = (old or "—")[:28]
        new_s = (new or "—")[:34]
        w = pill(c, x0, c.y, old_s, f, GREY, BG, outline=LINE)
        ax = x0 + w + 18
        c.d.text((ax, c.y + 8), "→", font=font("bold", 32), fill=ORANGE)
        pill(c, ax + 52, c.y, new_s, f, (20, 12, 6), ORANGE)
        c.y += f.size + 24 + 26
    if len(details) > 5:
        c.d.text((x0, c.y), f"и ещё {len(details) - 5}", font=font("reg", 24), fill=GREY)
        c.y += 40
    c.y += 8


def page_card(title: str, section: str | None, items: list[dict], name_of) -> bytes:
    """items: dicts with kind/who/details/text (see notion_watch.render)."""
    c = Canvas()
    header(c, "обновление в Notion")
    if section:
        c.text(PAD, section.upper(), font("bold", 24), ORANGE, lh=48)
    for ln in wrap(c.d, title, font("title", 64), W - 2 * PAD, max_lines=3):
        c.text(PAD, ln, font("title", 64), WHITE, lh=78)
    c.y += 30
    c.d.line((PAD, c.y, W - PAD, c.y), fill=LINE, width=2)
    c.y += 44
    verbs = {"create": "создал страницу", "delete": "удалил страницу",
             "comment": "оставил комментарий", "edit": "изменил"}
    anon = {"create": "Новая страница", "delete": "Страница удалена",
            "comment": "Новый комментарий", "edit": "Страница изменена"}
    for e in items[:6]:
        who = name_of(e["who"])
        if e["kind"] == "edit" and not e["details"]:
            act = "обновил текст" if who else "Обновлён текст"
        else:
            act = verbs[e["kind"]] if who else anon[e["kind"]]
        action_row(c, who, act)
        if e["kind"] == "comment" and e.get("text"):
            comment_box(c, e["text"])
        elif e["kind"] == "edit" and e["details"]:
            change_rows(c, e["details"])
        else:
            c.y += 12
    if len(items) > 6:
        c.text(PAD + 88, f"и ещё {len(items) - 6} действий", font("reg", 26), GREY)
    return c.done()


def bulk_card(who: str, count: int, section: str | None, titles: list[str]) -> bytes:
    c = Canvas()
    header(c, "обновление в Notion")
    if section:
        c.text(PAD, section.upper(), font("bold", 24), ORANGE, lh=48)
    c.text(PAD, f"+{count} записей", font("title", 64), WHITE, lh=90)
    c.d.line((PAD, c.y, W - PAD, c.y), fill=LINE, width=2)
    c.y += 44
    action_row(c, who, f"добавил {count} записей" if who else f"Добавлено {count} записей")
    for t in titles[:6]:
        for ln in wrap(c.d, "•  " + t, font("reg", 32), W - 2 * PAD - 88, max_lines=1):
            c.text(PAD + 88, ln, font("reg", 32), WHITE, lh=50)
    if count > 6:
        c.text(PAD + 88, f"и ещё {count - 6}", font("reg", 28), GREY)
    return c.done()
