"""Gera os ícones do PWA (PNG reais, não vazios)."""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parents[1] / "xhakinha/web/static/icons"
OUT.mkdir(parents=True, exist_ok=True)
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def icon(size: int, maskable: bool) -> Image.Image:
    img = Image.new("RGBA", (size, size), (15, 18, 32, 255))
    d = ImageDraw.Draw(img)
    for y in range(size):                                     # degradê roxo -> rosa
        t = y / size
        d.line([(0, y), (size, y)], fill=(int(124 + 131 * t), int(92 - 15 * t), int(255 - 98 * t), 255))
    pad = int(size * (0.22 if maskable else 0.12))
    if not maskable:
        m = Image.new("L", (size, size), 0)
        ImageDraw.Draw(m).rounded_rectangle([0, 0, size, size], int(size * 0.22), fill=255)
        img.putalpha(m)
    f = ImageFont.truetype(FONT, int(size * 0.42))
    d = ImageDraw.Draw(img)
    d.text((size / 2, size / 2), "XC", font=f, fill=(255, 255, 255, 255), anchor="mm")
    tri = [(size * 0.40, size * 0.76), (size * 0.40, size * 0.90), (size * 0.56, size * 0.83)]   # "play"
    d.polygon(tri, fill=(255, 255, 255, 255))
    return img


for n, sz, mk in (("icon-192", 192, False), ("icon-512", 512, False), ("maskable-512", 512, True)):
    icon(sz, mk).save(OUT / f"{n}.png")
print("ícones gerados em", OUT)
