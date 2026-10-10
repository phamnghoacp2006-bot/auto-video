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
