"""Native window for H2Sync: pick device and server, plan, confirm, live progress, cancel.

Renders entirely from Run.state, polled on the Tk main loop; the engine runs
in worker threads and never touches Tk."""

from __future__ import annotations

import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox, ttk

from h2sync import (APP_NAME, Run, Settings, Subsonic, SyncError, eject_device, fmt_bytes,
                    fmt_duration, list_volumes, load_device_settings, login_token, make_cfg,
                    normalize_url, plan_worker, playlist_selection, save_device_settings,
                    selected, server_key, start_execution, volume_label)

POLL_MS = 200
RUNNING = ["scrobbles", "organize", "transfer", "covers", "playlists", "cleanup"]
STEP_LABELS = ["Plays", "Organize", "Copy", "Covers", "Playlists", "Clean up"]
IDLE = ("setup", "planning", "ready", "done", "cancelled", "error")
RED, AMBER, GREEN = "#c4302b", "#b26a00", "#1f8a4c"


def rate(n) -> str:
    return f"{fmt_bytes(n)}/s" if n else "—"


def dur(s) -> str:
    return "—" if s is None else fmt_duration(s)


def in_thread(fn, *args):
    threading.Thread(target=fn, args=args, daemon=True).start()


class Fonts:
    """Sizes relative to the platform's own UI font, so the window looks native
    on macOS, Windows and Linux alike."""

    def __init__(self, root):
        base = tkfont.nametofont("TkDefaultFont", root=root)
        fam, size = base.actual("family"), abs(base.actual("size")) or 12
        self.base = size

        def f(delta=0, bold=False):
            return tkfont.Font(root=root, family=fam, size=size + delta, weight="bold" if bold else "normal")
        self.title, self.value, self.pct = f(7, True), f(4, True), f(17, True)
        self.strong, self.step_on, self.small = f(1, True), f(0, True), f(-2)
        self.mono = tkfont.nametofont("TkFixedFont", root=root)


class Window:
    def __init__(self, settings: Settings, device: Path | None, server_ref: str | None):
        self.settings = settings
        self.device = device
        self.server = settings.server(server_ref or settings.data.get("last_server")) or (
            settings.servers[0] if settings.servers else None)
        self.run: Run | None = None
        self.idle_message = ""
        self.seen_version = -1
        self.seen_run = None
        self.log_count = 0
        self.close_when_idle = False
        self.plan_shown = None
        self.volumes: list[dict] = []

        self.root = root = tk.Tk()
        root.title(APP_NAME)
        root.geometry("860x900")
        root.minsize(700, 660)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        F = self.F = Fonts(root)

        style = ttk.Style(root)
        style.configure("Title.TLabel", font=F.title)
        style.configure("Sub.TLabel", foreground="gray")
        style.configure("Key.TLabel", foreground="gray", font=F.small)
        style.configure("Value.TLabel", font=F.value)
        style.configure("Detail.TLabel", foreground="gray", font=F.small)
        style.configure("Pct.TLabel", font=F.pct)
        style.configure("Step.TLabel", foreground="gray")
        style.configure("StepOn.TLabel", font=F.step_on)
        style.configure("StepDone.TLabel", foreground=GREEN)

        outer = ttk.Frame(root, padding=16)
        outer.pack(fill="both", expand=True)

        # Header
        head = ttk.Frame(outer)
        head.pack(fill="x")
        self.title = ttk.Label(head, text=APP_NAME, style="Title.TLabel")
        self.title.pack(side="left")
        self.status = ttk.Label(head, text="", font=F.strong)
        self.status.pack(side="right")

        # Setup: device, server and playlists are chosen and saved independently.
        setup = ttk.Frame(outer, padding=(0, 8, 0, 4))
        setup.pack(fill="x")
        setup.columnconfigure(1, weight=1)
        ttk.Label(setup, text="Device", style="Key.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 10))
        self.device_box = ttk.Combobox(setup, state="readonly")
        self.device_box.grid(row=0, column=1, sticky="ew", pady=2)
        self.device_box.bind("<<ComboboxSelected>>", self.on_device_picked)
        self.device_box.bind("<Button-1>", lambda e: self.refresh_volumes(), add="+")
        dbtn = ttk.Frame(setup)
        dbtn.grid(row=0, column=2, sticky="w", padx=(8, 0))
        self.b_browse = ttk.Button(dbtn, text="Folder…", command=self.browse_device)
        self.b_browse.pack(side="left")

        ttk.Label(setup, text="Server", style="Key.TLabel").grid(row=1, column=0, sticky="w", padx=(0, 10))
        self.server_box = ttk.Combobox(setup, state="readonly")
        self.server_box.grid(row=1, column=1, sticky="ew", pady=2)
        self.server_box.bind("<<ComboboxSelected>>", self.on_server_picked)
        sbtn = ttk.Frame(setup)
        sbtn.grid(row=1, column=2, sticky="w", padx=(8, 0))
        self.b_edit_server = ttk.Button(sbtn, text="Edit…", command=lambda: self.server_dialog(self.server))
        self.b_edit_server.pack(side="left")
        self.b_add_server = ttk.Button(sbtn, text="Add…", command=lambda: self.server_dialog(None))
        self.b_add_server.pack(side="left", padx=(4, 0))

        ttk.Label(setup, text="Playlists", style="Key.TLabel").grid(row=2, column=0, sticky="w", padx=(0, 10))
        self.playlist_summary = ttk.Label(setup, text="")
        self.playlist_summary.grid(row=2, column=1, sticky="w", pady=2)
        self.b_playlists = ttk.Button(setup, text="Choose…", command=self.playlist_dialog)
        self.b_playlists.grid(row=2, column=2, sticky="w", padx=(8, 0))

        self.notes = tk.Label(outer, justify="left", anchor="w", wraplength=800, fg=RED, font=F.strong)
        self.notes.pack(fill="x", pady=(4, 0))

        # Connection
        conn = ttk.LabelFrame(outer, text="Connection", padding=10)
        conn.pack(fill="x", pady=(6, 10))
        self.conn = {}
        for col, key in enumerate(["Link", "Card writes", "Network", "Limited by", "Free space"]):
            self.conn[key] = self._tile(conn, key, 0, col)
            conn.columnconfigure(col, weight=1, uniform="conn")

        # Body: one of setup / planning / plan / progress shows at a time; result under it.
        self.body = ttk.Frame(outer)
        self.body.pack(fill="both", expand=True)

        self.setup_msg = ttk.Frame(self.body, padding=(0, 30))
        self.setup_text = ttk.Label(self.setup_msg, font=F.strong, justify="center")
        self.setup_text.pack()

        self.planning = ttk.Frame(self.body, padding=(0, 20))
        self.spinner = ttk.Progressbar(self.planning, mode="indeterminate", length=260)
        self.spinner.pack(pady=(0, 8))
        self.plan_step = ttk.Label(self.planning, text="Starting", font=F.strong)
        self.plan_step.pack()
        ttk.Label(self.planning, style="Sub.TLabel",
                  text="Nothing on the device changes until you press Start sync.").pack()

        self.plan = ttk.LabelFrame(self.body, text="Plan", padding=10)
        tiles = ttk.Frame(self.plan)
        tiles.pack(fill="x")
        self.ptile = {}
        names = ["Plays to send", "Tracks to copy", "Already on device",
                 "Files to organize", "To remove", "Album covers"]
        for i, key in enumerate(names):
            self.ptile[key] = self._tile(tiles, key, i // 3, i % 3)
        for c in range(3):
            tiles.columnconfigure(c, weight=1, uniform="plan")
        self.tabs = ttk.Notebook(self.plan)
        self.tabs.pack(fill="both", expand=True, pady=(10, 0))
        self.trees = {}
        for key, cols in [("Playlists", ("Playlist", "Tracks", "New")),
                          ("Plays", ("Track", "Played")),
                          ("To copy", ("Track", "Size")),
                          ("Organize", ("From", "To")),
                          ("Remove", ("File", "Why")),
                          ("Not on server", ("File (left as is)",))]:
            self.trees[key] = self._tree(self.tabs, key, cols)

        self.progress = ttk.LabelFrame(self.body, text="Sync", padding=12)
        steps = ttk.Frame(self.progress)
        steps.pack(fill="x", pady=(0, 10))
        self.step_labels = []
        for label in STEP_LABELS:
            lab = ttk.Label(steps, text=label, style="Step.TLabel")
            lab.pack(side="left", padx=(0, 14))
            self.step_labels.append(lab)
        row = ttk.Frame(self.progress)
        row.pack(fill="x")
        self.pct = ttk.Label(row, text="0%", style="Pct.TLabel")
        self.pct.pack(side="left")
        self.phase_step = ttk.Label(row, style="Sub.TLabel")
        self.phase_step.pack(side="right", anchor="s")
        self.bar = ttk.Progressbar(self.progress, mode="determinate", maximum=1000)
        self.bar.pack(fill="x", pady=(6, 10))
        stats = ttk.Frame(self.progress)
        stats.pack(fill="x")
        self.stat = {}
        for col, key in enumerate(["Tracks", "Data", "Speed", "Time left", "Elapsed"]):
            self.stat[key] = self._tile(stats, key, 0, col, small=True)
            stats.columnconfigure(col, weight=1, uniform="stats")
        cur = ttk.Frame(self.progress, padding=(0, 12, 0, 0))
        cur.pack(fill="x")
        self.cur_label = ttk.Label(cur, font=F.strong)
        self.cur_label.pack(anchor="w")
        self.cur_path = ttk.Label(cur, style="Detail.TLabel")
        self.cur_path.pack(anchor="w")
        self.cur_bar = ttk.Progressbar(cur, mode="determinate", maximum=1000)
        self.cur_bar.pack(fill="x", pady=(4, 0))

        self.result = ttk.LabelFrame(self.body, text="Finished", padding=10)
        self.rtile = {}
        for i, key in enumerate(["Plays sent", "Tracks copied", "Files organized",
                                 "Files removed", "Playlists written", "Time"]):
            self.rtile[key] = self._tile(self.result, key, i // 3, i % 3)
        for c in range(3):
            self.result.columnconfigure(c, weight=1, uniform="result")

        # Buttons
        self.actions = ttk.Frame(outer, padding=(0, 10))
        self.actions.pack(fill="x")
        self.btn = {
            "start": ttk.Button(self.actions, text="Start sync", command=self.start, default="active"),
            "cancel": ttk.Button(self.actions, text="Cancel sync", command=self.cancel),
            "replan": ttk.Button(self.actions, text="Check again", command=self.replan),
            "eject": ttk.Button(self.actions, text="Eject", command=self.eject),
            "close": ttk.Button(self.actions, text="Close", command=self.on_close),
        }
        self.hint = ttk.Label(self.actions, style="Sub.TLabel")

        # Activity
        logf = ttk.LabelFrame(outer, text="Activity", padding=6)
        logf.pack(fill="both", expand=False)
        self.log = tk.Text(logf, height=8, wrap="word", font=F.mono, borderwidth=0,
                           highlightthickness=0, state="disabled")
        if sys.platform == "darwin":
            try:
                self.log.configure(background="systemTextBackgroundColor", foreground="systemTextColor")
            except tk.TclError:
                pass
        scroll = ttk.Scrollbar(logf, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log.pack(fill="both", expand=True)
        for tag, color in (("ok", GREEN), ("warn", AMBER), ("error", RED), ("time", "gray")):
            self.log.tag_configure(tag, foreground=color)

        root.bind("<Return>", lambda e: self.start())
        root.bind("<Escape>", lambda e: self.cancel())

    # ------------------------------------------------ widgets

    def _tile(self, parent, key, row, col, small=False):
        f = ttk.Frame(parent, padding=(4, 4, 12, 4))
        f.grid(row=row, column=col, sticky="nsew")
        ttk.Label(f, text=key, style="Key.TLabel").pack(anchor="w")
        v = ttk.Label(f, text="…", style="Value.TLabel" if not small else "StepOn.TLabel")
        v.pack(anchor="w")
        d = ttk.Label(f, text="", style="Detail.TLabel", wraplength=150 if not small else 130)
        d.pack(anchor="w")
        return v, d

    def _tree(self, notebook, title, cols):
        frame = ttk.Frame(notebook)
        tree = ttk.Treeview(frame, columns=cols, show="headings", height=8)
        for c in cols:
            tree.heading(c, text=c)
            numeric = c in ("Tracks", "New", "Size", "Played")
            tree.column(c, anchor="e" if numeric else "w", stretch=not numeric,
                        width=110 if numeric else 300)
        sb = ttk.Scrollbar(frame, command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        tree.pack(fill="both", expand=True)
        notebook.add(frame, text=title)
        return tree

    @staticmethod
    def _set(tile, value, detail="", color=None):
        v, d = tile
        v.configure(text=value, foreground=color or "")
        d.configure(text=detail)

    # ------------------------------------------------ setup choices

    def phase(self) -> str:
        return self.run.state["phase"] if self.run else "setup"

    def device_name(self) -> str:
        return volume_label(self.device) if self.device else "device"

    def refresh_volumes(self):
        self.volumes = list_volumes()
        paths = [v["path"] for v in self.volumes]
        if self.device and self.device not in paths:
            self.volumes.append({"path": self.device, "name": volume_label(self.device),
                                 "rockbox": False, "folder": True})
        labels = []
        for v in self.volumes:
            kind = "Rockbox player" if v["rockbox"] else ("folder" if v.get("folder") else "drive")
            labels.append(f"{v['name']}   —   {v['path']}   ({kind})")
        self.device_box["values"] = labels
        idx = next((i for i, v in enumerate(self.volumes) if v["path"] == self.device), None)
        if idx is not None:
            self.device_box.current(idx)
        else:
            self.device_box.set("No device found: plug in a player or card, or pick a folder")

    def refresh_servers(self):
        self.server_box["values"] = [f"{s['name']}   —   {s['user']} @ {s['url']}" for s in self.settings.servers]
        idx = next((i for i, s in enumerate(self.settings.servers) if self.server and s["id"] == self.server["id"]), None)
        if idx is not None:
            self.server_box.current(idx)
        else:
            self.server_box.set("No server yet: press Add…")
        self.b_edit_server.state(["!disabled"] if self.server else ["disabled"])

    def refresh_playlist_summary(self):
        if not (self.device and self.server):
            self.playlist_summary.configure(text="—", foreground="gray")
            return
        try:
            sel = playlist_selection(load_device_settings(self.device), self.server)
        except SyncError as exc:
            self.playlist_summary.configure(text=str(exc), foreground=RED)
            return
        parts = sel["names"] + [f"names matching “{p}”" for p in sel["patterns"]]
        if not parts:
            self.playlist_summary.configure(text="None chosen for this device yet", foreground=AMBER)
        else:
            text = ", ".join(parts)
            self.playlist_summary.configure(text=text if len(text) < 90 else text[:87] + "…", foreground="")

    def on_device_picked(self, _event=None):
        i = self.device_box.current()
        if 0 <= i < len(self.volumes) and self.volumes[i]["path"] != self.device:
            self.set_device(self.volumes[i]["path"])

    def browse_device(self):
        path = filedialog.askdirectory(parent=self.root, title="Choose the device or folder to sync",
                                       initialdir=str(self.device or Path.home()))
        if path:
            self.set_device(Path(path))

    def set_device(self, device: Path):
        self.device = device
        self.settings.remember(device=device)
        self.refresh_volumes()
        self.restart()

    def on_server_picked(self, _event=None):
        i = self.server_box.current()
        if 0 <= i < len(self.settings.servers):
            self.server = self.settings.servers[i]
            self.settings.remember(server=self.server)
            self.restart()

    def restart(self):
        """New device or server: drop the old plan and make a new one."""
        if self.run:
            self.run.cancel_event.set()
        self.run = None
        self.refresh_servers()
        self.refresh_playlist_summary()
        self.root.title(f"{APP_NAME} — {self.device_name()}" if self.device else APP_NAME)
        self.title.configure(text=volume_label(self.device) if self.device else APP_NAME)
        if not self.server:
            self.idle_message = "Add your Navidrome server to get started."
        elif not self.server.get("token"):
            self.idle_message = f"Enter the password for {self.server['user']} (Edit…)."
        elif not self.device:
            self.idle_message = "Plug in your player or card, or choose a folder."
        else:
            self.idle_message = ""
            self.run = Run(make_cfg(self.device, self.server), Subsonic(self.server))
            in_thread(plan_worker, self.run)
        self.plan_shown = None
        self.seen_version = -1

    # ------------------------------------------------ dialogs

    def server_dialog(self, server: dict | None):
        if self.phase() not in IDLE:
            return
        dlg = tk.Toplevel(self.root)
        dlg.title("Edit server" if server else "Add server")
        dlg.transient(self.root)
        dlg.resizable(False, False)
        frm = ttk.Frame(dlg, padding=16)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)
        fields = {}
        rows = [("Name", "name", server["name"] if server else "Navidrome"),
                ("Address", "url", server["url"] if server else "http://"),
                ("Username", "user", server["user"] if server else ""),
                ("Password", "password", "")]
        for r, (label, key, value) in enumerate(rows):
            ttk.Label(frm, text=label).grid(row=r, column=0, sticky="w", pady=3, padx=(0, 10))
            e = ttk.Entry(frm, width=44, show="•" if key == "password" else "")
            e.insert(0, value)
            e.grid(row=r, column=1, sticky="ew", pady=3)
            fields[key] = e
        has_token = bool(server and server.get("token"))
        ttk.Label(frm, style="Detail.TLabel", wraplength=380, justify="left",
                  text=("Leave the password empty to keep the saved login. " if has_token else "")
                  + "The password itself is not stored. A login token made from it is saved in "
                    "the H2Sync folder, so anyone with this card can sign in to your account "
                    "with it. Changing your Navidrome password revokes it.").grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(6, 8))
        msg = ttk.Label(frm, text="", wraplength=380, justify="left")
        msg.grid(row=5, column=0, columnspan=2, sticky="w")
        btns = ttk.Frame(frm)
        btns.grid(row=6, column=0, columnspan=2, sticky="e", pady=(10, 0))

        def collect() -> dict | None:
            name, url = fields["name"].get().strip(), normalize_url(fields["url"].get())
            user, pw = fields["user"].get().strip(), fields["password"].get()
            if not (name and url and user):
                msg.configure(text="Name, address and username are needed.", foreground=RED)
                return None
            new = {**(server or {}), "name": name, "url": url, "user": user}
            if pw:
                new["salt"], new["token"] = login_token(pw)
            elif not (server and server.get("token") and server["url"] == url and server["user"] == user):
                msg.configure(text="Enter the password.", foreground=RED)
                return None
            return new

        def save():
            new = collect()
            if not new:
                return
            msg.configure(text=f"Connecting to {new['url']}…", foreground="gray")
            save_btn.state(["disabled"])

            def check():
                try:
                    Subsonic(new).ping()
                    err = None
                except SyncError as exc:
                    err = str(exc)
                self.root.after(0, done, err)

            def done(err):
                save_btn.state(["!disabled"])
                if err and not messagebox.askyesno("Couldn't sign in", f"{err}\n\nSave it anyway?", parent=dlg):
                    msg.configure(text=err, foreground=RED)
                    return
                self.server = self.settings.put_server(new)
                self.settings.remember(server=self.server)
                dlg.destroy()
                self.restart()
            in_thread(check)

        def delete():
            if messagebox.askyesno("Remove server", f"Remove {server['name']} and its saved login?", parent=dlg):
                self.settings.remove_server(server["id"])
                self.server = self.settings.servers[0] if self.settings.servers else None
                dlg.destroy()
                self.restart()

        if server:
            ttk.Button(btns, text="Remove", command=delete).pack(side="left", padx=(0, 20))
        ttk.Button(btns, text="Cancel", command=dlg.destroy).pack(side="left", padx=(0, 6))
        save_btn = ttk.Button(btns, text="Save", command=save, default="active")
        save_btn.pack(side="left")
        dlg.bind("<Return>", lambda e: save())
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        fields["password" if server else "url"].focus_set()
        dlg.grab_set()

    def playlist_dialog(self):
        if self.phase() not in IDLE or not (self.device and self.server and self.server.get("token")):
            return
        device, server = self.device, self.server
        dlg = tk.Toplevel(self.root)
        dlg.title(f"Playlists for {volume_label(device)}")
        dlg.transient(self.root)
        dlg.geometry("520x560")
        frm = ttk.Frame(dlg, padding=16)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, wraplength=480, justify="left",
                  text=f"Playlists from {server['name']} to keep on {volume_label(device)}. "
                       "Click a playlist to tick or untick it.").pack(anchor="w")
        tree = ttk.Treeview(frm, columns=("on", "name", "songs"), show="headings", height=14)
        tree.heading("on", text="")
        tree.heading("name", text="Playlist")
        tree.heading("songs", text="Songs")
        tree.column("on", width=36, anchor="center", stretch=False)
        tree.column("name", width=340)
        tree.column("songs", width=70, anchor="e", stretch=False)
        tree.pack(fill="both", expand=True, pady=(8, 8))
        ttk.Label(frm, text="Also include every playlist whose name matches (e.g. “fityp - *”, comma separated):",
                  wraplength=480, justify="left").pack(anchor="w")
        pattern = ttk.Entry(frm)
        pattern.pack(fill="x", pady=(4, 8))
        status = ttk.Label(frm, text="Loading playlists…", style="Detail.TLabel")
        status.pack(anchor="w")
        btns = ttk.Frame(frm)
        btns.pack(anchor="e", pady=(8, 0))

        ds = load_device_settings(device)
        sel = playlist_selection(ds, server)
        pattern.insert(0, ", ".join(sel["patterns"]))
        ticked: set[str] = set(sel["names"])

        def mark(name):
            return "☑" if name in ticked else ("◐" if selected(name, {"names": [], "patterns": patterns()}) else "☐")

        def patterns():
            return [p.strip() for p in pattern.get().split(",") if p.strip()]

        def redraw(*_):
            for iid in tree.get_children():
                tree.set(iid, "on", mark(tree.set(iid, "name")))

        def toggle(event):
            iid = tree.identify_row(event.y)
            if not iid:
                return
            name = tree.set(iid, "name")
            ticked.symmetric_difference_update({name})
            redraw()

        def loaded(items, err):
            if err:
                status.configure(text=err, foreground=RED)
                return
            for p in sorted(items, key=lambda p: p["name"].lower()):
                tree.insert("", "end", values=(mark(p["name"]), p["name"], p.get("songCount", 0)))
            status.configure(text="☑ chosen   ◐ included by a name pattern", foreground="gray")

        def load():
            try:
                items, err = Subsonic(server).playlists(), None
            except SyncError as exc:
                items, err = [], str(exc)
            self.root.after(0, loaded, items, err)

        def save():
            ds2 = load_device_settings(device)
            ds2["playlists"][server_key(server)] = {"names": sorted(ticked), "patterns": patterns()}
            try:
                save_device_settings(device, ds2)
            except OSError as exc:
                messagebox.showerror("Couldn't save", str(exc), parent=dlg)
                return
            dlg.destroy()
            self.restart()

        tree.bind("<Button-1>", toggle)
        pattern.bind("<KeyRelease>", redraw)
        ttk.Button(btns, text="Cancel", command=dlg.destroy).pack(side="left", padx=(0, 6))
        ttk.Button(btns, text="Save", command=save, default="active").pack(side="left")
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        dlg.grab_set()
        in_thread(load)

    # ------------------------------------------------ actions

    def start(self):
        if not self.run:
            return
        st = self.run.state
        if st["phase"] != "ready" or not st["plan"] or st["plan"]["nothing_to_do"] or not st["plan"]["fits"]:
            return
        self.run.update(phase="scrobbles", step="Starting")
        start_execution(self.run)

    def cancel(self):
        if not self.run or self.run.state["phase"] in IDLE or self.run.cancel_event.is_set():
            return
        if messagebox.askyesno("Cancel sync?", f"Files already copied stay on {self.device_name()}, and the "
                               "next sync carries on from there.", icon="warning", parent=self.root):
            self.run.cancel_event.set()
            self.run.update(step="Cancelling after the current file")

    def replan(self):
        if self.phase() in IDLE and self.phase() != "planning":
            self.refresh_volumes()
            self.restart()

    def eject(self):
        if not self.device:
            return
        name, device = self.device_name(), self.device

        def work():
            ok, msg = eject_device(device)
            if self.run is None:
                return
            if ok:
                self.run.update(device={"ejected": True})
                self.run.log(f"Ejected. It is safe to unplug {name}.", "ok")
            else:
                self.run.log(f"Eject failed: {msg}", "error")
        in_thread(work)

    def on_close(self):
        if self.phase() in IDLE:
            self.root.destroy()
            return
        if messagebox.askyesno("Sync in progress", f"Cancel the sync and close? Files already "
                               f"copied stay on {self.device_name()}.", icon="warning", parent=self.root):
            self.run.cancel_event.set()
            self.run.update(step="Cancelling after the current file")
            self.close_when_idle = True

    # ------------------------------------------------ rendering

    def setup_state(self) -> dict:
        return {"version": 0, "phase": "setup", "step": "", "error": None, "device": {},
                "plan": None, "transfer": None, "result": None, "log": []}

    def poll(self):
        st = self.run.snapshot() if self.run else self.setup_state()
        if self.run is not self.seen_run or st["version"] != self.seen_version:
            if self.run is not self.seen_run:
                self.seen_run = self.run
                self.log_count = 0
                self.log.configure(state="normal")
                self.log.delete("1.0", "end")
                self.log.configure(state="disabled")
            self.seen_version = st["version"]
            self.render(st)
        if self.close_when_idle and st["phase"] in IDLE and st["phase"] != "planning":
            self.root.destroy()
            return
        self.root.after(POLL_MS, self.poll)

    def render(self, st):
        ph, d, p, t = st["phase"], st["device"], st.get("plan"), st.get("transfer")
        labels = {"setup": ("", ""), "planning": ("Checking", "gray"), "ready": ("Ready", ""),
                  "done": ("Done", GREEN), "cancelled": ("Cancelled", AMBER), "error": ("Error", RED)}
        text, color = labels.get(ph, ("Syncing", "#2f6fde"))
        self.status.configure(text=text, foreground=color)
        base_title = f"{APP_NAME} — {self.device_name()}" if self.device else APP_NAME
        if ph in RUNNING and t:
            self.root.title(f"{int(100 * t['bytes_done'] / max(t['bytes_total'], 1))}% · {base_title}")
        else:
            self.root.title(base_title)

        busy = ph not in IDLE
        for w in (self.b_browse, self.b_edit_server, self.b_add_server, self.b_playlists):
            w.state(["disabled"] if busy else ["!disabled"])
        for box in (self.device_box, self.server_box):
            box.state(["disabled"] if busy else ["!disabled", "readonly"])
        if not self.server:
            self.b_edit_server.state(["disabled"])
        if not (self.device and self.server and self.server.get("token")):
            self.b_playlists.state(["disabled"])

        name = self.device_name()
        notes = []
        link = d.get("link") or {}
        if link.get("slow"):
            notes.append(f"{name} is connected at USB Full Speed ({link['mbps']:.0f} Mb/s): copying will "
                         "crawl at about 1 MB/s. Plug it straight into the computer (no hub), try another "
                         "cable or port, then press Check again.")
        if p and not p["fits"]:
            notes.append(f"Not enough free space: {fmt_bytes(p['download_bytes'])} to copy, "
                         f"{fmt_bytes(d.get('free', 0))} free.")
        if st.get("error"):
            notes.append(st["error"])
        info = []
        if ph == "ready" and p:
            if not (p["selection"]["names"] or p["selection"]["patterns"]):
                info.append("No playlists chosen for this device: press Choose… next to Playlists.")
            if not p["rockbox"]:
                info.append(f"{name} has no .rockbox folder, so there are no plays to send.")
            elif not p["scrobbles"]["logging"]:
                info.append(f"No playback log on {name}, so no plays to send. "
                            "Turn on Settings > Playback > Logging on the player.")
        if d.get("ejected"):
            info.append(f"Ejected. It is safe to unplug {name}.")
        self.notes.configure(text="\n".join(notes + info), fg=RED if notes else "gray")

        self._render_connection(st)
        for frame in (self.setup_msg, self.planning, self.plan, self.progress, self.result):
            frame.pack_forget()
        if ph == "setup":
            self.setup_text.configure(text=self.idle_message)
            self.setup_msg.pack(fill="both", expand=True)
        if ph == "planning":
            self.planning.pack(fill="both", expand=True)
            self.spinner.start(12)
            self.plan_step.configure(text=st["step"])
        else:
            self.spinner.stop()
        if ph == "ready" and p:
            self.plan.pack(fill="both", expand=True)
            if self.plan_shown != st["version"]:
                self._render_plan(p)
                self.plan_shown = st["version"]
        if ph in RUNNING:
            self.progress.pack(fill="both", expand=True)
            self._render_progress(st)
        if st.get("result") and ph in ("done", "cancelled"):
            self.result.configure(text="Cancelled" if st["result"].get("cancelled") else "Finished")
            self.result.pack(fill="x")
            self._render_result(st["result"])
        self._render_actions(st)
        self._render_log(st["log"])

    def _render_connection(self, st):
        d, t = st["device"], st.get("transfer") or {}
        link = d.get("link")
        if link:
            value = f"{link['mbps']:.0f} Mb/s" if link.get("mbps") else (
                "SD reader" if link.get("kind") == "card" else "USB" if link.get("kind") == "usb" else "—")
            self._set(self.conn["Link"], value, link["label"],
                      RED if link.get("slow") else GREEN if link.get("mbps") or link.get("kind") == "card" else None)
        else:
            self._set(self.conn["Link"], "…")
        w = d.get("write_bps")
        self._set(self.conn["Card writes"], rate(t.get("write_bps") or w) if (w or t.get("write_bps")) else "…",
                  "this sync" if t.get("write_bps") else "test write")
        self._set(self.conn["Network"], rate(t.get("net_bps")),
                  "this sync" if t.get("net_bps") else "once copying starts")
        if t.get("bottleneck"):
            dev = t["bottleneck"] == "device"
            self._set(self.conn["Limited by"], "The card" if dev else "Network", "slowest link")
        elif link and link.get("slow"):
            self._set(self.conn["Limited by"], "USB link", "~1 MB/s cap", RED)
        elif w:
            self._set(self.conn["Limited by"], "The card", "expected")
        else:
            self._set(self.conn["Limited by"], "…")
        if d.get("free") is not None:
            self._set(self.conn["Free space"], fmt_bytes(d["free"]), f"of {fmt_bytes(d['total'])}")
        else:
            self._set(self.conn["Free space"], "…")

    def _render_plan(self, p):
        s = p["scrobbles"]
        self._set(self.ptile["Plays to send"], str(s["pending"]),
                  f"{s['already']} already sent · {s['short']} too short"
                  + (f" · {s['unknown']} not identified" if s["unknown"] else "") if s["logging"] else "no playback log")
        self._set(self.ptile["Tracks to copy"], str(p["downloads"]),
                  fmt_bytes(p["download_bytes"]) + (f" · about {fmt_duration(p['estimate_s'])}" if p["estimate_s"] else ""))
        self._set(self.ptile["Already on device"], f"{p['on_device']} / {p['tracks_total']}",
                  f"playlist tracks · {p['kept_files']} library files kept")
        self._set(self.ptile["Files to organize"], str(p["moves"]),
                  "into /Music/Artist/Album (Year)" if p["moves"] else "layout is already standard")
        self._set(self.ptile["To remove"], str(p["deletions"] + p["duplicates"] + len(p["stale_playlists"])),
                  f"{p['deletions']} unused synced · {p['duplicates']} duplicates"
                  + (f" · {len(p['stale_playlists'])} old playlists" if p["stale_playlists"] else ""))
        self._set(self.ptile["Album covers"], str(p["covers"]), "cover.jpg for album art" if p["covers"] else "all albums have one")

        def fill(key, rows, total=None):
            tree = self.trees[key]
            tree.delete(*tree.get_children())
            for r in rows:
                tree.insert("", "end", values=r)
            if total and total > len(rows):
                tree.insert("", "end", values=(f"…and {total - len(rows)} more",))
            title = key if not (total or rows) else f"{key} ({total if total is not None else len(rows)})"
            self.tabs.tab(tree.master, text=title)

        fill("Playlists", [(x["name"], x["tracks"], x["new"] or "") for x in p["playlists"]])
        fill("Plays", [(x["label"], x["when"]) for x in s["items"]], s["pending"])
        fill("To copy", [(x["label"], fmt_bytes(x["size"])) for x in p["download_items"]], p["downloads"])
        fill("Organize", [(x["from"], x["to"]) for x in p["move_items"]], p["moves"])
        fill("Remove", [(x["label"], "no longer in a playlist") for x in p["deletion_items"]]
             + [(x["path"], "duplicate copy") for x in p["duplicate_items"]], p["deletions"] + p["duplicates"])
        fill("Not on server", [(x,) for x in p["unmatched_items"]], p["unmatched_files"])

    def _render_progress(self, st):
        ph, t = st["phase"], st.get("transfer")
        idx = RUNNING.index(ph)
        for i, lab in enumerate(self.step_labels):
            if i < idx:
                lab.configure(text=f"✓ {STEP_LABELS[i]}", style="StepDone.TLabel")
            elif i == idx:
                lab.configure(text=STEP_LABELS[i], style="StepOn.TLabel")
            else:
                lab.configure(text=STEP_LABELS[i], style="Step.TLabel")
        self.phase_step.configure(text=st["step"])
        frac = t["bytes_done"] / max(t["bytes_total"], 1) if t else 0.0
        self.pct.configure(text=f"{int(frac * 100)}%")
        self.bar["value"] = frac * 1000
        if t:
            n = t["files_done"] + t["failed"]
            self._set(self.stat["Tracks"], f"{n} / {t['files_total']}", f"{t['failed']} failed" if t["failed"] else "",
                      RED if t["failed"] else None)
            self._set(self.stat["Data"], fmt_bytes(t["bytes_done"]), f"of {fmt_bytes(t['bytes_total'])}")
            self._set(self.stat["Speed"], rate(t["rate"]), "last 10 s")
            self._set(self.stat["Time left"], dur(t["eta_s"]))
            self._set(self.stat["Elapsed"], dur(t["elapsed_s"]))
        else:
            for key in self.stat:
                self._set(self.stat[key], "—")
        c = (t or {}).get("current")
        if c:
            self.cur_label.configure(text=c["label"])
            self.cur_path.configure(text=f"{fmt_bytes(c['done'])} of {fmt_bytes(c['size'])}  ·  /{c['path']}")
            self.cur_bar["value"] = 1000 * c["done"] / c["size"] if c["size"] else 0
        else:
            self.cur_label.configure(text="")
            self.cur_path.configure(text="")
            self.cur_bar["value"] = 0

    def _render_result(self, r):
        self._set(self.rtile["Plays sent"], str(r.get("scrobbled", 0)))
        self._set(self.rtile["Tracks copied"], str(r.get("downloaded", 0)),
                  fmt_bytes(r.get("downloaded_bytes", 0)) + (f" · {r['failed']} failed" if r.get("failed") else ""),
                  AMBER if r.get("failed") else None)
        self._set(self.rtile["Files organized"], str(r.get("moved", 0)))
        self._set(self.rtile["Files removed"], str(r.get("deleted", 0)))
        self._set(self.rtile["Playlists written"], str(r.get("playlists", 0)),
                  f"{r['covers']} album covers added" if r.get("covers") else "")
        avg = r.get("downloaded_bytes", 0) / r["elapsed_s"] if r.get("elapsed_s") else 0
        self._set(self.rtile["Time"], dur(r.get("elapsed_s")), f"average {rate(avg)}" if avg else "")

    def _render_actions(self, st):
        ph, p, d = st["phase"], st.get("plan"), st["device"]
        for b in self.btn.values():
            b.pack_forget()
        self.hint.pack_forget()
        self.btn["eject"].configure(text=f"Eject {self.device_name()}")
        can_eject = bool(self.device) and not d.get("ejected")
        show, hint = [], ""
        if ph == "ready" and p:
            if p["nothing_to_do"]:
                hint = "Everything is already in sync."
            else:
                show.append("start")
                self.btn["start"].state(["!disabled"] if p["fits"] else ["disabled"])
                hint = f"Nothing changes on {self.device_name()} until you start."
            show += ["replan"] + (["eject"] if can_eject else []) + ["close"]
        elif ph in RUNNING:
            show = ["cancel"]
            cancelling = self.run.cancel_event.is_set()
            self.btn["cancel"].configure(text="Cancelling…" if cancelling else "Cancel sync")
            self.btn["cancel"].state(["disabled"] if cancelling else ["!disabled"])
            hint = "Cancelling keeps every finished file; the next sync carries on from there."
        elif ph == "planning":
            show = ["close"]
        elif ph == "setup":
            show = ["replan", "close"]
        else:
            show = (["eject"] if can_eject else []) + ["replan", "close"]
        for key in show:
            self.btn[key].pack(side="left", padx=(0, 8))
        if hint:
            self.hint.configure(text=hint)
            self.hint.pack(side="left", padx=(6, 0))

    def _render_log(self, entries):
        if len(entries) < self.log_count:
            self.log_count = 0
        new = entries[self.log_count:]
        if not new:
            return
        at_bottom = self.log.yview()[1] > 0.98
        self.log.configure(state="normal")
        for e in new:
            self.log.insert("end", f"{e['t']}  ", "time")
            self.log.insert("end", e["text"] + "\n", e["level"] if e["level"] != "info" else ())
        self.log.configure(state="disabled")
        self.log_count = len(entries)
        if at_bottom:
            self.log.see("end")

    # ------------------------------------------------ lifecycle

    def bring_to_front(self):
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(600, lambda: self.root.attributes("-topmost", False))
        self.root.focus_force()


def run(settings: Settings, device: Path | None, server_ref: str | None) -> int:
    win = Window(settings, device, server_ref)
    win.refresh_volumes()
    win.restart()
    win.root.after(100, win.bring_to_front)
    win.root.after(POLL_MS, win.poll)
    if not settings.servers:
        win.root.after(400, lambda: win.server_dialog(None))
    win.root.mainloop()
    return 0
