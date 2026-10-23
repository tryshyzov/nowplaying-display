"""Иконка: макет из Figma (design/icon_source.png) + ноты, провод наушников и тёплое свечение.
Запуск: python design/make_icon.py
Пишет design/icon_final.png (1024), иконки Android (res/drawable-*) и трея (pc/icon.png, pc/icon.ico),
и превью design/icon_preview.png.
"""
import math
import os

from PIL import Image, ImageChops, ImageDraw, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SS = 4                      # суперсэмплинг для гладких краёв
RADIUS = 0.22               # скругление углов (доля стороны), как у иконок iOS
ZOOM = 0.74                 # какую долю кадра оставить: меньше — фигура крупнее
CENTER_X = 0.61             # центр кадра по горизонтали (доля ширины), чтобы влезли ноты
WHITE = (255, 255, 255, 255)   # как у наушников в макете
ORANGE = (217, 119, 87)


def figure_mask(src, bg):
    """Маска фигуры: всё, что отличается от фона (с мягкими краями)."""
    diff = ImageChops.difference(src, Image.new('RGB', src.size, bg)).convert('L')
    return diff.point(lambda v: min(255, v * 6))


def rotated_rect(draw, cx, cy, w, h, angle_deg, fill):
    a = math.radians(angle_deg)
    ca, sa = math.cos(a), math.sin(a)
    pts = []
    for dx, dy in ((-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)):
        pts.append((cx + dx * ca - dy * sa, cy + dx * sa + dy * ca))
    draw.polygon(pts, fill=fill)


def note(draw, x, y, cell, angle, fill):
    """Пиксельная нота ♪ из прямоугольников (в стиле макета), повёрнута на angle."""
    blocks = [  # (dx, dy, w, h) в клетках относительно головки ноты
        (0, 0, 2, 1.4),       # головка
        (1.5, -3.2, 0.6, 3.6),  # штиль
        (2.1, -3.2, 1.0, 0.6),  # флажок
        (2.7, -2.7, 0.6, 0.9),
    ]
    a = math.radians(angle)
    for dx, dy, w, h in blocks:
        cx = dx + w / 2
        cy = dy + h / 2
        px = x + (cx * math.cos(a) - cy * math.sin(a)) * cell
        py = y + (cx * math.sin(a) + cy * math.cos(a)) * cell
        rotated_rect(draw, px, py, w * cell, h * cell, angle, fill)


def miter_stroke(pts, w):
    """Контур ломаной толщины w с острыми (miter) углами — как обводка в Figma."""
    def norm(ax, ay, bx, by):
        dx, dy = bx - ax, by - ay
        L = math.hypot(dx, dy)
        return -dy / L * w / 2, dx / L * w / 2
    left, right = [], []
    for i, (px, py) in enumerate(pts):
        if i == 0:
            nx, ny = norm(*pts[0], *pts[1])
        elif i == len(pts) - 1:
            nx, ny = norm(*pts[-2], *pts[-1])
        else:  # биссектриса нормалей соседних отрезков, удлинённая для острого угла
            n1 = norm(*pts[i - 1], px, py)
            n2 = norm(px, py, *pts[i + 1])
            mx, my = n1[0] + n2[0], n1[1] + n2[1]
            dot = (mx * n1[0] + my * n1[1]) / ((w / 2) ** 2)
            nx, ny = mx / dot, my / dot
        left.append((px + nx, py + ny))
        right.append((px - nx, py - ny))
    return left + right[::-1]


def cable(draw, pts, width, fill):
    """Гладкий провод по кривой Безье через контрольные точки."""
    def bez(t):
        n = len(pts) - 1
        x = y = 0.0
        for i, (px, py) in enumerate(pts):
            c = math.comb(n, i) * (1 - t) ** (n - i) * t ** i
            x += c * px
            y += c * py
        return x, y
    r = width / 2
    for i in range(1201):
        x, y = bez(i / 1200)
        draw.ellipse([x - r, y - r, x + r, y + r], fill=fill)


def build():
    src = Image.open(os.path.join(HERE, 'icon_source.png')).convert('RGB')
    n = src.size[0]
    bg = src.getpixel((5, n - 5))
    S = n * SS

    # 1) фон + тёплое свечение под фигурой
    base = Image.new('RGBA', (n, n), bg + (255,))
    glow = Image.new('RGBA', (n, n), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse([n * 0.22, n * 0.10, n * 0.86, n * 0.74], fill=ORANGE + (70,))
    glow = glow.filter(ImageFilter.GaussianBlur(n * 0.09))
    base = Image.alpha_composite(base, glow)

    # 2) провод — за фигурой, в пиксельном стиле макета: толщина как у дуги наушников,
    #    сначала вдоль наклона Clawd, потом под углом строго вниз и за нижний край
    k = n / 717  # координаты сняты с фрейма 717x717
    under = Image.new('RGBA', (S, S), (0, 0, 0, 0))
    du = ImageDraw.Draw(under)
    w = 20 * k * SS                                  # толщина = дуга наушников
    ux, uy = 0.507, 0.862                            # направление длинной оси фигуры
    x0, y0 = 485 * k * SS, 300 * k * SS              # центр нижней амбушюры
    seg = 70 * k * SS
    x1, y1 = x0 + ux * seg, y0 + uy * seg           # колено провода
    cable_poly = miter_stroke([(x0, y0), (x1, y1), (x1, S + w)], w)
    du.polygon(cable_poly, fill=WHITE)
    base = Image.alpha_composite(base, under.resize((n, n), Image.LANCZOS))

    # 3) сама фигура из макета поверх
    fig = src.convert('RGBA')
    fig.putalpha(figure_mask(src, bg))
    base = Image.alpha_composite(base, fig)

    # 4) ноты над наушниками + провод ещё раз поверх, чтобы закрыть шов у амбушюры
    #    (белое на белой амбушюре не видно, а тёмный сглаженный край макета пропадает)
    over = Image.new('RGBA', (S, S), (0, 0, 0, 0))
    do = ImageDraw.Draw(over)
    do.polygon(cable_poly, fill=WHITE)
    note(do, 570 * k * SS, 120 * k * SS, 20 * k * SS, 12, WHITE)
    note(do, 640 * k * SS, 225 * k * SS, 15 * k * SS, 18, WHITE[:3] + (190,))
    base = Image.alpha_composite(base, over.resize((n, n), Image.LANCZOS))

    # 5) приближаем: верх кадра на месте (рука уходит за верхний край), всё становится крупнее
    side = int(n * ZOOM)
    x0 = max(0, min(n - side, int(n * CENTER_X - side / 2)))
    base = base.crop((x0, 0, x0 + side, side)).resize((n, n), Image.LANCZOS)
    return base.convert('RGB'), bg


def rounded(img, size):
    """Уменьшаем до size и скругляем углы (прозрачные), с гладким краем."""
    big = img.convert('RGBA').resize((size * 4, size * 4), Image.LANCZOS)
    mask = Image.new('L', big.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, big.width - 1, big.height - 1],
                                           radius=int(big.width * RADIUS), fill=255)
    big.putalpha(mask)
    return big.resize((size, size), Image.LANCZOS)


def export(final, bg):
    rounded(final, 1024).save(os.path.join(HERE, 'icon_final.png'))
    for folder, size in (('mdpi', 48), ('hdpi', 72), ('xhdpi', 96), ('xxhdpi', 144)):
        d = os.path.join(ROOT, 'android', 'res', f'drawable-{folder}')
        os.makedirs(d, exist_ok=True)
        rounded(final, size).save(os.path.join(d, 'icon.png'))
    # трей: обрезаем по фигуре, чтобы она читалась в 16–32 px
    diff = figure_mask(final, bg).point(lambda v: 255 if v > 60 else 0)
    l, t, r, b = diff.getbbox()
    side = max(r - l, b - t) + 30
    cx, cy = (l + r) // 2, (t + b) // 2
    tray = Image.new('RGB', (side, side), bg)
    tray.paste(final.crop((cx - side // 2, cy - side // 2, cx + side // 2, cy + side // 2)), (0, 0))
    tray = rounded(tray, 256)
    tray.save(os.path.join(ROOT, 'pc', 'icon.png'))
    tray.save(os.path.join(ROOT, 'pc', 'icon.ico'),
              sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    prev = Image.new('RGB', (600, 240), (240, 240, 240))
    x = 10
    for s in (48, 96, 144):
        im = rounded(final, s); prev.paste(im, (x, 10), im); x += s + 20
    x = 10
    for s in (16, 24, 32, 64):
        im = tray.resize((s, s), Image.LANCZOS); prev.paste(im, (x, 170), im); x += s + 20
    prev.save(os.path.join(HERE, 'icon_preview.png'))


if __name__ == '__main__':
    img, bg = build()
    export(img, bg)
    print('ok')
