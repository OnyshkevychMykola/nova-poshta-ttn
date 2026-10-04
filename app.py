"""Просте вікно для створення накладних Нової Пошти."""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from create_ttn import (
    DEFAULTS,
    ROOT,
    VALUE_KEYS,
    config_from_values,
    error_message,
    load_env,
    run_batch,
)

LABELS = {
    "NP_API_KEY": "API-ключ",
    "SENDER_NAME": "ПІБ відправника",
    "SENDER_CITY": "Місто",
    "SENDER_WAREHOUSE": "Відділення",
    "PARCEL_DESCRIPTION": "Опис посилки",
    "PARCEL_WEIGHT": "Вага, кг",
    "PARCEL_LENGTH_CM": "Довжина, см",
    "PARCEL_WIDTH_CM": "Ширина, см",
    "PARCEL_HEIGHT_CM": "Висота, см",
}


def settings_path() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    folder = Path(base) / "NovaPoshtaTTN"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "settings.json"


def initial_values() -> tuple[dict[str, str], str]:
    values = {key: DEFAULTS.get(key, "") for key in VALUE_KEYS}
    values["NP_API_KEY"] = ""
    for key, raw in load_env(ROOT / ".env").items():
        if key in values and raw.strip():
            values[key] = raw.strip()
    csv_path = ""
    path = settings_path()
    if path.exists():
        saved = json.loads(path.read_text(encoding="utf-8"))
        for key in VALUE_KEYS:
            raw = str(saved.get(key, "")).strip()
            if raw:
                values[key] = raw
        csv_path = str(saved.get("csv_path", "")).strip()
    return values, csv_path


def save_settings(values: dict[str, str], csv_path: str) -> None:
    payload = {key: values.get(key, "").strip() for key in VALUE_KEYS}
    payload["csv_path"] = csv_path.strip()
    settings_path().write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


class QueueWriter:
    def __init__(self, events: queue.Queue[str | None]) -> None:
        self.events = events

    def write(self, text: str) -> None:
        if text:
            self.events.put(text)

    def flush(self) -> None:
        return


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.events: queue.Queue[str | None] = queue.Queue()
        self.running = False
        self.fields: dict[str, tk.StringVar] = {}
        values, csv_path = initial_values()
        self.csv_path = tk.StringVar(value=csv_path)
        self._build(values)
        root.protocol("WM_DELETE_WINDOW", self._close)

    def _build(self, values: dict[str, str]) -> None:
        self.root.title("Накладні Нової Пошти")
        self.root.minsize(680, 620)
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(2, weight=1)

        form = ttk.Frame(self.root, padding=12)
        form.grid(row=0, column=0, sticky="ew")
        form.columnconfigure(1, weight=1)

        self._add_field(form, "NP_API_KEY", values, 0, secret=True)
        self._add_field(form, "SENDER_NAME", values, 1)
        city = self._labeled_var("SENDER_CITY", values)
        warehouse = self._labeled_var("SENDER_WAREHOUSE", values)
        place = ttk.Frame(form)
        place.grid(row=2, column=0, columnspan=2, sticky="ew", pady=3)
        ttk.Label(place, text="Місто", width=18).pack(side="left")
        ttk.Entry(place, textvariable=city, width=24).pack(side="left")
        ttk.Label(place, text="Відділення").pack(side="left", padx=(12, 6))
        ttk.Entry(place, textvariable=warehouse, width=8).pack(side="left")
        self._add_field(form, "PARCEL_DESCRIPTION", values, 3)

        size = ttk.Frame(form)
        size.grid(row=4, column=0, columnspan=2, sticky="ew", pady=3)
        for key, width in (
            ("PARCEL_WEIGHT", 8),
            ("PARCEL_LENGTH_CM", 8),
            ("PARCEL_WIDTH_CM", 8),
            ("PARCEL_HEIGHT_CM", 8),
        ):
            ttk.Label(size, text=LABELS[key]).pack(side="left", padx=(0, 4))
            ttk.Entry(size, textvariable=self._labeled_var(key, values), width=width).pack(side="left", padx=(0, 10))

        file_row = ttk.Frame(self.root, padding=(12, 0, 12, 8))
        file_row.grid(row=1, column=0, sticky="ew")
        file_row.columnconfigure(1, weight=1)
        ttk.Label(file_row, text="CSV-файл").grid(row=0, column=0, sticky="w")
        ttk.Entry(file_row, textvariable=self.csv_path).grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Button(file_row, text="Обрати", command=self._choose_file).grid(row=0, column=2)
        buttons = ttk.Frame(file_row)
        buttons.grid(row=1, column=0, columnspan=3, sticky="w", pady=(10, 0))
        self.test_button = ttk.Button(buttons, text="Test", width=16, command=lambda: self.start(True))
        self.test_button.pack(side="left")
        self.run_button = ttk.Button(buttons, text="Run", width=16, command=lambda: self.start(False))
        self.run_button.pack(side="left", padx=8)
        ttk.Label(buttons, text="Test лише перевіряє. Run створює накладні.").pack(side="left", padx=8)

        log_frame = ttk.Frame(self.root, padding=(12, 0, 12, 12))
        log_frame.grid(row=2, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log = tk.Text(log_frame, height=16, wrap="word", state="disabled")
        self.log.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(log_frame, command=self.log.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=scroll.set)

    def _labeled_var(self, key: str, values: dict[str, str]) -> tk.StringVar:
        if key not in self.fields:
            self.fields[key] = tk.StringVar(value=values.get(key, ""))
        return self.fields[key]

    def _add_field(self, parent: ttk.Frame, key: str, values: dict[str, str], row: int, secret: bool = False) -> None:
        ttk.Label(parent, text=LABELS[key], width=18).grid(row=row, column=0, sticky="w", pady=3)
        entry = ttk.Entry(parent, textvariable=self._labeled_var(key, values), show="*" if secret else "")
        entry.grid(row=row, column=1, sticky="ew", pady=3)

    def _choose_file(self) -> None:
        selected = filedialog.askopenfilename(
            parent=self.root,
            title="Оберіть CSV",
            filetypes=[("CSV", "*.csv"), ("Усі файли", "*.*")],
        )
        if selected:
            self.csv_path.set(selected)
            self._persist()

    def _values(self) -> dict[str, str]:
        return {key: self.fields[key].get() for key in VALUE_KEYS if key in self.fields}

    def _persist(self) -> None:
        save_settings(self._values(), self.csv_path.get())

    def _append(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_busy(self, busy: bool) -> None:
        self.running = busy
        state = "disabled" if busy else "normal"
        self.test_button.configure(state=state)
        self.run_button.configure(state=state)

    def _close(self) -> None:
        if self.running:
            return
        self._persist()
        self.root.destroy()

    def start(self, dry_run: bool) -> None:
        if self.running:
            return
        self._persist()
        csv_path = self.csv_path.get().strip()
        if not csv_path:
            messagebox.showwarning("Файл", "Оберіть CSV-файл", parent=self.root)
            return
        try:
            config_from_values(self._values())
        except ValueError as error:
            messagebox.showerror("Налаштування", str(error), parent=self.root)
            return
        if not dry_run and not messagebox.askyesno(
            "Run",
            "Створити справжні накладні з цього файлу?",
            parent=self.root,
        ):
            return
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self._set_busy(True)
        worker = threading.Thread(
            target=self._run,
            args=(dry_run, csv_path, self._values()),
            daemon=True,
        )
        worker.start()
        self.root.after(100, self._poll)

    def _run(self, dry_run: bool, csv_path: str, values: dict[str, str]) -> None:
        writer = QueueWriter(self.events)
        previous_out, previous_err = sys.stdout, sys.stderr
        sys.stdout = writer
        sys.stderr = writer
        try:
            api_key, config = config_from_values(values)
            run_batch(Path(csv_path), api_key, config, dry_run, True)
        except (ValueError, SystemExit) as error:
            print(error_message(error))
        except Exception as error:
            print(f"Помилка: {error}")
        finally:
            sys.stdout = previous_out
            sys.stderr = previous_err
            self.events.put(None)

    def _poll(self) -> None:
        while True:
            try:
                item = self.events.get_nowait()
            except queue.Empty:
                break
            if item is None:
                self._set_busy(False)
                return
            self._append(item)
        if self.running:
            self.root.after(100, self._poll)


def main() -> None:
    try:
        from ctypes import windll

        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    style = ttk.Style(root)
    if "vista" in style.theme_names():
        style.theme_use("vista")
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
