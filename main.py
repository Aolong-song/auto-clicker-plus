import ctypes
import ctypes.wintypes as wt
import json
import random
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox, ttk

from pynput import keyboard, mouse


APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))
CONFIG_FILE = APP_DIR / "config.json"
ICON_FILE = RESOURCE_DIR / "assets" / "app.ico"

BUTTON_TEXT_TO_KEY = {
    "左键": "left",
    "右键": "right",
    "中键": "middle",
}
BUTTON_KEY_TO_OBJ = {
    "left": mouse.Button.left,
    "right": mouse.Button.right,
    "middle": mouse.Button.middle,
}
BUTTON_KEY_TO_MESSAGE = {
    "left": (0x0201, 0x0202),  # WM_LBUTTONDOWN / WM_LBUTTONUP
    "right": (0x0204, 0x0205),  # WM_RBUTTONDOWN / WM_RBUTTONUP
    "middle": (0x0207, 0x0208),  # WM_MBUTTONDOWN / WM_MBUTTONUP
}
REGION_STRATEGY = {
    "随机点": "random",
    "中心点": "center",
}
MODIFIER_ORDER = ["<ctrl>", "<alt>", "<shift>", "<cmd>"]

user32 = ctypes.windll.user32

WM_MOUSEMOVE = 0x0200
MK_LBUTTON = 0x0001
MK_RBUTTON = 0x0002
MK_MBUTTON = 0x0010


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wt.LONG),
        ("top", wt.LONG),
        ("right", wt.LONG),
        ("bottom", wt.LONG),
    ]


@dataclass
class WindowItem:
    hwnd: int
    title: str


def normalize_hotkey(user_input: str) -> str:
    text = user_input.strip().lower().replace(" ", "")
    if not text:
        raise ValueError("快捷键不能为空")

    alias = {
        "ctrl": "<ctrl>",
        "control": "<ctrl>",
        "alt": "<alt>",
        "shift": "<shift>",
        "cmd": "<cmd>",
        "win": "<cmd>",
    }

    parts = [p for p in text.split("+") if p]
    normalized = []
    for part in parts:
        if part in alias:
            normalized.append(alias[part])
        elif part in MODIFIER_ORDER:
            normalized.append(part)
        elif part.startswith("<f") and part.endswith(">") and part[2:-1].isdigit():
            normalized.append(part)
        elif part.startswith("f") and part[1:].isdigit():
            normalized.append(f"<{part}>")
        elif len(part) == 1 and part.isalnum():
            normalized.append(part)
        else:
            raise ValueError(f"无法识别按键：{part}")

    if not normalized:
        raise ValueError("快捷键格式无效")
    return "+".join(normalized)


def pack_lparam(x: int, y: int) -> int:
    return ((y & 0xFFFF) << 16) | (x & 0xFFFF)


def get_client_rect(hwnd: int) -> tuple[int, int] | None:
    rect = RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return None
    return rect.right - rect.left, rect.bottom - rect.top


def screen_to_client(hwnd: int, x: int, y: int) -> tuple[int, int]:
    point = wt.POINT(x, y)
    user32.ScreenToClient(hwnd, ctypes.byref(point))
    return int(point.x), int(point.y)


def enum_visible_windows(exclude_hwnd: int | None = None) -> list[WindowItem]:
    windows: list[WindowItem] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def callback(hwnd, _lparam):
        if exclude_hwnd and hwnd == exclude_hwnd:
            return True
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        title = buffer.value.strip()
        if title:
            windows.append(WindowItem(hwnd=int(hwnd), title=title))
        return True

    user32.EnumWindows(callback, 0)
    return windows


def send_window_click(hwnd: int, client_x: int, client_y: int, button_key: str) -> None:
    down_msg, up_msg = BUTTON_KEY_TO_MESSAGE[button_key]
    wparam_map = {
        "left": MK_LBUTTON,
        "right": MK_RBUTTON,
        "middle": MK_MBUTTON,
    }
    lparam = pack_lparam(client_x, client_y)
    user32.PostMessageW(hwnd, WM_MOUSEMOVE, 0, lparam)
    user32.PostMessageW(hwnd, down_msg, wparam_map[button_key], lparam)
    user32.PostMessageW(hwnd, up_msg, 0, lparam)


class AutoClickerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("自动点击器")
        self.root.geometry("660x760")
        self.root.resizable(False, False)
        if ICON_FILE.exists():
            try:
                self.root.iconbitmap(default=str(ICON_FILE))
            except Exception:  # noqa: BLE001
                pass

        self.mouse_controller = mouse.Controller()
        self.running = False
        self.worker_thread = None
        self.stop_event = threading.Event()
        self.hotkey_listener = None

        self.recording_hotkey = False
        self.record_listener = None
        self.record_modifiers = set()
        self.record_main_key = None

        self.current_position = None
        self.selected_region = None
        self.window_items: list[WindowItem] = []
        self.background_target: WindowItem | None = None
        self.background_point: tuple[int, int] | None = None
        self.background_region: tuple[int, int, int, int] | None = None

        self._build_ui()
        self._load_config()
        self.refresh_window_list(silent=True)
        self.apply_hotkey(show_dialog=False)
        self._refresh_status("就绪")

    def _build_ui(self) -> None:
        container = ttk.Frame(self.root, padding=12)
        container.pack(fill=tk.BOTH, expand=True)

        hotkey_box = ttk.LabelFrame(container, text="快捷键设置")
        hotkey_box.pack(fill=tk.X, pady=6)

        self.hotkey_var = tk.StringVar(value="f6")
        row1 = ttk.Frame(hotkey_box)
        row1.pack(fill=tk.X, padx=10, pady=(8, 4))
        ttk.Label(row1, text="当前快捷键：").pack(side=tk.LEFT)
        self.hotkey_show_label = ttk.Label(row1, text=self._format_hotkey(self.hotkey_var.get()), foreground="#1f4e79")
        self.hotkey_show_label.pack(side=tk.LEFT)

        row2 = ttk.Frame(hotkey_box)
        row2.pack(fill=tk.X, padx=10, pady=(0, 10))
        self.record_hotkey_btn = ttk.Button(row2, text="录制快捷键", command=self.start_hotkey_record)
        self.record_hotkey_btn.pack(side=tk.LEFT)
        ttk.Label(row2, text="点击后按下按键或组合键，松开后自动保存").pack(side=tk.LEFT, padx=10)

        click_box = ttk.LabelFrame(container, text="点击参数")
        click_box.pack(fill=tk.X, pady=6)

        self.interval_ms_var = tk.StringVar(value="100")
        self.button_var = tk.StringVar(value="左键")

        interval_row = ttk.Frame(click_box)
        interval_row.pack(fill=tk.X, padx=10, pady=(8, 6))
        ttk.Label(interval_row, text="点击间隔（毫秒）").pack(side=tk.LEFT)
        ttk.Entry(interval_row, textvariable=self.interval_ms_var, width=12).pack(side=tk.RIGHT)

        btn_row = ttk.Frame(click_box)
        btn_row.pack(fill=tk.X, padx=10, pady=(0, 10))
        ttk.Label(btn_row, text="点击按键").pack(side=tk.LEFT)
        ttk.Combobox(
            btn_row,
            textvariable=self.button_var,
            values=list(BUTTON_TEXT_TO_KEY.keys()),
            state="readonly",
            width=10,
        ).pack(side=tk.RIGHT)

        target_box = ttk.LabelFrame(container, text="点击模式")
        target_box.pack(fill=tk.X, pady=6)

        self.mode_var = tk.StringVar(value="position")
        self.region_click_mode_var = tk.StringVar(value="随机点")
        self.background_mode_var = tk.StringVar(value="point")
        self.background_region_click_mode_var = tk.StringVar(value="随机点")

        ttk.Radiobutton(
            target_box,
            text="前台 - 鼠标当前位置（动态）",
            variable=self.mode_var,
            value="position",
            command=self._persist_only,
        ).pack(anchor=tk.W, padx=10, pady=(8, 4))

        pos_row = ttk.Frame(target_box)
        pos_row.pack(fill=tk.X, padx=24, pady=(0, 8))
        ttk.Label(pos_row, text="说明：每次点击时读取鼠标当前所在位置").pack(side=tk.LEFT)

        ttk.Radiobutton(
            target_box,
            text="前台 - 拖拽框选区域",
            variable=self.mode_var,
            value="region",
            command=self._persist_only,
        ).pack(anchor=tk.W, padx=10, pady=(0, 4))

        region_row = ttk.Frame(target_box)
        region_row.pack(fill=tk.X, padx=24, pady=(0, 6))
        ttk.Button(region_row, text="开始框选区域", command=self.pick_region).pack(side=tk.LEFT)
        self.region_label = ttk.Label(region_row, text="未设置")
        self.region_label.pack(side=tk.LEFT, padx=10)

        region_mode_row = ttk.Frame(target_box)
        region_mode_row.pack(fill=tk.X, padx=24, pady=(0, 10))
        ttk.Label(region_mode_row, text="区域点击策略").pack(side=tk.LEFT)
        ttk.Combobox(
            region_mode_row,
            textvariable=self.region_click_mode_var,
            values=list(REGION_STRATEGY.keys()),
            state="readonly",
            width=10,
        ).pack(side=tk.RIGHT)

        ttk.Radiobutton(
            target_box,
            text="后台 - 指定软件窗口点击",
            variable=self.mode_var,
            value="background",
            command=self._persist_only,
        ).pack(anchor=tk.W, padx=10, pady=(8, 4))

        bg_box = ttk.Frame(target_box)
        bg_box.pack(fill=tk.X, padx=24, pady=(0, 10))

        top_row = ttk.Frame(bg_box)
        top_row.pack(fill=tk.X)
        ttk.Button(top_row, text="刷新窗口列表", command=self.refresh_window_list).pack(side=tk.LEFT)
        ttk.Button(top_row, text="选择当前项", command=self.select_background_window).pack(side=tk.LEFT, padx=8)

        self.window_var = tk.StringVar(value="")
        self.window_combo = ttk.Combobox(bg_box, textvariable=self.window_var, state="readonly")
        self.window_combo.pack(fill=tk.X, pady=(8, 6))

        bg_mode_row = ttk.Frame(bg_box)
        bg_mode_row.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(bg_mode_row, text="后台点击对象").pack(side=tk.LEFT)
        ttk.Combobox(
            bg_mode_row,
            textvariable=self.background_mode_var,
            values=["位置", "区域"],
            state="readonly",
            width=10,
        ).pack(side=tk.RIGHT)

        point_row = ttk.Frame(bg_box)
        point_row.pack(fill=tk.X, pady=(0, 6))
        ttk.Button(point_row, text="选择窗口点击位置", command=self.pick_background_point).pack(side=tk.LEFT)
        self.bg_point_label = ttk.Label(point_row, text="未设置")
        self.bg_point_label.pack(side=tk.LEFT, padx=10)

        region_row = ttk.Frame(bg_box)
        region_row.pack(fill=tk.X, pady=(0, 6))
        ttk.Button(region_row, text="选择窗口点击区域", command=self.pick_background_region).pack(side=tk.LEFT)
        self.bg_region_label = ttk.Label(region_row, text="未设置")
        self.bg_region_label.pack(side=tk.LEFT, padx=10)

        bg_region_mode_row = ttk.Frame(bg_box)
        bg_region_mode_row.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(bg_region_mode_row, text="区域点击策略").pack(side=tk.LEFT)
        ttk.Combobox(
            bg_region_mode_row,
            textvariable=self.background_region_click_mode_var,
            values=list(REGION_STRATEGY.keys()),
            state="readonly",
            width=10,
        ).pack(side=tk.RIGHT)

        self.bg_window_label = ttk.Label(bg_box, text="未选择窗口")
        self.bg_window_label.pack(anchor=tk.W, pady=(8, 0))

        control_box = ttk.LabelFrame(container, text="控制")
        control_box.pack(fill=tk.X, pady=6)

        control_row = ttk.Frame(control_box)
        control_row.pack(fill=tk.X, padx=10, pady=10)
        self.toggle_btn = ttk.Button(control_row, text="开始", command=self.toggle_running)
        self.toggle_btn.pack(side=tk.LEFT)
        ttk.Button(control_row, text="停止", command=self.stop_clicking).pack(side=tk.LEFT, padx=8)

        self.status_var = tk.StringVar(value="状态：就绪")
        ttk.Label(container, textvariable=self.status_var, foreground="#1f4e79").pack(anchor=tk.W, pady=(8, 0))

        ttk.Label(
            container,
            text="提示：后台点击依赖窗口消息机制，部分游戏或特殊程序可能不支持。",
            foreground="#555555",
        ).pack(anchor=tk.W, pady=(4, 0))

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.button_var.trace_add("write", lambda *_: self._persist_only())
        self.interval_ms_var.trace_add("write", lambda *_: self._persist_only())
        self.region_click_mode_var.trace_add("write", lambda *_: self._persist_only())
        self.background_mode_var.trace_add("write", lambda *_: self._persist_only())
        self.background_region_click_mode_var.trace_add("write", lambda *_: self._persist_only())

    def _refresh_status(self, text: str) -> None:
        self.status_var.set(f"状态：{text}")

    def _persist_only(self) -> None:
        self._save_config(silent=True)

    def _sync_labels(self) -> None:
        self.region_label.config(text=str(self.selected_region) if self.selected_region else "未设置")
        self.bg_point_label.config(text=str(self.background_point) if self.background_point else "未设置")
        self.bg_region_label.config(text=str(self.background_region) if self.background_region else "未设置")
        if self.background_target:
            self.bg_window_label.config(text=f"当前窗口：{self.background_target.title}")
            self.window_var.set(self._window_display(self.background_target))
        else:
            self.bg_window_label.config(text="未选择窗口")
        self.hotkey_show_label.config(text=self._format_hotkey(self.hotkey_var.get()))

    def _window_display(self, item: WindowItem) -> str:
        return f"{item.title} [{item.hwnd}]"

    def _selected_window_from_combo(self) -> WindowItem | None:
        value = self.window_var.get().strip()
        for item in self.window_items:
            if self._window_display(item) == value:
                return item
        return None

    def refresh_window_list(self, silent: bool = False) -> None:
        self.window_items = enum_visible_windows(exclude_hwnd=self.root.winfo_id())
        values = [self._window_display(item) for item in self.window_items]
        self.window_combo["values"] = values
        if values and not self.window_var.get():
            self.window_var.set(values[0])
        elif self.background_target:
            self.window_var.set(self._window_display(self.background_target))
        if not silent:
            self._refresh_status(f"已刷新窗口列表，共 {len(values)} 个")

    def select_background_window(self) -> None:
        item = self._selected_window_from_combo()
        if not item:
            messagebox.showwarning("窗口选择", "请先从列表中选择一个窗口")
            return
        self.background_target = item
        self.bg_window_label.config(text=f"当前窗口：{item.title}")
        self._save_config(silent=True)
        self._refresh_status(f"已选择窗口：{item.title}")

    def _load_config(self) -> None:
        if not CONFIG_FILE.exists():
            self._sync_labels()
            return

        try:
            config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            self._sync_labels()
            return

        self.hotkey_var.set(str(config.get("hotkey", "f6")))
        self.interval_ms_var.set(str(config.get("interval_ms", "100")))
        self.button_var.set(str(config.get("button_text", "左键")))
        self.mode_var.set(str(config.get("mode", "position")))
        self.region_click_mode_var.set(str(config.get("region_click_mode_text", "随机点")))
        background_mode = str(config.get("background_mode", "位置"))
        if background_mode in {"point", "position", "定点"}:
            background_mode = "位置"
        elif background_mode in {"region", "area", "区域"}:
            background_mode = "区域"
        self.background_mode_var.set(background_mode)
        self.background_region_click_mode_var.set(str(config.get("background_region_click_mode_text", "随机点")))

        region = config.get("region")
        if isinstance(region, list) and len(region) == 4:
            self.selected_region = tuple(int(v) for v in region)

        bg_hwnd = config.get("bg_hwnd")
        bg_title = config.get("bg_title")
        bg_point = config.get("bg_point")
        if isinstance(bg_hwnd, int) and isinstance(bg_title, str):
            self.background_target = WindowItem(bg_hwnd, bg_title)
        if isinstance(bg_point, list) and len(bg_point) == 2:
            self.background_point = (int(bg_point[0]), int(bg_point[1]))
        bg_region = config.get("bg_region")
        if isinstance(bg_region, list) and len(bg_region) == 4:
            self.background_region = tuple(int(v) for v in bg_region)

        if "cps" in config and "interval_ms" not in config:
            try:
                cps = float(str(config["cps"]))
                if cps > 0:
                    self.interval_ms_var.set(str(round(1000.0 / cps, 3)))
            except Exception:  # noqa: BLE001
                pass
        if "button" in config and "button_text" not in config:
            old_button = str(config["button"])
            for text, key in BUTTON_TEXT_TO_KEY.items():
                if key == old_button:
                    self.button_var.set(text)
                    break
        if "region_click_mode" in config and "region_click_mode_text" not in config:
            old_mode = str(config["region_click_mode"])
            for text, mode_key in REGION_STRATEGY.items():
                if mode_key == old_mode:
                    self.region_click_mode_var.set(text)
                    break

        self._sync_labels()

    def _save_config(self, silent: bool = False) -> None:
        data = {
            "hotkey": self.hotkey_var.get().strip(),
            "interval_ms": self.interval_ms_var.get().strip(),
            "button_text": self.button_var.get().strip(),
            "mode": self.mode_var.get().strip(),
            "region_click_mode_text": self.region_click_mode_var.get().strip(),
            "region": list(self.selected_region) if self.selected_region else None,
            "bg_hwnd": self.background_target.hwnd if self.background_target else None,
            "bg_title": self.background_target.title if self.background_target else None,
            "bg_point": list(self.background_point) if self.background_point else None,
            "bg_region": list(self.background_region) if self.background_region else None,
            "background_mode": self.background_mode_var.get().strip(),
            "background_region_click_mode_text": self.background_region_click_mode_var.get().strip(),
        }
        try:
            CONFIG_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            if not silent:
                messagebox.showerror("保存配置失败", str(exc))

    def _validate_interval_ms(self) -> float:
        try:
            interval_ms = float(self.interval_ms_var.get().strip())
        except ValueError as exc:
            raise ValueError("点击间隔必须是数字") from exc

        if interval_ms <= 0:
            raise ValueError("点击间隔必须大于 0 毫秒")
        if interval_ms < 1:
            raise ValueError("点击间隔建议不小于 1 毫秒")
        if interval_ms > 60000:
            raise ValueError("点击间隔过大，请设置在 60000 毫秒以内")
        return interval_ms

    def _resolve_front_target(self) -> tuple[int, int]:
        mode = self.mode_var.get()
        if mode == "position":
            pos = self.mouse_controller.position
            return int(pos[0]), int(pos[1])

        if mode == "region":
            if self.selected_region is None:
                raise ValueError("请先框选点击区域")
            x1, y1, x2, y2 = self.selected_region
            left, right = min(x1, x2), max(x1, x2)
            top, bottom = min(y1, y2), max(y1, y2)
            strategy = REGION_STRATEGY.get(self.region_click_mode_var.get(), "random")
            if strategy == "center":
                return (left + right) // 2, (top + bottom) // 2
            return random.randint(left, right), random.randint(top, bottom)

        raise ValueError("当前模式不是前台模式")

    def _resolve_background_target(self) -> tuple[int, int]:
        if not self.background_target:
            raise ValueError("请先选择后台窗口")
        mode = self.background_mode_var.get()
        if mode in {"位置", "point", "position"}:
            if not self.background_point:
                raise ValueError("请先选择窗口内点击位置")
            return self.background_point
        if mode in {"区域", "region", "area"}:
            if not self.background_region:
                raise ValueError("请先选择窗口内点击区域")
            x1, y1, x2, y2 = self.background_region
            left, right = min(x1, x2), max(x1, x2)
            top, bottom = min(y1, y2), max(y1, y2)
            strategy = REGION_STRATEGY.get(self.background_region_click_mode_var.get(), "random")
            if strategy == "center":
                return (left + right) // 2, (top + bottom) // 2
            return random.randint(left, right), random.randint(top, bottom)
        raise ValueError("未知的后台点击对象")

    def _click_worker(self, interval_sec: float, button_key: str) -> None:
        next_tick = time.perf_counter()
        while not self.stop_event.is_set():
            try:
                mode = self.mode_var.get()
                if mode == "background":
                    if not self.background_target:
                        raise ValueError("请先选择后台窗口")
                    bg_mode = self.background_mode_var.get()
                    if bg_mode in {"位置", "point", "position"} and not self.background_point:
                        raise ValueError("请先选择窗口内点击位置")
                    if bg_mode in {"区域", "region", "area"} and not self.background_region:
                        raise ValueError("请先选择窗口内点击区域")
                    if not user32.IsWindow(self.background_target.hwnd):
                        raise ValueError("目标窗口已失效，请重新选择窗口")
                    client_x, client_y = self._resolve_background_target()
                    send_window_click(self.background_target.hwnd, client_x, client_y, button_key)
                else:
                    target = self._resolve_front_target()
                    self.mouse_controller.position = target
                    self.mouse_controller.click(BUTTON_KEY_TO_OBJ[button_key], 1)
            except Exception as exc:  # noqa: BLE001
                self.root.after(0, lambda: self._handle_runtime_error(str(exc)))
                return

            next_tick += interval_sec
            wait = max(0.0, next_tick - time.perf_counter())
            if self.stop_event.wait(wait):
                return

    def _handle_runtime_error(self, msg: str) -> None:
        self.stop_clicking()
        messagebox.showerror("运行错误", msg)

    def start_clicking(self) -> None:
        if self.running:
            return

        try:
            interval_ms = self._validate_interval_ms()
            button_text = self.button_var.get()
            if button_text not in BUTTON_TEXT_TO_KEY:
                raise ValueError("鼠标按键设置无效")
            button_key = BUTTON_TEXT_TO_KEY[button_text]

            if self.mode_var.get() == "background":
                if not self.background_target:
                    raise ValueError("请先选择后台窗口")
                bg_mode = self.background_mode_var.get()
                if bg_mode in {"位置", "point", "position"} and not self.background_point:
                    raise ValueError("请先选择窗口内点击位置")
                if bg_mode in {"区域", "region", "area"} and not self.background_region:
                    raise ValueError("请先选择窗口内点击区域")
            else:
                _ = self._resolve_front_target()
        except Exception as exc:  # noqa: BLE001
            messagebox.showwarning("参数错误", str(exc))
            return

        self.stop_event.clear()
        self.running = True
        self.worker_thread = threading.Thread(
            target=self._click_worker,
            args=(interval_ms / 1000.0, button_key),
            daemon=True,
        )
        self.worker_thread.start()
        self.toggle_btn.config(text="运行中（按快捷键可停止）")
        mode_text = "后台" if self.mode_var.get() == "background" else "前台"
        self._refresh_status(f"{mode_text}运行中：间隔 {interval_ms}ms，按键 {button_text}")

    def stop_clicking(self) -> None:
        if not self.running:
            return
        self.stop_event.set()
        self.running = False
        self.toggle_btn.config(text=f"开始（{self._format_hotkey(self.hotkey_var.get())}）")
        self._refresh_status("已停止")

    def toggle_running(self) -> None:
        if self.running:
            self.stop_clicking()
        else:
            self.start_clicking()

    def _update_hotkey_listener(self, normalized_hotkey: str) -> None:
        if self.hotkey_listener:
            self.hotkey_listener.stop()
            self.hotkey_listener = None

        self.hotkey_listener = keyboard.GlobalHotKeys(
            {
                normalized_hotkey: lambda: self.root.after(0, self.toggle_running),
            }
        )
        self.hotkey_listener.start()

    def apply_hotkey(self, show_dialog: bool = True) -> None:
        try:
            normalized = normalize_hotkey(self.hotkey_var.get())
            self.hotkey_var.set(normalized)
            if not self.recording_hotkey:
                self._update_hotkey_listener(normalized)
            self._save_config(silent=True)
            self._sync_labels()
        except Exception as exc:  # noqa: BLE001
            if show_dialog:
                messagebox.showerror("快捷键错误", str(exc))
            return

        if not self.running:
            self.toggle_btn.config(text=f"开始（{self._format_hotkey(normalized)}）")
        self._refresh_status(f"快捷键已更新：{self._format_hotkey(normalized)}")

    def _token_from_key(self, key) -> str | None:
        modifier_map = {
            keyboard.Key.ctrl: "<ctrl>",
            keyboard.Key.ctrl_l: "<ctrl>",
            keyboard.Key.ctrl_r: "<ctrl>",
            keyboard.Key.alt: "<alt>",
            keyboard.Key.alt_l: "<alt>",
            keyboard.Key.alt_r: "<alt>",
            keyboard.Key.shift: "<shift>",
            keyboard.Key.shift_l: "<shift>",
            keyboard.Key.shift_r: "<shift>",
            keyboard.Key.cmd: "<cmd>",
            keyboard.Key.cmd_l: "<cmd>",
            keyboard.Key.cmd_r: "<cmd>",
        }
        if key in modifier_map:
            return modifier_map[key]

        if isinstance(key, keyboard.KeyCode):
            if key.char and key.char.isalnum():
                return key.char.lower()
            vk = getattr(key, "vk", None)
            if isinstance(vk, int):
                if 65 <= vk <= 90:
                    return chr(vk).lower()
                if 48 <= vk <= 57:
                    return chr(vk)
            return None

        if isinstance(key, keyboard.Key):
            name = key.name or ""
            if name.startswith("f") and name[1:].isdigit():
                return f"<{name.lower()}>"
        return None

    def _finish_hotkey_record(self) -> None:
        try:
            if not self.record_main_key:
                raise ValueError("请至少按一个非修饰键（例如 F6、A）")
            ordered_mod = [mod for mod in MODIFIER_ORDER if mod in self.record_modifiers]
            tokens = ordered_mod.copy()
            if self.record_main_key not in tokens:
                tokens.append(self.record_main_key)
            new_hotkey = "+".join(tokens)
            self.hotkey_var.set(new_hotkey)
            self.recording_hotkey = False
            self._stop_record_listener()
            self.record_hotkey_btn.config(text="录制快捷键")
            self.apply_hotkey(show_dialog=True)
        except Exception as exc:  # noqa: BLE001
            self.recording_hotkey = False
            self._stop_record_listener()
            self.record_hotkey_btn.config(text="录制快捷键")
            self.apply_hotkey(show_dialog=False)
            messagebox.showerror("录制失败", str(exc))

    def _stop_record_listener(self) -> None:
        if self.record_listener:
            self.record_listener.stop()
            self.record_listener = None

    def start_hotkey_record(self) -> None:
        if self.recording_hotkey:
            return

        self.recording_hotkey = True
        self.record_modifiers = set()
        self.record_main_key = None

        if self.hotkey_listener:
            self.hotkey_listener.stop()
            self.hotkey_listener = None

        self.record_hotkey_btn.config(text="请按下快捷键...")
        self._refresh_status("录制中：请按下快捷键组合，松开主键后自动保存")

        def on_press(key):
            token = self._token_from_key(key)
            if not token:
                return
            if token in MODIFIER_ORDER:
                self.record_modifiers.add(token)
            else:
                self.record_main_key = token

        def on_release(key):
            token = self._token_from_key(key)
            if not token:
                return
            if token in MODIFIER_ORDER and not self.record_main_key and len(self.record_modifiers) == 1:
                self.record_main_key = token
                self.root.after(0, self._finish_hotkey_record)
                return False
            if token == self.record_main_key:
                self.root.after(0, self._finish_hotkey_record)
                return False
            return None

        self.record_listener = keyboard.Listener(on_press=on_press, on_release=on_release)
        self.record_listener.start()

    def _format_hotkey(self, hotkey_str: str) -> str:
        text = hotkey_str.replace("<ctrl>", "Ctrl")
        text = text.replace("<alt>", "Alt")
        text = text.replace("<shift>", "Shift")
        text = text.replace("<cmd>", "Win")
        for i in range(1, 25):
            text = text.replace(f"<f{i}>", f"F{i}")
        return text

    def capture_position(self) -> None:
        pos = self.mouse_controller.position
        self.current_position = (int(pos[0]), int(pos[1]))
        self._refresh_status("已捕获当前位置")

    def pick_region(self) -> None:
        self._refresh_status("请拖拽鼠标框选区域")

        overlay = tk.Toplevel(self.root)
        overlay.attributes("-fullscreen", True)
        overlay.attributes("-alpha", 0.25)
        overlay.attributes("-topmost", True)
        overlay.configure(bg="black")
        overlay.config(cursor="crosshair")

        canvas = tk.Canvas(overlay, bg="black", highlightthickness=0)
        canvas.pack(fill=tk.BOTH, expand=True)

        data = {"x1": 0, "y1": 0, "x2": 0, "y2": 0, "rect_id": None}

        def on_press(event):
            data["x1"], data["y1"] = event.x_root, event.y_root
            data["x2"], data["y2"] = event.x_root, event.y_root
            data["cx1"], data["cy1"] = event.x, event.y
            if data["rect_id"]:
                canvas.delete(data["rect_id"])
            data["rect_id"] = canvas.create_rectangle(
                event.x, event.y, event.x, event.y, outline="red", width=2
            )

        def on_move(event):
            data["x2"], data["y2"] = event.x_root, event.y_root
            if data["rect_id"]:
                canvas.coords(data["rect_id"], data["cx1"], data["cy1"], event.x, event.y)

        def on_release(event):
            x1 = min(data["x1"], event.x_root)
            y1 = min(data["y1"], event.y_root)
            x2 = max(data["x1"], event.x_root)
            y2 = max(data["y1"], event.y_root)
            if abs(x2 - x1) < 3 or abs(y2 - y1) < 3:
                overlay.destroy()
                self._refresh_status("框选取消：区域太小")
                return

            self.selected_region = (x1, y1, x2, y2)
            self.region_label.config(text=str(self.selected_region))
            overlay.destroy()
            self._save_config(silent=True)
            self._refresh_status("已设置点击区域")

        def cancel(_event=None):
            overlay.destroy()
            self._refresh_status("已取消框选")

        canvas.bind("<ButtonPress-1>", on_press)
        canvas.bind("<B1-Motion>", on_move)
        canvas.bind("<ButtonRelease-1>", on_release)
        overlay.bind("<Escape>", cancel)

    def pick_background_point(self) -> None:
        if not self.background_target:
            item = self._selected_window_from_combo()
            if item:
                self.background_target = item
            else:
                messagebox.showwarning("后台点击", "请先选择一个窗口")
                return

        self._refresh_status("请点击目标窗口内的点击位置")

        overlay = tk.Toplevel(self.root)
        overlay.attributes("-fullscreen", True)
        overlay.attributes("-alpha", 0.2)
        overlay.attributes("-topmost", True)
        overlay.configure(bg="black")
        overlay.config(cursor="crosshair")

        canvas = tk.Canvas(overlay, bg="black", highlightthickness=0)
        canvas.pack(fill=tk.BOTH, expand=True)

        def on_release(event):
            if not self.background_target:
                overlay.destroy()
                return
            client_x, client_y = screen_to_client(self.background_target.hwnd, event.x_root, event.y_root)
            client_rect = get_client_rect(self.background_target.hwnd)
            if client_rect:
                width, height = client_rect
                if client_x < 0 or client_y < 0 or client_x >= width or client_y >= height:
                    overlay.destroy()
                    self._refresh_status("选择失败：点击位置不在窗口客户区内")
                    messagebox.showwarning("后台点击", "请选择窗口客户区内的位置")
                    return
            self.background_point = (int(client_x), int(client_y))
            self.bg_point_label.config(text=str(self.background_point))
            self.background_mode_var.set("位置")
            overlay.destroy()
            self._save_config(silent=True)
            self._refresh_status(f"已设置后台点击位置：{self.background_point}")

        def cancel(_event=None):
            overlay.destroy()
            self._refresh_status("已取消后台位置选择")

        canvas.bind("<ButtonRelease-1>", on_release)
        overlay.bind("<Escape>", cancel)

    def pick_background_region(self) -> None:
        if not self.background_target:
            item = self._selected_window_from_combo()
            if item:
                self.background_target = item
            else:
                messagebox.showwarning("后台点击", "请先选择一个窗口")
                return

        self._refresh_status("请拖拽选择后台点击区域")

        overlay = tk.Toplevel(self.root)
        overlay.attributes("-fullscreen", True)
        overlay.attributes("-alpha", 0.2)
        overlay.attributes("-topmost", True)
        overlay.configure(bg="black")
        overlay.config(cursor="crosshair")

        canvas = tk.Canvas(overlay, bg="black", highlightthickness=0)
        canvas.pack(fill=tk.BOTH, expand=True)

        data = {"screen_x1": 0, "screen_y1": 0, "canvas_x1": 0, "canvas_y1": 0, "rect_id": None}

        def on_press(event):
            data["screen_x1"], data["screen_y1"] = event.x_root, event.y_root
            data["canvas_x1"], data["canvas_y1"] = event.x, event.y
            if data["rect_id"]:
                canvas.delete(data["rect_id"])
            data["rect_id"] = canvas.create_rectangle(
                event.x,
                event.y,
                event.x,
                event.y,
                outline="cyan",
                width=2,
            )

        def on_move(event):
            if data["rect_id"]:
                canvas.coords(
                    data["rect_id"],
                    data["canvas_x1"],
                    data["canvas_y1"],
                    event.x,
                    event.y,
                )

        def on_release(event):
            if not self.background_target:
                overlay.destroy()
                return

            x1 = min(data["screen_x1"], event.x_root)
            y1 = min(data["screen_y1"], event.y_root)
            x2 = max(data["screen_x1"], event.x_root)
            y2 = max(data["screen_y1"], event.y_root)

            client_x1, client_y1 = screen_to_client(self.background_target.hwnd, x1, y1)
            client_x2, client_y2 = screen_to_client(self.background_target.hwnd, x2, y2)
            client_rect = get_client_rect(self.background_target.hwnd)
            if client_rect:
                width, height = client_rect
                left = min(client_x1, client_x2)
                right = max(client_x1, client_x2)
                top = min(client_y1, client_y2)
                bottom = max(client_y1, client_y2)
                if left < 0 or top < 0 or right > width or bottom > height:
                    overlay.destroy()
                    self._refresh_status("选择失败：区域不在窗口客户区内")
                    messagebox.showwarning("后台点击", "请选择窗口客户区内的区域")
                    return

            self.background_region = (
                int(client_x1),
                int(client_y1),
                int(client_x2),
                int(client_y2),
            )
            self.bg_region_label.config(text=str(self.background_region))
            self.background_mode_var.set("区域")
            overlay.destroy()
            self._save_config(silent=True)
            self._refresh_status(f"已设置后台点击区域：{self.background_region}")

        def cancel(_event=None):
            overlay.destroy()
            self._refresh_status("已取消后台区域选择")

        canvas.bind("<ButtonPress-1>", on_press)
        canvas.bind("<B1-Motion>", on_move)
        canvas.bind("<ButtonRelease-1>", on_release)
        overlay.bind("<Escape>", cancel)

    def on_close(self) -> None:
        self.stop_clicking()
        if self.hotkey_listener:
            self.hotkey_listener.stop()
        self._stop_record_listener()
        self._save_config(silent=True)
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    style = ttk.Style(root)
    if "vista" in style.theme_names():
        style.theme_use("vista")
    app = AutoClickerApp(root)
    app.toggle_btn.config(text=f"开始（{app._format_hotkey(app.hotkey_var.get())}）")
    root.mainloop()


if __name__ == "__main__":
    main()
