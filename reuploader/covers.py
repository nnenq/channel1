"""Local thumbnail templates. No AI services or tokens are used."""
import io
import os
import re
import subprocess
import uuid
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps

from .ffmpeg_path import ffmpeg_exe
from .smartcut.media import probe

STYLES = {"lemon": ("#101827", "#FFE35B"),
          "ocean": ("#092C3C", "#64EDD3"),
          "coral": ("#301A2B", "#FF9B86")}
SIZE = (1080, 1920)
MAX_IMAGE_BYTES = 8 * 1024 * 1024


def suggested_text(title):
    text = re.sub(r"https?://\S+|[#@]\S+", "", title or "")
    return " ".join(text.split()[:7])[:100].strip()


def font(size):
    candidates = [os.getenv("COVER_FONT", ""), "C:/Windows/Fonts/arialbd.ttf",
                  "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                  "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return ImageFont.truetype(candidate, size)
    raise RuntimeError("Не найден шрифт. Укажи путь к TTF в COVER_FONT.")


def save_jpeg(im, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Stay below the YouTube API's 2 MB limit, including noisy uploaded images.
    for quality in (92, 85, 75, 60):
        buf = io.BytesIO()
        im.convert("RGB").save(buf, "JPEG", quality=quality, optimize=True)
        if buf.tell() < 2 * 1024 * 1024:
            temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
            try:
                temporary.write_bytes(buf.getvalue())
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
            return path
    raise ValueError("Не удалось уменьшить обложку до 2 МБ.")


def normalize_image(raw, path):
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError("Картинка должна быть не больше 8 МБ.")
    with Image.open(io.BytesIO(raw)) as im:
        if im.format not in ("JPEG", "PNG", "WEBP"):
            raise ValueError("Выбери JPG, PNG или WebP.")
        if im.width * im.height > 24_000_000:
            raise ValueError("Картинка слишком большая: максимум 24 мегапикселя.")
        im = ImageOps.exif_transpose(im).convert("RGB")
        # Contain rather than crop a ready-made design.
        canvas = Image.new("RGB", SIZE, "#101827")
        fitted = ImageOps.contain(im, SIZE)
        canvas.paste(fitted, ((SIZE[0] - fitted.width) // 2, (SIZE[1] - fitted.height) // 2))
        return save_jpeg(canvas, path)


def extract_frames(video, directory):
    """Choose the sharpest well-exposed frame in each of three temporal regions."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    duration = probe(video).duration
    if duration <= 0:
        raise ValueError("Не удалось определить длину ролика.")
    selected = []
    for region in range(3):
        candidates = []
        for offset in (0.18, 0.5, 0.82):
            t = min(duration - 0.04, duration * (region + offset) / 3)
            result = subprocess.run(
                [ffmpeg_exe(), "-v", "error", "-ss", str(max(0, t)), "-i", str(video),
                 "-frames:v", "1", "-vf", "scale=720:-2", "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
                capture_output=True, check=True, timeout=45)
            if not result.stdout:
                continue
            with Image.open(io.BytesIO(result.stdout)) as im:
                frame = im.convert("RGB")
                pixels = np.asarray(frame.resize((180, 240)).convert("L"), dtype=float)
                sharp = np.mean(np.abs(np.diff(pixels, axis=0))) + np.mean(np.abs(np.diff(pixels, axis=1)))
                exposed = np.mean((pixels > 15) & (pixels < 245))
                candidates.append((sharp * exposed, frame))
        if not candidates:
            raise ValueError("Не удалось извлечь кадры из видео.")
        best = max(candidates, key=lambda item: item[0])[1]
        path = directory / f"frame{region}.jpg"
        save_jpeg(best, path)
        selected.append(path)
    return selected


def render(frame, text, style, output):
    bg, accent = STYLES[style]
    canvas = Image.new("RGB", SIZE, bg)
    with Image.open(frame) as im:
        # Preserve the complete frame, including subjects near the edges.
        picture = ImageOps.contain(im.convert("RGB"), (984, 1250))
    canvas.paste(picture, ((1080 - picture.width) // 2, 70 + (1250 - picture.height) // 2))
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle((48, 1350, 178, 1366), radius=8, fill=accent)
    text = " ".join(str(text).split())[:100].upper()
    for size in range(96, 27, -2):
        face = font(size)
        lines, line = [], ""
        # Character wrapping also handles long unbroken titles.
        for word in text.split():
            trial = (line + " " + word).strip()
            if draw.textlength(trial, font=face) <= 950:
                line = trial
                continue
            if line:
                lines.append(line)
            line = ""
            for char in word:
                if draw.textlength(line + char, font=face) > 950:
                    lines.append(line)
                    line = ""
                line += char
        if line:
            lines.append(line)
        if len(lines) <= 4 and len(lines) * (size + 18) <= 430:
            break
    y = 1410
    for i, line in enumerate(lines):
        draw.text((58, y), line, font=face, fill=accent if i == 0 else "#FFFFFF")
        y += size + 18
    return save_jpeg(canvas, output)
