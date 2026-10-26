#!/usr/bin/env python3
"""Sleep Tool: переключатель `pmset -a disablesleep 0/1` в строке меню macOS."""

import fcntl
import getpass
import logging
import math
import os
import plistlib
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import objc
from AppKit import (
    NSAlert,
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSImage,
    NSMenu,
    NSMenuItem,
    NSStatusBar,
    NSVariableStatusItemLength,
)
from Foundation import NSObject, NSRunLoop, NSRunLoopCommonModes, NSTimer
from PyObjCTools import AppHelper

HERE = Path(__file__).resolve().parent
ICON_ON = str(HERE / "icons" / "on.pdf")
ICON_OFF = str(HERE / "icons" / "off.pdf")

PMSET = "/usr/bin/pmset"
SUDOERS_FILE = "/etc/sudoers.d/sleep-tool"
LABEL = "com.chill.sleep-tool"
AGENT_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "sleep-tool"
LOG_FILE = Path.home() / "Library" / "Logs" / "sleep-tool.log"

logging.basicConfig(filename=LOG_FILE, level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("sleep-tool")

POLL_SECONDS = 2
DURATIONS = [
    ("15 минут", 15 * 60),
    ("30 минут", 30 * 60),
    ("1 час", 60 * 60),
    ("2 часа", 2 * 60 * 60),
    ("5 часов", 5 * 60 * 60),
]

CANCELLED = "cancelled"


# --- pmset ---------------------------------------------------------------

def sleep_disabled() -> bool:
    out = subprocess.run([PMSET, "-g"], capture_output=True, text=True).stdout
    m = re.search(r"^\s*SleepDisabled\s+(\d)", out, re.MULTILINE)
    return bool(m and m.group(1) == "1")


def run_as_admin(shell_cmd: str, prompt: str):
    """Запускает команду от root через системный диалог пароля.
    Возвращает None при успехе, CANCELLED при отмене или текст ошибки."""
    esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
    script = (f'do shell script "{esc(shell_cmd)}" '
              f'with prompt "{esc(prompt)}" with administrator privileges')
    r = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, text=True)
    log.info("admin %r -> code %s %s", shell_cmd[:60], r.returncode, r.stderr.strip())
    if r.returncode == 0:
        return None
    if "-128" in r.stderr:
        return CANCELLED
    return r.stderr.strip() or "osascript завершился с ошибкой"


def set_sleep_disabled(on: bool):
    cmd = [PMSET, "-a", "disablesleep", "1" if on else "0"]
    # Если правило sudoers стоит, обходимся без пароля.
    if os.path.exists(SUDOERS_FILE):
        r = subprocess.run(["/usr/bin/sudo", "-n", *cmd], capture_output=True, text=True)
        if r.returncode == 0:
            return None
    action = "запретить" if on else "разрешить"
    return run_as_admin(" ".join(cmd), f"Sleep Tool хочет {action} сон Mac.")


# --- правило sudoers (чтобы не спрашивать пароль) -----------------------

def install_sudoers():
    user = getpass.getuser()
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", user):
        return f"Странное имя пользователя: {user!r}"
    rule = f"{user} ALL=(root) NOPASSWD: {PMSET} -a disablesleep 0, {PMSET} -a disablesleep 1"
    cmd = (f"t=$(/usr/bin/mktemp) && echo '{rule}' > \"$t\" "
           f"&& /usr/sbin/visudo -cqf \"$t\" "
           f"&& /usr/bin/install -m 0440 -o root -g wheel \"$t\" {SUDOERS_FILE}; "
           f"r=$?; /bin/rm -f \"$t\"; exit $r")
    return run_as_admin(cmd, "Sleep Tool хочет переключать сон без пароля.")


def remove_sudoers():
    return run_as_admin(f"/bin/rm -f {SUDOERS_FILE}", "Sleep Tool хочет снова спрашивать пароль.")


# --- запуск через launchd ------------------------------------------------
# Приложение всегда стартует как агент launchd. Если запускать Python из
# скрипта внутри .app, macOS 26 прячет иконку в строке меню.

def write_agent(run_at_load: bool):
    AGENT_PLIST.parent.mkdir(parents=True, exist_ok=True)
    AGENT_PLIST.write_bytes(plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": [sys.executable, str(Path(__file__).resolve())],
        "RunAtLoad": run_at_load,
        "KeepAlive": {"SuccessfulExit": False},  # перезапуск после падения, но не после «Выход»
        "ProcessType": "Interactive",
        "StandardOutPath": str(LOG_FILE),
        "StandardErrorPath": str(LOG_FILE),
    }))


def login_item_enabled() -> bool:
    try:
        return bool(plistlib.loads(AGENT_PLIST.read_bytes()).get("RunAtLoad"))
    except (OSError, plistlib.InvalidFileException):
        return False


def set_login_item(enabled: bool):
    # launchd перечитает файл при следующем входе, текущий процесс не трогаем.
    write_agent(enabled)


# --- приложение ----------------------------------------------------------

def fmt_left(seconds: float) -> str:
    minutes = max(1, math.ceil(seconds / 60))
    h, m = divmod(minutes, 60)
    if h and m:
        return f"{h} ч {m} мин"
    return f"{h} ч" if h else f"{m} мин"


def load_icon(path):
    img = NSImage.alloc().initWithContentsOfFile_(path)
    img.setSize_((20, 20))
    img.setTemplate_(True)  # macOS сама красит под светлую и тёмную строку меню
    return img


class SleepTool(NSObject):
    # Здесь чистый PyObjC, а не rumps: в macOS 26 иконка rumps не получала
    # места в строке меню (окно высотой 0), а эта показывается.
    def applicationDidFinishLaunching_(self, _):
        self.state = None   # True: сон запрещён
        self.until = None   # time.time(), когда закончится сессия на время
        self.busy = False
        self.img_on = load_icon(ICON_ON)
        self.img_off = load_icon(ICON_OFF)

        self.item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
        self.item.setAutosaveName_("SleepTool")
        self.item.button().setToolTip_("Sleep Tool")
        self.build_menu()
        self.refresh()

        timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
            POLL_SECONDS, self, "tick:", None, True)
        NSRunLoop.currentRunLoop().addTimer_forMode_(timer, NSRunLoopCommonModes)
        log.info("started, sleep disabled: %s", self.state)
        AppHelper.callLater(2, self.log_placement)

    @objc.python_method
    def log_placement(self):
        f = self.item.button().window().frame()
        log.info("menu bar icon at x=%d, size %dx%d (0 height = hidden by macOS)",
                 f.origin.x, f.size.width, f.size.height)

    @objc.python_method
    def add_item(self, menu, title, action=None, key=""):
        it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, key)
        if action:
            it.setTarget_(self)
        menu.addItem_(it)
        return it

    @objc.python_method
    def build_menu(self):
        menu = NSMenu.alloc().init()
        menu.setDelegate_(self)
        self.status_item = self.add_item(menu, "")  # без action: серая строка-заголовок
        menu.addItem_(NSMenuItem.separatorItem())
        self.toggle_item = self.add_item(menu, "", "toggle:")

        timed = NSMenu.alloc().init()
        for title, secs in DURATIONS:
            self.add_item(timed, title, "timed:").setTag_(secs)
        self.add_item(menu, "Запретить на время").setSubmenu_(timed)

        menu.addItem_(NSMenuItem.separatorItem())
        self.login_item = self.add_item(menu, "Запускать при входе", "login:")
        self.nopass_item = self.add_item(menu, "Не спрашивать пароль", "nopass:")
        menu.addItem_(NSMenuItem.separatorItem())
        self.add_item(menu, "Выход", "quit:", "q")
        self.item.setMenu_(menu)

    # Состояние и интерфейс

    @objc.python_method
    def refresh(self):
        state = sleep_disabled()
        if not state:
            self.until = None  # сон разрешили (в том числе из терминала): сессия на время закончилась
        if state != self.state:
            self.state = state
            self.item.button().setImage_(self.img_on if state else self.img_off)
        self.update_menu()

    @objc.python_method
    def update_menu(self):
        if self.busy:
            status = "Переключаю…"
        elif self.state and self.until:
            end = time.strftime("%H:%M", time.localtime(self.until))
            status = f"Сон запрещён до {end} (ещё {fmt_left(self.until - time.time())})"
        elif self.state:
            status = "Сон запрещён"
        else:
            status = "Сон разрешён"
        self.status_item.setTitle_(status)
        self.toggle_item.setTitle_("Разрешить сон" if self.state else "Запретить сон")
        self.login_item.setState_(1 if login_item_enabled() else 0)
        self.nopass_item.setState_(1 if os.path.exists(SUDOERS_FILE) else 0)

    def menuNeedsUpdate_(self, _menu):
        if not self.busy:
            self.refresh()

    def tick_(self, _timer):
        if self.busy:
            return
        if self.state and self.until and time.time() >= self.until:
            self.until = None  # если диалог пароля отменят, не спрашиваем снова каждые 2 секунды
            self.apply(False)
            return
        self.refresh()

    # Переключение

    @objc.python_method
    def apply(self, on, until=None):
        if self.busy:
            return
        self.busy = True
        self.update_menu()

        def work():
            err = set_sleep_disabled(on)
            AppHelper.callAfter(self.applied, on, until, err)

        threading.Thread(target=work, daemon=True).start()

    @objc.python_method
    def applied(self, on, until, err):
        log.info("set disablesleep=%d -> %s", on, err or "ok")
        self.busy = False
        if err is None:
            self.until = until if on else None
        elif err != CANCELLED:
            self.alert("Не получилось переключить сон", err)
        self.refresh()

    def toggle_(self, _sender):
        self.apply(not self.state)

    def timed_(self, sender):
        until = time.time() + sender.tag()
        if self.state:
            self.until = until  # сон уже запрещён, просто ставим таймер
            self.update_menu()
        else:
            self.apply(True, until)

    # Настройки

    def login_(self, _sender):
        try:
            set_login_item(not login_item_enabled())
        except OSError as e:
            self.alert("Не получилось настроить автозапуск", str(e))
        self.update_menu()

    def nopass_(self, _sender):
        err = remove_sudoers() if os.path.exists(SUDOERS_FILE) else install_sudoers()
        if err and err != CANCELLED:
            self.alert("Не получилось", err)
        self.update_menu()

    def quit_(self, _sender):
        # Сессию на время заканчиваем, как Amphetamine. Бессрочный запрет оставляем как есть.
        if self.state and self.until and not self.busy:
            set_sleep_disabled(False)
        NSApplication.sharedApplication().terminate_(None)

    @objc.python_method
    def alert(self, title, message):
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        a = NSAlert.alloc().init()
        a.setMessageText_(title)
        a.setInformativeText_(message)
        a.runModal()


def hold_single_instance_lock():
    SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
    lock = open(SUPPORT_DIR / "lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(0)  # уже запущен
    return lock


def main():
    _lock = hold_single_instance_lock()
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)  # без иконки в Dock
    delegate = SleepTool.alloc().init()
    app.setDelegate_(delegate)
    AppHelper.runEventLoop()


if __name__ == "__main__":
    if "--write-agent" in sys.argv:  # для install.sh
        write_agent(run_at_load=True)
    else:
        main()
