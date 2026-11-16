import tkinter as tk
from tkinter import ttk

from PIL import Image, ImageDraw, ImageTk

from radar_visuals import THEMES, compass_frame


BG = "#110d20"
PANEL = "#19132b"
FIELD = "#25193e"
RIM = "#493267"
FG = "#faf1ff"
MUTED = "#beadd7"
ACCENT = "#d49aff"
PURPLE = "#752ee6"
FONT = "Segoe UI"

DEFAULTS = {
    "theme": "Неон", "size": 100, "opacity": 94, "speed": 100,
    "pulse": True, "scan": True, "glow": True, "icons": True, "notices": True, "boxes": True,
    "near": True, "close": True, "errors": True, "restored": True,
    "position": "У мини-карты", "duration": 4,
}
POSITIONS = ("У мини-карты", "Слева по центру")


def clean_preferences(raw):
    result = DEFAULTS.copy()
    if not isinstance(raw, dict):
        return result
    for key, default in DEFAULTS.items():
        value = raw.get(key)
        if isinstance(default, bool) and isinstance(value, bool):
            result[key] = value
    for key, low, high in (("size", 75, 106), ("opacity", 35, 100),
                           ("speed", 35, 180), ("duration", 2, 7)):
        value = raw.get(key)
        if type(value) is int and low <= value <= high:
            result[key] = value
    if isinstance(raw.get("theme"), str) and raw["theme"] in THEMES:
        result["theme"] = raw["theme"]
    if raw.get("position") in POSITIONS:
        result["position"] = raw["position"]
    return result


class Settings:
    def __init__(self, app, logo, preview, notice):
        self.app = app
        self.root = app.root
        self.tabs = {}
        self.theme_buttons = {}
        self.root.configure(bg=BG)
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("Radar.TCombobox", fieldbackground=FIELD, foreground=FG,
                        background=FIELD, bordercolor=RIM, arrowcolor=ACCENT, padding=4)
        style.map("Radar.TCombobox", fieldbackground=[("readonly", FIELD)],
                  foreground=[("readonly", FG)], selectbackground=[("readonly", FIELD)],
                  selectforeground=[("readonly", FG)])
        self.switches = {}
        for active in (False, True):
            icon = Image.new("RGBA", (76, 42))
            draw = ImageDraw.Draw(icon)
            draw.rounded_rectangle((1, 2, 74, 39), radius=19, fill=PURPLE if active else FIELD,
                                   outline=ACCENT if active else RIM, width=2)
            x = 55 if active else 20
            draw.ellipse((x-13, 8, x+13, 34), fill=FG if active else MUTED)
            self.switches[active] = ImageTk.PhotoImage(
                icon.resize((38, 21), Image.Resampling.LANCZOS), master=self.root
            )
        shell = tk.Frame(self.root, bg=BG, padx=24, pady=18)
        shell.pack(fill="both", expand=True)
        header = tk.Frame(shell, bg=BG)
        header.pack(fill="x", pady=(0, 17))
        tk.Label(header, image=logo, bg=BG, bd=0).pack(side="left", padx=(0, 11))
        identity = tk.Frame(header, bg=BG)
        identity.pack(side="left")
        self.label(identity, "Радар сундуков", 24, FG, BG, True).pack(anchor="w")
        self.label(identity, "Fortnite  /  Карта v43.10", 10, ACCENT, BG).pack(anchor="w")
        body = tk.Frame(shell, bg=BG)
        body.pack(fill="both")
        left = tk.Frame(body, bg=BG)
        left.pack(side="left", fill="y", padx=(0, 18))
        self.label(left, "Живое превью", 12, FG, BG, True).pack(anchor="w", pady=(0, 7))
        tk.Label(left, image=preview, bg=BG, bd=0).pack()
        tk.Label(left, image=notice, bg=BG, bd=0).pack(pady=(4, 0))
        tk.Label(left, textvariable=app.status, anchor="w", justify="left", wraplength=400,
                 bg=BG, fg=MUTED, font=(FONT, 10)).pack(fill="x", pady=(0, 6))
        right = tk.Frame(body, bg=PANEL, padx=13, pady=12,
                         highlightbackground=RIM, highlightthickness=1)
        right.pack(side="left", fill="both", expand=True)
        nav = tk.Frame(right, bg=PANEL)
        nav.pack(fill="x", pady=(0, 14))
        self.sections = ("Визуал", "Объекты", "Анимации", "Оповещения")
        for name in self.sections:
            button = self.button(nav, name, lambda tab=name: self.open(tab), size=9)
            button.pack(side="left", fill="x", expand=True, padx=(0, 4))
            self.tabs[name] = button
        self.slot = tk.Frame(right, bg=PANEL, width=388, height=319)
        self.slot.pack(fill="both", expand=True)
        self.slot.pack_propagate(False)
        builders = (self.visual, self.objects, self.animation, self.notices)
        self.pages = {}
        self.swatches = []
        for name, build in zip(self.sections, builders):
            page = tk.Frame(self.slot, bg=PANEL)
            self.pages[name] = page
            build(page)
        self.open("Визуал")

    @staticmethod
    def label(parent, text, size, fg=FG, bg=PANEL, bold=False):
        return tk.Label(parent, text=text, fg=fg, bg=bg, bd=0,
                        font=(FONT, size, "bold" if bold else "normal"))

    def button(self, parent, text, command, primary=False, size=10, **kwargs):
        return tk.Button(parent, text=text, command=command, fg=FG, bg=PURPLE if primary else FIELD,
                         activebackground="#8748e8", activeforeground=FG, padx=7, pady=7,
                         bd=0, relief="flat", highlightthickness=1, highlightcolor=ACCENT,
                         highlightbackground=RIM, font=(FONT, size, "bold"), cursor="hand2", **kwargs)

    def open(self, name):
        for page in self.pages.values():
            page.pack_forget()
        self.pages[name].pack(fill="both", expand=True)
        for key, tab in self.tabs.items():
            tab.configure(bg=PURPLE if key == name else FIELD)

    def set(self, key, value):
        self.app.set_preference(key, value)

    def title(self, page, text):
        self.label(page, text, 12, FG, PANEL, True).pack(anchor="w", pady=(0, 9))

    def slider(self, page, key, label, low, high, display):
        row = tk.Frame(page, bg=PANEL)
        row.pack(fill="x", pady=(2, 9))
        tk.Label(row, text=label, bg=PANEL, fg=FG, font=(FONT, 10),
                 width=15, anchor="w").pack(side="left")
        value = tk.IntVar(value=self.app.prefs[key])
        readout = tk.StringVar(value=display(value.get()))
        tk.Label(row, textvariable=readout, bg=PANEL, fg=ACCENT, width=7,
                 font=(FONT, 10, "bold"), anchor="e").pack(side="right")

        track = tk.Canvas(row, height=23, width=85, bg=PANEL, borderwidth=0,
                          highlightthickness=1, highlightbackground=PANEL, highlightcolor=RIM,
                          cursor="hand2", takefocus=True)
        track.pack(side="left", fill="x", expand=True)

        def paint(_=None):
            track.delete("all")
            width = max(20, track.winfo_width())
            point = 8 + (width - 16) * (value.get() - low) / (high - low)
            track.create_line(8, 12, width-8, 12, fill=RIM, width=5, capstyle="round")
            track.create_line(8, 12, point, 12, fill="#a85bfa", width=5, capstyle="round")
            track.create_oval(point-5, 7, point+5, 17, fill=FG, outline=ACCENT)

        def change(number):
            value.set(max(low, min(high, number)))
            readout.set(display(value.get()))
            self.set(key, value.get())
            paint()

        def pointer(event):
            track.focus_set()
            ratio = (event.x - 8) / max(1, track.winfo_width() - 16)
            change(round(low + ratio*(high-low)))

        track.bind("<Configure>", paint)
        track.bind("<Button-1>", pointer)
        track.bind("<B1-Motion>", pointer)
        track.bind("<Left>", lambda _: change(value.get()-1))
        track.bind("<Right>", lambda _: change(value.get()+1))

    def check(self, page, key, label):
        def flip():
            new = not self.app.prefs[key]
            self.set(key, new)
            button.configure(image=self.switches[new])

        button = tk.Button(page, text=f"  {label}", image=self.switches[self.app.prefs[key]],
                           compound="left", command=flip, anchor="w", bg=PANEL, fg=FG, bd=0,
                           activebackground=FIELD, activeforeground=ACCENT, padx=3, pady=4,
                           cursor="hand2", highlightthickness=1, highlightcolor=ACCENT,
                           highlightbackground=PANEL, font=(FONT, 10))
        button.pack(fill="x")

    def choose_theme(self, name):
        self.set("theme", name)
        for key, button in self.theme_buttons.items():
            button.configure(bg=PURPLE if key == name else FIELD)

    def visual(self, page):
        self.title(page, "Стиль радара")
        tiles = tk.Frame(page, bg=PANEL)
        tiles.pack(fill="x", pady=(0, 17))
        for name in THEMES:
            art = ImageTk.PhotoImage(compass_frame(63, 63, name, -90, 1), master=self.root)
            self.swatches.append(art)
            button = self.button(tiles, name, lambda theme=name: self.choose_theme(theme),
                                 self.app.prefs["theme"] == name, size=9, image=art, compound="top")
            button.pack(side="left", fill="x", expand=True, padx=(0, 4))
            self.theme_buttons[name] = button
        self.slider(page, "size", "Контур", 75, 106, lambda n: f"{n}%")
        self.slider(page, "opacity", "Прозрачность", 35, 100, lambda n: f"{n}%")

    def objects(self, page):
        self.title(page, "Метки сундуков")
        self.check(page, "icons", "Иконки сундуков")
        self.check(page, "glow", "Золотое свечение")
        self.check(page, "boxes", "Подсвечивать светящиеся ящики")
        self.label(page, "Калибровка мини-карты", 12, FG, PANEL, True).pack(anchor="w", pady=(16, 8))
        self.button(page, "Выделить мини-карту", self.app.calibrate, True).pack(fill="x")
        self.button(page, "Сбросить выделение", self.app.reset).pack(fill="x", pady=(7, 0))

    def animation(self, page):
        self.title(page, "Движение радара")
        self.slider(page, "speed", "Скорость", 35, 180, lambda n: f"{n / 100:.2f}×")
        self.check(page, "pulse", "Пульсация контура")
        self.check(page, "scan", "Луч сканирования")

    def notices(self, page):
        self.check(page, "notices", "Показывать оповещения")
        for key, label in (("near", "Сундук рядом"), ("close", "Очень близко"),
                           ("errors", "Ошибка радара"), ("restored", "Сигнал восстановлен")):
            self.check(page, key, label)
        row = tk.Frame(page, bg=PANEL)
        row.pack(fill="x", pady=(9, 8))
        self.label(row, "Позиция", 10).pack(side="left")
        value = tk.StringVar(value=self.app.prefs["position"])
        menu = ttk.Combobox(row, textvariable=value, values=POSITIONS, state="readonly",
                            style="Radar.TCombobox", width=21)
        menu.pack(side="right")
        menu.bind("<<ComboboxSelected>>", lambda _: self.set("position", value.get()))
        self.slider(page, "duration", "Длительность", 2, 7, lambda n: f"{n} с")
        self.button(page, "Пример оповещения", self.app.cycle_preview, True).pack(fill="x")
