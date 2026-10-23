"""Tkinter desktop UI for WheresMyGPU.

Everything the CLI can do, with input areas for the thresholds and for adding
servers, plus a live view of the cluster and an email notification setup.

Tkinter ships with CPython, so the GUI adds no third-party dependency.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .analyzer import evaluate, rule_description, unsatisfiable_reason
from .config import (
    AlertRule,
    AppSettings,
    Config,
    ConfigError,
    ServerConfig,
    SshSettings,
    load_config,
)
from .config_writer import (
    dump_config,
    find_channel,
    has_channel,
    remove_channel,
    upsert_channel,
)
from .monitor import Monitor, setup_logging
from .notify import AlertEvent, build_channels, notify_all

log = logging.getLogger("wheresmygpu.gui")

APP_TITLE = "WheresMyGPU — GPU 空闲监控"
UI_FONT = ("Microsoft YaHei UI", 10)
MONO_FONT = ("Consolas", 9)

# provider -> (smtp_host, smtp_port, use_ssl)
EMAIL_PRESETS: dict[str, tuple[str | None, int | None, bool | None]] = {
    "QQ 邮箱 (smtp.qq.com)": ("smtp.qq.com", 465, True),
    "腾讯企业邮 (smtp.exmail.qq.com)": ("smtp.exmail.qq.com", 465, True),
    "163 邮箱 (smtp.163.com)": ("smtp.163.com", 465, True),
    "126 邮箱 (smtp.126.com)": ("smtp.126.com", 465, True),
    "Gmail (smtp.gmail.com)": ("smtp.gmail.com", 465, True),
    "Outlook (smtp.office365.com)": ("smtp.office365.com", 587, False),
    "阿里云邮 (smtp.mxhichina.com)": ("smtp.mxhichina.com", 465, True),
    "自定义": (None, None, None),
}

# type -> (label, tooltip)
EXTRA_CHANNELS: list[tuple[str, str]] = [
    ("logfile", "把每条提醒追加写入 logs/alerts.log（本地留存，不算打扰）"),
    ("console", "同时打印到命令行窗口（GUI 下作用不大）"),
    ("windows_toast", "Windows 桌面通知气泡"),
    ("sound", "响一声提示音"),
]

STATUS_FREE = "空闲 ✅"
STATUS_BUSY = "忙"
STATUS_UNREACHABLE = "连不上"


# --------------------------------------------------------------------------- #
# Logging bridge
# --------------------------------------------------------------------------- #

class QueueLogHandler(logging.Handler):
    """Forward log records to the GUI thread through a queue."""

    def __init__(self, sink: queue.Queue) -> None:
        super().__init__()
        self.sink = sink
        self.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S")
        )

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - ui
        try:
            self.sink.put(("log", self.format(record), record.levelno))
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Small widget helpers
# --------------------------------------------------------------------------- #

def _grid_label(parent, text: str, row: int, column: int = 0, **kw) -> ttk.Label:
    label = ttk.Label(parent, text=text, **kw)
    label.grid(row=row, column=column, sticky="w", padx=(0, 8), pady=4)
    return label


def _hint(parent, text: str, row: int, column: int = 1, span: int = 3) -> ttk.Label:
    label = ttk.Label(parent, text=text, foreground="#666666", font=("Microsoft YaHei UI", 8))
    label.grid(row=row, column=column, columnspan=span, sticky="w", pady=(0, 6))
    return label


# --------------------------------------------------------------------------- #
# Server add / edit dialog
# --------------------------------------------------------------------------- #

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_ANGLE_RE = re.compile(r"<([^<>]+)>\s*$")


def _looks_like_email(value: str) -> bool:
    """Accept a bare address and also the `Display Name <address>` form.

    SMTP servers reject anything that is not a real address here, which is why
    a bare application name like "WheresMyGPU" comes back as a cryptic 502.
    """
    text = (value or "").strip()
    match = _ANGLE_RE.search(text)
    if match:
        text = match.group(1).strip()
    return bool(_EMAIL_RE.match(text))


class ServerDialog(tk.Toplevel):
    """Modal editor for a single :class:`ServerConfig`."""

    AUTH_PASSWORD = "password"
    AUTH_PASSWORD_FILE = "password_file"
    AUTH_KEY = "key_file"

    def __init__(self, parent: "WheresMyGPUApp", server: ServerConfig | None = None) -> None:
        super().__init__(parent)
        self.result: ServerConfig | None = None
        self.title("编辑服务器" if server else "添加服务器")
        self.transient(parent)
        self.resizable(False, False)

        self.var_name = tk.StringVar(value=server.name if server else "")
        self.var_label = tk.StringVar(value=server.label if server else "")
        self.var_host = tk.StringVar(value=server.host if server else "")
        self.var_port = tk.StringVar(value=str(server.port if server else 22))
        self.var_user = tk.StringVar(value=server.user if server else "ubuntu")
        self.var_enabled = tk.BooleanVar(value=server.enabled if server else True)

        if server and server.key_file:
            auth = self.AUTH_KEY
        elif server and server.password_file:
            auth = self.AUTH_PASSWORD_FILE
        else:
            auth = self.AUTH_PASSWORD
        self.var_auth = tk.StringVar(value=auth)

        self.var_password = tk.StringVar(value=(server.password or "") if server else "")
        self.var_password_file = tk.StringVar(
            value=(server.password_file or "") if server else ""
        )
        self.var_key_file = tk.StringVar(value=(server.key_file or "") if server else "")
        self.var_key_passphrase = tk.StringVar(
            value=(server.key_passphrase or "") if server else ""
        )
        self.var_show_password = tk.BooleanVar(value=False)

        self.var_override = tk.BooleanVar(value=bool(server.alert_overrides) if server else False)
        self.var_ov_min_free = tk.StringVar(
            value=str(server.alert_overrides.get("min_free_memory_gb", ""))
            if server and server.alert_overrides
            else ""
        )
        self.var_ov_max_util = tk.StringVar(
            value=str(server.alert_overrides.get("max_gpu_utilization", ""))
            if server and server.alert_overrides
            else ""
        )
        self.var_transport = tk.StringVar(
            value=(server.transport or "继承全局") if server else "继承全局"
        )

        body = ttk.Frame(self, padding=16)
        body.pack(fill="both", expand=True)

        ttk.Label(body, text="连接信息", font=("Microsoft YaHei UI", 10, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 8)
        )

        _grid_label(body, "名称 (唯一标识) *", 1)
        ttk.Entry(body, textvariable=self.var_name, width=42).grid(
            row=1, column=1, columnspan=2, sticky="we", pady=4
        )
        _hint(body, "只用于日志和状态文件，建议用英文或数字，例如 3090-server-3", 2)

        _grid_label(body, "显示名", 3)
        ttk.Entry(body, textvariable=self.var_label, width=42).grid(
            row=3, column=1, columnspan=2, sticky="we", pady=4
        )
        _hint(body, "界面上展示的名字，可以写中文，例如 3090 服务器 3", 4)

        _grid_label(body, "主机 / IP *", 5)
        ttk.Entry(body, textvariable=self.var_host, width=30).grid(
            row=5, column=1, sticky="we", pady=4
        )
        ttk.Label(body, text="端口").grid(row=5, column=2, sticky="e", padx=(12, 6))
        ttk.Entry(body, textvariable=self.var_port, width=8).grid(
            row=5, column=3, sticky="w", pady=4
        )

        _grid_label(body, "登录用户 *", 6)
        ttk.Entry(body, textvariable=self.var_user, width=30).grid(
            row=6, column=1, sticky="we", pady=4
        )
        ttk.Checkbutton(body, text="启用监控", variable=self.var_enabled).grid(
            row=6, column=2, columnspan=2, sticky="w", padx=(12, 0)
        )

        ttk.Separator(body, orient="horizontal").grid(
            row=7, column=0, columnspan=4, sticky="we", pady=10
        )
        ttk.Label(body, text="登录方式", font=("Microsoft YaHei UI", 10, "bold")).grid(
            row=8, column=0, columnspan=4, sticky="w", pady=(0, 6)
        )

        auth_row = ttk.Frame(body)
        auth_row.grid(row=9, column=0, columnspan=4, sticky="w")
        for value, text in (
            (self.AUTH_PASSWORD, "密码"),
            (self.AUTH_PASSWORD_FILE, "密码文件"),
            (self.AUTH_KEY, "密钥文件"),
        ):
            ttk.Radiobutton(
                auth_row, text=text, value=value, variable=self.var_auth,
                command=self._sync_auth_state,
            ).pack(side="left", padx=(0, 18))

        _grid_label(body, "密码", 10)
        self.entry_password = ttk.Entry(
            body, textvariable=self.var_password, width=42, show="•"
        )
        self.entry_password.grid(row=10, column=1, sticky="we", pady=4)
        self.chk_show = ttk.Checkbutton(
            body, text="显示", variable=self.var_show_password, command=self._toggle_password
        )
        self.chk_show.grid(row=10, column=2, columnspan=2, sticky="w", padx=(12, 0))
        _hint(body, "建议填邮箱/服务器密码本身；这个值会明文保存在 config.yaml 里", 11)

        _grid_label(body, "密码文件", 12)
        self.entry_pwfile = ttk.Entry(body, textvariable=self.var_password_file, width=34)
        self.entry_pwfile.grid(row=12, column=1, sticky="we", pady=4)
        self.btn_pwfile = ttk.Button(body, text="浏览…", command=self._browse_password_file)
        self.btn_pwfile.grid(row=12, column=2, sticky="w", padx=(12, 0))

        _grid_label(body, "密钥文件", 13)
        self.entry_key = ttk.Entry(body, textvariable=self.var_key_file, width=34)
        self.entry_key.grid(row=13, column=1, sticky="we", pady=4)
        self.btn_key = ttk.Button(body, text="浏览…", command=self._browse_key_file)
        self.btn_key.grid(row=13, column=2, sticky="w", padx=(12, 0))

        _grid_label(body, "密钥口令", 14)
        self.entry_passphrase = ttk.Entry(
            body, textvariable=self.var_key_passphrase, width=42, show="•"
        )
        self.entry_passphrase.grid(row=14, column=1, columnspan=2, sticky="we", pady=4)

        _grid_label(body, "SSH 后端", 15)
        ttk.Combobox(
            body, textvariable=self.var_transport, width=18, state="readonly",
            values=["继承全局", "auto", "paramiko", "system_ssh"],
        ).grid(row=15, column=1, sticky="w", pady=4)
        _hint(body, "一般保持「继承全局」即可；密钥登录失败时程序会自动回退系统 ssh", 16)

        ttk.Separator(body, orient="horizontal").grid(
            row=17, column=0, columnspan=4, sticky="we", pady=10
        )
        ttk.Checkbutton(
            body, text="这台服务器使用独立的阈值（覆盖全局设置）",
            variable=self.var_override, command=self._sync_override_state,
        ).grid(row=18, column=0, columnspan=4, sticky="w")

        ov = ttk.Frame(body)
        ov.grid(row=19, column=0, columnspan=4, sticky="w", pady=(6, 0))
        self.lbl_ov_min = ttk.Label(ov, text="剩余显存下限 (GB)")
        self.lbl_ov_min.pack(side="left")
        self.entry_ov_min = ttk.Entry(ov, textvariable=self.var_ov_min_free, width=10)
        self.entry_ov_min.pack(side="left", padx=(8, 20))
        self.lbl_ov_util = ttk.Label(ov, text="GPU 利用率上限 (%)")
        self.lbl_ov_util.pack(side="left")
        self.entry_ov_util = ttk.Entry(ov, textvariable=self.var_ov_max_util, width=10)
        self.entry_ov_util.pack(side="left", padx=(8, 0))

        buttons = ttk.Frame(body)
        buttons.grid(row=20, column=0, columnspan=4, sticky="e", pady=(18, 0))
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="right", padx=(8, 0))
        ttk.Button(buttons, text="确定", command=self._on_ok).pack(side="right")

        self._sync_auth_state()
        self._sync_override_state()
        self.bind("<Return>", lambda _e: self._on_ok())
        self.bind("<Escape>", lambda _e: self.destroy())
        self._center(parent)
        self.grab_set()
        self.entry_host_focus()

    # -- helpers ---------------------------------------------------------- #

    def entry_host_focus(self) -> None:
        for child in self.winfo_children():
            pass
        self.focus_set()

    def _center(self, parent) -> None:
        self.update_idletasks()
        px, py = parent.winfo_rootx(), parent.winfo_rooty()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        w, h = self.winfo_width(), self.winfo_height()
        self.geometry(f"+{px + max(0, (pw - w) // 2)}+{py + max(0, (ph - h) // 3)}")

    def _toggle_password(self) -> None:
        self.entry_password.configure(show="" if self.var_show_password.get() else "•")

    def _sync_auth_state(self) -> None:
        mode = self.var_auth.get()
        self.entry_password.state(["!disabled"] if mode == self.AUTH_PASSWORD else ["disabled"])
        self.entry_pwfile.state(
            ["!disabled"] if mode == self.AUTH_PASSWORD_FILE else ["disabled"]
        )
        self.btn_pwfile.state(
            ["!disabled"] if mode == self.AUTH_PASSWORD_FILE else ["disabled"]
        )
        self.entry_key.state(["!disabled"] if mode == self.AUTH_KEY else ["disabled"])
        self.btn_key.state(["!disabled"] if mode == self.AUTH_KEY else ["disabled"])
        self.entry_passphrase.state(["!disabled"] if mode == self.AUTH_KEY else ["disabled"])

    def _sync_override_state(self) -> None:
        state = ["!disabled"] if self.var_override.get() else ["disabled"]
        self.entry_ov_min.state(state)
        self.entry_ov_util.state(state)

    def _browse_password_file(self) -> None:
        path = filedialog.askopenfilename(
            title="选择保存密码的文件", parent=self,
            filetypes=[("所有文件", "*.*"), ("文本文件", "*.txt")],
        )
        if path:
            self.var_password_file.set(path)

    def _browse_key_file(self) -> None:
        path = filedialog.askopenfilename(
            title="选择私钥文件", parent=self, initialdir=str(Path.home() / ".ssh"),
            filetypes=[("所有文件", "*.*")],
        )
        if path:
            self.var_key_file.set(path)

    def _on_ok(self) -> None:
        name = self.var_name.get().strip()
        host = self.var_host.get().strip()
        user = self.var_user.get().strip()

        if not name:
            messagebox.showwarning("还差一点", "请填写「名称」。", parent=self)
            return
        if not host:
            messagebox.showwarning("还差一点", "请填写「主机 / IP」。", parent=self)
            return
        if not user:
            messagebox.showwarning("还差一点", "请填写「登录用户」。", parent=self)
            return

        try:
            port = int(self.var_port.get().strip() or "22")
        except ValueError:
            messagebox.showwarning("端口不对", "端口必须是数字，例如 22 或 2222。", parent=self)
            return
        if not 1 <= port <= 65535:
            messagebox.showwarning("端口不对", "端口需要在 1 ~ 65535 之间。", parent=self)
            return

        mode = self.var_auth.get()
        password = self.var_password.get() if mode == self.AUTH_PASSWORD else ""
        password_file = (
            self.var_password_file.get().strip() if mode == self.AUTH_PASSWORD_FILE else ""
        )
        key_file = self.var_key_file.get().strip() if mode == self.AUTH_KEY else ""
        passphrase = self.var_key_passphrase.get() if mode == self.AUTH_KEY else ""

        if mode == self.AUTH_PASSWORD and not password:
            if not messagebox.askyesno(
                "没填密码",
                "你没有填密码。\n\n如果服务器已经装好了你的公钥，可以继续；"
                "否则连不上。\n\n要继续吗？",
                parent=self,
            ):
                return
        if mode == self.AUTH_PASSWORD_FILE and not password_file:
            messagebox.showwarning("还差一点", "请选择「密码文件」。", parent=self)
            return
        if mode == self.AUTH_KEY and not key_file:
            messagebox.showwarning("还差一点", "请选择「密钥文件」。", parent=self)
            return

        overrides: dict = {}
        if self.var_override.get():
            raw_min = self.var_ov_min_free.get().strip()
            raw_util = self.var_ov_max_util.get().strip()
            if raw_min:
                try:
                    overrides["min_free_memory_gb"] = float(raw_min)
                except ValueError:
                    messagebox.showwarning(
                        "数值不对", "「剩余显存下限」必须是数字。", parent=self
                    )
                    return
            if raw_util:
                try:
                    overrides["max_gpu_utilization"] = float(raw_util)
                except ValueError:
                    messagebox.showwarning(
                        "数值不对", "「GPU 利用率上限」必须是数字。", parent=self
                    )
                    return
            if not overrides:
                messagebox.showwarning(
                    "什么都没填",
                    "你勾选了「独立阈值」，但两个数值都是空的。\n"
                    "请至少填一个，或取消勾选。",
                    parent=self,
                )
                return

        transport = self.var_transport.get()
        if transport == "继承全局":
            transport = None

        self.result = ServerConfig(
            name=name,
            label=self.var_label.get().strip() or name,
            host=host,
            port=port,
            user=user,
            enabled=bool(self.var_enabled.get()),
            password=password or None,
            password_file=password_file or None,
            key_file=key_file or None,
            key_passphrase=passphrase or None,
            transport=transport,
            alert_overrides=overrides,
        )
        self.destroy()


# --------------------------------------------------------------------------- #
# Main window
# --------------------------------------------------------------------------- #

class WheresMyGPUApp(tk.Tk):
    """The WheresMyGPU desktop application."""

    def __init__(self, config_path: Path) -> None:
        super().__init__()
        self.config_path = Path(config_path)
        self.queue: queue.Queue = queue.Queue()

        self.config: Config | None = None
        self.servers: list[ServerConfig] = []
        self.channels: list[dict] = []

        self.monitor: Monitor | None = None
        self.monitor_thread: threading.Thread | None = None
        self.log_handler: QueueLogHandler | None = None

        self._next_check_ts: float | None = None
        self._last_snapshots: list = []
        self._dirty = False

        self.title(APP_TITLE)
        self.geometry("1200x780")
        self.minsize(1000, 660)
        self.option_add("*Font", UI_FONT)
        self._setup_style()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_ui()
        self._load_config_into_ui()

        self.after(150, self._pump)

    # ------------------------------------------------------------------ #
    # Styling
    # ------------------------------------------------------------------ #

    def _setup_style(self) -> None:
        style = ttk.Style(self)
        for theme in ("vista", "winnative", "clam"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break
        style.configure("Treeview", rowheight=24, font=UI_FONT)
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 13, "bold"))
        style.configure("Sub.TLabel", foreground="#555555")
        style.configure("Good.TLabel", foreground="#1a7f37", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Bad.TLabel", foreground="#b42318", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Accent.TButton", font=("Microsoft YaHei UI", 10, "bold"))

    # ------------------------------------------------------------------ #
    # UI construction
    # ------------------------------------------------------------------ #

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=(12, 10, 12, 8))
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer)
        header.pack(fill="x", pady=(0, 8))
        ttk.Label(header, text="WheresMyGPU", style="Title.TLabel").pack(side="left")
        ttk.Label(
            header, text="  持续监控多台 GPU 服务器，空闲时发邮件提醒", style="Sub.TLabel"
        ).pack(side="left")

        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill="both", expand=True)

        self.tab_status = ttk.Frame(self.notebook, padding=10)
        self.tab_servers = ttk.Frame(self.notebook, padding=10)
        self.tab_thresholds = ttk.Frame(self.notebook, padding=10)
        self.tab_email = ttk.Frame(self.notebook, padding=10)
        self.tab_log = ttk.Frame(self.notebook, padding=10)

        self.notebook.add(self.tab_status, text="  监控状态  ")
        self.notebook.add(self.tab_servers, text="  服务器  ")
        self.notebook.add(self.tab_thresholds, text="  阈值设置  ")
        self.notebook.add(self.tab_email, text="  邮件提醒  ")
        self.notebook.add(self.tab_log, text="  运行日志  ")

        self._build_status_tab()
        self._build_servers_tab()
        self._build_thresholds_tab()
        self._build_email_tab()
        self._build_log_tab()
        self._build_footer()

    # -- tab 1: live status ---------------------------------------------- #

    def _build_status_tab(self) -> None:
        bar = ttk.Frame(self.tab_status)
        bar.pack(fill="x", pady=(0, 8))

        self.btn_start = ttk.Button(
            bar, text="▶  开始监控", style="Accent.TButton", command=self._start_monitor
        )
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(bar, text="■  停止", command=self._stop_monitor)
        self.btn_stop.pack(side="left", padx=(8, 0))
        self.btn_once = ttk.Button(bar, text="⟳  立即检查一次", command=self._run_once)
        self.btn_once.pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="查看详情", command=self._show_selected_detail).pack(
            side="left", padx=(8, 0)
        )

        self.lbl_run_state = ttk.Label(bar, text="已停止", style="Bad.TLabel")
        self.lbl_run_state.pack(side="right")
        self.lbl_next = ttk.Label(bar, text="", style="Sub.TLabel")
        self.lbl_next.pack(side="right", padx=(0, 14))

        summary = ttk.Frame(self.tab_status)
        summary.pack(fill="x", pady=(0, 6))
        self.lbl_summary = ttk.Label(summary, text="还没有数据，点「开始监控」或「立即检查一次」。")
        self.lbl_summary.pack(side="left")

        table_wrap = ttk.Frame(self.tab_status)
        table_wrap.pack(fill="both", expand=True)

        columns = ("server", "gpu", "name", "vram", "free", "util", "mem", "temp", "proc", "state")
        headings = {
            "server": ("服务器", 150),
            "gpu": ("GPU", 50),
            "name": ("型号", 175),
            "vram": ("显存 已用/总", 110),
            "free": ("剩余", 80),
            "util": ("利用率", 70),
            "mem": ("显存利用率", 90),
            "temp": ("温度", 60),
            "proc": ("进程", 55),
            "state": ("状态", 90),
        }
        self.tree = ttk.Treeview(
            table_wrap, columns=columns, show="headings", selectmode="browse"
        )
        for key in columns:
            text, width = headings[key]
            self.tree.heading(key, text=text)
            self.tree.column(
                key, width=width, anchor="center" if key not in ("server", "name") else "w"
            )
        self.tree.tag_configure("free", background="#e8f7ea")
        self.tree.tag_configure("busy", background="#ffffff")
        self.tree.tag_configure("down", background="#fdecea")
        self.tree.tag_configure("down-text", foreground="#b42318")

        vsb = ttk.Scrollbar(table_wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda _e: self._show_selected_detail())

        alerts_box = ttk.LabelFrame(self.tab_status, text=" 最近的提醒 ", padding=6)
        alerts_box.pack(fill="both", expand=False, pady=(8, 0))
        self.txt_alerts = tk.Text(
            alerts_box, height=7, wrap="word", font=UI_FONT, state="disabled",
            background="#fbfbfb", relief="flat",
        )
        alerts_scroll = ttk.Scrollbar(alerts_box, orient="vertical", command=self.txt_alerts.yview)
        self.txt_alerts.configure(yscrollcommand=alerts_scroll.set)
        self.txt_alerts.pack(side="left", fill="both", expand=True)
        alerts_scroll.pack(side="right", fill="y")
        self.txt_alerts.tag_configure("free", foreground="#1a7f37")
        self.txt_alerts.tag_configure("warn", foreground="#b42318")
        self.txt_alerts.tag_configure("info", foreground="#333333")

    # -- tab 2: servers --------------------------------------------------- #

    def _build_servers_tab(self) -> None:
        ttk.Label(
            self.tab_servers,
            text="在这里添加 / 修改要监控的 GPU 服务器。改动后记得点右下角「保存配置」。",
            style="Sub.TLabel",
        ).pack(anchor="w", pady=(0, 8))

        bar = ttk.Frame(self.tab_servers)
        bar.pack(fill="x", pady=(0, 8))
        ttk.Button(bar, text="＋ 添加服务器", style="Accent.TButton", command=self._add_server).pack(side="left")
        ttk.Button(bar, text="编辑", command=self._edit_server).pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="删除", command=self._delete_server).pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="测试连接", command=self._test_server).pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="↑ 上移", command=lambda: self._move_server(-1)).pack(side="left", padx=(24, 0))
        ttk.Button(bar, text="↓ 下移", command=lambda: self._move_server(1)).pack(side="left", padx=(8, 0))

        wrap = ttk.Frame(self.tab_servers)
        wrap.pack(fill="both", expand=True)
        columns = ("enabled", "name", "label", "target", "auth", "extra")
        headings = {
            "enabled": ("启用", 60),
            "name": ("名称", 160),
            "label": ("显示名", 160),
            "target": ("地址", 250),
            "auth": ("登录方式", 110),
            "extra": ("独立阈值", 110),
        }
        self.server_tree = ttk.Treeview(wrap, columns=columns, show="headings", selectmode="browse")
        for key in columns:
            text, width = headings[key]
            self.server_tree.heading(key, text=text)
            self.server_tree.column(key, width=width, anchor="w" if key != "enabled" else "center")
        self.server_tree.tag_configure("disabled", foreground="#999999")
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.server_tree.yview)
        self.server_tree.configure(yscrollcommand=vsb.set)
        self.server_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.server_tree.bind("<Double-1>", lambda _e: self._edit_server())

        self.lbl_server_hint = ttk.Label(self.tab_servers, text="", style="Sub.TLabel")
        self.lbl_server_hint.pack(anchor="w", pady=(6, 0))

    # -- tab 3: thresholds ------------------------------------------------ #

    def _build_thresholds_tab(self) -> None:
        canvas = tk.Canvas(self.tab_thresholds, highlightthickness=0)
        scroll = ttk.Scrollbar(self.tab_thresholds, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        # -- polling
        poll = ttk.LabelFrame(inner, text=" 轮询 ", padding=12)
        poll.pack(fill="x", pady=(0, 10))
        self.var_interval = tk.StringVar()
        _grid_label(poll, "每隔多久检查一次 (秒)", 0)
        ttk.Spinbox(poll, from_=10, to=86400, textvariable=self.var_interval, width=10).grid(
            row=0, column=1, sticky="w"
        )
        _hint(poll, "5 分钟 = 300 秒。最小 10 秒；改完需要重启监控才生效。", 1)

        # -- idle definition
        idle = ttk.LabelFrame(inner, text=" 什么算「空闲可用」——以下条件必须全部满足 ", padding=12)
        idle.pack(fill="x", pady=(0, 10))

        self.var_min_free_enabled = tk.BooleanVar(value=True)
        self.var_min_free_gb = tk.StringVar()
        self.var_min_free_pct_enabled = tk.BooleanVar(value=False)
        self.var_min_free_pct = tk.StringVar()
        self.var_max_util_enabled = tk.BooleanVar(value=True)
        self.var_max_util = tk.StringVar()
        self.var_max_mem_util_enabled = tk.BooleanVar(value=True)
        self.var_max_mem_util = tk.StringVar()
        self.var_max_temp_enabled = tk.BooleanVar(value=False)
        self.var_max_temp = tk.StringVar()
        self.var_no_proc = tk.BooleanVar(value=False)
        self.var_max_proc_mem_enabled = tk.BooleanVar(value=False)
        self.var_max_proc_mem = tk.StringVar()

        rows = [
            (self.var_min_free_enabled, "剩余显存 ≥", self.var_min_free_gb, "GB",
             "最常用的一项。注意 24GB 的卡（如 3090）空闲时也只有约 23.6G，填 24 永远不会触发。"),
            (self.var_min_free_pct_enabled, "剩余显存 ≥", self.var_min_free_pct, "%",
             "按比例判断，换显卡型号不用改配置，例如填 85。"),
            (self.var_max_util_enabled, "GPU 计算利用率 ≤", self.var_max_util, "%",
             "任务在两次 kernel 之间可能瞬时为 0%，所以配合下面的「连续满足次数」一起用。"),
            (self.var_max_mem_util_enabled, "显存带宽利用率 ≤", self.var_max_mem_util, "%",
             "显存带宽利用率，反映是不是在读写显存。"),
            (self.var_max_temp_enabled, "温度 ≤", self.var_max_temp, "℃",
             "一般不用开。"),
            (self.var_max_proc_mem_enabled, "单个进程占用显存 ≤", self.var_max_proc_mem, "GB",
             "允许卡上留着很轻的进程。"),
        ]
        for index, (flag, label, var, unit, tip) in enumerate(rows):
            base = index * 2
            ttk.Checkbutton(idle, variable=flag, command=self._refresh_rule_preview).grid(
                row=base, column=0, sticky="w", padx=(0, 4)
            )
            ttk.Label(idle, text=label).grid(row=base, column=1, sticky="w")
            entry = ttk.Entry(idle, textvariable=var, width=12)
            entry.grid(row=base, column=2, sticky="w", padx=(6, 6))
            ttk.Label(idle, text=unit).grid(row=base, column=3, sticky="w")
            var.trace_add("write", lambda *_a: self._refresh_rule_preview())
            flag.trace_add("write", lambda *_a: self._refresh_rule_preview())
            _hint(idle, tip, base + 1, column=1, span=3)

        ttk.Checkbutton(
            idle, text="要求显卡上完全没有进程", variable=self.var_no_proc,
            command=self._refresh_rule_preview,
        ).grid(row=len(rows) * 2, column=1, columnspan=3, sticky="w", pady=(8, 0))

        # -- debounce / cooldown
        steady = ttk.LabelFrame(inner, text=" 防误报 / 防刷屏 ", padding=12)
        steady.pack(fill="x", pady=(0, 10))
        self.var_sustained = tk.StringVar()
        self.var_cooldown = tk.StringVar()
        _grid_label(steady, "连续满足几次才提醒", 0)
        ttk.Spinbox(steady, from_=1, to=50, textvariable=self.var_sustained, width=10).grid(
            row=0, column=1, sticky="w"
        )
        _hint(steady, "默认 2。5 分钟一轮时约 10 分钟后确认空闲才提醒，能过滤掉瞬时抖动。", 1)
        _grid_label(steady, "同一张卡多久内不重复提醒 (分钟)", 2)
        ttk.Spinbox(steady, from_=0, to=10080, textvariable=self.var_cooldown, width=10).grid(
            row=2, column=1, sticky="w"
        )
        _hint(steady, "默认 60。冷却结束后若仍然空闲会再提醒一次。填 0 = 不限制。", 3)

        # -- what to notify about
        events = ttk.LabelFrame(inner, text=" 哪些情况要发邮件 ", padding=12)
        events.pack(fill="x", pady=(0, 10))
        self.var_notify_busy = tk.BooleanVar()
        self.var_notify_recovery = tk.BooleanVar()
        self.var_notify_unreachable = tk.BooleanVar()
        self.var_unreachable_after = tk.StringVar()
        ttk.Checkbutton(events, text="显卡空闲可用了（核心功能，建议必开）",
                        variable=tk.BooleanVar(value=True), state="disabled").grid(
            row=0, column=0, columnspan=3, sticky="w"
        )
        ttk.Checkbutton(events, text="空闲的卡又被别人占用了", variable=self.var_notify_busy).grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(4, 0)
        )
        ttk.Checkbutton(events, text="服务器从连不上恢复正常", variable=self.var_notify_recovery).grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(4, 0)
        )
        ttk.Checkbutton(events, text="服务器连不上了", variable=self.var_notify_unreachable).grid(
            row=3, column=0, columnspan=3, sticky="w", pady=(4, 0)
        )
        _grid_label(events, "连续失败几次算「连不上」", 4)
        ttk.Spinbox(events, from_=1, to=20, textvariable=self.var_unreachable_after, width=10).grid(
            row=4, column=1, sticky="w"
        )

        # -- live preview
        preview = ttk.LabelFrame(inner, text=" 当前规则预览 ", padding=12)
        preview.pack(fill="x")
        self.lbl_rule = ttk.Label(preview, text="", wraplength=900, justify="left")
        self.lbl_rule.pack(anchor="w")
        self.lbl_rule_warn = ttk.Label(
            preview, text="", wraplength=900, justify="left",
            foreground="#b42318", font=("Microsoft YaHei UI", 9),
        )
        self.lbl_rule_warn.pack(anchor="w", pady=(6, 0))

    # -- tab 4: email ----------------------------------------------------- #

    def _build_email_tab(self) -> None:
        ttk.Label(
            self.tab_email,
            text="提醒方式：邮件。只要有显卡空闲，程序就给你发一封邮件。",
            style="Sub.TLabel",
        ).pack(anchor="w", pady=(0, 8))

        self.var_email_enabled = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            self.tab_email, text="启用邮件提醒", variable=self.var_email_enabled,
            command=self._on_email_toggle,
        ).pack(anchor="w")

        box = ttk.LabelFrame(self.tab_email, text=" SMTP 发信设置 ", padding=12)
        box.pack(fill="x", pady=(8, 10))

        self.var_preset = tk.StringVar(value="自定义")
        _grid_label(box, "邮箱服务商", 0)
        combo = ttk.Combobox(
            box, textvariable=self.var_preset, width=34, state="readonly",
            values=list(EMAIL_PRESETS.keys()),
        )
        combo.grid(row=0, column=1, columnspan=2, sticky="w")
        combo.bind("<<ComboboxSelected>>", self._apply_email_preset)
        _hint(box, "选一个会自动填好服务器和端口；拿不准就选「自定义」手动填。", 1, column=1, span=2)

        self.var_smtp_host = tk.StringVar()
        self.var_smtp_port = tk.StringVar()
        self.var_smtp_ssl = tk.BooleanVar(value=True)
        self.var_smtp_starttls = tk.BooleanVar(value=True)
        self.var_smtp_user = tk.StringVar()
        self.var_smtp_password = tk.StringVar()
        self.var_smtp_from = tk.StringVar()
        self.var_smtp_to = tk.StringVar()
        self.var_show_smtp_pw = tk.BooleanVar(value=False)

        _grid_label(box, "SMTP 服务器 *", 2)
        ttk.Entry(box, textvariable=self.var_smtp_host, width=34).grid(
            row=2, column=1, sticky="w"
        )
        ttk.Label(box, text="端口").grid(row=2, column=2, sticky="e", padx=(14, 6))
        ttk.Entry(box, textvariable=self.var_smtp_port, width=8).grid(row=2, column=3, sticky="w")

        _grid_label(box, "加密方式", 3)
        ttk.Checkbutton(box, text="SSL (通常 465 端口)", variable=self.var_smtp_ssl).grid(
            row=3, column=1, sticky="w"
        )
        ttk.Checkbutton(box, text="STARTTLS (通常 587 端口)", variable=self.var_smtp_starttls).grid(
            row=3, column=2, columnspan=2, sticky="w"
        )

        _grid_label(box, "登录账号 *", 4)
        ttk.Entry(box, textvariable=self.var_smtp_user, width=34).grid(
            row=4, column=1, columnspan=3, sticky="w"
        )
        _hint(box, "通常是你的完整邮箱地址", 5, column=1, span=3)

        _grid_label(box, "密码 / 授权码 *", 6)
        self.entry_smtp_pw = ttk.Entry(
            box, textvariable=self.var_smtp_password, width=34, show="•"
        )
        self.entry_smtp_pw.grid(row=6, column=1, sticky="w")
        ttk.Checkbutton(
            box, text="显示", variable=self.var_show_smtp_pw, command=self._toggle_smtp_pw
        ).grid(row=6, column=2, columnspan=2, sticky="w", padx=(14, 0))
        _hint(
            box,
            "QQ/163 等邮箱这里要填「授权码」，不是网页登录密码。"
            "在邮箱设置 → 账户 → POP3/SMTP 服务里开启并获取。",
            7, column=1, span=3,
        )

        _grid_label(box, "发件人", 8)
        ttk.Entry(box, textvariable=self.var_smtp_from, width=34).grid(
            row=8, column=1, columnspan=3, sticky="w"
        )
        _hint(box, "邮箱地址；想让邮件显示成名字就写  名字 <邮箱>；留空则用登录账号", 9, column=1, span=3)

        _grid_label(box, "收件人 *", 10)
        ttk.Entry(box, textvariable=self.var_smtp_to, width=34).grid(
            row=10, column=1, columnspan=3, sticky="w"
        )
        _hint(box, "填完整邮箱地址；多个收件人用英文逗号分隔", 11, column=1, span=3)

        actions = ttk.Frame(self.tab_email)
        actions.pack(fill="x")
        ttk.Button(
            actions, text="✉  发送测试邮件", style="Accent.TButton", command=self._test_email
        ).pack(side="left")
        self.lbl_email_status = ttk.Label(actions, text="", style="Sub.TLabel")
        self.lbl_email_status.pack(side="left", padx=(12, 0))

        extra = ttk.LabelFrame(self.tab_email, text=" 其它提醒方式（可选，默认关闭） ", padding=12)
        extra.pack(fill="x", pady=(12, 0))
        self.channel_vars: dict[str, tk.BooleanVar] = {}
        for index, (kind, tip) in enumerate(EXTRA_CHANNELS):
            var = tk.BooleanVar(value=False)
            self.channel_vars[kind] = var
            ttk.Checkbutton(extra, text=tip, variable=var).grid(
                row=index, column=0, sticky="w", pady=2
            )

    # -- tab 5: log ------------------------------------------------------- #

    def _build_log_tab(self) -> None:
        bar = ttk.Frame(self.tab_log)
        bar.pack(fill="x", pady=(0, 8))
        ttk.Button(bar, text="清空", command=self._clear_log).pack(side="left")
        ttk.Button(bar, text="打开日志文件夹", command=self._open_log_dir).pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="打开告警文件", command=self._open_alerts_file).pack(side="left", padx=(8, 0))
        ttk.Label(bar, text="  程序运行日志（含每一轮的检查结果）", style="Sub.TLabel").pack(side="left")

        wrap = ttk.Frame(self.tab_log)
        wrap.pack(fill="both", expand=True)
        self.txt_log = tk.Text(
            wrap, wrap="none", font=MONO_FONT, state="disabled",
            background="#1e1e1e", foreground="#d4d4d4", insertbackground="#d4d4d4",
        )
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=vsb.set)
        self.txt_log.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.txt_log.tag_configure("WARNING", foreground="#e5c07b")
        self.txt_log.tag_configure("ERROR", foreground="#e06c75")
        self.txt_log.tag_configure("CRITICAL", foreground="#e06c75")
        self.txt_log.tag_configure("DEBUG", foreground="#7f848e")

    # -- footer ----------------------------------------------------------- #

    def _build_footer(self) -> None:
        footer = ttk.Frame(self, padding=(12, 0, 12, 10))
        footer.pack(fill="x")

        self.lbl_file = ttk.Label(footer, text="", style="Sub.TLabel")
        self.lbl_file.pack(side="left")

        ttk.Button(footer, text="保存配置", style="Accent.TButton", command=self._save_config).pack(
            side="right"
        )
        ttk.Button(footer, text="重新加载", command=self._load_config_into_ui).pack(
            side="right", padx=(0, 8)
        )
        ttk.Button(footer, text="另存为…", command=self._save_config_as).pack(
            side="right", padx=(0, 8)
        )
        ttk.Button(footer, text="打开配置文件夹", command=self._open_config_dir).pack(
            side="right", padx=(0, 8)
        )
        self.lbl_dirty = ttk.Label(footer, text="", foreground="#b42318")
        self.lbl_dirty.pack(side="right", padx=(0, 14))

    # ------------------------------------------------------------------ #
    # Config <-> widgets
    # ------------------------------------------------------------------ #

    def _load_config_into_ui(self) -> None:
        try:
            config = load_config(self.config_path)
        except ConfigError as exc:
            if not messagebox.askyesno(
                "配置读取失败",
                f"读不了配置文件：\n{self.config_path}\n\n{exc}\n\n"
                "要新建一份默认配置吗？",
            ):
                self.destroy()
                return
            config = self._default_config()
            try:
                dump_config(config, self.config_path)
            except OSError as write_exc:
                messagebox.showerror("保存失败", f"无法写入配置：{write_exc}")
                self.destroy()
                return
            config = load_config(self.config_path)

        self.config = config
        self.servers = [dataclasses.replace(s) for s in config.servers]
        self.channels = [dict(c) for c in config.channels]
        self._populate_widgets()
        self.lbl_file.configure(text=f"配置文件: {self.config_path}")
        self._set_dirty(False)
        self._refresh_rule_preview()

    def _default_config(self) -> Config:
        return Config(
            path=Path(self.config_path),
            app=AppSettings(),
            ssh=SshSettings(),
            alert=AlertRule(min_free_memory_gb=20.0),
            servers=[],
            channels=[
                {"type": "logfile", "path": "logs/alerts.log"},
                {
                    "type": "email",
                    "smtp_host": "",
                    "smtp_port": 465,
                    "use_ssl": True,
                    "username": "",
                    "password": "",
                    "to": "",
                },
            ],
        )

    def _populate_widgets(self) -> None:
        assert self.config is not None
        app, alert = self.config.app, self.config.alert

        self.var_interval.set(str(app.interval_seconds))

        self.var_min_free_gb.set(self._fmt(alert.min_free_memory_gb))
        self.var_min_free_enabled.set(alert.min_free_memory_gb > 0)
        self.var_min_free_pct.set(self._fmt(alert.min_free_memory_pct))
        self.var_min_free_pct_enabled.set(alert.min_free_memory_pct is not None)
        self.var_max_util.set(self._fmt(alert.max_gpu_utilization))
        self.var_max_util_enabled.set(alert.max_gpu_utilization < 100)
        self.var_max_mem_util.set(self._fmt(alert.max_memory_utilization))
        self.var_max_mem_util_enabled.set(alert.max_memory_utilization is not None)
        self.var_max_temp.set(self._fmt(alert.max_temperature_c))
        self.var_max_temp_enabled.set(alert.max_temperature_c is not None)
        self.var_no_proc.set(alert.require_no_processes)
        self.var_max_proc_mem.set(self._fmt(alert.max_process_memory_gb))
        self.var_max_proc_mem_enabled.set(alert.max_process_memory_gb is not None)
        self.var_sustained.set(str(alert.sustained_checks))
        self.var_cooldown.set(str(alert.cooldown_minutes))
        self.var_notify_busy.set(alert.notify_on_busy)
        self.var_notify_recovery.set(alert.notify_on_recovery)
        self.var_notify_unreachable.set(alert.notify_on_unreachable)
        self.var_unreachable_after.set(str(alert.unreachable_after_failures))

        spec = find_channel(self.channels, "email") or {}
        enabled = has_channel(self.channels, "email")
        self.var_email_enabled.set(enabled)
        self.var_smtp_host.set(str(spec.get("smtp_host") or ""))
        self.var_smtp_port.set(str(spec.get("smtp_port") or 465))
        self.var_smtp_ssl.set(bool(spec.get("use_ssl", True)))
        self.var_smtp_starttls.set(bool(spec.get("starttls", True)))
        self.var_smtp_user.set(str(spec.get("username") or ""))
        self.var_smtp_password.set(str(spec.get("password") or ""))
        self.var_smtp_from.set(str(spec.get("from") or ""))
        to_value = spec.get("to") or ""
        if isinstance(to_value, (list, tuple)):
            to_value = ", ".join(str(v) for v in to_value)
        self.var_smtp_to.set(str(to_value))
        self._guess_preset()

        for kind, var in self.channel_vars.items():
            var.set(has_channel(self.channels, kind))

        self._refresh_server_tree()
        self._on_email_toggle()

    @staticmethod
    def _fmt(value: float | None) -> str:
        if value is None:
            return ""
        if float(value) == int(float(value)):
            return str(int(float(value)))
        return str(value)

    def _guess_preset(self) -> None:
        host = self.var_smtp_host.get().strip().lower()
        for name, (phost, _port, _ssl) in EMAIL_PRESETS.items():
            if phost and phost.lower() == host:
                self.var_preset.set(name)
                return
        self.var_preset.set("自定义")

    # -- server tree ------------------------------------------------------ #

    def _refresh_server_tree(self) -> None:
        self.server_tree.delete(*self.server_tree.get_children())
        for server in self.servers:
            if server.key_file:
                auth = "密钥文件"
            elif server.password_file:
                auth = "密码文件"
            elif server.password:
                auth = "密码"
            else:
                auth = "（未设置）"
            overrides = server.alert_overrides or {}
            extra = "、".join(f"{k}={v}" for k, v in overrides.items()) or "—"
            self.server_tree.insert(
                "", "end", iid=server.name,
                values=(
                    "✓" if server.enabled else "",
                    server.name,
                    server.label,
                    f"{server.user}@{server.host}:{server.port}",
                    auth,
                    extra,
                ),
                tags=() if server.enabled else ("disabled",),
            )
        count = len(self.servers)
        self.lbl_server_hint.configure(
            text=f"共 {count} 台服务器。" + ("（双击一行可以编辑）" if count else "还没有服务器，点「＋ 添加服务器」开始。")
        )

    def _selected_server(self) -> ServerConfig | None:
        selection = self.server_tree.selection()
        if not selection:
            return None
        name = selection[0]
        for server in self.servers:
            if server.name == name:
                return server
        return None

    def _add_server(self) -> None:
        dialog = ServerDialog(self)
        self.wait_window(dialog)
        if dialog.result is None:
            return
        if any(s.name == dialog.result.name for s in self.servers):
            messagebox.showwarning("名称重复", f"已经有一台叫「{dialog.result.name}」的服务器了。")
            return
        self.servers.append(dialog.result)
        self._refresh_server_tree()
        self.server_tree.selection_set(dialog.result.name)
        self._set_dirty(True)

    def _edit_server(self) -> None:
        server = self._selected_server()
        if server is None:
            messagebox.showinfo("先选一台", "请先在列表里选中要编辑的服务器。")
            return
        index = self.servers.index(server)
        dialog = ServerDialog(self, server)
        self.wait_window(dialog)
        if dialog.result is None:
            return
        if any(s.name == dialog.result.name for s in self.servers if s is not server):
            messagebox.showwarning("名称重复", f"已经有一台叫「{dialog.result.name}」的服务器了。")
            return
        self.servers[index] = dialog.result
        self._refresh_server_tree()
        self.server_tree.selection_set(dialog.result.name)
        self._set_dirty(True)

    def _delete_server(self) -> None:
        server = self._selected_server()
        if server is None:
            messagebox.showinfo("先选一台", "请先在列表里选中要删除的服务器。")
            return
        if not messagebox.askyesno("确认删除", f"要删除服务器「{server.label}」吗？"):
            return
        self.servers = [s for s in self.servers if s is not server]
        self._refresh_server_tree()
        self._set_dirty(True)

    def _move_server(self, delta: int) -> None:
        server = self._selected_server()
        if server is None:
            return
        index = self.servers.index(server)
        target = index + delta
        if not 0 <= target < len(self.servers):
            return
        self.servers[index], self.servers[target] = self.servers[target], self.servers[index]
        self._refresh_server_tree()
        self.server_tree.selection_set(server.name)
        self._set_dirty(True)

    def _test_server(self) -> None:
        server = self._selected_server()
        if server is None:
            messagebox.showinfo("先选一台", "请先在列表里选中要测试的服务器。")
            return
        try:
            config = self._collect_config()
        except ValueError as exc:
            messagebox.showwarning("数值有问题", str(exc))
            return

        target = next((s for s in config.servers if s.name == server.name), None)
        if target is None:
            return

        self.lbl_server_hint.configure(text=f"正在连接 {target.target} …")
        self.update_idletasks()

        result: dict = {}

        def work() -> None:
            from .nvidia import NvidiaSmiCollector
            from .ssh_runner import SshError, SshRunner

            try:
                runner = SshRunner(target, config)
                collector = NvidiaSmiCollector(target, config, runner)
                snapshot = collector.collect()
                runner.close()
                result["snapshot"] = snapshot
            except Exception as exc:  # noqa: BLE001 - surfaced to the user
                result["error"] = str(exc)

        thread = threading.Thread(target=work, daemon=True)
        thread.start()

        def poll() -> None:
            if thread.is_alive():
                self.after(150, poll)
                return
            snapshot = result.get("snapshot")
            if snapshot is None:
                messagebox.showerror("连接失败", result.get("error", "未知错误"))
                self.lbl_server_hint.configure(text="连接失败。")
                return
            if not snapshot.ok:
                messagebox.showerror("连接失败", snapshot.error or "未知错误")
                self.lbl_server_hint.configure(text="连接失败。")
                return
            free = 0
            lines = []
            rule = config.alert_for(target)
            for gpu in snapshot.gpus:
                verdict = evaluate(gpu, rule, snapshot.server, snapshot.label)
                if verdict.is_free:
                    free += 1
                lines.append(
                    f"GPU {gpu.index}: 剩余 {gpu.mem_free_gb:.1f}G, "
                    f"利用率 {gpu.gpu_util:.0f}%, 进程 {len(gpu.processes)} "
                    f"→ {'空闲' if verdict.is_free else '忙'}"
                )
            messagebox.showinfo(
                "连接成功",
                f"{target.label}\n驱动 {snapshot.driver_version or '未知'}\n"
                f"共 {len(snapshot.gpus)} 张显卡，其中 {free} 张空闲。\n\n"
                + "\n".join(lines[:12])
                + ("\n…" if len(lines) > 12 else ""),
            )
            self.lbl_server_hint.configure(text=f"{target.label} 连接正常。")

        self.after(150, poll)

    # ------------------------------------------------------------------ #
    # Collect widgets -> Config
    # ------------------------------------------------------------------ #

    def _collect_config(self, validate_email: bool = True) -> Config:
        """Read every input area into a Config.

        When ``validate_email`` is False an enabled-but-incomplete email setup is
        tolerated, so the live rule preview keeps working while the user is still
        filling in their SMTP details.
        """
        assert self.config is not None

        def number(var: tk.StringVar, label: str, *, required: bool, minimum=None) -> float | None:
            raw = var.get().strip()
            if not raw:
                if required:
                    raise ValueError(f"「{label}」不能为空。")
                return None
            try:
                value = float(raw)
            except ValueError:
                raise ValueError(f"「{label}」必须是数字，现在是「{raw}」。") from None
            if minimum is not None and value < minimum:
                raise ValueError(f"「{label}」不能小于 {minimum}，现在是 {value}。")
            return value

        def integer(var: tk.StringVar, label: str, *, minimum: int) -> int:
            raw = var.get().strip()
            if not raw:
                raise ValueError(f"「{label}」不能为空。")
            try:
                value = int(float(raw))
            except ValueError:
                raise ValueError(f"「{label}」必须是整数，现在是「{raw}」。") from None
            if value < minimum:
                raise ValueError(f"「{label}」不能小于 {minimum}，现在是 {value}。")
            return value

        interval = integer(self.var_interval, "轮询间隔", minimum=10)

        min_free = 0.0
        if self.var_min_free_enabled.get():
            min_free = number(self.var_min_free_gb, "剩余显存下限", required=True, minimum=0.0) or 0.0

        pct = number(self.var_min_free_pct, "剩余显存下限(%)", required=False, minimum=0.0) \
            if self.var_min_free_pct_enabled.get() else None
        if pct is not None and not 0 < pct <= 100:
            raise ValueError("「剩余显存下限(%)」需要在 0 ~ 100 之间。")

        max_util = 100.0
        if self.var_max_util_enabled.get():
            max_util = number(self.var_max_util, "GPU 利用率上限", required=True, minimum=0.0) or 0.0

        max_mem = number(self.var_max_mem_util, "显存带宽利用率上限", required=False, minimum=0.0) \
            if self.var_max_mem_util_enabled.get() else None
        max_temp = number(self.var_max_temp, "温度上限", required=False, minimum=0.0) \
            if self.var_max_temp_enabled.get() else None
        max_proc = number(self.var_max_proc_mem, "单进程显存上限", required=False, minimum=0.0) \
            if self.var_max_proc_mem_enabled.get() else None

        alert = AlertRule(
            min_free_memory_gb=float(min_free),
            min_free_memory_pct=pct,
            max_gpu_utilization=float(max_util),
            max_memory_utilization=max_mem,
            max_temperature_c=max_temp,
            require_no_processes=bool(self.var_no_proc.get()),
            max_process_memory_gb=max_proc,
            sustained_checks=integer(self.var_sustained, "连续满足次数", minimum=1),
            cooldown_minutes=integer(self.var_cooldown, "冷却时间", minimum=0),
            notify_on_busy=bool(self.var_notify_busy.get()),
            notify_on_recovery=bool(self.var_notify_recovery.get()),
            notify_on_unreachable=bool(self.var_notify_unreachable.get()),
            unreachable_after_failures=integer(
                self.var_unreachable_after, "连续失败次数", minimum=1
            ),
        )

        channels: list[dict] = []
        for kind, var in self.channel_vars.items():
            if not var.get():
                continue
            if kind == "logfile":
                channels.append({"type": "logfile", "path": "logs/alerts.log"})
            else:
                channels.append({"type": kind})

        if self.var_email_enabled.get():
            try:
                channels.insert(0, self._collect_email_spec())
            except ValueError:
                if validate_email:
                    raise
                # Tolerated: keeps the rule preview alive while the user is
                # still typing in their SMTP details.
                channels.insert(0, {"type": "email", "enabled": True})

        app = dataclasses.replace(self.config.app, interval_seconds=interval)
        return Config(
            path=Path(self.config_path),
            app=app,
            ssh=self.config.ssh,
            alert=alert,
            servers=[dataclasses.replace(s) for s in self.servers],
            channels=channels,
        )

    def _collect_email_spec(self) -> dict:
        host = self.var_smtp_host.get().strip()
        user = self.var_smtp_user.get().strip()
        password = self.var_smtp_password.get()
        to_raw = self.var_smtp_to.get().strip()
        port_raw = self.var_smtp_port.get().strip() or "465"

        try:
            port = int(port_raw)
        except ValueError:
            raise ValueError("SMTP 端口必须是数字，例如 465 或 587。") from None
        if not 1 <= port <= 65535:
            raise ValueError("SMTP 端口需要在 1 ~ 65535 之间。")

        recipients = [part.strip() for part in to_raw.replace("；", ";").replace(",", ";").split(";") if part.strip()]

        if not host:
            raise ValueError("邮件提醒已启用，但「SMTP 服务器」是空的。")
        if not user:
            raise ValueError("邮件提醒已启用，但「登录账号」是空的。")
        if not _looks_like_email(user):
            raise ValueError(
                "「登录账号」要填完整的邮箱地址，现在是「" + user + "」。\n\n"
                "请填完整的邮箱地址"
            )
        if not password:
            raise ValueError("邮件提醒已启用，但「密码 / 授权码」是空的。")
        if not recipients:
            raise ValueError("邮件提醒已启用，但「收件人」是空的。")
        bad = [r for r in recipients if not _looks_like_email(r)]
        if bad:
            raise ValueError(
                "「收件人」必须是邮箱地址，这几个不是：" + "、".join(bad) + "\n\n"
                "多个收件人请用英文逗号分隔"
            )

        spec: dict = {
            "type": "email",
            "smtp_host": host,
            "smtp_port": port,
            "use_ssl": bool(self.var_smtp_ssl.get()),
            "starttls": bool(self.var_smtp_starttls.get()),
            "username": user,
            "password": password,
            "to": recipients if len(recipients) > 1 else recipients[0],
        }
        sender = self.var_smtp_from.get().strip()
        if sender:
            if not _looks_like_email(sender):
                raise ValueError(
                    "「发件人」必须是邮箱地址，现在是「" + sender + "」。\n\n"
                    "不能只填名字（QQ 服务器会直接拒收）。"
                    "想让邮件显示成别的名字，请写成这种格式：\n"
                    "    WheresMyGPU <" + user + ">\n\n"
                    "或者直接清空这一栏 —— 程序会自动用「登录账号」（"
                    + user + "）发信。"
                )
            spec["from"] = sender
        return spec

    # ------------------------------------------------------------------ #
    # Saving
    # ------------------------------------------------------------------ #

    def _save_config(self, path: Path | None = None) -> bool:
        target = Path(path) if path else Path(self.config_path)
        try:
            config = self._collect_config()
        except ValueError as exc:
            messagebox.showwarning("还有地方要改", str(exc))
            return False

        # Validate by round-tripping through the real loader, so the GUI can
        # never write a file the monitor/CLI cannot read back.
        probe = target.with_suffix(target.suffix + ".check")
        try:
            dump_config(config, probe)
            load_config(probe)
        except ConfigError as exc:
            messagebox.showerror("配置有问题", f"这份配置读不回来：\n{exc}")
            return False
        except OSError as exc:
            messagebox.showerror("保存失败", f"无法写入文件：\n{exc}")
            return False
        finally:
            try:
                probe.unlink()
            except OSError:
                pass

        try:
            dump_config(config, target)
        except OSError as exc:
            messagebox.showerror("保存失败", f"无法写入文件：\n{exc}")
            return False

        self.config_path = target
        self.config = config
        self.lbl_file.configure(text=f"配置文件: {target}")
        self._set_dirty(False)
        messagebox.showinfo("已保存", f"配置已保存到：\n{target}\n\n旧文件备份在同目录的 .bak 文件里。")
        return True

    def _save_config_as(self) -> None:
        path = filedialog.asksaveasfilename(
            title="另存配置", defaultextension=".yaml",
            initialfile=self.config_path.name,
            initialdir=str(self.config_path.parent),
            filetypes=[("YAML 配置", "*.yaml *.yml"), ("所有文件", "*.*")],
        )
        if path:
            self._save_config(Path(path))

    def _set_dirty(self, dirty: bool) -> None:
        self._dirty = dirty
        self.lbl_dirty.configure(text="● 有未保存的修改" if dirty else "")

    # ------------------------------------------------------------------ #
    # Rule preview
    # ------------------------------------------------------------------ #

    def _refresh_rule_preview(self) -> None:
        try:
            config = self._collect_config(validate_email=False)
        except ValueError as exc:
            self.lbl_rule.configure(text="规则还不完整")
            self.lbl_rule_warn.configure(text=f"⚠ {exc}")
            return

        self.lbl_rule.configure(
            text="当一张显卡同时满足以下条件时 → 判定为空闲并给你发邮件\n\n"
            + rule_description(config.alert)
        )

        warnings: list[str] = []
        if config.alert.min_free_memory_gb >= 24:
            warnings.append(
                f"⚠ 「剩余显存 ≥ {config.alert.min_free_memory_gb:g}G」对 24GB 的显卡"
                "（如 RTX 3090）等于要求整张卡完全空着，实测空闲时也只有约 23.6G，"
                "这条规则几乎永远不会触发。建议改成 20G 左右，或改用百分比。"
            )
        if config.alert.require_no_processes and config.alert.max_process_memory_gb is not None:
            warnings.append(
                "⚠ 同时勾选了「要求完全没有进程」和「单进程显存上限」，"
                "后者会被前者完全覆盖，建议只留一个。"
            )
        if config.alert.max_gpu_utilization >= 100:
            warnings.append("⚠ 「GPU 利用率 ≤ 100%」等于不判断利用率。")
        if not config.enabled_servers():
            warnings.append("⚠ 还没有启用任何服务器，监控不会有任何结果。")
        if not config.channels:
            warnings.append("⚠ 没有启用任何提醒方式，即使检测到空闲也不会通知你。")
        if self.var_email_enabled.get():
            try:
                self._collect_email_spec()
            except ValueError as exc:
                warnings.append(f"⚠ 邮件提醒还没配好：{exc}")

        self.lbl_rule_warn.configure(text="\n\n".join(warnings))
        self._set_dirty(True)

    def _on_email_toggle(self) -> None:
        state = ["!disabled"] if self.var_email_enabled.get() else ["disabled"]
        for child in self.tab_email.winfo_children():
            if isinstance(child, ttk.LabelFrame) and "SMTP" in str(child.cget("text")):
                self._set_state_recursive(child, state)

    def _set_state_recursive(self, widget, state) -> None:
        for child in widget.winfo_children():
            try:
                child.state(state)
            except tk.TclError:
                pass
            self._set_state_recursive(child, state)

    def _toggle_smtp_pw(self) -> None:
        self.entry_smtp_pw.configure(show="" if self.var_show_smtp_pw.get() else "•")

    def _apply_email_preset(self, _event=None) -> None:
        preset = EMAIL_PRESETS.get(self.var_preset.get())
        if not preset or preset[0] is None:
            return
        host, port, use_ssl = preset
        self.var_smtp_host.set(host or "")
        self.var_smtp_port.set(str(port or 465))
        self.var_smtp_ssl.set(bool(use_ssl))
        self.var_smtp_starttls.set(not bool(use_ssl))
        if not self.var_smtp_from.get() and self.var_smtp_user.get():
            self.var_smtp_from.set(self.var_smtp_user.get())
        self._set_dirty(True)

    # ------------------------------------------------------------------ #
    # Email test
    # ------------------------------------------------------------------ #

    def _test_email(self) -> None:
        try:
            spec = self._collect_email_spec()
        except ValueError as exc:
            messagebox.showwarning("邮件设置不完整", str(exc))
            return

        self.lbl_email_status.configure(text="正在发送…", foreground="#555555")
        self.update_idletasks()

        assert self.config is not None
        probe_config = dataclasses.replace(self.config, channels=[spec])
        recipients = spec.get("to")
        if isinstance(recipients, str):
            recipients = [recipients]

        def work() -> None:
            try:
                channels = build_channels(probe_config)
                if not channels:
                    self.queue.put(("email_result", False, "通道创建失败，请检查设置。"))
                    return
                event = AlertEvent(
                    kind="test",
                    title="【测试】WheresMyGPU 邮件提醒已就绪",
                    body=(
                        "这是一封来自 WheresMyGPU 的测试邮件。\n"
                        "收到它就说明邮件提醒配置正确，\n"
                        "之后只要有显卡空闲，你就会收到同样的邮件。"
                    ),
                    level="info",
                    lines=[
                        "这是一封来自 WheresMyGPU 的测试邮件。",
                        "收到它说明邮件提醒配置正确。",
                        "",
                        f"SMTP 服务器: {spec.get('smtp_host')}:{spec.get('smtp_port')}",
                        f"收件人: {', '.join(recipients or [])}",
                        f"发送时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
                    ],
                )
                sent = notify_all(channels, event)
                if sent:
                    self.queue.put(("email_result", True, f"已发送给 {', '.join(recipients or [])}"))
                else:
                    self.queue.put(("email_result", False, "发送失败，请检查账号密码和网络。"))
            except Exception as exc:  # noqa: BLE001 - surfaced to the user
                self.queue.put(("email_result", False, str(exc)))

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------------ #
    # Monitoring
    # ------------------------------------------------------------------ #

    def _build_monitor(self) -> Monitor | None:
        try:
            config = self._collect_config(validate_email=False)
        except ValueError as exc:
            messagebox.showwarning("还有地方要改", str(exc))
            return None
        if not config.enabled_servers():
            messagebox.showwarning("没有服务器", "请先在「服务器」标签页添加至少一台启用的服务器。")
            return None

        if self.var_email_enabled.get():
            try:
                self._collect_email_spec()
            except ValueError as exc:
                if not messagebox.askyesno(
                    "邮件还没配好",
                    f"{exc}\n\n监控可以照常开始并记录日志，但不会发邮件。\n要先开始吗？",
                ):
                    self.notebook.select(self.tab_email)
                    return None
                config = dataclasses.replace(
                    config, channels=remove_channel(config.channels, "email")
                )
        elif not config.channels:
            if not messagebox.askyesno(
                "没有提醒方式",
                "你还没有启用任何提醒方式。\n监控会照常运行并记录日志，但不会通知你。\n\n要继续吗？",
            ):
                return None

        # The GUI shows everything in-process, so keep the console quiet (also
        # makes the app safe to launch with pythonw.exe, which has no stdout).
        config = dataclasses.replace(config, app=dataclasses.replace(config.app, console=False))

        try:
            setup_logging(config)
        except OSError as exc:
            messagebox.showerror("日志启动失败", f"无法创建日志文件：{exc}")
            return None

        handler = QueueLogHandler(self.queue)
        handler.setLevel(logging.DEBUG)
        logging.getLogger("wheresmygpu").addHandler(handler)
        self.log_handler = handler

        try:
            monitor = Monitor(config)
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            messagebox.showerror("启动失败", str(exc))
            return None

        monitor.on_snapshots = lambda snaps: self.queue.put(("snapshots", snaps))
        monitor.on_event = lambda event: self.queue.put(("event", event))
        return monitor

    def _start_monitor(self) -> None:
        if self.monitor_thread and self.monitor_thread.is_alive():
            return
        monitor = self._build_monitor()
        if monitor is None:
            return

        self.monitor = monitor
        self._append_log("正在启动监控…", logging.INFO)

        def loop() -> None:
            try:
                monitor.run_forever()
            except Exception as exc:  # noqa: BLE001 - reported in the UI
                log.exception("monitor loop crashed")
                self.queue.put(("error", str(exc)))
            finally:
                self.queue.put(("stopped", None))

        self.monitor_thread = threading.Thread(target=loop, daemon=True, name="wheresmygpu-monitor")
        self.monitor_thread.start()

        self.lbl_run_state.configure(text=f"● 监控中（每 {monitor.interval}s）", style="Good.TLabel")
        self.btn_start.state(["disabled"])
        self.btn_stop.state(["!disabled"])
        self._next_check_ts = time.time()

    def _stop_monitor(self) -> None:
        if self.monitor is None:
            return
        self.lbl_run_state.configure(text="正在停止…", style="Sub.TLabel")
        self.monitor.request_stop()

    def _run_once(self) -> None:
        if self.monitor_thread and self.monitor_thread.is_alive():
            messagebox.showinfo("正在监控中", "监控已经在运行了，它会自动检查。")
            return
        monitor = self._build_monitor()
        if monitor is None:
            return
        self.monitor = monitor
        self.btn_once.state(["disabled"])
        self.lbl_run_state.configure(text="正在检查…", style="Sub.TLabel")

        def work() -> None:
            try:
                monitor.run_round()
            except Exception as exc:  # noqa: BLE001 - reported in the UI
                self.queue.put(("error", str(exc)))
            finally:
                monitor.close()
                self.queue.put(("once_done", None))

        threading.Thread(target=work, daemon=True, name="wheresmygpu-once").start()

    def _on_monitor_stopped(self) -> None:
        self.btn_start.state(["!disabled"])
        self.btn_stop.state(["disabled"])
        self.btn_once.state(["!disabled"])
        self.lbl_run_state.configure(text="已停止", style="Bad.TLabel")
        self.lbl_next.configure(text="")
        self._next_check_ts = None
        if self.log_handler is not None:
            try:
                logging.getLogger("wheresmygpu").removeHandler(self.log_handler)
            except Exception:
                pass
            self.log_handler = None
        self.monitor = None

    # ------------------------------------------------------------------ #
    # Queue pump
    # ------------------------------------------------------------------ #

    def _pump(self) -> None:
        try:
            while True:
                message = self.queue.get_nowait()
                kind = message[0]
                if kind == "log":
                    self._append_log(message[1], message[2])
                elif kind == "snapshots":
                    self._render_snapshots(message[1])
                    if self.monitor is not None:
                        self._next_check_ts = time.time() + self.monitor.interval
                elif kind == "event":
                    self._render_event(message[1])
                elif kind == "email_result":
                    ok, text = message[1], message[2]
                    self.lbl_email_status.configure(
                        text=("✓ " if ok else "✗ ") + text,
                        foreground="#1a7f37" if ok else "#b42318",
                    )
                    if ok:
                        messagebox.showinfo("测试邮件已发出", text)
                    else:
                        messagebox.showerror("测试邮件发送失败", text)
                elif kind == "error":
                    self._append_log(f"出错: {message[1]}", logging.ERROR)
                    messagebox.showerror("出错了", message[1])
                elif kind == "once_done":
                    self.btn_once.state(["!disabled"])
                    self.lbl_run_state.configure(text="已停止", style="Bad.TLabel")
                    self.monitor = None
                elif kind == "stopped":
                    self._on_monitor_stopped()
        except queue.Empty:
            pass

        if self._next_check_ts is not None:
            remaining = self._next_check_ts - time.time()
            if remaining > 0:
                self.lbl_next.configure(text=f"下次检查: {int(remaining)} 秒后")
            else:
                self.lbl_next.configure(text="正在检查…")

        self.after(150, self._pump)

    # ------------------------------------------------------------------ #
    # Rendering
    # ------------------------------------------------------------------ #

    def _render_snapshots(self, snapshots: list) -> None:
        self._last_snapshots = snapshots
        self.tree.delete(*self.tree.get_children())
        free = busy = down = 0

        for snapshot in snapshots:
            server = next((s for s in self.servers if s.name == snapshot.server), None)
            rule = self.config.alert_for(server) if (self.config and server) else self.config.alert
            if not snapshot.ok:
                down += 1
                self.tree.insert(
                    "", "end", iid=f"{snapshot.server}:down",
                    values=(snapshot.label, "—", "连不上", "—", "—", "—", "—", "—", "—",
                            STATUS_UNREACHABLE),
                    tags=("down", "down-text"),
                )
                continue
            for gpu in snapshot.gpus:
                verdict = evaluate(gpu, rule, snapshot.server, snapshot.label)
                if verdict.is_free:
                    free += 1
                    tag = "free"
                else:
                    busy += 1
                    tag = "busy"
                temp = f"{gpu.temperature_c:.0f}℃" if gpu.temperature_c is not None else "n/a"
                self.tree.insert(
                    "", "end", iid=f"{snapshot.server}:{gpu.index}",
                    values=(
                        snapshot.label,
                        gpu.index,
                        gpu.name,
                        f"{gpu.mem_used_gb:.1f}/{gpu.mem_total_gb:.0f}G",
                        f"{gpu.mem_free_gb:.1f}G",
                        f"{gpu.gpu_util:.0f}%",
                        f"{gpu.memory_util:.0f}%",
                        temp,
                        len(gpu.processes),
                        STATUS_FREE if verdict.is_free else STATUS_BUSY,
                    ),
                    tags=(tag,),
                )

        when = time.strftime("%H:%M:%S")
        self.lbl_summary.configure(
            text=f"最近一次检查 {when}　共 {free + busy} 张显卡："
                 f"空闲 {free} 张，忙 {busy} 张"
                 + (f"，{down} 台服务器连不上" if down else "")
        )

    def _render_event(self, event: AlertEvent) -> None:
        if event.kind == "test":
            return
        style = {
            "gpu_free": "free",
            "server_recovered": "free",
            "gpu_busy": "info",
        }.get(event.kind, "warn")
        stamp = time.strftime("%H:%M:%S", time.localtime(event.timestamp))
        self.txt_alerts.configure(state="normal")
        self.txt_alerts.insert("end", f"[{stamp}] {event.title}\n", style)
        if event.lines:
            self.txt_alerts.insert("end", "    " + " / ".join(event.lines[:3]) + "\n", "info")
        self.txt_alerts.see("end")
        self.txt_alerts.configure(state="disabled")

    def _append_log(self, text: str, level: int = logging.INFO) -> None:
        name = logging.getLevelName(level)
        self.txt_log.configure(state="normal")
        self.txt_log.insert("end", text + "\n", name if isinstance(name, str) else "INFO")
        lines = int(self.txt_log.index("end-1c").split(".")[0])
        if lines > 4000:
            self.txt_log.delete("1.0", f"{lines - 3000}.0")
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")

    def _clear_log(self) -> None:
        self.txt_log.configure(state="normal")
        self.txt_log.delete("1.0", "end")
        self.txt_log.configure(state="disabled")

    def _show_selected_detail(self) -> None:
        selection = self.tree.selection()
        if not selection:
            messagebox.showinfo("先选一张卡", "请先在表格里选中一行。")
            return
        iid = selection[0]
        if iid.endswith(":down"):
            return
        server_name, _, gpu_index = iid.rpartition(":")
        snapshot = next((s for s in self._last_snapshots if s.server == server_name), None)
        if snapshot is None:
            return
        gpu = next((g for g in snapshot.gpus if str(g.index) == gpu_index), None)
        if gpu is None:
            return
        server = next((s for s in self.servers if s.name == server_name), None)
        rule = self.config.alert_for(server) if (self.config and server) else self.config.alert
        verdict = evaluate(gpu, rule, snapshot.server, snapshot.label)

        good = [c for c in verdict.conditions if c.ok]
        bad = verdict.failing()
        lines = [
            f"{snapshot.label} · GPU {gpu.index}",
            f"{gpu.name}",
            "",
            "判定结果: " + ("空闲可用 ✅" if verdict.is_free else "忙"),
            "",
        ]
        if bad:
            lines.append("没满足的条件:")
            lines += [f"  ✗ {c.name}: {c.detail}" for c in bad]
            lines.append("")
        if good:
            lines.append("已满足的条件:")
            lines += [f"  ✓ {c.name}: {c.detail}" for c in good]
            lines.append("")
        lines.append("详细数据:")
        lines += ["  " + line for line in verdict.detail_lines()]

        warning = unsatisfiable_reason(gpu, rule)
        if warning:
            lines += ["", f"⚠ {warning}"]

        messagebox.showinfo("显卡详情", "\n".join(lines))

    # ------------------------------------------------------------------ #
    # Misc actions
    # ------------------------------------------------------------------ #

    def _open_config_dir(self) -> None:
        self._open_path(self.config_path.parent)

    def _open_log_dir(self) -> None:
        assert self.config is not None
        self._open_path(self.config.resolve_path(self.config.app.log_dir))

    def _open_alerts_file(self) -> None:
        assert self.config is not None
        path = self.config.resolve_path(self.config.app.log_dir) / "alerts.log"
        if path.is_file():
            self._open_path(path)
        else:
            messagebox.showinfo("还没有告警文件", f"{path}\n\n还没有触发过提醒。")

    def _open_path(self, path: Path) -> None:
        try:
            if path.is_dir():
                os.startfile(str(path))  # noqa: S606 - intentional shell open
            elif path.is_file():
                os.startfile(str(path))  # noqa: S606
            else:
                messagebox.showinfo("找不到", f"{path} 不存在。")
        except OSError as exc:
            messagebox.showerror("打不开", str(exc))

    def _on_close(self) -> None:
        if self.monitor_thread and self.monitor_thread.is_alive():
            if not messagebox.askyesno("还在监控中", "监控正在运行，确定要退出吗？"):
                return
            if self.monitor is not None:
                self.monitor.request_stop()
        if self._dirty:
            answer = messagebox.askyesnocancel("有未保存的修改", "要保存配置再退出吗？")
            if answer is None:
                return
            if answer and not self._save_config():
                return
        self.destroy()


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def run_gui(config_path: Path | str | None = None) -> int:
    """Launch the desktop UI. Returns a process exit code."""
    # pythonw.exe gives us no stdout/stderr; make print() harmless.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115

    path = Path(config_path) if config_path else Path.cwd() / "config.yaml"
    app = WheresMyGPUApp(path)
    app.mainloop()
    return 0