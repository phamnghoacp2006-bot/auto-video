"""Tạo video 9:16 (dọc, kiểu TikTok) từ một chủ đề: kịch bản (Gemini) -> giọng đọc (edge-tts)
-> ảnh (Pollinations) -> ghép FFmpeg + phụ đề. Kết quả: out/video.mp4, out/meta.json
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
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")  # đổi nếu Google đổi tên model
VOICE = os.environ.get("VOICE", "vi-VN-HoaiMyNeural")        # nam: vi-VN-NamMinhNeural
W, H, FPS = 1080, 1920, 25

OUT = pathlib.Path("out")
TMP = pathlib.Path("tmp")
OUT.mkdir(exist_ok=True)
TMP.mkdir(exist_ok=True)


def sh(*args):
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr[-3000:])
        raise RuntimeError(f"Lệnh lỗi: {args[0]}")
    return r.stdout


def make_script():
    prompt = (
        f"Viết kịch bản video ngắn 60-90 giây bằng tiếng Việt về chủ đề: {TOPIC}\n"
        "Chỉ trả về JSON đúng dạng: "
        '{"title": "...", "caption": "mô tả ngắn kèm 3-5 hashtag", '
        '"scenes": [{"narration": "1-2 câu tiếng Việt, tự nhiên để đọc thành lời", '
        '"image_prompt": "English, detailed cinematic description, vertical composition, no text in image"}]}\n'
        "Có 8 đến 12 cảnh. Mở đầu bằng một câu thu hút."
    )
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    for attempt in range(3):
        r = requests.post(
            url,
            headers={"x-goog-api-key": GEMINI_KEY},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"responseMimeType": "application/json"},
            },
            timeout=120,
        )
        if r.status_code == 200:
            return json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])
        print("Gemini lỗi", r.status_code, r.text[:300])
        time.sleep(10 * (attempt + 1))
    raise RuntimeError("Không gọi được Gemini")


async def tts(text, path):
    await edge_tts.Communicate(text, VOICE).save(str(path))


def audio_duration(path):
    out = sh("ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path))
    return float(out.strip())


def get_image(prompt, path):
    full = prompt + ", cinematic lighting, highly detailed, vertical 9:16"
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
    # dự phòng: nền màu trơn để pipeline không chết
    sh("ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c=0x1c1c28:s={W}x{H}",
       "-frames:v", "1", str(path))


def srt_time(t):
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def main():
    data = make_script()
    scenes = data["scenes"]
    print(f"Kịch bản: {data['title']} ({len(scenes)} cảnh)")

    srt, cursor, segs = [], 0.0, []
    for i, sc in enumerate(scenes):
        mp3 = TMP / f"a{i}.mp3"
        img = TMP / f"i{i}.jpg"
        seg = TMP / f"seg{i}.mp4"

        asyncio.run(tts(sc["narration"], mp3))
        dur = audio_duration(mp3) + 0.4
        get_image(sc["image_prompt"], img)

        frames = int(dur * FPS) + 1
        # zoom chậm (Ken Burns); đổi hướng zoom xen kẽ cho đỡ đơn điệu
        z = "min(zoom+0.0007,1.2)" if i % 2 == 0 else "if(eq(on,1),1.2,max(zoom-0.0007,1.0))"
        vf = (f"scale=1620:2880,zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
              f":d={frames}:s={W}x{H}:fps={FPS},format=yuv420p")
        sh("ffmpeg", "-y", "-i", str(img), "-i", str(mp3),
           "-vf", vf, "-af", "apad", "-t", f"{dur:.2f}",
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

    style = "FontName=DejaVu Sans,FontSize=11,Bold=1,Outline=2,Shadow=1,Alignment=2,MarginL=30,MarginR=30,MarginV=70"
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
