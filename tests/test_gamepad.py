import pytest

from gogl_ui import gamepad as gp


def test_buttons_and_dpad():
    t = gp.Translator()
    assert t.feed(gp.EV_KEY, gp.BTN_SOUTH, 1) == [("accept", True)]
    assert t.feed(gp.EV_KEY, gp.BTN_SOUTH, 2) == []  # autorrepetición del núcleo
    assert t.feed(gp.EV_KEY, gp.BTN_EAST, 0) == [("back", False)]
    assert t.feed(gp.EV_ABS, gp.ABS_HAT0Y, 1) == [("down", True)]
    assert t.feed(gp.EV_ABS, gp.ABS_HAT0Y, 0) == [("down", False)]


def test_stick_has_hysteresis_and_merges_with_dpad():
    t = gp.Translator(axes={gp.ABS_X: gp.Axis(-32768, 32767)})
    assert t.feed(gp.EV_ABS, gp.ABS_X, 30000) == [("right", True)]
    assert t.feed(gp.EV_ABS, gp.ABS_X, 15000) == []           # entre umbrales: sigue pulsado
    assert t.feed(gp.EV_KEY, gp.BTN_DPAD_RIGHT, 1) == []      # ya estaba a la derecha
    assert t.feed(gp.EV_ABS, gp.ABS_X, 0) == []               # la cruceta lo mantiene
    assert t.feed(gp.EV_KEY, gp.BTN_DPAD_RIGHT, 0) == [("right", False)]
    assert t.feed(gp.EV_ABS, gp.ABS_X, -32768) == [("left", True)]


def test_triggers_and_right_stick():
    t = gp.Translator(axes={gp.ABS_RZ: gp.Axis(0, 255), gp.ABS_RY: gp.Axis(-32768, 32767)})
    assert t.feed(gp.EV_ABS, gp.ABS_RZ, 250) == [("rt", True)]
    assert t.feed(gp.EV_ABS, gp.ABS_RZ, 255) == []
    assert t.feed(gp.EV_ABS, gp.ABS_RZ, 0) == [("rt", False)]
    t.feed(gp.EV_ABS, gp.ABS_RY, 32767)
    assert t.scroll == pytest.approx(1.0)
    t.feed(gp.EV_ABS, gp.ABS_RY, 2000)
    assert t.scroll == 0.0


def test_steam_ignore_list():
    ign, only = gp.ignored_devices({"SDL_GAMECONTROLLER_IGNORE_DEVICES": "0x28DE/0x1205,0x045e/0x028e"})
    assert ign == {(0x28DE, 0x1205), (0x045E, 0x028E)} and only is None


def test_spatial_pick():
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QRect

    from gogl_ui.navigation import pick
    cur = QRect(100, 100, 50, 20)
    below_far_left = QRect(0, 300, 40, 20)
    below_in_beam = QRect(110, 400, 40, 20)
    right = QRect(300, 105, 40, 20)
    cands = [("a", below_far_left), ("b", below_in_beam), ("c", right)]
    assert pick(cur, cands, "down") == "b"   # el que está alineado gana aunque esté más lejos
    assert pick(cur, cands, "right") == "c"
    assert pick(cur, cands, "up") is None


def test_right_stick_emits_directions():
    t = gp.Translator(axes={gp.ABS_RX: gp.Axis(-32768, 32767), gp.ABS_RY: gp.Axis(-32768, 32767)})
    assert t.feed(gp.EV_ABS, gp.ABS_RY, 32767) == [("rs-down", True)]
    assert t.feed(gp.EV_ABS, gp.ABS_RY, 15000) == []                    # histéresis
    assert t.feed(gp.EV_ABS, gp.ABS_RY, -32768) == [("rs-down", False), ("rs-up", True)]
    assert t.feed(gp.EV_ABS, gp.ABS_RY, 0) == [("rs-up", False)]
    assert t.feed(gp.EV_ABS, gp.ABS_RX, 32767) == [("rs-right", True)]
    assert t.reset() == [("rs-right", False)]
    assert gp.RIGHT_STICK["rs-up"] == "up"


def test_dialogs_and_menus_stay_inside_the_window(monkeypatch):
    """En gamescope las ventanas aparte no reciben el foco: con el mando, el diálogo,
    el menú y el desplegable se dibujan dentro de la ventana y se recorren con el
    stick derecho."""
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QMainWindow, QMenu,
                                   QPushButton, QRadioButton, QVBoxLayout, QWidget)

    from gogl_ui import navigation, overlay
    app = QApplication.instance() or QApplication([])
    nav = navigation.install(app, require_active=False)
    monkeypatch.setattr(nav, "devices", {})
    nav.set_active(True)
    nav.mouse_watch.stop()  # sin pantalla el cursor «se mueve» solo y saldría del modo mando

    class Main(QMainWindow):
        gamepad_overlays = True

    main = Main()
    main.resize(1000, 700)
    body = QWidget()
    main.setCentralWidget(body)
    manage = QPushButton("Gestionar ▾")
    menu = QMenu(manage)
    picked = []
    menu.addAction("Uno", lambda: picked.append(1))
    menu.addSeparator()
    menu.addAction("Dos", lambda: picked.append(2))
    manage.setMenu(menu)
    QVBoxLayout(body).addWidget(manage)
    main.show()
    main.activateWindow()
    app.processEvents()

    def press(name):
        nav.handle(name, True)
        nav.handle(name, False)
        app.processEvents()

    manage.setFocus()
    press("accept")
    assert overlay.top(main) is not None and QApplication.activePopupWidget() is None
    assert QApplication.focusWidget().text() == "Uno"
    press("rs-down")
    assert QApplication.focusWidget().text() == "Dos"
    QApplication.focusWidget().click()
    assert picked == [2] and overlay.top(main) is None

    dlg = QDialog(main)
    lay = QVBoxLayout(dlg)
    first, second, lang = QRadioButton("A"), QRadioButton("B"), QComboBox()
    lang.addItems(["Español", "English"])
    for w in (first, second, lang):
        lay.addWidget(w)
    seen = []

    def drive():
        seen.append(QApplication.focusWidget())
        press("rs-down")
        seen.append(QApplication.focusWidget())
        press("rs-down")
        press("accept")      # el desplegable, como lista dentro de la ventana
        press("rs-down")
        QApplication.focusWidget().click()
        seen.append(lang.currentText())
        press("back")

    QTimer.singleShot(0, lambda: QTimer.singleShot(0, drive))  # tras el foco inicial del diálogo
    QTimer.singleShot(3000, dlg.reject)  # que un fallo no deje el test colgado
    assert dlg.exec() == 0
    assert seen == [first, second, "English"]
    assert overlay.top(main) is None and dlg.isWindow()
    main.close()

