import os
import queue
import shutil
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import __version__, ffmpeg, win32
from .browser import BROWSER_LABELS, BrowserWindow, available_browsers
from .config import Settings, Slot, profile_dir
from .recorder import SlotRecorder
from .splitjob import LoginBrowser, SplitJob, cleanup_stale_work, fmt_time, restore_speakers_if_needed

SIZE_PRESETS = ["1920x1080", "1280x720", "2560x1440", "3840x2160", "1080x1920", "1024x768"]
ENCODER_CHOICES = [("auto", "自動 (GPU優先)"), ("h264_nvenc", "NVIDIA NVENC"), ("h264_amf", "AMD AMF"), ("h264_qsv", "Intel Quick Sync"), ("libx264", "CPU (x264)")]
CASCADE_STEP = 48


def parse_size(text):
    try:
        w, h = text.lower().replace("×", "x").split("x")
        w, h = int(w), int(h)
        if 160 <= w <= 7680 and 120 <= h <= 4320:
            return w - w % 2, h - h % 2
    except ValueError:
        pass
    return None


class SlotDialog(tk.Toplevel):
    def __init__(self, parent, slot, screen):
        super().__init__(parent)
        self.title("ダッシュボード設定")
        self.resizable(False, False)
        self.transient(parent)
        self.result = None
        self.screen = screen
        pad = {"padx": 8, "pady": 4}

        self.name = tk.StringVar(value=slot.name)
        self.url = tk.StringVar(value=slot.url)
        self.size = tk.StringVar(value=f"{slot.width}x{slot.height}")

        ttk.Label(self, text="名前").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(self, textvariable=self.name, width=30).grid(row=0, column=1, sticky="we", **pad)
        ttk.Label(self, text="URL").grid(row=1, column=0, sticky="w", **pad)
        url_entry = ttk.Entry(self, textvariable=self.url, width=60)
        url_entry.grid(row=1, column=1, sticky="we", **pad)
        ttk.Label(self, text="録画サイズ").grid(row=2, column=0, sticky="w", **pad)
        ttk.Combobox(self, textvariable=self.size, values=SIZE_PRESETS, width=14).grid(row=2, column=1, sticky="w", **pad)
        self.note = ttk.Label(self, foreground="#b06000")
        self.note.grid(row=3, column=0, columnspan=2, sticky="w", **pad)
        self.size.trace_add("write", lambda *_: self._update_note())
        self._update_note()

        buttons = ttk.Frame(self)
        buttons.grid(row=4, column=0, columnspan=2, sticky="e", **pad)
        ttk.Button(buttons, text="OK", command=self._ok).pack(side="left", padx=4)
        ttk.Button(buttons, text="キャンセル", command=self.destroy).pack(side="left")
        self.bind("<Return>", lambda e: self._ok())
        self.bind("<Escape>", lambda e: self.destroy())
        url_entry.focus_set()
        url_entry.select_range(0, "end")
        self.grab_set()

    def _update_note(self):
        size = parse_size(self.size.get())
        sw, sh = self.screen
        if size is None:
            self.note.config(text="サイズは「幅x高さ」で入力してください (例: 1920x1080)")
        elif size[0] > sw or size[1] > sh - 40:
            self.note.config(text=f"画面 ({sw}x{sh}) より大きいため、表示可能な最大サイズで録画して拡大保存します")
        else:
            self.note.config(text="")

    def _ok(self):
        size = parse_size(self.size.get())
        url = self.url.get().strip()
        if size is None or not url:
            messagebox.showwarning("入力エラー", "URLとサイズを正しく入力してください", parent=self)
            return
        if "://" not in url:
            url = "https://" + url
        self.result = Slot(self.name.get().strip() or "dashboard", url, size[0], size[1], True)
        self.destroy()


class App:
    def __init__(self):
        win32.set_dpi_awareness()
        restore_speakers_if_needed()
        cleanup_stale_work()
        self.settings = Settings.load()
        self.browsers_found = available_browsers()
        if self.settings.browser not in self.browsers_found and self.browsers_found:
            self.settings.browser = next(iter(self.browsers_found))
        self.ffmpeg_path = ffmpeg.find_ffmpeg()
        self.encoder = None
        self.browsers = {}
        self.recorders = []
        self.events = queue.Queue()
        self.busy = False
        self.keep_awake = win32.KeepAwake("MultiCapture: 録画中のためスリープを抑止しています")
        self.login = None
        self.split_job = None
        self.split_started_at = None
        self.auto_duration = None
        self.stop_at = None
        self.exit_when_idle = False

        self.root = tk.Tk()
        self.root.title(f"MultiCapture {__version__}")
        self.root.minsize(860, 600)
        self._style()
        self._build()
        self._refresh_tree()
        self._update_buttons()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._poll)
        if self.ffmpeg_path:
            threading.Thread(target=self._detect_encoder, daemon=True).start()

    def schedule(self, start, duration):
        self.auto_duration = duration
        if start:
            self.notebook.select(self.dash_tab)
            self.root.after(800, self._start_recording)

    def _style(self):
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        self.root.option_add("*Font", ("Yu Gothic UI", 10))
        style.configure("Treeview", rowheight=26)
        style.configure("Accent.TButton", font=("Yu Gothic UI", 10, "bold"))
        style.configure("Hint.TLabel", foreground="#555")

    def _build(self):
        root = self.root
        top = ttk.Frame(root, padding=(10, 10, 10, 4))
        top.pack(fill="x")

        ttk.Label(top, text="保存先").grid(row=0, column=0, sticky="w")
        self.output_var = tk.StringVar(value=self.settings.output_dir)
        ttk.Entry(top, textvariable=self.output_var).grid(row=0, column=1, columnspan=5, sticky="we", padx=6)
        ttk.Button(top, text="参照…", command=self._choose_output).grid(row=0, column=6)
        ttk.Button(top, text="開く", command=self._open_output).grid(row=0, column=7, padx=(4, 0))

        ttk.Label(top, text="ブラウザ").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.browser_var = tk.StringVar(value=BROWSER_LABELS.get(self.settings.browser, ""))
        ttk.Combobox(
            top, textvariable=self.browser_var, state="readonly", width=16,
            values=[BROWSER_LABELS[k] for k in self.browsers_found],
        ).grid(row=1, column=1, sticky="w", padx=6, pady=(8, 0))

        ttk.Label(top, text="FPS").grid(row=1, column=2, sticky="e", pady=(8, 0))
        self.fps_var = tk.IntVar(value=self.settings.fps)
        ttk.Spinbox(top, from_=5, to=60, textvariable=self.fps_var, width=5).grid(row=1, column=3, sticky="w", padx=6, pady=(8, 0))

        ttk.Label(top, text="エンコーダ").grid(row=1, column=4, sticky="e", pady=(8, 0))
        labels = dict(ENCODER_CHOICES)
        self.encoder_var = tk.StringVar(value=labels.get(self.settings.encoder, labels["auto"]))
        ttk.Combobox(top, textvariable=self.encoder_var, state="readonly", width=16, values=list(labels.values())).grid(
            row=1, column=5, sticky="w", padx=6, pady=(8, 0)
        )
        self.keep_display_var = tk.BooleanVar(value=self.settings.keep_display_on)
        ttk.Checkbutton(top, text="録画中は画面をオフにしない", variable=self.keep_display_var).grid(
            row=1, column=6, columnspan=2, sticky="w", padx=(10, 0), pady=(8, 0)
        )
        top.columnconfigure(1, weight=1)

        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=(6, 0))
        self.split_tab = ttk.Frame(self.notebook, padding=10)
        self.dash_tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.split_tab, text="  動画を高速録画  ")
        self.notebook.add(self.dash_tab, text="  ページを同時録画  ")
        self._build_split_tab(self.split_tab)
        self._build_dash_tab(self.dash_tab)

        self.info_var = tk.StringVar()
        ttk.Label(root, textvariable=self.info_var, padding=(10, 6, 10, 8), style="Hint.TLabel").pack(fill="x")
        self._set_info()

    def _build_split_tab(self, tab):
        form = ttk.Frame(tab)
        form.pack(fill="x")
        ttk.Label(form, text="動画ページのURL").grid(row=0, column=0, sticky="w")
        self.split_url_var = tk.StringVar(value=self.settings.split_url)
        ttk.Entry(form, textvariable=self.split_url_var).grid(row=0, column=1, columnspan=5, sticky="we", padx=6)

        ttk.Label(form, text="同時に録画する数").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.split_count_var = tk.IntVar(value=self.settings.split_count)
        ttk.Spinbox(form, from_=1, to=8, textvariable=self.split_count_var, width=5).grid(row=1, column=1, sticky="w", padx=6, pady=(8, 0))
        ttk.Label(form, text="録画サイズ").grid(row=1, column=2, sticky="e", pady=(8, 0))
        self.split_size_var = tk.StringVar(value=self.settings.split_size)
        ttk.Combobox(form, textvariable=self.split_size_var, values=SIZE_PRESETS[:4], width=12).grid(row=1, column=3, sticky="w", padx=6, pady=(8, 0))
        self.split_pin_var = tk.BooleanVar(value=self.settings.split_pin_video)
        ttk.Checkbutton(form, text="動画部分だけを録画する", variable=self.split_pin_var).grid(row=1, column=4, sticky="w", padx=(10, 0), pady=(8, 0))
        self.mute_var = tk.BooleanVar(value=self.settings.mute_speakers)
        ttk.Checkbutton(form, text="録画中はPCの音をミュート（録音はされます）", variable=self.mute_var).grid(row=1, column=5, sticky="w", padx=(10, 0), pady=(8, 0))
        form.columnconfigure(5, weight=1)

        ttk.Label(
            tab, style="Hint.TLabel", wraplength=800, justify="left",
            text="例: 「同時に録画する数」を4にすると、2時間の動画を4つの区間に分けて同時に再生・録画し、約30分で1本の動画にします。"
                 "ログインが必要なサイトは、先に「ログイン用ブラウザを開く」でログインしてください（ログイン状態は自動で引き継がれます）。",
        ).pack(fill="x", pady=(8, 6))

        buttons = ttk.Frame(tab)
        buttons.pack(fill="x")
        self.login_btn = ttk.Button(buttons, text="ログイン用ブラウザを開く", command=self._open_login)
        self.login_btn.pack(side="left")
        self.login_state = ttk.Label(buttons, style="Hint.TLabel")
        self.login_state.pack(side="left", padx=10)
        self.split_cancel_btn = ttk.Button(buttons, text="■ 中止", command=self._cancel_split)
        self.split_cancel_btn.pack(side="right")
        self.split_start_btn = ttk.Button(buttons, text="● 高速録画開始", style="Accent.TButton", command=self._start_split)
        self.split_start_btn.pack(side="right", padx=6)

        columns = ("seg", "range", "status", "progress")
        self.seg_tree = ttk.Treeview(tab, columns=columns, show="headings", height=8)
        for col, text, width, stretch in (("seg", "区間", 60, False), ("range", "範囲", 180, False), ("status", "状態", 260, True), ("progress", "進捗", 90, False)):
            self.seg_tree.heading(col, text=text)
            self.seg_tree.column(col, width=width, stretch=stretch, anchor="w" if col == "status" else "center")
        self.seg_tree.pack(fill="both", expand=True, pady=(10, 6))

        bottom = ttk.Frame(tab)
        bottom.pack(fill="x")
        self.split_progress = ttk.Progressbar(bottom, maximum=1000)
        self.split_progress.pack(fill="x")
        self.split_status_var = tk.StringVar(value="")
        ttk.Label(bottom, textvariable=self.split_status_var).pack(fill="x", pady=(4, 0))

    def _build_dash_tab(self, tab):
        middle = ttk.Frame(tab)
        middle.pack(fill="both", expand=True)
        columns = ("enabled", "name", "url", "size", "status")
        self.tree = ttk.Treeview(middle, columns=columns, show="headings", selectmode="browse")
        for col, text, width, stretch in (
            ("enabled", "録画", 50, False), ("name", "名前", 130, False), ("url", "URL", 260, True),
            ("size", "サイズ", 90, False), ("status", "状態", 220, True),
        ):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, stretch=stretch, anchor="center" if col in ("enabled", "size") else "w")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<ButtonRelease-1>", self._on_click)

        side = ttk.Frame(middle, padding=(8, 0, 0, 0))
        side.pack(side="left", fill="y")
        self.edit_buttons = []
        for text, cmd in (("追加", self._add), ("編集", self._edit), ("削除", self._delete), ("↑", lambda: self._move(-1)), ("↓", lambda: self._move(1))):
            b = ttk.Button(side, text=text, command=cmd, width=8)
            b.pack(pady=2)
            self.edit_buttons.append(b)

        bottom = ttk.Frame(tab, padding=(0, 8, 0, 0))
        bottom.pack(fill="x")
        self.open_btn = ttk.Button(bottom, text="ブラウザを開く", command=self._open_browsers)
        self.open_btn.pack(side="left")
        self.close_btn = ttk.Button(bottom, text="ブラウザを閉じる", command=self._close_browsers)
        self.close_btn.pack(side="left", padx=6)
        self.stop_btn = ttk.Button(bottom, text="■ 録画停止", command=self._stop_recording)
        self.stop_btn.pack(side="right")
        self.rec_btn = ttk.Button(bottom, text="● 録画開始", style="Accent.TButton", command=self._start_recording)
        self.rec_btn.pack(side="right", padx=6)

    def _set_info(self, extra=None):
        parts = []
        if not self.ffmpeg_path:
            parts.append("⚠ ffmpeg.exe が見つかりません（アプリと同じフォルダに置いてください）")
        elif self.encoder:
            parts.append(f"エンコーダ: {ffmpeg.ENCODER_LABELS[self.encoder]}")
        else:
            parts.append("エンコーダを確認中…")
        if not self.browsers_found:
            parts.append("⚠ Edge / Chrome が見つかりません")
        parts.append("録画中のウィンドウは重なっていても裏に隠れていてもOK（最小化のみ不可）")
        if extra:
            parts.insert(0, extra)
        self.info_var.set("   |   ".join(parts))

    def _detect_encoder(self):
        encoder = ffmpeg.detect_encoder(self.ffmpeg_path, self._encoder_pref())
        self.events.put(("encoder", encoder))

    def _encoder_pref(self):
        label = self.encoder_var.get()
        return next((k for k, v in ENCODER_CHOICES if v == label), "auto")

    def _browser_kind(self):
        label = self.browser_var.get()
        return next((k for k, v in BROWSER_LABELS.items() if v == label), self.settings.browser)

    def _collect_settings(self):
        s = self.settings
        s.output_dir = self.output_var.get().strip() or s.output_dir
        s.browser = self._browser_kind()
        try:
            s.fps = max(5, min(60, int(self.fps_var.get())))
        except (tk.TclError, ValueError):
            s.fps = 30
        s.encoder = self._encoder_pref()
        s.keep_display_on = bool(self.keep_display_var.get())
        s.split_url = self.split_url_var.get().strip()
        try:
            s.split_count = max(1, min(8, int(self.split_count_var.get())))
        except (tk.TclError, ValueError):
            s.split_count = 4
        s.split_size = self.split_size_var.get().strip() or "1920x1080"
        s.split_pin_video = bool(self.split_pin_var.get())
        s.mute_speakers = bool(self.mute_var.get())
        try:
            s.save()
        except OSError:
            pass

    def _refresh_tree(self):
        selected = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        for i, slot in enumerate(self.settings.slots):
            status = "待機中 (ブラウザ表示中)" if self.browsers.get(i) else ""
            self.tree.insert("", "end", iid=str(i), values=(
                "✔" if slot.enabled else "—", slot.name, slot.url, f"{slot.width}x{slot.height}", status,
            ))
        if selected and self.tree.exists(selected[0]):
            self.tree.selection_set(selected)

    def _set_status(self, index, text):
        if self.tree.exists(str(index)):
            self.tree.set(str(index), "status", text)

    def _selected_index(self):
        sel = self.tree.selection()
        return int(sel[0]) if sel else None

    def _dash_recording(self):
        return any(r.running for r in self.recorders)

    def _split_running(self):
        return self.split_job is not None and self.split_job.running

    def _recording(self):
        return self._dash_recording() or self._split_running()

    def _login_open(self):
        return self.login is not None and self.login.is_open()

    def _update_buttons(self):
        dash = self._dash_recording()
        split = self._split_running()
        any_browser = any(self.browsers.values())
        idle = not dash and not split and not self.busy
        ready = bool(self.ffmpeg_path) and bool(self.browsers_found)
        state = lambda on: "!disabled" if on else "disabled"
        self.rec_btn.state([state(idle and ready)])
        self.stop_btn.state([state(dash)])
        self.open_btn.state([state(idle and bool(self.browsers_found))])
        self.close_btn.state([state(idle and any_browser)])
        for b in self.edit_buttons:
            b.state([state(idle and not any_browser)])
        self.split_start_btn.state([state(idle and ready)])
        self.split_cancel_btn.state([state(split)])
        self.login_btn.state([state(idle and bool(self.browsers_found) and not self._login_open())])
        self.login_state.config(text="ログイン用ブラウザが開いています。ログインが済んだら「● 高速録画開始」を押してください" if self._login_open() else "")

    def _choose_output(self):
        path = filedialog.askdirectory(initialdir=self.output_var.get() or os.path.expanduser("~"))
        if path:
            self.output_var.set(os.path.normpath(path))
            self._collect_settings()

    def _open_output(self):
        path = self.output_var.get()
        os.makedirs(path, exist_ok=True)
        os.startfile(path)

    def _on_click(self, event):
        if self._recording() or self.busy or any(self.browsers.values()):
            return
        if self.tree.identify_column(event.x) == "#1" and (row := self.tree.identify_row(event.y)):
            slot = self.settings.slots[int(row)]
            slot.enabled = not slot.enabled
            self._collect_settings()
            self._refresh_tree()

    def _on_double_click(self, event):
        if self.tree.identify_column(event.x) != "#1" and self.tree.identify_row(event.y):
            self._edit()

    def _dialog(self, slot):
        dlg = SlotDialog(self.root, slot, win32.primary_screen_size())
        self.root.wait_window(dlg)
        return dlg.result

    def _add(self):
        n = len(self.settings.slots) + 1
        result = self._dialog(Slot(f"dashboard{n}", "https://", 1920, 1080))
        if result:
            self.settings.slots.append(result)
            self._collect_settings()
            self._refresh_tree()

    def _edit(self):
        i = self._selected_index()
        if i is None or self._recording() or self.busy or any(self.browsers.values()):
            return
        old = self.settings.slots[i]
        result = self._dialog(old)
        if result:
            result.enabled = old.enabled
            result.id = old.id
            self.settings.slots[i] = result
            self._collect_settings()
            self._refresh_tree()

    def _delete(self):
        i = self._selected_index()
        if i is None:
            return
        slot = self.settings.slots[i]
        if messagebox.askyesno("削除", f"「{slot.name}」を削除しますか？\n(このダッシュボード用のログイン情報も削除されます)"):
            shutil.rmtree(profile_dir(slot), ignore_errors=True)
            del self.settings.slots[i]
            self._collect_settings()
            self._refresh_tree()

    def _move(self, delta):
        i = self._selected_index()
        if i is None or self.browsers:
            return
        j = i + delta
        if 0 <= j < len(self.settings.slots):
            s = self.settings.slots
            s[i], s[j] = s[j], s[i]
            self._collect_settings()
            self._refresh_tree()
            self.tree.selection_set(str(j))

    def _run_background(self, target, *args):
        self.busy = True
        self._update_buttons()

        def wrapper():
            try:
                target(*args)
            except Exception as exc:
                self.events.put(("error", str(exc)))
            finally:
                self.events.put(("idle", None))

        threading.Thread(target=wrapper, daemon=True).start()

    def _enabled_indices(self):
        return [i for i, s in enumerate(self.settings.slots) if s.enabled]

    def _launch_missing(self, indices):
        exe = self.browsers_found.get(self.settings.browser)
        if not exe:
            raise RuntimeError("ブラウザが見つかりません")
        for order, i in enumerate(indices):
            if self.browsers.get(i) and self.browsers[i].alive():
                continue
            slot = self.settings.slots[i]
            self.events.put(("status", (i, "ブラウザ起動中…")))
            b = BrowserWindow(exe, profile_dir(slot), slot.url, 20 + CASCADE_STEP * order, 20 + CASCADE_STEP * order, slot.width, slot.height)
            try:
                b.launch()
                b.fit_content(slot.width, slot.height)
                self.browsers[i] = b
                self.events.put(("status", (i, "待機中 (ブラウザ表示中)")))
            except Exception as exc:
                self.browsers.pop(i, None)
                self.events.put(("status", (i, f"エラー: {exc}")))

    def _open_browsers(self):
        indices = self._enabled_indices()
        if not indices:
            messagebox.showinfo("MultiCapture", "録画するページがありません。「追加」から登録してください。")
            return
        self._collect_settings()
        self._run_background(self._launch_missing, indices)

    def _close_browsers(self):
        def work():
            for i, b in list(self.browsers.items()):
                if b:
                    b.close()
                self.browsers.pop(i, None)
                self.events.put(("status", (i, "")))
        self._run_background(work)

    def _start_recording(self):
        indices = self._enabled_indices()
        if not indices:
            messagebox.showinfo("MultiCapture", "録画するページがありません。「追加」から登録してください。")
            return
        self._collect_settings()

        def work():
            self._launch_missing(indices)
            encoder = ffmpeg.detect_encoder(self.ffmpeg_path, self.settings.encoder)
            self.events.put(("encoder", encoder))
            recorders = []
            for i in indices:
                b = self.browsers.get(i)
                if not b or not b.alive():
                    continue
                r = SlotRecorder(i, self.settings.slots[i], b, self.settings, self.ffmpeg_path, encoder,
                                 lambda idx, text: self.events.put(("status", (idx, text))))
                recorders.append(r)
            if not recorders:
                raise RuntimeError("録画できるブラウザがありません")
            self.recorders = recorders
            for r in recorders:
                r.start()
            self.events.put(("started", None))

        self._run_background(work)

    def _stop_recording(self):
        recorders = list(self.recorders)
        for r in recorders:
            r.stop()

        def work():
            for r in recorders:
                r.join(90)
            saved = [r.output_path for r in recorders if r.output_path and not r.error]
            self.events.put(("saved", saved))

        self._run_background(work)

    def _split_url(self):
        url = self.split_url_var.get().strip()
        if url and "://" not in url:
            url = "https://" + url
        return url

    def _open_login(self):
        url = self._split_url()
        if not url:
            messagebox.showinfo("MultiCapture", "先に動画ページのURLを入力してください。")
            return
        self._collect_settings()
        exe = self.browsers_found.get(self.settings.browser)

        def work():
            login = LoginBrowser(exe, url)
            login.open()
            self.login = login

        self._run_background(work)

    def _start_split(self):
        url = self._split_url()
        size = parse_size(self.split_size_var.get())
        if not url:
            messagebox.showinfo("MultiCapture", "動画ページのURLを入力してください。")
            return
        if size is None:
            messagebox.showwarning("入力エラー", "録画サイズは「幅x高さ」で入力してください (例: 1920x1080)")
            return
        self._collect_settings()
        exe = self.browsers_found.get(self.settings.browser)
        self.seg_tree.delete(*self.seg_tree.get_children())
        self.split_progress["value"] = 0
        self.split_status_var.set("準備中…")

        def work():
            cookies = []
            if self._login_open():
                self.events.put(("split", ("status", "ログイン情報を引き継いでいます…")))
                try:
                    cookies = self.login.export_cookies()
                except Exception:
                    cookies = []
                self.login.close()
            self.login = None
            encoder = ffmpeg.detect_encoder(self.ffmpeg_path, self.settings.encoder)
            self.events.put(("encoder", encoder))
            job = SplitJob(
                url, self.settings.split_count, size[0], size[1], self.settings.split_pin_video,
                self.settings.output_dir, self.settings, exe, self.ffmpeg_path, encoder,
                self.settings.mute_speakers, cookies,
                lambda kind, payload: self.events.put(("split", (kind, payload))),
            )
            self.split_job = job
            job.start()

        self._run_background(work)

    def _cancel_split(self):
        if self.split_job:
            self.split_job.cancel()
            self.split_status_var.set("中止しています…")

    def _on_split_event(self, kind, payload):
        if kind == "status":
            self.split_status_var.set(payload)
        elif kind == "started":
            self.split_started_at = time.monotonic()
            self.keep_awake.acquire(keep_display=self.settings.keep_display_on)
        elif kind == "segments":
            self.seg_tree.delete(*self.seg_tree.get_children())
            for seg in payload:
                self.seg_tree.insert("", "end", values=(
                    seg["index"] + 1, f"{fmt_time(seg['start'])} 〜 {fmt_time(seg['end'])}", seg["status"], f"{seg['progress'] * 100:.0f}%",
                ))
            if payload:
                total = sum(s["end"] - s["start"] for s in payload)
                done = sum((s["end"] - s["start"]) * s["progress"] for s in payload)
                ratio = done / total if total else 0
                self.split_progress["value"] = ratio * 1000
                if self.split_started_at and 0.02 < ratio < 1:
                    elapsed = time.monotonic() - self.split_started_at
                    remaining = elapsed * (1 - ratio) / ratio
                    self.split_status_var.set(f"録画中 {ratio * 100:.0f}%  残り約 {fmt_time(remaining)}（全体の長さ {fmt_time(total)}）")
        elif kind == "done":
            self.split_progress["value"] = 1000
            self.split_status_var.set(f"保存しました: {payload}")
            self._set_info("保存しました")
            if messagebox.askyesno("MultiCapture", f"録画が完了しました。\n\n{payload}\n\n保存先フォルダを開きますか？"):
                os.startfile(os.path.dirname(payload))
        elif kind == "error":
            self.split_status_var.set(f"エラー: {payload}")
            if payload != "中止しました":
                messagebox.showerror("MultiCapture", payload)

    def _poll(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "status":
                    self._set_status(*payload)
                elif kind == "split":
                    self._on_split_event(*payload)
                elif kind == "encoder":
                    self.encoder = payload
                    self._set_info()
                elif kind == "error":
                    messagebox.showerror("MultiCapture", payload)
                elif kind == "idle":
                    self.busy = False
                elif kind == "saved":
                    if payload:
                        self._set_info(f"{len(payload)} 件保存しました")
                elif kind == "started":
                    self.keep_awake.acquire(keep_display=self.settings.keep_display_on)
                    if self.auto_duration:
                        self.stop_at = time.monotonic() + self.auto_duration
        except queue.Empty:
            pass
        if not self._recording() and not self.busy:
            self.keep_awake.release()
        if self.stop_at and time.monotonic() >= self.stop_at:
            self.stop_at = None
            self.exit_when_idle = True
            self._stop_recording()
        if self.exit_when_idle and not self.busy and not self._recording():
            self._on_close()
            return
        for i, b in list(self.browsers.items()):
            if b and not b.alive() and not any(r.index == i and r.running for r in self.recorders):
                self.browsers.pop(i, None)
                self._set_status(i, "ブラウザが閉じられました")
        self._update_buttons()
        self.root.after(200, self._poll)

    def _on_close(self):
        if self._recording():
            if not messagebox.askyesno("終了", "録画中です。録画を中止して終了しますか？"):
                return
            for r in self.recorders:
                r.stop()
            if self.split_job:
                self.split_job.cancel()
            for r in self.recorders:
                r.join(90)
            if self.split_job:
                self.split_job.join(120)
        self._collect_settings()
        for b in list(self.browsers.values()):
            if b:
                try:
                    b.close(timeout=3)
                except Exception:
                    pass
        if self._login_open():
            try:
                self.login.close()
            except Exception:
                pass
        self.keep_awake.release()
        restore_speakers_if_needed()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main(argv=None):
    if sys.getwindowsversion().build < 22000:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("MultiCapture", "このアプリはWindows 11以降が必要です。\n(ブラウザごとの音声取得機能がWindows 10にはありません)")
        root.destroy()
        return 1
    args = sys.argv[1:] if argv is None else argv
    duration = None
    if "--duration" in args:
        try:
            duration = max(1, int(float(args[args.index("--duration") + 1])))
        except (IndexError, ValueError):
            duration = None
    app = App()
    app.schedule("--start" in args, duration)
    app.run()
    return 0
