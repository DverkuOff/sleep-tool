#!/usr/bin/env python3
"""Берёт из macOS настоящие ассеты для видео: курсор, шрифты SF и значки SF Symbols.

Файлы Apple в репозиторий не кладём: скрипт создаёт их локально перед рендером
(assets/mac/ и assets/fonts/ в .gitignore). Нужен PyObjC: ../../.venv/bin/python.
"""

import json
import shutil
from pathlib import Path

from AppKit import (
    NSApplication,
    NSBitmapImageRep,
    NSColor,
    NSCompositingOperationSourceAtop,
    NSCursor,
    NSDeviceRGBColorSpace,
    NSFontWeightMedium,
    NSGraphicsContext,
    NSImage,
    NSImageSymbolConfiguration,
    NSPNGFileType,
    NSRectFillUsingOperation,
)

ROOT = Path(__file__).resolve().parent.parent
MAC = ROOT / "assets" / "mac"
FONTS = ROOT / "assets" / "fonts"
SCALE = 4  # пикселей на пункт: с запасом для кадра 1920×1080

# Значки строки меню и меню: имя SF Symbol, размер в пунктах, режим цвета
SYMBOLS = {
    "wifi": ("wifi", 15, "mono"),
    "battery": ("battery.75percent", 15, "hier"),
    "spotlight": ("magnifyingglass", 14, "mono"),
    "control-center": ("switch.2", 14, "mono"),
    "checkmark": ("checkmark", 12, "mono"),
    "chevron": ("chevron.right", 11, "mono"),
}


def bitmap(w, h):
    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, round(w * SCALE), round(h * SCALE), 8, 4, True, False, NSDeviceRGBColorSpace, 0, 0)
    rep.setSize_((w, h))
    return rep


def save(rep, path):
    rep.representationUsingType_properties_(NSPNGFileType, {}).writeToFile_atomically_(str(path), True)


def export_symbol(out, name, points, mode):
    conf = NSImageSymbolConfiguration.configurationWithPointSize_weight_(points, NSFontWeightMedium)
    if mode == "hier":
        conf = conf.configurationByApplyingConfiguration_(
            NSImageSymbolConfiguration.configurationWithHierarchicalColor_(NSColor.whiteColor()))
    img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
    img = img.imageWithSymbolConfiguration_(conf)
    w, h = img.size()
    rep = bitmap(w, h)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep))
    img.drawInRect_(((0, 0), (w, h)))
    if mode == "mono":  # белый, как в тёмной строке меню
        NSColor.whiteColor().set()
        NSRectFillUsingOperation(((0, 0), (w, h)), NSCompositingOperationSourceAtop)
    NSGraphicsContext.restoreGraphicsState()
    save(rep, MAC / f"{out}.png")
    return {"w": w, "h": h}


def export_cursor():
    cur = NSCursor.arrowCursor()
    img = cur.image()
    best = max(img.representations(), key=lambda r: r.pixelsWide())  # самое крупное представление
    save(best, MAC / "cursor-arrow.png")
    w, h = img.size()
    x, y = cur.hotSpot()
    return {"w": w, "h": h, "hotX": x, "hotY": y}


def main():
    NSApplication.sharedApplication()  # без него NSCursor возвращает None
    MAC.mkdir(parents=True, exist_ok=True)
    FONTS.mkdir(parents=True, exist_ok=True)
    for f in ("SFNS.ttf", "SFNSMono.ttf"):
        shutil.copyfile(f"/System/Library/Fonts/{f}", FONTS / f)  # без системных флагов файла

    sizes = {"cursor-arrow": export_cursor()}
    for out, (name, points, mode) in SYMBOLS.items():
        sizes[out] = export_symbol(out, name, points, mode)
    (MAC / "sizes.json").write_text(json.dumps(sizes, indent=2))
    print(json.dumps(sizes, indent=2))


if __name__ == "__main__":
    main()
