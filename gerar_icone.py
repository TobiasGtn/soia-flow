#!/usr/bin/env python3
"""Gera soiaflow.ico a partir do logo SOIA (mesmo desenho do app)."""
from PIL import Image, ImageDraw


def logo(size: int) -> Image.Image:
    k = 4
    s = size * k
    grad = Image.new("RGBA", (s, s))
    gd = ImageDraw.Draw(grad)
    topo, base = (20, 154, 111), (12, 107, 80)
    for y in range(s):
        t = y / (s - 1)
        cor = tuple(int(topo[i] + (base[i] - topo[i]) * t) for i in range(3))
        gd.line([(0, y), (s, y)], fill=cor + (255,))
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, s - 1, s - 1], radius=int(s * 7 / 32), fill=255)
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    img.paste(grad, (0, 0), mask)
    d = ImageDraw.Draw(img)
    e = s / 32
    # Topo reto, base arredondada — o acabamento do favicon.svg original
    for wbar, y in ((24.0, 8.0), (15.6, 14.4), (7.6, 20.8)):
        x0 = (32 - wbar) / 2 * e
        d.rounded_rectangle([x0, y * e, x0 + wbar * e, (y + 3.2) * e],
                            radius=1.6 * e, fill="white",
                            corners=(False, False, True, True))
    return img.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    tamanhos = [16, 24, 32, 48, 64, 128, 256]
    quadros = [logo(t) for t in tamanhos]
    quadros[-1].save("soiaflow.ico", format="ICO",
                     sizes=[(t, t) for t in tamanhos],
                     append_images=quadros[:-1])
    print("soiaflow.ico gerado")
