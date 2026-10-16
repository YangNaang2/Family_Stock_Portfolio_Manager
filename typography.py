"""Shared font setup: bundled Pretendard (SIL OFL) for Qt widgets and matplotlib charts."""
from pathlib import Path

from PyQt5.QtGui import QFontDatabase
from PyQt5.QtWidgets import QApplication
from matplotlib import font_manager, rcParams

FONT_DIR = Path(__file__).resolve().parent / "fonts"
FONT_FAMILIES = ("Pretendard", "Malgun Gothic", "AppleGothic", "Noto Sans CJK JP", "NanumGothic", "DejaVu Sans")


def configure_fonts():
    """Use the bundled Pretendard for widgets and charts.

    Falls back to a system Korean font when the bundled files cannot be loaded.
    Safe to call repeatedly; returns the family applied to the Qt application.
    """
    bundled = sorted(FONT_DIR.glob("*.ttf"))
    for path in bundled:
        try:
            font_manager.fontManager.addfont(str(path))
        except (OSError, RuntimeError):
            pass
    available = {font.name for font in font_manager.fontManager.ttflist}
    for name in FONT_FAMILIES:
        if name in available:
            rcParams["font.family"] = name
            break
    rcParams["axes.unicode_minus"] = False
    app = QApplication.instance()
    if app is None:
        return None
    for path in bundled:
        QFontDatabase.addApplicationFont(str(path))
    families = set(QFontDatabase().families())
    family = next((name for name in FONT_FAMILIES if name in families), None)
    if family:
        font = app.font()
        font.setFamily(family)
        app.setFont(font)
    return family
