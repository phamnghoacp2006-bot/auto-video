"""Truyện kinh dị kể liền mạch: kịch bản 2 lượt (viết + biên tập) -> một đoạn giọng đọc liền
-> ảnh u ám đổi theo mốc thời gian -> nền ambient tạo bằng FFmpeg -> phụ đề từng câu.
Kết quả: out/video.mp4, out/meta.json
"""
import asyncio
import json
import os
import pathlib
import random
import re
import subprocess
import time
import urllib.parse

import edge_tts
import requests

TOPIC = os.environ["TOPIC"]
GEMINI_KEY = os.environ["GEMINI_API_KEY"]
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")  # đổi nếu Google đổi tên model
W, H, FPS = 1080, 1920, 25
TAIL = 2.0   # giây lặng ở cuối video
FADE = 0.5   # giây mờ dần giữa các cảnh
BED_VOLUME = float(os.environ.get("BED_VOLUME", "0.12"))  # âm lượng nền ambient (thử 0.08 đến 0.25)

try:
    DURATION = max(30, min(120, int(os.environ.get("DURATION_SEC") or 90)))
except ValueError:
    DURATION = 90

OUT = pathlib.Path("out")
TMP = pathlib.Path("tmp")
OUT.mkdir(exist_ok=True)
TMP.mkdir(exist_ok=True)

BASE_GRADE = "eq=saturation=0.6:contrast=1.18:brightness=-0.06,vignette=PI/5"
GRAIN = BASE_GRADE + ",noise=alls=10:allf=t"
GRAIN_HEAVY = BASE_GRADE + ",noise=alls=22:allf=t"

GENRE_MAP = {
    "URBAN": "urban", "PSYCHOLOGICAL": "psychological", "FOLK": "folk",
    "ANALOG": "analog", "GOTHIC": "gothic",
}
VOICE_MAP = {"DEEP": "tram", "WHISPER": "thi_tham", "EMOTIONLESS": "vo_hon"}

GENRES = {
    "psychological": {
        "name": "Psychological Horror (kinh dị tâm lý)",
        "desc": "Nỗi sợ đến từ chính tâm trí nhân vật: ảo giác, nghi ngờ thực tại, người kể chuyện không đáng tin, những chi tiết nhỏ dần sai lệch.",
        "look": "empty apartment at night, harsh shadows, distorted reflections, uneasy atmosphere",
        "voice": "thi_tham",
        "grade": GRAIN,
    },
    "folk": {
        "name": "Folk Horror (kinh dị dân gian)",
        "desc": "Làng quê biệt lập, tục lệ và nghi lễ cổ, lời nguyền, thiên nhiên và cộng đồng che giấu điều đáng sợ. Bối cảnh gần gũi với khán giả Việt.",
        "look": "foggy rural village at dusk, old wooden shrine, dark forest path, ritual objects, eerie",
        "voice": "tram",
        "grade": GRAIN,
    },
    "urban": {
        "name": "Urban Legends (truyền thuyết đô thị)",
        "desc": "Chuyện đồn đại hiện đại kể như chuyện có thật: địa điểm quen thuộc, quy tắc kỳ lạ, nhân vật bí ẩn, kết thúc để ngỏ.",
        "look": "empty city street at 3am, flickering streetlight, deserted alley, wet pavement, lonely",
        "voice": "tram",
        "grade": GRAIN,
    },
    "analog": {
        "name": "Analog Horror (kinh dị băng hình analog)",
        "desc": "Kể như một bản tin, băng ghi hình hoặc thông báo cũ bị rò rỉ: giọng điệu lạnh lùng, quy trình khô khan, chi tiết bất thường lộ dần.",
        "look": "1990s VHS footage, CRT television static, empty room, found footage, grainy",
        "voice": "vo_hon",
        "grade": GRAIN_HEAVY,
    },
    "gothic": {
        "name": "Gothic Horror (kinh dị gothic)",
        "desc": "Dinh thự cổ, bí mật gia tộc, tang tóc, ánh nến, bầu không khí u uất và lãng mạn tăm tối.",
        "look": "old victorian mansion interior, candlelight, long dark corridor, dusty portraits, gloomy",
        "voice": "thi_tham",
        "grade": GRAIN,
    },
}

KEYWORDS = [
    ("psychological", "psychological"), ("tâm lý", "psychological"),
    ("folk", "folk"), ("dân gian", "folk"),
    ("urban", "urban"), ("đô thị", "urban"),
    ("analog", "analog"), ("gothic", "gothic"),
]

# edge-tts tiếng Việt chỉ có 2 giọng; các kiểu giọng được mô phỏng bằng cao độ, tốc độ, âm lượng và hiệu ứng FFmpeg
VOICES = {
    "tram": {
        "voice": "vi-VN-NamMinhNeural", "rate": "-12%", "pitch": "-28Hz", "volume": "+0%",
        "fx": "equalizer=f=110:t=q:w=1.2:g=5,lowpass=f=7000,aecho=0.8:0.55:70|140:0.3|0.2",
    },
    "vo_hon": {
        "voice": "vi-VN-NamMinhNeural", "rate": "-8%", "pitch": "-8Hz", "volume": "+0%",
        "fx": "highpass=f=250,lowpass=f=4000",
    },
    "thi_tham": {
        "voice": "vi-VN-HoaiMyNeural", "rate": "-18%", "pitch": "-6Hz", "volume": "-10%",
        "fx": "highpass=f=150,equalizer=f=3500:t=q:w=1:g=3,aecho=0.8:0.6:90:0.3",
    },
}

VOICE_HINTS = [("giọng trầm", "tram"), ("giọng vô hồn", "vo_hon"), ("giọng thì thầm", "thi_tham")]


def pick_genre():
    forced = GENRE_MAP.get(os.environ.get("GENRE", "").strip().upper())
    if forced:
        return forced
    t = TOPIC.lower()
    for kw, key in KEYWORDS:
        if kw in t:
            return key
    return random.choice(list(GENRES))


def pick_voice(genre_key):
    forced = VOICE_MAP.get(os.environ.get("VOICE_STYLE", "").strip().upper())
    if forced:
        return forced
    t = TOPIC.lower()
    for kw, key in VOICE_HINTS:
        if kw in t:
            return key
    return GENRES[genre_key]["voice"]


def sh(*args):
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr[-3000:])
        raise RuntimeError(f"Lệnh lỗi: {args[0]}")
    return r.stdout


def gemini_json(prompt):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    for attempt in range(3):
        try:
            r = requests.post(
                url,
                headers={"x-goog-api-key": GEMINI_KEY},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"responseMimeType": "application/json"},
                },
                timeout=300,
            )
        except requests.RequestException as e:
            print("Gemini lỗi kết nối hoặc hết thời gian chờ:", e)
            time.sleep(10 * (attempt + 1))
            continue
        if r.status_code == 200:
            try:
                return json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])
            except (KeyError, IndexError, json.JSONDecodeError) as e:
                print("Gemini trả về dữ liệu không đọc được:", e)
        else:
            print("Gemini lỗi", r.status_code, r.text[:300])
        time.sleep(10 * (attempt + 1))
    raise RuntimeError("Không gọi được Gemini")


def draft_prompt(genre):
    words = int(DURATION * 1.9)
    n = max(5, round(DURATION / 9))
    return (
        "Bạn là tác giả truyện kinh dị đọc thành tiếng cho kênh TikTok tiếng Việt, nổi tiếng vì những câu chuyện "
        "khiến người nghe bất an dù không có một giọt máu.\n"
        f"Thể loại: {genre['name']}. {genre['desc']}\n"
        f"Ý tưởng hoặc chủ đề gợi ý: {TOPIC}\n\n"
        f"Viết MỘT truyện hư cấu bằng tiếng Việt, đọc thành lời khoảng {DURATION} giây (khoảng {words} từ). "
        "Đây là một mạch kể liền, không phải các câu rời rạc.\n"
        "NGHỆ THUẬT KINH DỊ (bắt buộc):\n"
        "- Mở bằng một chi tiết cụ thể, bình thường nhưng sai sai (một giờ giấc, một con số, một quy tắc lạ). "
        "Không mở bằng lời dẫn chung chung kiểu 'Tôi không bao giờ quên đêm đó'.\n"
        "- Nhân vật và địa điểm cụ thể: tên riêng người Việt, tên phố hoặc làng, số tầng, giờ phút chính xác.\n"
        "- Tăng tiến: mỗi đoạn thêm MỘT chi tiết bất thường mới, rõ hơn đoạn trước. Nhân vật cố tìm lời giải thích hợp lý rồi thất bại.\n"
        "- Tả chứ không kể cảm xúc: âm thanh, mùi, nhiệt độ, sự im lặng, vật bị xê dịch. "
        "Tuyệt đối không viết 'tôi sợ', 'rùng rợn', 'kinh hoàng', 'lạnh sống lưng', 'tim đập thình thịch'.\n"
        "- Tránh sáo ngữ: không dùng 'bóng đen', 'tiếng thì thầm', 'ánh mắt trống rỗng', 'không khí đặc quánh', 'một luồng khí lạnh'.\n"
        "- Không giải thích nguồn gốc hay bản chất của thứ đáng sợ. Điều không được nói ra mới đáng sợ.\n"
        "- Nhịp: câu ngắn xen câu dài, câu cực ngắn ở thời điểm đáng sợ. Dùng ngôi thứ nhất hoặc thì hiện tại để tạo cảm giác đang xảy ra.\n"
        "- Kết thúc bằng một câu ngắn, lạnh, làm nghĩa lại một chi tiết đã xuất hiện ở đầu truyện.\n"
        "Ví dụ giọng văn (chỉ để học nhịp, KHÔNG chép): "
        "'Mỗi đêm, đúng hai giờ mười bốn, đèn hành lang tầng ba tắt. Không phải mất điện. Chỉ hành lang tầng ba. Mà tôi ở tầng ba.'\n"
        "AN TOÀN: KHÔNG máu me, bạo lực chi tiết, tự hại, tình dục; không dùng người thật hay thảm kịch có thật.\n"
        f"Chia truyện thành khoảng {n} đoạn (từ {n - 1} đến {n + 1}). Mỗi đoạn là 2 đến 4 câu liền nhau.\n"
        "Chỉ trả về JSON đúng dạng: "
        '{"title": "tiêu đề ngắn gây tò mò", '
        '"caption": "mô tả ngắn gây tò mò, ghi rõ đây là truyện hư cấu, kèm 4-6 hashtag như #kinhdi #truyenma", '
        '"visual_style": "...", '
        '"scenes": [{"narration": "...", "image_prompt": "..."}]}\n'
        "visual_style: MỘT câu tiếng Anh mô tả phong cách hình ảnh nhất quán cho cả truyện "
        "(địa điểm chính, bảng màu, ánh sáng, chất phim). "
        "image_prompt: tiếng Anh, MỘT cảnh tĩnh hợp với đoạn truyện, tối và u ám, không chữ, không máu, không cận mặt người."
    )


def edit_prompt(draft):
    return (
        "Bạn là biên tập viên truyện kinh dị rất khó tính. Dưới đây là bản nháp (JSON). "
        "Viết lại phần narration để truyện ĐÁNG SỢ HƠN NHIỀU và liền mạch hơn, và giữ nguyên:\n"
        "- cấu trúc JSON, số lượng đoạn, các trường title, caption, visual_style, image_prompt "
        "(chỉ chỉnh image_prompt nếu nội dung đoạn đổi);\n"
        "- tổng độ dài trong khoảng cộng trừ 10%.\n"
        "Cách làm: thay mọi chỗ kể lể cảm xúc bằng chi tiết giác quan cụ thể; xóa sáo ngữ; "
        "mở đầu bằng một chi tiết sai sai thay vì lời dẫn; "
        "đặt một chi tiết nhỏ ở đầu truyện để câu cuối làm nó đổi nghĩa; câu cuối ngắn và lạnh; "
        "các đoạn nối tiếp tự nhiên như một mạch kể, không lặp ý, không tóm tắt. "
        "Không máu me, bạo lực chi tiết, tự hại, tình dục, người thật.\n"
        "Chỉ trả về JSON.\n\nBẢN NHÁP:\n" + json.dumps(draft, ensure_ascii=False)
    )


def make_script(genre):
    draft = gemini_json(draft_prompt(genre))
    try:
        final = gemini_json(edit_prompt(draft))
        if final.get("scenes") and all(s.get("narration") and s.get("image_prompt") for s in final["scenes"]):
            print("Đã biên tập kịch bản ở lượt 2")
            return final
    except Exception as e:
        print("Bỏ qua lượt biên tập:", e)
    return draft


async def tts(text, path, v):
    comm = edge_tts.Communicate(text, v["voice"], rate=v["rate"], volume=v["volume"], pitch=v["pitch"])
    await comm.save(str(path))


def audio_duration(path):
    out = sh("ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path))
    return float(out.strip())


def get_image(prompt, look, path):
    full = f"{prompt}, {look}, dark moody cinematic horror, vertical 9:16, no text, no gore"
    url = (
        "https://image.pollinations.ai/prompt/" + urllib.parse.quote(full)
        + f"?width={W}&height={H}&nologo=true&seed={random.randint(1, 99999)}"
    )
    for attempt in range(4):
        try:
            r = requests.get(url, timeout=120)
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("image"):
                path.write_bytes(r.content)
                return
        except requests.RequestException as e:
            print("Ảnh lỗi:", e)
        time.sleep(8 * (attempt + 1))
    # dự phòng: nền tối để pipeline không chết
    sh("ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c=0x0e0e14:s={W}x{H}",
       "-frames:v", "1", str(path))


def srt_time(t):
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def split_sentences(text):
    parts = re.split(r"(?<=[.!?…])\s+", text.strip())
    return [p.strip() for p in parts if p.strip()]


def weight(sentence):
    """Ước lượng thời gian đọc tương đối: số từ + nghỉ ở dấu phẩy và cuối câu."""
    words = len(sentence.split())
    pauses = sum(sentence.count(c) for c in ",;:—")
    end = 2.5 if re.search(r"[.!?…]$", sentence) else 1.0
    return words + pauses + end


def build_timeline(scenes, total):
    items = []
    for i, sc in enumerate(scenes):
        for s in split_sentences(sc["narration"]):
            items.append((i, s))
    weights = [weight(s) for _, s in items]
    tot_w = sum(weights)
    spans, t = [], 0.0
    for (i, s), w in zip(items, weights):
        d = total * w / tot_w
        spans.append((i, s, t, t + d))
        t += d
    return spans


def scene_bounds(spans, total):
    starts = {}
    for i, _, a, _ in spans:
        starts.setdefault(i, a)
    idxs = sorted(starts)
    out = []
    for k, i in enumerate(idxs):
        start = 0.0 if k == 0 else starts[i]
        end = starts[idxs[k + 1]] if k + 1 < len(idxs) else total + TAIL
        out.append((i, start, end))
    return out


def main():
    genre_key = pick_genre()
    genre = GENRES[genre_key]
    voice_key = pick_voice(genre_key)
    v = VOICES[voice_key]
    print(f"Thể loại: {genre['name']} | Kiểu giọng: {voice_key} | Thời lượng mục tiêu: {DURATION}s")

    data = make_script(genre)
    scenes = [s for s in data.get("scenes", []) if s.get("narration")]
    if not scenes:
        raise RuntimeError("Kịch bản không có cảnh nào")
    style_note = data.get("visual_style", "")
    print(f"Kịch bản: {data['title']} ({len(scenes)} cảnh)")

    # một lần đọc liền toàn bộ truyện
    narration = TMP / "narration.mp3"
    full_text = " ".join(s["narration"].strip() for s in scenes)
    asyncio.run(tts(full_text, narration, v))
    total = audio_duration(narration)
    total_len = total + TAIL
    print(f"Độ dài giọng đọc: {total:.1f}s")

    spans = build_timeline(scenes, total)
    bounds = scene_bounds(spans, total)

    segs = []
    for k, (i, a, b) in enumerate(bounds):
        sc = scenes[i]
        d = b - a
        img = TMP / f"i{k}.jpg"
        seg = TMP / f"seg{k}.mp4"
        get_image(f"{sc['image_prompt']}, {style_note}", genre["look"], img)

        frames = int(d * FPS) + 1
        fd = min(FADE, d / 3)
        # zoom chậm; đổi hướng xen kẽ; mờ dần vào/ra giữa các cảnh
        z = "min(zoom+0.0007,1.2)" if k % 2 == 0 else "if(eq(on,1),1.2,max(zoom-0.0007,1.0))"
        vf = (f"scale=1620:2880,zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
              f":d={frames}:s={W}x{H}:fps={FPS},{genre['grade']},"
              f"fade=t=in:st=0:d={fd:.2f},fade=t=out:st={d - fd:.2f}:d={fd:.2f},format=yuv420p")
        sh("ffmpeg", "-y", "-i", str(img), "-vf", vf, "-t", f"{d:.2f}", "-an",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", str(seg))
        segs.append(seg.name)
        print(f"Xong cảnh {k + 1}/{len(bounds)}")

    # phụ đề từng câu, theo mốc thời gian của giọng đọc
    srt = []
    for n, (_, s, a, b) in enumerate(spans, 1):
        srt.append(f"{n}\n{srt_time(a)} --> {srt_time(max(a + 0.3, b - 0.05))}\n{s}\n")
    (TMP / "subs.srt").write_text("\n".join(srt), encoding="utf-8")

    (TMP / "list.txt").write_text("".join(f"file '{s}'\n" for s in segs), encoding="utf-8")
    sh("ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(TMP / "list.txt"),
       "-c", "copy", str(TMP / "joined.mp4"))

    # ghép: video + giọng đọc (có hiệu ứng) + nền ambient tự tạo + phụ đề
    style = "FontName=DejaVu Sans,FontSize=6,Bold=1,Outline=1,Shadow=1,Alignment=2,MarginL=40,MarginR=40,MarginV=60"
    fc = (
        f"[1:a]{v['fx']},aresample=44100[voice];"
        "[2:a][3:a][4:a]amix=inputs=3:duration=longest:normalize=0,"
        f"lowpass=f=260,volume={BED_VOLUME}[bed];"
        "[voice][bed]amix=inputs=2:duration=longest:normalize=0[aout];"
        f"[0:v]subtitles={TMP / 'subs.srt'}:force_style='{style}'[vout]"
    )
    tl = f"{total_len:.2f}"
    sh("ffmpeg", "-y",
       "-i", str(TMP / "joined.mp4"), "-i", str(narration),
       "-f", "lavfi", "-t", tl, "-i", "sine=frequency=55:sample_rate=44100",
       "-f", "lavfi", "-t", tl, "-i", "sine=frequency=58:sample_rate=44100",
       "-f", "lavfi", "-t", tl, "-i", "anoisesrc=color=brown:amplitude=0.25:sample_rate=44100",
       "-filter_complex", fc, "-map", "[vout]", "-map", "[aout]", "-t", tl,
       "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
       "-c:a", "aac", "-b:a", "160k", "-ac", "2", str(OUT / "video.mp4"))

    (OUT / "meta.json").write_text(
        json.dumps({"title": data["title"], "caption": data["caption"]}, ensure_ascii=False),
        encoding="utf-8",
    )
    print("Hoàn tất:", OUT / "video.mp4")


if __name__ == "__main__":
    main()
