"""Tema visual: oscuro, con un acento violeta.

Inspirado en la sobriedad de GOG Galaxy (fondo oscuro, carátulas protagonistas,
barra lateral de navegación) pero con paleta y componentes propios."""

from PySide6.QtCore import QLibraryInfo, QLocale, QTranslator
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication

BG = "#121116"
SIDEBAR = "#0c0b0f"
SURFACE = "#1b1a21"
SURFACE_2 = "#25232d"
BORDER = "#2f2c39"
TEXT = "#eceaf2"
MUTED = "#9a95a8"
ACCENT = "#8b6cf0"
ACCENT_HOVER = "#9d82f5"
ACCENT_PRESSED = "#7556dc"
OK = "#4ade80"
WARN = "#f5b942"
DANGER = "#f06c6c"

QSS = f"""
* {{ font-size: 10pt; }}
QWidget {{ background: {BG}; color: {TEXT}; }}
QToolTip {{ background: {SURFACE_2}; color: {TEXT}; border: 1px solid {BORDER}; padding: 4px 8px; }}

#Sidebar {{ background: {SIDEBAR}; border-right: 1px solid {BORDER}; }}
#Sidebar QWidget {{ background: transparent; }}
#Brand {{ font-size: 15pt; font-weight: 800; letter-spacing: 1px; padding: 4px 0; }}
#UserName {{ font-weight: 600; }}
#UserSub, #Muted {{ color: {MUTED}; }}
#SectionLabel {{ color: {MUTED}; font-size: 8pt; font-weight: 700; letter-spacing: 1px; padding-top: 12px; }}

QPushButton#NavButton {{
    text-align: left; padding: 9px 14px; border: none; border-radius: 8px;
    color: {MUTED}; background: transparent; font-weight: 600;
}}
QPushButton#NavButton:hover {{ color: {TEXT}; background: {SURFACE}; }}
QPushButton#NavButton:checked {{ color: {TEXT}; background: {SURFACE_2}; border-left: 3px solid {ACCENT}; }}

QPushButton {{
    background: {SURFACE_2}; border: 1px solid {BORDER}; border-radius: 8px;
    padding: 8px 16px; font-weight: 600;
}}
QPushButton:hover {{ border-color: {ACCENT}; }}
QPushButton:disabled {{ color: #5d5868; border-color: {SURFACE_2}; }}
QPushButton#Primary {{ background: {ACCENT}; border: none; color: white; padding: 11px 34px; font-size: 11pt; }}
QPushButton#Primary:hover {{ background: {ACCENT_HOVER}; }}
QPushButton#Primary:pressed {{ background: {ACCENT_PRESSED}; }}
QPushButton#Primary:disabled {{ background: {SURFACE_2}; color: {MUTED}; }}
QPushButton#Ghost {{ background: transparent; border: none; color: {MUTED}; padding: 6px 10px; }}
QPushButton#Ghost:hover {{ color: {TEXT}; }}
QPushButton#Chip {{ border-radius: 14px; padding: 5px 14px; background: transparent; color: {MUTED}; }}
QPushButton#Chip:checked {{ background: {SURFACE_2}; color: {TEXT}; border-color: {ACCENT}; }}

QLineEdit {{
    background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 18px;
    padding: 7px 14px; selection-background-color: {ACCENT};
}}
QLineEdit:focus {{ border-color: {ACCENT}; }}
QComboBox {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 8px; padding: 6px 10px; }}
QComboBox QAbstractItemView {{ background: {SURFACE}; border: 1px solid {BORDER}; selection-background-color: {SURFACE_2}; }}

QListView {{ background: {BG}; border: none; outline: none; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {SURFACE_2}; border-radius: 4px; min-height: 40px; }}
QScrollBar::handle:vertical:hover {{ background: {BORDER}; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{ height: 0; background: none; }}

#Card {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 12px; }}
#Card QLabel, #Hero QLabel {{ background: transparent; }}
#CardTitle {{ font-size: 11pt; font-weight: 700; }}
#GameTitle {{ font-size: 26pt; font-weight: 800; background: transparent; }}
#HeroMeta {{ color: {MUTED}; background: transparent; }}
#Pill {{ border-radius: 10px; padding: 3px 10px; font-weight: 700; font-size: 9pt; }}
#PathLabel {{ font-family: monospace; color: {MUTED}; }}
QPlainTextEdit#Log {{
    background: {SIDEBAR}; border: 1px solid {BORDER}; border-radius: 8px;
    font-family: monospace; font-size: 9pt; color: {MUTED};
}}
QTextBrowser {{ background: transparent; border: none; color: {MUTED}; }}
QDialog {{ background: {BG}; }}
QRadioButton, QCheckBox {{ spacing: 10px; padding: 3px 0; }}
QRadioButton::indicator, QCheckBox::indicator {{
    width: 14px; height: 14px; border: 2px solid {MUTED}; background: {SURFACE};
}}
QRadioButton::indicator {{ border-radius: 9px; }}
QCheckBox::indicator {{ border-radius: 4px; }}
QRadioButton::indicator:checked {{ width: 8px; height: 8px; border: 5px solid {ACCENT}; border-radius: 9px; background: white; }}
QCheckBox::indicator:checked {{ border-color: {ACCENT}; background: {ACCENT}; }}
QRadioButton::indicator:hover, QCheckBox::indicator:hover {{ border-color: {ACCENT}; }}
QTableWidget {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 8px; gridline-color: {BORDER}; }}
QHeaderView::section {{ background: {SURFACE_2}; color: {MUTED}; border: none; padding: 6px; font-weight: 700; }}
QProgressBar {{ background: {SURFACE_2}; border: none; border-radius: 3px; max-height: 6px; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 3px; }}
"""


def apply(app: QApplication) -> None:
    app.setStyle("Fusion")
    pal = QPalette()
    for role, color in ((QPalette.Window, BG), (QPalette.Base, SURFACE), (QPalette.Text, TEXT),
                        (QPalette.WindowText, TEXT), (QPalette.Button, SURFACE_2),
                        (QPalette.ButtonText, TEXT), (QPalette.Highlight, ACCENT),
                        (QPalette.HighlightedText, "#ffffff"), (QPalette.PlaceholderText, MUTED)):
        pal.setColor(role, QColor(color))
    app.setPalette(pal)
    f = QFont(app.font())
    f.setFamilies(["Inter", "Cantarell", "Noto Sans", "sans-serif"])
    app.setFont(f)
    app.setStyleSheet(QSS)
    # Botones estándar de Qt («Sí», «No», «Cancelar»…) en el idioma del sistema.
    tr = QTranslator(app)
    if tr.load(QLocale.system(), "qtbase", "_", QLibraryInfo.path(QLibraryInfo.TranslationsPath)):
        app.installTranslator(tr)
