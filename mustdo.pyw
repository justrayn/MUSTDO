"""
MUSTDO.EXE - an always-on-top retro checklist for Windows.

Double-click this file (needs Python from python.org, which includes tkinter).
For the system-tray icon, run once:  pip install pystray pillow
Emergency exit: tray icon -> Quit, or Ctrl+Shift+Q while the window is focused
(or end "pythonw.exe" in Task Manager).
"""
import tkinter as tk
import json, math, os, queue, random, re, time
import datetime as dt

try:
    import winsound
except ImportError:
    winsound = None

# ----------------------------- settings you can edit -----------------------------
MAX_SKIPS = 3            # "skip 15 min" uses allowed per day
SKIP_MINUTES = 15
EXTRA_MINUTES = 10       # added when you answer "Not yet" after a timer
DIM_SCREEN = True       # True = darken the whole screen behind the window
DAY_STARTS_AT_HOUR = 0   # 0 = list resets at 12:00 AM. Use 4 if you stay up late (resets at 4 AM)
CONGRATS_SECONDS = 12    # how long the "see you tomorrow" window stays before it hides
DEFAULT_TASKS = [        # (name, minutes or None for a plain checkbox)
    ("Clean the room", None),
    ("Take medicine", None),
    ("Exercise", 30),
    ("Start assignments", 45),
    ("Practice streaming", 60),
]
# ----------------------------------------------------------------------------------

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mustdo_state.json")
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]  # index = Python weekday()

FACE, HI, LO, DK = "#c0c0c0", "#ffffff", "#808080", "#0a0a0a"
FIELD, TEXT, MUTED = "#ffffff", "#000000", "#404040"
NAVY1, NAVY2, OK, WARN = (0, 0, 128), (16, 132, 208), "#008000", "#fff3a8"
FONT = ("Tahoma", 10)
FONT_B = ("Tahoma", 10, "bold")
FONT_BIG = ("Tahoma", 11)
TITLE_FONT = ("Courier New", 12, "bold")
TIMER_FONT = ("Courier New", 14, "bold")
TRANSP = "#010101"
CONFETTI = ["#ff3b30", "#ffcc00", "#34c759", "#0a84ff", "#ff2d92", "#af52de", "#ff9500"]


# ----------------------------- state -----------------------------
def _day_now():
    return dt.datetime.now() - dt.timedelta(hours=DAY_STARTS_AT_HOUR)


def today():
    return _day_now().date().isoformat()


def yesterday():
    return (_day_now().date() - dt.timedelta(days=1)).isoformat()


def fresh():
    return {
        "tasks": [{"id": "t%d" % i, "text": n, "mins": m, "done": False, "endAt": None}
                  for i, (n, m) in enumerate(DEFAULT_TASKS)],
        "classUntil": None, "skipUntil": None, "skipsUsed": 0, "schedOff": 0,
        "date": today(), "streak": 0, "best": 0, "lastComplete": None, "schedule": [],
    }


def load():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
    except Exception:
        s = None
    if not s or "tasks" not in s:
        s = fresh()
    for k, v in fresh().items():
        s.setdefault(k, v)
    if s["date"] != today():  # new day: everything unchecked
        for t in s["tasks"]:
            t["done"], t["endAt"] = False, None
        s["classUntil"] = s["skipUntil"] = None
        s["skipsUsed"], s["schedOff"], s["date"] = 0, 0, today()
    return s


def save(s):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=1)
    except Exception:
        pass


def fmt(sec):
    sec = max(0, int(sec))
    h, m, x = sec // 3600, sec % 3600 // 60, sec % 60
    return "%d:%02d:%02d" % (h, m, x) if h else "%d:%02d" % (m, x)


def clock(ts):
    return dt.datetime.fromtimestamp(ts).strftime("%I:%M %p").lstrip("0")


def to_min(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


# ----------------------------- app -----------------------------
class App:
    def __init__(self):
        try:  # sharp text on high-DPI screens
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
        self.s = load()
        self.root = tk.Tk()
        self.root.withdraw()
        self.alerted = set()
        self.last_key = ""
        self.editing = False
        self.view = "main"
        self.show_class_panel = False
        self.peek = False
        self.manual_open = False      # opened on purpose from the tray icon
        self.auto_close_edit = False
        self.congrats_until = 0
        self.confetti_pending = False
        self.confetti_running = False
        self.confetti_win = None
        self.congrats_canvas = None
        self.cur_mode = "overlay"
        self.was_done = False
        self.cmds = queue.Queue()     # commands coming from the tray thread
        self.tray = None
        self.dyn = []  # (label, function returning text) refreshed every second

        self.dim = self.make_window()
        self.dim.configure(bg="black")
        self.dim.attributes("-alpha", 0.45)
        self.dim.geometry("%dx%d+0+0" % (self.root.winfo_screenwidth(), self.root.winfo_screenheight()))
        self.dim.withdraw()
        self.win = self.make_window()
        self.win.withdraw()
        self.pill = self.make_window()
        self.pill.withdraw()

        self.check_complete()
        self.congrats_until, self.confetti_pending = 0, False  # no party for work finished earlier
        self.was_done = self.all_done()
        save(self.s)
        self.tray_ok = self.start_tray()
        self.render()
        self.root.after(1000, self.tick)
        self.root.after(200, self.poll)

    # ---- windows ----
    def make_window(self):
        w = tk.Toplevel(self.root)
        w.overrideredirect(True)          # no native title bar -> can't be closed/minimised
        w.attributes("-topmost", True)
        w.configure(bg=FACE)
        w.protocol("WM_DELETE_WINDOW", lambda: None)
        w.bind("<Alt-F4>", lambda e: "break")
        w.bind("<Control-Shift-Q>", self.quit_app)
        w.bind("<Control-Shift-q>", self.quit_app)
        w.bind("<Control-Shift-C>", lambda e: self.handle("preview"))
        w.bind("<Control-Shift-c>", lambda e: self.handle("preview"))
        return w

    def quit_app(self, _e=None):
        try:
            if self.tray:
                self.tray.stop()
        except Exception:
            pass
        self.root.destroy()

    def btn(self, parent, text, cmd, bold=False, disabled=False):
        return tk.Button(parent, text=text, command=cmd, font=FONT_B if bold else FONT, bg=FACE,
                         activebackground=FACE, disabledforeground=LO, relief="raised", bd=2,
                         padx=8, pady=1, state="disabled" if disabled else "normal",
                         cursor="arrow" if disabled else "hand2")

    def entry(self, parent, width=12):
        e = tk.Entry(parent, width=width, font=FONT, bg=FIELD, fg=TEXT, relief="sunken", bd=2,
                     insertbackground=TEXT)
        e.bind("<Button-1>", lambda ev: ev.widget.focus_force())
        return e

    def title_bar(self, parent, text):
        c = tk.Canvas(parent, height=24, highlightthickness=0, bd=0, bg="#000080")
        c.pack(fill="x", padx=2, pady=2)

        def paint(e):
            c.delete("all")
            w = max(e.width, 2)
            for i in range(0, w, 3):
                r = i / (w - 1)
                col = "#%02x%02x%02x" % tuple(int(NAVY1[k] + (NAVY2[k] - NAVY1[k]) * r) for k in range(3))
                c.create_rectangle(i, 0, i + 3, 24, fill=col, outline=col)
            c.create_text(8, 12, text=text, anchor="w", fill="white", font=TITLE_FONT)
            for k, ch in enumerate("_\u25a1x"):
                x = w - 60 + k * 19
                c.create_rectangle(x, 4, x + 17, 20, fill=FACE, outline=DK)
                c.create_text(x + 9, 12, text=ch, fill=LO, font=("Tahoma", 9, "bold"))
        c.bind("<Configure>", paint)

    # ---- logic ----
    def all_done(self):
        t = self.s["tasks"]
        return len(t) > 0 and all(x["done"] for x in t)

    def overdue(self):
        n = time.time()
        return [t for t in self.s["tasks"] if t["endAt"] and not t["done"] and t["endAt"] <= n]

    def running(self):
        n = time.time()
        return sorted([t for t in self.s["tasks"] if t["endAt"] and not t["done"] and t["endAt"] > n],
                      key=lambda t: t["endAt"])

    def cur_streak(self):
        s = self.s
        return s["streak"] if s["lastComplete"] in (today(), yesterday()) else 0

    def check_complete(self):
        s = self.s
        if self.all_done() and s["lastComplete"] != today():
            s["streak"] = s["streak"] + 1 if s["lastComplete"] == yesterday() else 1
            s["lastComplete"] = today()
            s["best"] = max(s["best"], s["streak"])

    def celebrate(self):
        self.congrats_until = time.time() + CONGRATS_SECONDS
        self.confetti_pending = True

    def class_info(self):
        s, now = self.s, time.time()
        if s["classUntil"] and s["classUntil"] > now:
            return {"until": s["classUntil"], "name": "Class", "manual": True}
        if now < s["schedOff"]:
            return None
        d = dt.datetime.now()
        mins = d.hour * 60 + d.minute
        for e in s["schedule"]:
            if d.weekday() in e["days"]:
                a, b = to_min(e["start"]), to_min(e["end"])
                if a <= mins < b:
                    until = d.replace(hour=b // 60, minute=b % 60, second=0, microsecond=0).timestamp()
                    return {"until": until, "name": e.get("name") or "Class"}
        return None

    def mode(self):
        nat = self.natural_mode()
        return "overlay" if (self.manual_open and nat != "overlay") else nat

    def natural_mode(self):
        now = time.time()
        if self.class_info():
            return "class"
        if self.s["skipUntil"] and self.s["skipUntil"] > now:
            return "skip"
        if self.congrats_until > now:
            return "congrats"
        if self.all_done() and not self.editing and self.view == "main":
            return "done"
        if (not self.overdue() and self.running() and not self.peek
                and not self.editing and self.view == "main"):
            return "working"
        return "overlay"

    def mode_key(self):
        return "%s:%d" % (self.mode(), len(self.overdue()))

    def notify(self):
        try:
            if winsound:
                winsound.PlaySound("SystemExclamation", winsound.SND_ALIAS | winsound.SND_ASYNC)
            else:
                self.root.bell()
        except Exception:
            pass

    # ---- rendering ----
    def render(self):
        s, now = self.s, time.time()
        if s["classUntil"] and s["classUntil"] <= now:
            s["classUntil"] = None
        if s["skipUntil"] and s["skipUntil"] <= now:
            s["skipUntil"] = None
        if self.manual_open and self.natural_mode() == "overlay":
            self.manual_open = self.auto_close_edit = False
        self.last_key = self.mode_key()
        m = self.mode()
        self.cur_mode = m
        self.dyn = []
        if m != "congrats":
            self.stop_confetti()
        if m in ("overlay", "congrats"):
            self.pill.withdraw()
            self.show_win()
            if m == "congrats" and self.confetti_pending:
                self.confetti_pending = False
                self.start_confetti(self.congrats_canvas)
        elif m == "done" and self.tray_ok:
            # finished for today: nothing on screen at all (use the tray icon to reopen)
            self.win.withdraw()
            self.dim.withdraw()
            self.pill.withdraw()
        else:
            self.win.withdraw()
            self.dim.withdraw()
            self.show_pill(m)
        self.update_tray()

    def place_center(self, w):
        w.update_idletasks()
        sw, sh = w.winfo_screenwidth(), w.winfo_screenheight()
        ww = 470
        hh = min(w.winfo_reqheight(), sh - 80)
        w.geometry("%dx%d+%d+%d" % (ww, hh, (sw - ww) // 2, max(10, (sh - hh) // 2 - 20)))

    def show_win(self):
        w = self.win
        for c in w.winfo_children():
            c.destroy()
        outer = tk.Frame(w, bg=FACE, bd=2, relief="raised")
        outer.pack(fill="both", expand=True)
        self.title_bar(outer, "MUSTDO.EXE - Daily Checklist")
        body = tk.Frame(outer, bg=FACE)
        body.pack(fill="both", expand=True, padx=10, pady=8)
        if self.cur_mode == "congrats":
            self.build_congrats(body)
        elif self.view == "schedule":
            self.build_sched(body)
        else:
            self.build_main(body)
        # status bar
        s = self.s
        bar = tk.Frame(outer, bg=FACE)
        bar.pack(fill="x", padx=3, pady=(0, 3))
        done = sum(1 for t in s["tasks"] if t["done"])
        for txt in ("%d/%d done" % (done, len(s["tasks"])),
                    "Streak: %d day(s)" % self.cur_streak(),
                    "Skips left: %d" % (MAX_SKIPS - s["skipsUsed"])):
            tk.Label(bar, text=txt, font=("Tahoma", 9), bg=FACE, relief="sunken", bd=1,
                     anchor="w", padx=6).pack(side="left", fill="x", expand=True, padx=1)
        self.place_center(w)
        if DIM_SCREEN and not self.manual_open:
            self.dim.deiconify()
            self.dim.lift()
        w.deiconify()
        w.lift()
        w.attributes("-topmost", True)

    def build_main(self, body):
        s, now = self.s, time.time()
        done = sum(1 for t in s["tasks"] if t["done"])
        total = max(len(s["tasks"]), 1)

        lead = tk.Frame(body, bg=FACE)
        lead.pack(fill="x")
        tk.Label(lead, text="!", font=("Tahoma", 20, "bold"), fg="#c00000", bg=FACE).pack(side="left", padx=(0, 10))
        headline = ("Everything is done for today. Nice work!" if self.all_done()
                    else "Finish everything on this list to close this window.")
        tk.Label(lead, text="%s\nBest streak: %d day(s)" % (headline, s["best"]),
                 font=FONT, bg=FACE, justify="left", anchor="w").pack(side="left")

        prog = tk.Canvas(body, height=20, bg=FIELD, relief="sunken", bd=2, highlightthickness=0)
        prog.pack(fill="x", pady=8)
        prog.update_idletasks()
        blocks = int(done / total * 30)
        for i in range(blocks):
            prog.create_rectangle(3 + i * 14, 3, 3 + i * 14 + 11, 17, fill="#000080", outline="#000080")

        lst = tk.Frame(body, bg=FIELD, relief="sunken", bd=2)
        lst.pack(fill="x")
        for t in s["tasks"]:
            self.build_task(lst, t, now)

        if self.editing:
            row = tk.Frame(body, bg=FACE)
            row.pack(fill="x", pady=(8, 0))
            self.new_text = self.entry(row, 22)
            self.new_text.pack(side="left", padx=(0, 4))
            self.new_mins = self.entry(row, 6)
            self.new_mins.pack(side="left")
            tk.Label(row, text="min (optional)", font=("Tahoma", 8), bg=FACE).pack(side="left", padx=4)
            r2 = tk.Frame(body, bg=FACE)
            r2.pack(fill="x", pady=(8, 0))
            self.btn(r2, "Add task", self.add_task, bold=True).pack(side="left", padx=(0, 6))
            self.btn(r2, "Done editing", self.toggle_edit).pack(side="left")
            return

        foot = tk.Frame(body, bg=FACE)
        foot.pack(fill="x", pady=(10, 0))
        left = MAX_SKIPS - s["skipsUsed"]
        self.btn(foot, "I'm in class", self.toggle_class_panel).pack(side="left", padx=(0, 6))
        self.btn(foot, "Skip %d min (%d left)" % (SKIP_MINUTES, left), self.skip, disabled=left <= 0).pack(side="left", padx=(0, 6))
        foot2 = tk.Frame(body, bg=FACE)
        foot2.pack(fill="x", pady=(6, 0))
        self.btn(foot2, "Schedule", self.open_sched).pack(side="left", padx=(0, 6))
        self.btn(foot2, "Edit", self.toggle_edit).pack(side="left", padx=(0, 6))
        if self.running() and not self.manual_open:
            self.btn(foot2, "Back to work", self.hide_peek, bold=True).pack(side="left")
        if self.manual_open:
            self.btn(foot2, "Close", self.close_manual, bold=True).pack(side="left")

        if self.show_class_panel:
            g = tk.LabelFrame(body, text=" How long is your class? ", font=FONT, bg=FACE, padx=8, pady=6)
            g.pack(fill="x", pady=(10, 0))
            r = tk.Frame(g, bg=FACE)
            r.pack(fill="x")
            for h in (1, 1.5, 2, 3):
                self.btn(r, "%g hr" % h, lambda h=h: self.start_class(h)).pack(side="left", padx=(0, 6))
            r3 = tk.Frame(g, bg=FACE)
            r3.pack(fill="x", pady=(6, 0))
            self.class_hours = self.entry(r3, 6)
            self.class_hours.pack(side="left")
            tk.Label(r3, text="hours", font=FONT, bg=FACE).pack(side="left", padx=4)
            self.btn(r3, "Hide window", self.start_custom_class, bold=True).pack(side="left", padx=6)

    def build_task(self, lst, t, now):
        row = tk.Frame(lst, bg=FIELD)
        row.pack(fill="x", padx=2, pady=2)
        top = tk.Frame(row, bg=FIELD)
        top.pack(fill="x")
        box = tk.Label(top, text="\u2713" if t["done"] else " ", width=2, font=FONT_B, fg=OK, bg=FIELD,
                       relief="sunken", bd=2, cursor="hand2")
        box.bind("<Button-1>", lambda e, t=t: self.toggle(t))
        box.pack(side="left", padx=(2, 8), pady=2)
        label = t["text"] + ("   (%d min)" % t["mins"] if t["mins"] else "")
        tk.Label(top, text=label, font=("Tahoma", 11, "overstrike") if t["done"] else FONT_BIG,
                 fg=MUTED if t["done"] else TEXT, bg=FIELD, anchor="w").pack(side="left", fill="x", expand=True)
        if self.editing:
            self.btn(top, "Delete", lambda t=t: self.delete(t)).pack(side="right", padx=2)
        if t["mins"] and not t["done"] and not self.editing:
            sub = tk.Frame(row, bg=FIELD)
            sub.pack(fill="x", padx=(34, 2), pady=(4, 2))
            if t["endAt"] and t["endAt"] <= now:
                box2 = tk.Frame(sub, bg=WARN, bd=1, relief="sunken")
                box2.pack(fill="x")
                tk.Label(box2, text="Time's up! Are you done with \"%s\"?" % t["text"], font=FONT, bg=WARN,
                         anchor="w", wraplength=360, justify="left").pack(fill="x", padx=6, pady=(4, 2))
                br = tk.Frame(box2, bg=WARN)
                br.pack(fill="x", padx=6, pady=(0, 6))
                self.btn(br, "Yes, done", lambda t=t: self.mark_done(t), bold=True).pack(side="left", padx=(0, 6))
                self.btn(br, "Not yet (+%d min)" % EXTRA_MINUTES, lambda t=t: self.more_time(t)).pack(side="left")
            elif t["endAt"]:
                lb = tk.Label(sub, font=TIMER_FONT, bg=FIELD, fg="#000080")
                lb.pack(side="left")
                self.dyn.append((lb, lambda t=t: "\u23f3 " + fmt(t["endAt"] - time.time())))
                lb.config(text="\u23f3 " + fmt(t["endAt"] - now))
                self.btn(sub, "Cancel", lambda t=t: self.stop_timer(t)).pack(side="left", padx=8)
            else:
                self.btn(sub, "Start %d min timer" % t["mins"], lambda t=t: self.start_timer(t)).pack(side="left")
                tk.Label(sub, text="(hides this window while you work)", font=("Tahoma", 8), fg=MUTED,
                         bg=FIELD).pack(side="left", padx=6)
        tk.Frame(lst, bg=LO, height=1).pack(fill="x")

    def build_congrats(self, body):
        cv = tk.Canvas(body, width=420, height=120, bg="#34b4f5", highlightthickness=0, bd=0, relief="sunken")
        cv.pack()
        cv.create_text(213, 63, text="ALL DONE!", font=("Courier New", 34, "bold"), fill="#000080")
        cv.create_text(210, 60, text="ALL DONE!", font=("Courier New", 34, "bold"), fill="#ffffff")
        self.congrats_canvas = cv
        tk.Label(body, text="You've done everything for today!\nSee you tomorrow!", font=("Tahoma", 13, "bold"),
                 bg=FACE, justify="center").pack(pady=(12, 4))
        tk.Label(body, text="Streak: %d day(s)   Best: %d" % (self.cur_streak(), self.s["best"]), font=FONT,
                 bg=FACE, fg=MUTED).pack()
        self.btn(body, "See you tomorrow!", self.dismiss_congrats, bold=True).pack(pady=(10, 2))

    def build_sched(self, body):
        s = self.s
        lead = tk.Frame(body, bg=FACE)
        lead.pack(fill="x")
        tk.Label(lead, text="Class schedule\nThe checklist hides itself during these times every week.",
                 font=FONT, bg=FACE, justify="left").pack(anchor="w")
        lst = tk.Frame(body, bg=FIELD, relief="sunken", bd=2)
        lst.pack(fill="x", pady=8)
        if not s["schedule"]:
            tk.Label(lst, text="No classes yet.", bg=FIELD, fg=MUTED, font=FONT).pack(pady=10)
        for i, e in enumerate(s["schedule"]):
            r = tk.Frame(lst, bg=FIELD)
            r.pack(fill="x", padx=4, pady=3)
            days = ", ".join(DAYS[d] for d in sorted(e["days"]))
            tk.Label(r, text="%s\n%s  %s-%s" % (e.get("name") or "Class", days, e["start"], e["end"]),
                     font=FONT, bg=FIELD, justify="left", anchor="w").pack(side="left")
            self.btn(r, "Delete", lambda i=i: self.del_sched(i)).pack(side="right")
        g = tk.LabelFrame(body, text=" Add a class ", font=FONT, bg=FACE, padx=8, pady=6)
        g.pack(fill="x")
        dr = tk.Frame(g, bg=FACE)
        dr.pack(fill="x")
        self.day_vars = []
        for i, d in enumerate(DAYS):
            v = tk.IntVar(value=0)
            self.day_vars.append(v)
            tk.Checkbutton(dr, text=d, variable=v, font=("Tahoma", 9), bg=FACE, activebackground=FACE).pack(side="left")
        tr = tk.Frame(g, bg=FACE)
        tr.pack(fill="x", pady=4)
        tk.Label(tr, text="From", font=FONT, bg=FACE).pack(side="left")
        self.s_start = self.entry(tr, 6)
        self.s_start.insert(0, "09:00")
        self.s_start.pack(side="left", padx=4)
        tk.Label(tr, text="to", font=FONT, bg=FACE).pack(side="left")
        self.s_end = self.entry(tr, 6)
        self.s_end.insert(0, "10:30")
        self.s_end.pack(side="left", padx=4)
        tk.Label(tr, text="(24h HH:MM)", font=("Tahoma", 8), bg=FACE).pack(side="left")
        nr = tk.Frame(g, bg=FACE)
        nr.pack(fill="x")
        tk.Label(nr, text="Name", font=FONT, bg=FACE).pack(side="left")
        self.s_name = self.entry(nr, 24)
        self.s_name.pack(side="left", padx=4)
        br = tk.Frame(body, bg=FACE)
        br.pack(fill="x", pady=(8, 0))
        self.btn(br, "Add class", self.add_sched, bold=True).pack(side="left", padx=(0, 6))
        self.btn(br, "Back", self.back_main).pack(side="left")

    def show_pill(self, m):
        p, s, now = self.pill, self.s, time.time()
        for c in p.winfo_children():
            c.destroy()
        f = tk.Frame(p, bg=FACE, bd=2, relief="raised")
        f.pack()
        row = tk.Frame(f, bg=FACE)
        row.pack(padx=8, pady=6)

        def add_label(fn):
            lb = tk.Label(row, font=FONT_B, bg=FACE, text=fn())
            lb.pack(side="left", padx=(0, 8))
            self.dyn.append((lb, fn))

        if m == "class":
            ci = self.class_info()
            add_label(lambda: "%s - back at %s (%s)" % (ci["name"], clock(ci["until"]), fmt(ci["until"] - time.time())))
            self.btn(row, "End early", self.end_class).pack(side="left")
        elif m == "skip":
            add_label(lambda: "Snoozed - back in " + fmt(s["skipUntil"] - time.time()))
            self.btn(row, "Back now", self.unskip).pack(side="left")
        elif m == "done":
            add_label(lambda: "All done! Streak: %d day(s)" % self.cur_streak())
            self.btn(row, "Edit list", self.toggle_edit).pack(side="left", padx=(0, 6))
            self.btn(row, "Reset", self.reset_all).pack(side="left")
        else:  # working
            t = self.running()[0]
            add_label(lambda: "Working: %s  %s" % (t["text"], fmt(t["endAt"] - time.time())))
            self.btn(row, "Done early", lambda: self.mark_done(t)).pack(side="left", padx=(0, 6))
            self.btn(row, "Open list", self.open_peek).pack(side="left")
        p.update_idletasks()
        w, h = p.winfo_reqwidth(), p.winfo_reqheight()
        p.geometry("+%d+%d" % (p.winfo_screenwidth() - w - 14, p.winfo_screenheight() - h - 70))
        p.deiconify()
        p.lift()
        p.attributes("-topmost", True)

    # ---- actions ----
    def changed(self):
        self.check_complete()
        now_done = self.all_done()
        if now_done and not self.was_done:
            self.celebrate()
        self.was_done = now_done
        save(self.s)
        self.render()

    def find(self, t):
        return t

    def toggle(self, t):
        t["done"] = not t["done"]
        if t["done"]:
            t["endAt"] = None
        self.changed()

    def start_timer(self, t):
        t["endAt"] = time.time() + t["mins"] * 60
        self.alerted.discard(t["id"])
        self.peek = False
        self.changed()

    def stop_timer(self, t):
        t["endAt"] = None
        self.changed()

    def mark_done(self, t):
        t["done"], t["endAt"] = True, None
        self.peek = False
        self.changed()

    def more_time(self, t):
        t["endAt"] = time.time() + EXTRA_MINUTES * 60
        self.alerted.discard(t["id"])
        self.peek = False
        self.changed()

    def delete(self, t):
        self.s["tasks"].remove(t)
        self.changed()

    def add_task(self):
        name = self.new_text.get().strip()
        try:
            mins = int(self.new_mins.get().strip())
        except ValueError:
            mins = None
        if name:
            self.s["tasks"].append({"id": "t%d" % int(time.time() * 1000), "text": name,
                                    "mins": mins if mins and mins > 0 else None, "done": False, "endAt": None})
        self.changed()

    def toggle_edit(self):
        self.editing = not self.editing
        if not self.editing and self.manual_open and self.auto_close_edit:
            self.manual_open = self.auto_close_edit = False
        self.changed()

    def close_manual(self):
        self.manual_open = self.auto_close_edit = self.editing = False
        self.view = "main"
        self.changed()

    def dismiss_congrats(self):
        self.congrats_until = 0
        self.stop_confetti()
        self.changed()

    def toggle_class_panel(self):
        self.show_class_panel = not self.show_class_panel
        self.render()

    def start_class(self, hours):
        self.s["classUntil"] = time.time() + hours * 3600
        self.show_class_panel = False
        self.changed()

    def start_custom_class(self):
        try:
            h = float(self.class_hours.get().strip())
        except ValueError:
            return
        if h > 0:
            self.start_class(h)

    def end_class(self):
        ci = self.class_info()
        self.s["classUntil"] = None
        if ci and not ci.get("manual"):  # ending a scheduled class early: ignore it until its end time
            self.s["schedOff"] = ci["until"]
        self.changed()

    def skip(self):
        if self.s["skipsUsed"] < MAX_SKIPS:
            self.s["skipsUsed"] += 1
            self.s["skipUntil"] = time.time() + SKIP_MINUTES * 60
            self.changed()

    def unskip(self):
        self.s["skipUntil"] = None
        self.changed()

    def open_peek(self):
        self.peek = True
        self.render()

    def hide_peek(self):
        self.peek = False
        self.render()

    def reset_all(self):
        for t in self.s["tasks"]:
            t["done"], t["endAt"] = False, None
        self.changed()

    def open_sched(self):
        self.view = "schedule"
        self.render()

    def back_main(self):
        self.view = "main"
        self.render()

    def del_sched(self, i):
        del self.s["schedule"][i]
        self.changed()

    def add_sched(self):
        days = [i for i, v in enumerate(self.day_vars) if v.get()]
        a, b = self.s_start.get().strip(), self.s_end.get().strip()
        ok = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")
        if not (days and ok.match(a) and ok.match(b)):
            return
        a, b = "%02d:%s" % (int(a.split(":")[0]), a.split(":")[1]), "%02d:%s" % (int(b.split(":")[0]), b.split(":")[1])
        if to_min(b) <= to_min(a):
            return
        self.s["schedule"].append({"days": days, "start": a, "end": b, "name": self.s_name.get().strip()})
        self.changed()

    # ---- confetti ----
    def start_confetti(self, fallback=None):
        self.stop_confetti()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        top = cv = None
        try:  # full-screen see-through window (Windows)
            top = tk.Toplevel(self.root)
            top.overrideredirect(True)
            top.configure(bg=TRANSP)
            top.attributes("-topmost", True)
            top.attributes("-transparentcolor", TRANSP)
            top.geometry("%dx%d+0+0" % (sw, sh))
            cv = tk.Canvas(top, width=sw, height=sh, bg=TRANSP, highlightthickness=0, bd=0)
            cv.pack()
            W, H, n = sw, sh, 170
        except tk.TclError:  # fallback: confetti only inside the message window
            if top is not None:
                top.destroy()
            top = None
            if fallback is None:
                return
            cv = fallback
            W, H, n = int(cv.cget("width")), int(cv.cget("height")), 60
        self.confetti_win, self.confetti_cv = top, cv
        parts = []
        for _ in range(n):
            w, h = random.uniform(6, 11), random.uniform(8, 15)
            x, y = random.uniform(0, W), random.uniform(-H * 0.8, 0)
            col = random.choice(CONFETTI)
            parts.append({"id": cv.create_rectangle(x, y, x + w, y + h, fill=col, outline=col),
                          "x": x, "y": y, "w": w, "h": h, "vx": random.uniform(-1.2, 1.2),
                          "vy": random.uniform(1, 3), "a": random.uniform(0, 6.28),
                          "spin": random.uniform(-0.3, 0.3), "ph": random.uniform(0, 6.28)})
        self.confetti_running = True
        t0 = time.time()

        def step():
            if not self.confetti_running:
                return
            try:
                alive = 0
                for q in parts:
                    q["vy"] = min(q["vy"] + 0.16, 5.5)
                    q["x"] += q["vx"] + math.sin(q["y"] / 28 + q["ph"]) * 0.9
                    q["y"] += q["vy"]
                    q["a"] += q["spin"]
                    wd = max(1.0, q["w"] * abs(math.cos(q["a"])))
                    cv.coords(q["id"], q["x"] - wd / 2, q["y"], q["x"] + wd / 2, q["y"] + q["h"])
                    if q["y"] < H + 20:
                        alive += 1
                if alive == 0 or time.time() - t0 > 9:
                    self.stop_confetti()
                    return
            except tk.TclError:
                self.confetti_running = False
                return
            self.root.after(30, step)
        step()

    def stop_confetti(self):
        was = self.confetti_running or self.confetti_win is not None
        self.confetti_running = False
        if not was:
            return
        try:
            if self.confetti_win is not None:
                self.confetti_win.destroy()
            elif getattr(self, "confetti_cv", None) is not None:
                self.confetti_cv.delete("all")
        except tk.TclError:
            pass
        self.confetti_win = None
        self.confetti_cv = None

    # ---- system tray icon ----
    def start_tray(self):
        try:
            import pystray
            from PIL import Image, ImageDraw
            img = Image.new("RGBA", (64, 64), (0, 0, 128, 255))
            d = ImageDraw.Draw(img)
            d.rectangle([1, 1, 62, 62], outline=(255, 255, 255, 255), width=2)
            d.line([(14, 34), (27, 47), (50, 17)], fill=(255, 255, 255, 255), width=8)
            q = self.cmds
            menu = pystray.Menu(
                pystray.MenuItem("Open checklist", lambda i, it: q.put("open"), default=True),
                pystray.MenuItem("Edit list", lambda i, it: q.put("edit")),
                pystray.MenuItem("Class schedule", lambda i, it: q.put("sched")),
                pystray.MenuItem("Preview celebration", lambda i, it: q.put("preview")),
                pystray.MenuItem("Quit", lambda i, it: q.put("quit")),
            )
            self.tray = pystray.Icon("mustdo", img, "MUSTDO - daily checklist", menu)
            self.tray.run_detached()
            return True
        except Exception:
            self.tray = None
            return False

    def update_tray(self):
        if not self.tray:
            return
        try:
            done = sum(1 for t in self.s["tasks"] if t["done"])
            self.tray.title = "MUSTDO: %d/%d done today" % (done, len(self.s["tasks"]))
        except Exception:
            pass

    def poll(self):
        try:
            while True:
                self.handle(self.cmds.get_nowait())
        except queue.Empty:
            pass
        except tk.TclError:
            return
        self.root.after(200, self.poll)

    def handle(self, cmd):
        if cmd == "quit":
            self.quit_app()
            return
        if cmd == "preview":
            self.manual_open = self.auto_close_edit = False
            self.celebrate()
            self.render()
            return
        self.manual_open = True
        self.auto_close_edit = False
        self.view = "main"
        if cmd == "edit":
            self.editing = True
            self.auto_close_edit = True
        elif cmd == "sched":
            self.view = "schedule"
        self.render()

    # ---- clock ----
    def tick(self):
        try:
            now = time.time()
            if self.s["date"] != today():  # new day (midnight): everything unchecked, window comes back
                save(self.s)
                self.s = load()
                self.congrats_until = 0
                self.manual_open = self.editing = self.auto_close_edit = False
                self.view = "main"
                self.alerted.clear()
                self.was_done = self.all_done()
                self.last_key = ""
            fire = False
            if self.mode() not in ("class", "skip"):
                for t in self.s["tasks"]:
                    if t["endAt"] and not t["done"] and t["endAt"] <= now and t["id"] not in self.alerted:
                        self.alerted.add(t["id"])
                        fire = True
            if fire:
                self.notify()
            if fire or self.mode_key() != self.last_key:
                self.render()
            else:
                for lb, fn in self.dyn:
                    try:
                        lb.config(text=fn())
                    except tk.TclError:
                        pass
                # keep whichever window is showing on top of everything
                for w in (self.win, self.pill):
                    if w.state() == "normal":
                        w.attributes("-topmost", True)
                        w.lift()
        except tk.TclError:
            return  # app is closing
        except Exception:
            pass
        self.root.after(1000, self.tick)


if __name__ == "__main__":
    app = App()
    app.root.mainloop()
