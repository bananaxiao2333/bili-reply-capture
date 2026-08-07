import argparse
import io
import json
import os
import re
import sys
from datetime import datetime

import requests
from PIL import Image, ImageDraw, ImageFont

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

API_VIEW = "https://api.bilibili.com/x/web-interface/view"
API_MAIN = "https://api.bilibili.com/x/v2/reply/main"
API_SUB = "https://api.bilibili.com/x/v2/reply/reply"

FONTS = (
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyh.ttf",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/Deng.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
)

WHITE = (255, 255, 255)
BG = (246, 247, 248)
TEXT = (24, 25, 28)
GRAY = (148, 153, 160)
BORDER = (231, 233, 235)
GOLD = (255, 173, 66)
BLUE = (0, 174, 236)
GREEN = (0, 173, 65)
RECAP_BG = (235, 245, 255)
RECAP_BORDER = (190, 221, 250)

CARD_W = 900
PAD = 28
AVATAR_R = 56
EMOTE_RE = re.compile(r"\[[^\[\]]{1,12}\]")

_EMOTE_CACHE = {}


def get_font(size):
    cache = getattr(get_font, "cache", {})
    if size in cache:
        return cache[size]
    font = None
    for p in FONTS:
        try:
            font = ImageFont.truetype(p, size)
            break
        except Exception:
            continue
    if font is None:
        font = ImageFont.load_default()
    cache[size] = font
    return font


def api_get(path, params=None):
    resp = requests.get(path, params=params, headers=HEADERS, timeout=25)
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"API {path} error {data.get('code')}: {data.get('message')}")
    return data.get("data")


def fetch_view(bvid):
    return api_get(API_VIEW, {"bvid": bvid})


def fetch_root_and_floors(oid, rpid):
    main = api_get(API_MAIN, {"type": 1, "oid": oid, "mode": 3, "root": rpid})
    replies_main = main.get("replies") or []
    root = main.get("root")
    if root is None or str(root.get("rpid")) != str(rpid):
        root = next((r for r in replies_main if str(r.get("rpid")) == str(rpid)), None)
    if root is None:
        raise RuntimeError("无法从 API 获取根评论")
    floors = []
    seen = set()
    for pn in range(1, 200):
        req = api_get(API_SUB, {"type": 1, "oid": oid, "root": rpid, "ps": 20, "pn": pn})
        page = req.get("replies") or []
        if not page:
            break
        for r in page:
            if str(r.get("rpid")) not in seen:
                seen.add(str(r.get("rpid")))
                floors.append(r)
        if len(page) < 20:
            break
    return root, floors


def parse_target(raw):
    m = re.search(r"reply(\d+)", raw)
    if not m:
        raise ValueError(f"URL 中未找到 #reply<id> 评论锚点: {raw}")
    rpid = m.group(1)
    bv = re.search(r"BV[\w]+", raw)
    return {"url": raw, "rpid": rpid, "bvid": bv.group(0) if bv else None}


def fmt_time(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def circular_avatar(url, size):
    resp = requests.get(url, headers=HEADERS, timeout=20)
    img = Image.open(io.BytesIO(resp.content)).convert("RGBA")
    img = img.resize((size, size), Image.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((1, 1, size - 1, size - 1), fill=255)
    img.putalpha(mask)
    return img


def get_emote(url, size):
    if url in _EMOTE_CACHE:
        return _EMOTE_CACHE[url]
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        img = Image.open(io.BytesIO(resp.content)).convert("RGBA").resize((size, size), Image.LANCZOS)
        _EMOTE_CACHE[url] = img
        return img
    except Exception:
        return None


def tokenize(message, emote_map):
    tokens = []
    pos = 0
    for m in EMOTE_RE.finditer(message):
        name = m.group(0)
        if name in emote_map:
            if m.start() > pos:
                tokens.append(("t", message[pos:m.start()], None))
            tokens.append(("e", name, None))
            pos = m.end()
    if pos < len(message):
        tokens.append(("t", message[pos:], None))
    if not tokens:
        tokens.append(("t", message, None))
    return tokens


def wrap_tokens(draw, tokens, font, max_w, emote_w):
    lines = []
    cur = []
    cur_w = 0.0
    for kind, val, color in tokens:
        if kind == "e":
            if cur and cur_w + emote_w > max_w:
                lines.append(cur)
                cur = []
                cur_w = 0.0
            cur.append((kind, val, color))
            cur_w += emote_w
        else:
            for ch in val:
                if ch == "\n":
                    lines.append(cur)
                    cur = []
                    cur_w = 0.0
                    continue
                cw = draw.textlength(ch, font=font)
                if cur and cur_w + cw > max_w:
                    lines.append(cur)
                    cur = [(kind, ch, color)]
                    cur_w = cw
                else:
                    cur.append((kind, ch, color))
                    cur_w += cw
    if cur:
        lines.append(cur)
    return lines


def wrap_plain(draw, text, font, max_w):
    return ["".join(t[1] for t in line) for line in wrap_tokens(draw, [("t", text, None)], font, max_w, 0)]


def draw_tokens(img, d, x, y, lines, font, line_gap, emote_map, emote_size):
    for line in lines:
        cx = x
        for kind, val, color in line:
            if kind == "e":
                info = emote_map.get(val)
                url = (info or {}).get("url") or (info or {}).get("gif_url")
                if url:
                    em = get_emote(url, emote_size)
                    if em:
                        img.paste(em, (int(cx), int(y + (line_gap - emote_size) / 2)), em)
                cx += emote_size
            else:
                d.text((cx, y), val, font=font, fill=color or TEXT)
                cx += d.textlength(val, font=font)
        y += line_gap
    return y


def make_floor_item(fr, root_rpid, name_map, floor_no):
    parent = fr.get("parent")
    reply_to = None
    if parent and str(parent) != str(root_rpid):
        reply_to = name_map.get(str(parent))
    return {
        "kind": "floor",
        "floor": floor_no,
        "rpid": str(fr["rpid"]),
        "uname": fr["member"]["uname"],
        "avatar_url": fr["member"].get("avatar"),
        "message": fr["content"]["message"],
        "emote": fr["content"].get("emote") or {},
        "like": fr.get("like", 0),
        "ctime": fr["ctime"],
        "reply_to": reply_to,
    }


def build_items(root, floors):
    name_map = {str(r["rpid"]): r["member"]["uname"] for r in floors}
    items = [
        {
            "kind": "root",
            "rpid": str(root["rpid"]),
            "uname": root["member"]["uname"],
            "avatar_url": root["member"].get("avatar"),
            "message": root["content"]["message"],
            "emote": root["content"].get("emote") or {},
            "like": root.get("like", 0),
            "ctime": root["ctime"],
        }
    ]
    for i, fr in enumerate(floors, start=1):
        items.append(make_floor_item(fr, root["rpid"], name_map, i))
    return items


def content_prefix(item):
    if item.get("reply_to"):
        msg = re.sub(r"^\s*回复\s*@[^:：]{1,32}[:：]?\s*", "", item["message"])
        return msg, [("t", "回复 ", GRAY), ("t", "@" + item["reply_to"], BLUE), ("t", " : ", GRAY)]
    return item["message"], []


def render_card(item, with_avatar, is_new=False):
    root = item["kind"] == "root"
    name_f = get_font(21)
    small_f = get_font(16)
    body_f = get_font(23 if root else 20)
    line_gap = 34 if root else 30
    emote_size = body_f.size + 8

    content_w = CARD_W - PAD * 2 - AVATAR_R - 18
    td = ImageDraw.Draw(Image.new("RGB", (10, 10)))

    msg, prefix = content_prefix(item)
    emote_map = item.get("emote") or {}
    full_tokens = list(prefix) + tokenize(msg, emote_map)
    lines = wrap_tokens(td, full_tokens, body_f, content_w, emote_size)

    content_h = line_gap * max(len(lines), 1)
    top_pad = PAD + 10 if root else PAD
    head_h = 46 if root else 44
    foot_h = 46
    h = top_pad + head_h + 6 + content_h + 10 + foot_h

    img = Image.new("RGB", (CARD_W, h), WHITE)
    d = ImageDraw.Draw(img)

    x0 = PAD
    y = top_pad
    avatar = None
    if with_avatar and item.get("avatar_url"):
        try:
            avatar = circular_avatar(item["avatar_url"], AVATAR_R)
        except Exception:
            avatar = None
    if avatar:
        img.paste(avatar, (x0, y), avatar)
        tx = x0 + AVATAR_R + 16
    else:
        tx = x0

    ty = y
    if root:
        tag_w = name_f.size + 34
        d.rounded_rectangle((tx, ty - 5, tx + tag_w, ty + name_f.size + 3), radius=6, fill=GOLD)
        d.text((tx + 9, ty - 4), "楼主", font=name_f, fill=WHITE)
        tx += tag_w + 12
    d.text((tx, ty), item["uname"], font=name_f, fill=TEXT)
    if item.get("ctime"):
        tstr = fmt_time(item["ctime"])
        w_time = d.textlength(tstr, font=small_f)
        d.text((CARD_W - PAD - w_time, ty + 4), tstr, font=small_f, fill=GRAY)

    cy = y + head_h
    cy = draw_tokens(img, d, tx, cy, lines, body_f, line_gap, emote_map, emote_size)

    foot_y = cy + 10
    d.line((x0, foot_y, CARD_W - PAD, foot_y), fill=BORDER, width=1)
    d.text((x0, foot_y + 8), f"赞 {item['like']}", font=small_f, fill=TEXT)
    if is_new:
        tag_txt = "新增"
        tag_w = small_f.size * 2 + 20
        tag_x = CARD_W - PAD - tag_w
        d.rounded_rectangle((tag_x, foot_y + 6, tag_x + tag_w, foot_y + 6 + small_f.size + 6), radius=4, fill=GREEN)
        tw = d.textlength(tag_txt, font=small_f)
        d.text((tag_x + (tag_w - tw) / 2, foot_y + 8), tag_txt, font=small_f, fill=WHITE)
    label = f"楼层 {item['floor']}" if not root else "评论"
    w_label = d.textlength(label, font=small_f)
    d.text((CARD_W - PAD - w_label, foot_y + 8), label, font=small_f, fill=GRAY)

    return img


def watermark(img, text):
    w, h = img.size
    ov = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    od = ImageDraw.Draw(ov)
    font = get_font(13)
    tw = od.textlength(text, font=font)
    fg = (130, 130, 130, 60)
    y = 60
    while y < h:
        x = w - tw - 16
        od.text((x, y), text, font=font, fill=fg)
        y += 800
    od.text((3, 3), text, font=font, fill=fg)
    return Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")


def watermark_card(img, text_w):
    ov = Image.new("RGBA", img.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(ov)
    font = get_font(12)
    tw = od.textlength(text_w, font=font)
    w, h = img.size
    od.text((w - tw - 12, h - 22), text_w, font=font, fill=(130, 130, 130, 50))
    return Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")


def render_recap_card(last_run, last_count, now_str, new_count, prev_preview):
    small_f = get_font(17)
    title_f = get_font(21)
    line_gap = 28
    td = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    lines = [
        ("h", "前情提要"),
        ("t", f"上次截图：{last_run}（已收录 {last_count} 层）"),
        ("t", f"本次截图：{now_str}，本轮新增 {new_count} 层"),
    ]
    for uname, brief in prev_preview:
        lines.append(("p", f"· {uname}：{brief}"))
    lines.append(("h2", "实时模式下仅截取新增楼层，完整合并图请用普通模式。"))
    h = PAD + len(lines) * line_gap + PAD
    img = Image.new("RGB", (CARD_W, h), RECAP_BG)
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, CARD_W - 1, h - 1), outline=RECAP_BORDER, width=2)
    y = PAD
    for kind, text in lines:
        if kind == "h":
            d.text((PAD, y), text, font=title_f, fill=BLUE)
        elif kind == "h2":
            d.text((PAD, y), text, font=get_font(14), fill=GRAY)
        elif kind == "p":
            d.text((PAD, y), text, font=get_font(15), fill=GRAY)
        else:
            d.text((PAD, y), text, font=small_f, fill=TEXT)
        y += line_gap
    return img


def load_state(out, rpid):
    p = os.path.join(out, "state.json")
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                st = json.load(f)
            if st.get("rpid") == str(rpid):
                return st
        except Exception:
            pass
    return None


def save_state(out, state):
    with open(os.path.join(out, "state.json"), "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def main():
    ap = argparse.ArgumentParser(description="抓取 bilibili 评论全部楼层图片并合成长图")
    ap.add_argument("url", nargs="?", help="带 #reply<id> 的评论链接，或 --bvid/--rpid")
    ap.add_argument("--bvid")
    ap.add_argument("--rpid")
    ap.add_argument("-o", "--out", default="out")
    ap.add_argument("--no-avatar", action="store_true")
    ap.add_argument("--no-watermark", action="store_true")
    ap.add_argument("--live", action="store_true", help="实时模式：记录已截取楼层，只截新增楼层")
    args = ap.parse_args()

    if args.url:
        target = parse_target(args.url)
        if args.bvid:
            target["bvid"] = args.bvid
    elif args.rpid:
        target = {"rpid": args.rpid, "bvid": args.bvid, "url": ""}
    else:
        print("请提供评论链接，或 --rpid 与 --bvid")
        sys.exit(1)

    if not target.get("bvid"):
        print("需要 bvid (BV...) 或完整链接")
        sys.exit(1)

    view = fetch_view(target["bvid"])
    oid = view["aid"]
    root, floors = fetch_root_and_floors(oid, target["rpid"])

    os.makedirs(args.out, exist_ok=True)
    floors_dir = os.path.join(args.out, "floors")
    os.makedirs(floors_dir, exist_ok=True)

    with_avatar = not args.no_avatar
    with_wm = not args.no_watermark
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    wm_text = f"{target['bvid']} · {now_str}"

    if not args.live:
        print(f"[1/3] 视频《{view['title']}》(aid={oid}) 楼层回复 {len(floors)} 条")
        items = build_items(root, floors)
        images = []
        for i, item in enumerate(items):
            img = render_card(item, with_avatar)
            if with_wm:
                img = watermark_card(img, wm_text)
            fp = os.path.join(floors_dir, f"{i:03d}.png")
            img.save(fp)
            images.append((i, fp, img.size[1]))
        print(f"[2/3] 已生成 {len(images)} 张楼层图片 -> {floors_dir}")
        gap = 6
        total_h = sum(h for _, _, h in images) + gap * (len(images) - 1)
        merged = Image.new("RGB", (CARD_W, total_h), BG)
        y = 0
        for _, fp, h in images:
            merged.paste(Image.open(fp), (0, y))
            y += h + gap
        if with_wm:
            merged = watermark(merged, wm_text)
        merged_path = os.path.join(args.out, "merged_comments.png")
        merged.save(merged_path)
        print(f"[3/3] 已合并 -> {merged_path} ({CARD_W}x{total_h})")
    else:
        state = load_state(args.out, target["rpid"])
        known = set(state["floors"]) if state else set()
        new_floors = [f for f in floors if str(f["rpid"]) not in known]
        first_run = state is None
        prev_preview = (state or {}).get("preview", [])
        prev_run = (state or {}).get("last_run", "—")
        prev_count = (state or {}).get("count", 0)

        print(f"[实时] 已收录 {len(known)} 层，当前 {len(floors)} 层，本轮新增 {len(new_floors)} 层")

        last_run = "首次截图（无记录）" if first_run else prev_run
        recap = render_recap_card(last_run, prev_count, now_str, len(new_floors), [] if first_run else prev_preview)

        name_map = {str(f["rpid"]): f["member"]["uname"] for f in floors}
        new_images = []
        new_no = len(known)
        for f in new_floors:
            new_no += 1
            item = make_floor_item(f, root["rpid"], name_map, new_no)
            img = render_card(item, with_avatar, is_new=True)
            if with_wm:
                img = watermark_card(img, wm_text)
            fp = os.path.join(floors_dir, f"live_{f['rpid']}.png")
            img.save(fp)
            new_images.append((fp, img.size[1]))

        gap = 6
        pieces = [(recap, recap.size[1])] + [(Image.open(fp), h) for fp, h in new_images]
        total_h = sum(h for _, h in pieces) + gap * (len(pieces) - 1)
        merged = Image.new("RGB", (CARD_W, total_h), BG)
        y = 0
        for im, h in pieces:
            merged.paste(im, (0, y))
            y += h + gap
        if with_wm:
            merged = watermark(merged, wm_text)
        merged_path = os.path.join(args.out, "merged_live.png")
        merged.save(merged_path)
        print(f"[实时] 已输出 -> {merged_path} ({CARD_W}x{total_h})")

        all_rpids = [str(f["rpid"]) for f in floors]
        preview = [
            {"uname": f["member"]["uname"], "brief": f["content"]["message"][:24]}
            for f in floors[-3:]
        ]
        runs = (state or {}).get("runs", [])
        runs.append({"time": now_str, "new": len(new_floors)})
        save_state(
            args.out,
            {
                "rpid": str(target["rpid"]),
                "last_run": now_str,
                "count": len(floors),
                "floors": all_rpids,
                "preview": preview,
                "runs": runs[-20:],
            },
        )
        print(f"[实时] 状态已记录 -> {os.path.join(args.out, 'state.json')}")

    info = format_info(view, target, root, floors)
    info_path = os.path.join(args.out, "comment_info.txt")
    with open(info_path, "w", encoding="utf-8") as f:
        f.write(info)


def format_info(view, target, root, floors):
    lines = []
    lines.append(f"视频: {view['title']}  BVid={target['bvid']}  aid={view['aid']}")
    lines.append(f"UP主: {view['owner']['name']} (mid={view['owner']['mid']})")
    lines.append(f"链接: {target.get('url')}")
    lines.append("")
    r = root
    lines.append(f"根评论(楼主) rpid={r['rpid']} by {r['member']['uname']} (mid={r['member']['mid']})")
    lines.append(f"  点赞={r.get('like')} 回复数={r.get('rcount')} 时间={fmt_time(r['ctime'])}")
    for seg in r["content"]["message"].split("\n"):
        lines.append(f"  {seg}")
    lines.append("")
    lines.append(f"楼层回复 共 {len(floors)} 条:")
    for i, fr in enumerate(floors, start=1):
        lines.append(f"\n{i:>3}. rpid={fr['rpid']} by {fr['member']['uname']} 赞={fr.get('like')} 时间={fmt_time(fr['ctime'])}")
        for seg in fr["content"]["message"].split("\n"):
            lines.append(f"     {seg}")
    return "\n".join(lines)


if __name__ == "__main__":
    main()