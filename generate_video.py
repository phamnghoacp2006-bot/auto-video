"""Truyện kinh dị kể bằng giọng đọc: kịch bản (Gemini) -> giọng đọc (edge-tts + hiệu ứng)
-> ảnh u ám (Pollinations) -> ghép FFmpeg + phụ đề. Kết quả: out/video.mp4, out/meta.json
"""
import asyncio
import json
import os
import pathlib
import random
import subprocess
import time
import urllib.parse

import edge_tts
import requests

TOPIC = os.environ["TOPIC"]
GEMINI_KEY = os.environ["GEMINI_API_KEY"]
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")  # đổi nếu Google đổi tên model
W, H, FPS = 1080, 1920, 25
PAUSE = 0.7  # khoảng lặng giữa các cảnh (giây)

try:
    DURATION = max(30, min(120, int(os.environ.get("DURATION_SEC") or 90)))
except ValueError:
    DURATION = 90

OUT = pathlib.Path("out")
TMP = pathlib.Path("tmp")
OUT.mkdir(exist_ok=True)
TMP.mkdir(exist_ok=True)

GRADE = "eq=saturation=0.65:contrast=1.15:brightness=-0.05,vignette=PI/5"

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
        "grade": GRADE,
    },
    "folk": {
        "name": "Folk Horror (kinh dị dân gian)",
        "desc": "Làng quê biệt lập, tục lệ và nghi lễ cổ, lời nguyền, thiên nhiên và cộng đồng che giấu điều đáng sợ. Bối cảnh gần gũi với khán giả Việt.",
        "look": "foggy rural village at dusk, old wooden shrine, dark forest path, ritual objects, eerie",
        "voice": "tram",
        "grade": GRADE,
    },
    "urban": {
        "name": "Urban Legends (truyền thuyết đô thị)",
        "desc": "Chuyện đồn đại hiện đại kể như chuyện có thật: địa điểm quen thuộc, quy tắc kỳ lạ, nhân vật bí ẩn, kết thúc để ngỏ.",
        "look": "empty city street at 3am, flickering streetlight, deserted alley, wet pavement, lonely",
        "voice": "tram",
        "grade": GRADE,
    },
    "analog": {
        "name": "Analog Horror (kinh dị băng hình analog)",
        "desc": "Kể như một bản tin, băng ghi hình hoặc thông báo cũ bị rò rỉ: giọng điệu lạnh lùng, quy trình khô khan, chi tiết bất thường lộ dần.",
        "look": "1990s VHS footage, CRT television static, empty room, found footage, grainy",
        "voice": "vo_hon",
        "grade": GRADE + ",noise=alls=18:allf=t",
    },
    "gothic": {
        "name": "Gothic Horror (kinh dị gothic)",
        "desc": "Dinh thự cổ, bí mật gia tộc, tang tóc, ánh nến, bầu không khí u uất và lãng mạn tăm tối.",
        "look": "old victorian mansion interior, candlelight, long dark corridor, dusty portraits, gloomy",
        "voice": "thi_tham",
        "grade": GRADE,
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
        "voice": "vi-VN-NamMinhNeural", "rate": "-10%", "pitch": "-25Hz", "volume": "+0%",
        "fx": "equalizer=f=110:t=q:w=1.2:g=5,lowpass=f=7000,aecho=0.8:0.5:60:0.2",
    },
    "vo_hon": {
        "voice": "vi-VN-NamMinhNeural", "rate": "-5%", "pitch": "-8Hz", "volume": "+0%",
        "fx": "highpass=f=250,lowpass=f=4000",
    },
    "thi_tham": {
        "voice": "vi-VN-HoaiMyNeural", "rate": "-15%", "pitch": "-5Hz", "volume": "-15%",
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


def make_script(genre):
    words = int(DURATION * 2.0)
    n_scenes = max(6, round(DURATION / 7))
    prompt = (
        "Bạn là người kể chuyện kinh dị chuyên nghiệp cho kênh TikTok tiếng Việt.\n"
        f"Thể loại: {genre['name']}. {genre['desc']}\n"
        f"Ý tưởng hoặc chủ đề gợi ý: {TOPIC}\n"
        f"Viết MỘT truyện kinh dị hư cấu ngắn bằng tiếng Việt, đọc thành lời khoảng {DURATION} giây "
        f"(khoảng {words} từ, không lệch quá 15%). Câu đầu tiên phải là câu móc hút người nghe. "
        "Câu ngắn, nhịp chậm, căng thẳng tăng dần, kết bằng một cú twist hoặc chi tiết rợn gáy để ngỏ. "
        "Kể ở ngôi thứ nhất hoặc người dẫn truyện, bối cảnh tự nhiên với khán giả Việt Nam.\n"
        "Chỉ gây sợ bằng không khí và chi tiết bất thường. KHÔNG miêu tả máu me, bạo lực chi tiết, "
        "tự hại, tình dục; không dùng người thật hoặc thảm kịch có thật.\n"
        "Chỉ trả về JSON đúng dạng: "
        '{"title": "tiêu đề ngắn gây tò mò", '
        '"caption": "mô tả ngắn gây tò mò, ghi rõ đây là truyện hư cấu, kèm 4-6 hashtag như #kinhdi #truyenma", '
        '"scenes": [{"narration": "1-3 câu của truyện, để đọc thành lời", '
        '"image_prompt": "English, one still scene, dark and atmospheric, no text, no gore, no close-up faces"}]}\n'
        f"Có khoảng {n_scenes} cảnh (từ {n_scenes - 1} đến {n_scenes + 1}), nối tiếp nhau thành một câu chuyện liền mạch."
    )
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
            return json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])
        print("Gemini lỗi", r.status_code, r.text[:300])
        time.sleep(10 * (attempt + 1))
    raise RuntimeError("Không gọi được Gemini")


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


def main():
    genre_key = pick_genre()
    genre = GENRES[genre_key]
    voice_key = pick_voice(genre_key)
    v = VOICES[voice_key]
    print(f"Thể loại: {genre['name']} | Kiểu giọng: {voice_key} | Thời lượng mục tiêu: {DURATION}s")

    data = make_script(genre)
    scenes = data.get("scenes") or []
    if not scenes:
        raise RuntimeError("Kịch bản không có cảnh nào")
    print(f"Kịch bản: {data['title']} ({len(scenes)} cảnh)")

    srt, cursor, segs = [], 0.0, []
    for i, sc in enumerate(scenes):
        mp3 = TMP / f"a{i}.mp3"
        img = TMP / f"i{i}.jpg"
        seg = TMP / f"seg{i}.mp4"

        asyncio.run(tts(sc["narration"], mp3, v))
        dur = audio_duration(mp3) + PAUSE
        get_image(sc["image_prompt"], genre["look"], img)

        frames = int(dur * FPS) + 1
        # zoom chậm; đổi hướng xen kẽ cho đỡ đơn điệu
        z = "min(zoom+0.0007,1.2)" if i % 2 == 0 else "if(eq(on,1),1.2,max(zoom-0.0007,1.0))"
        vf = (f"scale=1620:2880,zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
              f":d={frames}:s={W}x{H}:fps={FPS},{genre['grade']},format=yuv420p")
        sh("ffmpeg", "-y", "-i", str(img), "-i", str(mp3),
           "-vf", vf, "-af", f"{v['fx']},apad", "-t", f"{dur:.2f}",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
           "-c:a", "aac", "-ar", "44100", "-ac", "2", str(seg))

        srt.append(f"{i + 1}\n{srt_time(cursor)} --> {srt_time(cursor + dur - 0.1)}\n{sc['narration']}\n")
        cursor += dur
        segs.append(seg.name)
        print(f"Xong cảnh {i + 1}/{len(scenes)}")

    (TMP / "subs.srt").write_text("\n".join(srt), encoding="utf-8")
    (TMP / "list.txt").write_text("".join(f"file '{s}'\n" for s in segs), encoding="utf-8")
    sh("ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(TMP / "list.txt"),
       "-c", "copy", str(TMP / "joined.mp4"))

    style = "FontName=DejaVu Sans,FontSize=6,Bold=1,Outline=1,Shadow=1,Alignment=2,MarginL=40,MarginR=40,MarginV=60"
    sh("ffmpeg", "-y", "-i", str(TMP / "joined.mp4"),
       "-vf", f"subtitles={TMP / 'subs.srt'}:force_style='{style}'",
       "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-c:a", "copy",
       str(OUT / "video.mp4"))

    (OUT / "meta.json").write_text(
        json.dumps({"title": data["title"], "caption": data["caption"]}, ensure_ascii=False),
        encoding="utf-8",
    )
    print("Hoàn tất:", OUT / "video.mp4")


if __name__ == "__main__":
    main()
