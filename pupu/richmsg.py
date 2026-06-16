"""Parse rich QQ messages into text + image URLs for Claude API."""

import base64

import httpx

QQ_FACE_MAP = {
    "0": "[微笑]", "1": "[撇嘴]", "2": "[色]", "3": "[发呆]", "4": "[得意]",
    "5": "[流泪]", "6": "[害羞]", "7": "[闭嘴]", "8": "[睡]", "9": "[大哭]",
    "10": "[尴尬]", "11": "[发怒]", "12": "[调皮]", "13": "[呲牙]", "14": "[惊讶]",
    "15": "[难过]", "16": "[酷]", "17": "[冷汗]", "18": "[抓狂]", "19": "[吐]",
    "20": "[偷笑]", "21": "[可爱]", "22": "[白眼]", "23": "[傲慢]", "24": "[饥饿]",
    "25": "[困]", "26": "[惊恐]", "27": "[流汗]", "28": "[憨笑]", "29": "[悠闲]",
    "30": "[奋斗]", "31": "[咒骂]", "32": "[疑问]", "33": "[嘘]", "34": "[晕]",
    "35": "[折磨]", "36": "[衰]", "37": "[骷髅]", "38": "[敲打]", "39": "[再见]",
    "41": "[发抖]", "42": "[爱情]", "43": "[跳跳]", "46": "[猪头]",
    "49": "[拥抱]", "53": "[蛋糕]", "54": "[闪电]", "55": "[炸弹]", "56": "[刀]",
    "57": "[足球]", "59": "[便便]", "60": "[咖啡]", "61": "[饭]", "63": "[玫瑰]",
    "64": "[凋谢]", "66": "[爱心]", "67": "[心碎]", "69": "[礼物]",
    "74": "[太阳]", "75": "[月亮]", "76": "[赞]", "77": "[踩]", "78": "[握手]",
    "79": "[胜利]", "85": "[飞吻]", "86": "[怄火]",
    "96": "[冷汗]", "97": "[擦汗]", "98": "[抠鼻]", "99": "[鼓掌]",
    "100": "[糗大了]", "101": "[坏笑]", "102": "[左哼哼]", "103": "[右哼哼]",
    "104": "[哈欠]", "105": "[鄙视]", "106": "[委屈]", "107": "[快哭了]",
    "108": "[阴险]", "109": "[亲亲]", "110": "[吓]", "111": "[可怜]",
    "112": "[菜刀]", "113": "[啤酒]", "114": "[篮球]", "115": "[乒乓]",
    "116": "[示爱]", "117": "[瓢虫]", "118": "[抱拳]", "119": "[勾引]",
    "120": "[拳头]", "121": "[差劲]", "122": "[爱你]", "123": "[NO]", "124": "[OK]",
    "125": "[转圈]", "126": "[磕头]", "127": "[回头]", "128": "[跳绳]",
    "129": "[挥手]", "130": "[激动]", "131": "[街舞]", "132": "[献吻]",
    "133": "[左太极]", "134": "[右太极]", "136": "[双喜]", "137": "[鞭炮]",
    "138": "[灯笼]", "140": "[K歌]", "144": "[喝彩]", "145": "[祈祷]",
    "146": "[爆筋]", "147": "[棒棒糖]", "148": "[喝奶]", "171": "[茶]",
    "172": "[泪奔]", "173": "[无奈]", "174": "[卖萌]", "175": "[小纠结]",
    "176": "[喷血]", "177": "[斜眼笑]", "178": "[doge]", "179": "[惊喜]",
    "180": "[骚扰]", "181": "[笑哭]", "182": "[我最美]", "183": "[河蟹]",
    "184": "[羊驼]", "187": "[幽灵]", "188": "[蛋]", "190": "[菊花]",
    "192": "[红包]", "193": "[大笑]", "194": "[不开心]", "197": "[冷漠]",
    "198": "[呃]", "199": "[好棒]", "200": "[拜托]", "201": "[点赞]",
    "202": "[无聊]", "203": "[托脸]", "204": "[吃]", "205": "[送花]",
    "206": "[害怕]", "207": "[花痴]", "208": "[小样儿]", "210": "[飙泪]",
    "211": "[我不看]", "212": "[托腮]", "214": "[啵啵]", "215": "[糊脸]",
    "216": "[拍头]", "217": "[扯一扯]", "218": "[舔一舔]", "219": "[蹭一蹭]",
    "220": "[拽炸天]", "221": "[顶呱呱]", "222": "[抱抱]", "223": "[暴击]",
    "224": "[开枪]", "225": "[撩一撩]", "226": "[拍桌]", "227": "[拍手]",
    "228": "[恭喜]", "229": "[干杯]", "230": "[嘲讽]", "231": "[哼]",
    "232": "[佛系]", "233": "[掐一掐]", "234": "[惊呆]", "235": "[颤抖]",
    "236": "[啃头]", "237": "[偷看]", "238": "[扇脸]", "239": "[原谅]",
    "240": "[喷脸]", "241": "[生日快乐]", "242": "[头撞击]", "243": "[甩头]",
    "244": "[扔狗]", "245": "[加油必胜]", "246": "[加油抱抱]", "247": "[口罩护体]",
    "260": "[搬砖中]", "261": "[忙到飞起]", "262": "[脑阔疼]", "263": "[沧桑]",
    "264": "[捂脸]", "265": "[辣眼睛]", "266": "[哦哟]", "267": "[头秃]",
    "268": "[问号脸]", "269": "[暗中观察]", "270": "[emm]", "271": "[吃瓜]",
    "272": "[呵呵哒]", "273": "[我酸了]", "274": "[太南了]", "276": "[辣椒酱]",
    "277": "[汪汪]", "278": "[汗]", "279": "[打脸]", "280": "[击掌]",
    "281": "[无眼笑]", "282": "[敬礼]", "283": "[狂笑]", "284": "[面无表情]",
    "285": "[摸鱼]", "286": "[魔鬼笑]", "287": "[哦]", "288": "[请]",
    "289": "[睁眼]", "290": "[敲开心]", "291": "[震惊]", "292": "[让我康康]",
    "293": "[摸锦鲤]", "294": "[期待]", "295": "[拿到红包]", "296": "[真好]",
    "297": "[拜谢]", "298": "[元宝]", "299": "[牛啊]", "300": "[胖三斤]",
    "301": "[好闪]", "302": "[左拜年]", "303": "[右拜年]", "304": "[红脸]",
    "305": "[对我吹]", "306": "[嘿嘿嘿]", "307": "[甩甩甩]", "308": "[打call]",
    "309": "[变形]", "310": "[仔细分析]", "311": "[加油]", "312": "[我没事]",
    "313": "[菜狗]", "314": "[崇拜]", "315": "[比心]", "316": "[庆祝]",
    "317": "[老色批]", "318": "[拒绝]", "319": "[嫌弃]", "320": "[吃糖]",
    "322": "[惊吓]", "323": "[生气]",
    "324": "[加1]", "325": "[错号]",
    "326": "[完成]", "327": "[明白]",
}


def face_id_to_text(face_id: str) -> str:
    return QQ_FACE_MAP.get(str(face_id), f"[表情{face_id}]")


def download_image_as_base64(url: str) -> tuple[str, str] | None:
    """Download image and return (base64_data, media_type). Returns None on failure."""
    try:
        if not url.startswith("http"):
            url = "https://" + url
        resp = httpx.get(url, follow_redirects=True, timeout=15)
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "image/jpeg")
        media_type = content_type.split(";")[0].strip()
        if media_type not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
            media_type = "image/jpeg"
        b64 = base64.standard_b64encode(resp.content).decode("ascii")
        return b64, media_type
    except Exception:
        return None


def _is_sticker(seg) -> bool:
    """Detect if an image segment is a sticker/emoji pack rather than a real photo."""
    data = seg.data if hasattr(seg, "data") else {}
    if seg.type == "mface":
        return True
    sub_type = data.get("subType", data.get("sub_type"))
    if sub_type is not None and int(sub_type) != 0:
        return True
    summary = data.get("summary", "")
    if "表情" in summary:
        return True
    return False


def parse_onebot_message(message) -> tuple[str, list[str]]:
    """Parse OneBot v11 message into (text, image_urls).

    Stickers (mface, subType!=0) are silently ignored.
    Real images are collected as URLs for the look_at_image tool.
    """
    text_parts = []
    image_urls = []

    for seg in message:
        if seg.type == "text":
            text_parts.append(seg.data.get("text", ""))
        elif seg.type == "face":
            text_parts.append(face_id_to_text(seg.data.get("id", "")))
        elif seg.type in ("image", "mface"):
            if _is_sticker(seg):
                continue
            url = seg.data.get("url") or seg.data.get("file", "")
            if url:
                image_urls.append(url)
        elif seg.type == "at":
            qq = str(seg.data.get("qq") or "").strip()
            if qq == "all":
                text_parts.append("@全体成员")
            elif qq:
                text_parts.append(f"@{qq}")

    text = "".join(text_parts).strip()
    return text, image_urls


def parse_qq_official_message(message, attachments=None) -> tuple[str, list[str]]:
    """Parse QQ Official adapter message into (text, image_urls).

    Stickers are silently ignored. Real images collected as URLs.
    """
    text_parts = []
    image_urls = []

    for seg in message:
        if seg.type == "text":
            text_parts.append(seg.data.get("text", ""))
        elif seg.type == "emoji":
            text_parts.append(face_id_to_text(seg.data.get("id", "")))
        elif seg.type == "image":
            if _is_sticker(seg):
                continue
            url = seg.data.get("url", "")
            if url:
                image_urls.append(url)

    if attachments and not image_urls:
        for att in attachments:
            if att.content_type and att.content_type.startswith("image") and att.url:
                image_urls.append(att.url)

    text = "".join(text_parts).strip()
    return text, image_urls
