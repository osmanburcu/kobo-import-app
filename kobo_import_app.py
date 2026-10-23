#!/usr/bin/env python3
"""Kobo CSV Import - a small desktop app.

Pick a CSV, check the settings, preview the JSON and send every row to a
KoboToolbox form. Each column name becomes a field ("geo/p1" -> geo.p1),
the same way the Postman collection works.

Only needs Python 3 (Tkinter and urllib are built in).
"""

import csv
import io
import json
import os
import queue
import threading
import time
import urllib.error
import urllib.request
import uuid

SETTINGS_FILE = os.path.join(os.path.expanduser("~"), ".kobo_import_app.json")
DEFAULTS = {
    "kobo_url": "https://kf.kobotoolbox.org/osmanburcu/submission?format=json",
    "asset_uid": "aNAmpcbU9opH9xA6NtbL3x",
    "column_map": "{}",
    "skip_columns": "",
    "delay_ms": "200",
    "repeat_paths": "",
}
PATH_SEPARATOR = " | "  # between repeat file paths in the text box


# --------------------------------------------------------------------------
# Logic (no UI)
# --------------------------------------------------------------------------

def read_csv(path):
    """Return (columns, rows). Handles ',', ';' and tab, UTF-8 or Windows encoding."""
    with open(path, "rb") as f:
        raw = f.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1254")  # Turkish Windows encoding, also covers Latin
    try:
        dialect = csv.Sniffer().sniff(text[:10000], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    columns = [c.strip() for c in (reader.fieldnames or [])]
    rows = [{(k or "").strip(): (v or "") for k, v in row.items() if k is not None}
            for row in reader]
    return columns, rows


def is_metadata(column):
    """Kobo export columns that are not answers (_id, _submission_time, meta/...)."""
    return column.startswith("_") or column.startswith("formhub/") or column.startswith("meta/")


def _norm_index(value):
    """'3', '3.0' and ' 3 ' all mean row 3 (Excel often adds '.0')."""
    value = (value or "").strip()
    try:
        return str(int(float(value)))
    except ValueError:
        return value


def _put(node, path, value):
    """Set node['a']['b']['c'] = value for path 'a/b/c'."""
    keys = path.split("/")
    for key in keys[:-1]:
        if not isinstance(node.get(key), dict):
            node[key] = {}
        node = node[key]
    node[keys[-1]] = value


def _add_answers(node, row, column_map, skip, strip_prefix=""):
    for column, value in row.items():
        if is_metadata(column) or column in skip or value.strip() == "":
            continue
        name = column_map.get(column, column)
        if strip_prefix and name.startswith(strip_prefix + "/"):
            name = name[len(strip_prefix) + 1:]
        _put(node, name, value.replace("%20", " "))


def load_repeats(paths, column_map=None, skip=()):
    """Read one CSV per repeat group (a sheet of Kobo's Excel export saved as CSV).

    The file name is the repeat group name. Rows are linked to their parent with
    _parent_index, and _parent_table_name says if the parent is another repeat
    (nested repeat) or the main sheet.
    """
    column_map = column_map or {}
    tables = []
    for path in paths:
        name = os.path.splitext(os.path.basename(path))[0]
        columns, rows = read_csv(path)
        names = [column_map.get(c, c).split("/") for c in columns
                 if not is_metadata(c) and c not in skip]
        # Repeat path = group path shared by all its questions, e.g. "hh/members"
        prefix = os.path.commonprefix([n[:-1] for n in names]) if names else []
        if name in prefix:  # cut sub-groups inside the repeat
            prefix = prefix[:len(prefix) - prefix[::-1].index(name)]
        tables.append({"name": name, "file": os.path.basename(path),
                       "path": "/".join(prefix) or name, "rows": rows})

    table_names = {t["name"] for t in tables}
    for t in tables:
        t["children"] = {}
        for r in t["rows"]:
            parent = r.get("_parent_table_name", "").strip()
            parent = parent if parent in table_names and parent != t["name"] else None
            key = (parent, _norm_index(r.get("_parent_index")))
            t["children"].setdefault(key, []).append(r)
    return tables


def _add_repeats(node, tables, parent_table, parent_index, parent_path, column_map, skip):
    for t in tables:
        entries = []
        for r in t["children"].get((parent_table, parent_index), []):
            entry = {}
            _add_answers(entry, r, column_map, skip, strip_prefix=t["path"])
            _add_repeats(entry, tables, t["name"], _norm_index(r.get("_index")),
                         t["path"], column_map, skip)
            if entry:
                entries.append(entry)
        if entries:
            path = t["path"]
            if parent_path and path.startswith(parent_path + "/"):
                path = path[len(parent_path) + 1:]
            _put(node, path, entries)


def main_index(row, row_number):
    """The _index that repeat rows point to; falls back to the row's position."""
    return _norm_index(row.get("_index")) or str(row_number)


def orphan_count(table, main_rows, tables):
    """Repeat rows whose parent row cannot be found."""
    valid = {(None, main_index(r, n)) for n, r in enumerate(main_rows, start=1)}
    for t in tables:
        valid.update((t["name"], _norm_index(r.get("_index"))) for r in t["rows"])
    return sum(len(rows) for key, rows in table["children"].items() if key not in valid)


def build_submission(row, asset_uid, column_map=None, skip=(), repeats=(), row_number=1):
    """Turn one CSV row (plus its repeat rows) into the JSON Kobo expects."""
    column_map = column_map or {}
    submission = {}
    _add_answers(submission, row, column_map, skip)
    if repeats:
        _add_repeats(submission, repeats, None, main_index(row, row_number), "",
                     column_map, skip)

    # Reuse the original ID so a re-import is rejected as duplicate
    original = row.get("meta/instanceID", "").replace("uuid:", "") or row.get("_uuid", "")
    submission["meta"] = {"instanceID": "uuid:" + (original.strip() or str(uuid.uuid4()))}
    return {"id": asset_uid, "submission": submission}


def post_submission(url, token, payload, retries=3):
    """Send one submission. Returns (status, message): ok / duplicate / failed."""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, data=data, method="POST", headers={
            "Authorization": f"Token {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = resp.read().decode("utf-8", "replace")
                if "duplicate" in body.lower():
                    return "duplicate", f"HTTP {resp.status} already in Kobo"
                return "ok", f"HTTP {resp.status}"
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            if (e.code == 429 or e.code >= 500) and attempt < retries:
                time.sleep(2 ** attempt)
                continue
            hint = {401: " (check the token)", 403: " (no permission for this form)",
                    404: " (check the URL and form UID)"}.get(e.code, "")
            return "failed", f"HTTP {e.code}{hint}: {body}"
        except (urllib.error.URLError, OSError) as e:
            if attempt < retries:
                time.sleep(2 ** attempt)
                continue
            return "failed", f"Could not reach Kobo: {getattr(e, 'reason', e)}"


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------

def run_app():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    from tkinter.scrolledtext import ScrolledText

    class App(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title("Kobo CSV Import")
            self.geometry("820x680")
            self.minsize(640, 520)
            self.rows, self.columns, self.results = [], [], []
            self.events = queue.Queue()
            self.stop_flag = threading.Event()
            self.settings = self._load_settings()
            self._build()
            self.after(100, self._poll_events)

        # ---------- layout ----------
        def _build(self):
            pad = {"padx": 8, "pady": 4}
            form = ttk.LabelFrame(self, text="Settings")
            form.pack(fill="x", padx=10, pady=(10, 4))
            form.columnconfigure(1, weight=1)

            self.vars = {}

            def field(row, label, key, show=None, browse=None):
                ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", **pad)
                var = tk.StringVar(value=self.settings.get(key, ""))
                ttk.Entry(form, textvariable=var, show=show).grid(
                    row=row, column=1, sticky="ew", **pad)
                if browse:
                    ttk.Button(form, text="Browse…", command=browse).grid(
                        row=row, column=2, **pad)
                self.vars[key] = var

            field(0, "CSV file", "csv_path", browse=self._browse)
            field(1, "Repeat CSV files (optional)", "repeat_paths",
                  browse=self._browse_repeats)
            field(2, "Kobo URL", "kobo_url")
            field(3, "Form UID", "asset_uid")
            field(4, "API token", "token", show="•")
            field(5, "Column map (JSON)", "column_map")
            field(6, "Skip columns", "skip_columns")
            field(7, "Delay between rows (ms)", "delay_ms")
            ttk.Label(form, foreground="gray",
                      text="The token is not saved. Repeat files: one CSV per repeat group, "
                           "named after the group (e.g. members.csv).").grid(
                row=8, column=0, columnspan=3, sticky="w", **pad)

            buttons = ttk.Frame(self)
            buttons.pack(fill="x", padx=10, pady=4)
            ttk.Button(buttons, text="Preview", command=self._preview).pack(side="left")
            self.send_btn = ttk.Button(buttons, text="Send to Kobo", command=self._send)
            self.send_btn.pack(side="left", padx=6)
            self.test_btn = ttk.Button(buttons, text="Send first row only",
                                       command=lambda: self._send(limit=1))
            self.test_btn.pack(side="left")
            self.stop_btn = ttk.Button(buttons, text="Stop", state="disabled",
                                       command=self.stop_flag.set)
            self.stop_btn.pack(side="left", padx=6)
            self.save_btn = ttk.Button(buttons, text="Save results…", state="disabled",
                                       command=self._save_results)
            self.save_btn.pack(side="right")

            self.progress = ttk.Progressbar(self, mode="determinate")
            self.progress.pack(fill="x", padx=10, pady=4)
            self.status = ttk.Label(self, text="Choose a CSV file to start.")
            self.status.pack(fill="x", padx=10)

            self.log = ScrolledText(self, height=18, font=("Consolas", 9), state="disabled")
            self.log.pack(fill="both", expand=True, padx=10, pady=(4, 10))
            self.log.tag_config("ok", foreground="#1a7f37")
            self.log.tag_config("duplicate", foreground="#9a6700")
            self.log.tag_config("failed", foreground="#cf222e")

        # ---------- helpers ----------
        def _write(self, text, tag=None, clear=False):
            self.log.configure(state="normal")
            if clear:
                self.log.delete("1.0", "end")
            self.log.insert("end", text + "\n", tag)
            self.log.see("end")
            self.log.configure(state="disabled")

        def _load_settings(self):
            settings = dict(DEFAULTS)
            try:
                with open(SETTINGS_FILE, encoding="utf-8") as f:
                    settings.update(json.load(f))
            except (OSError, ValueError):
                pass
            return settings

        def _save_settings(self):
            data = {k: v.get() for k, v in self.vars.items() if k != "token"}
            try:
                with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
            except OSError:
                pass

        def _browse(self):
            path = filedialog.askopenfilename(
                title="Choose CSV file",
                filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
            if path:
                self.vars["csv_path"].set(path)
                self._preview()

        def _browse_repeats(self):
            paths = filedialog.askopenfilenames(
                title="Choose the repeat group CSV files (select all of them)",
                filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
            if paths:
                self.vars["repeat_paths"].set(PATH_SEPARATOR.join(paths))
                if self.vars["csv_path"].get().strip():
                    self._preview()

        def _read_inputs(self, need_token):
            """Validate the form. Returns a dict or None after showing an error."""
            v = {k: var.get().strip() for k, var in self.vars.items()}
            if not v["csv_path"] or not os.path.isfile(v["csv_path"]):
                messagebox.showerror("Kobo CSV Import", "Choose an existing CSV file.")
                return None
            try:
                v["column_map"] = json.loads(v["column_map"] or "{}")
                assert isinstance(v["column_map"], dict)
            except (ValueError, AssertionError):
                messagebox.showerror("Kobo CSV Import",
                                     'Column map must be JSON like {"old/name": "new/name"} or {}.')
                return None
            v["skip_columns"] = [s.strip() for s in v["skip_columns"].split(",") if s.strip()]
            try:
                v["delay_ms"] = max(0, int(v["delay_ms"] or 0))
            except ValueError:
                v["delay_ms"] = 200
            if not v["asset_uid"]:
                messagebox.showerror("Kobo CSV Import", "Enter the Form UID.")
                return None
            if need_token:
                if "{username}" in v["kobo_url"] or not v["kobo_url"].startswith("http"):
                    messagebox.showerror("Kobo CSV Import",
                                         "Enter the Kobo URL, with {username} replaced "
                                         "by your Kobo username.")
                    return None
                if not v["token"]:
                    messagebox.showerror("Kobo CSV Import", "Enter your Kobo API token.")
                    return None
            repeat_paths = [p.strip() for p in v["repeat_paths"].split("|") if p.strip()]
            missing = [p for p in repeat_paths if not os.path.isfile(p)]
            if missing:
                messagebox.showerror("Kobo CSV Import",
                                     "Repeat file not found:\n" + "\n".join(missing))
                return None
            try:
                self.columns, self.rows = read_csv(v["csv_path"])
                v["repeats"] = load_repeats(repeat_paths, v["column_map"], v["skip_columns"])
            except OSError as e:
                messagebox.showerror("Kobo CSV Import", f"Could not read the CSV:\n{e}")
                return None
            self._save_settings()
            return v

        # ---------- actions ----------
        def _preview(self):
            v = self._read_inputs(need_token=False)
            if not v:
                return
            sent = [c for c in self.columns
                    if not is_metadata(c) and c not in v["skip_columns"]]
            skipped = [c for c in self.columns if c not in sent]
            self._write(f"{len(self.rows)} rows, {len(self.columns)} columns", clear=True)
            self._write(f"\nSent as answers ({len(sent)}):")
            for c in sent:
                target = v["column_map"].get(c)
                self._write(f"  {c}" + (f"  →  {target}" if target else ""))
            if skipped:
                self._write(f"\nNot sent ({len(skipped)}): " + ", ".join(skipped))

            names = {t["name"] for t in v["repeats"]}
            for t in v["repeats"]:
                parents = {key[0] for key in t["children"]} - {None}
                where = ("inside " + ", ".join(sorted(parents))) if parents & names \
                    else "in the main form"
                self._write(f"\nRepeat group '{t['name']}' ({t['file']}): {len(t['rows'])} "
                            f"entries, sent as '{t['path']}' {where}")
                orphans = orphan_count(t, self.rows, v["repeats"])
                if orphans:
                    self._write(f"  WARNING: {orphans} entries have a _parent_index that "
                                f"matches no row, they will not be sent", "failed")

            if self.rows:
                # Show a row that has repeat entries, if there is one
                n, row = 1, self.rows[0]
                for i, r in enumerate(self.rows, start=1):
                    if any(k == (None, main_index(r, i)) for t in v["repeats"]
                           for k in t["children"]):
                        n, row = i, r
                        break
                payload = build_submission(row, v["asset_uid"], v["column_map"],
                                           v["skip_columns"], v["repeats"], n)
                self._write(f"\nRow {n} will be sent as:")
                self._write(json.dumps(payload, indent=2, ensure_ascii=False))
            self.status.configure(text=f"Ready: {len(self.rows)} rows.")

        def _send(self, limit=None):
            v = self._read_inputs(need_token=True)
            if not v:
                return
            rows = self.rows[:limit] if limit else self.rows
            if not rows:
                messagebox.showinfo("Kobo CSV Import", "The CSV has no rows.")
                return
            if not limit and not messagebox.askyesno(
                    "Kobo CSV Import", f"Send {len(rows)} rows to Kobo?"):
                return

            self.results = []
            self.stop_flag.clear()
            self.progress.configure(maximum=len(rows), value=0)
            for b in (self.send_btn, self.test_btn, self.save_btn):
                b.configure(state="disabled")
            self.stop_btn.configure(state="normal")
            self._write(f"Sending {len(rows)} row(s) to {v['kobo_url']}", clear=True)
            threading.Thread(target=self._worker, args=(v, rows), daemon=True).start()

        def _worker(self, v, rows):
            for n, row in enumerate(rows, start=1):
                if self.stop_flag.is_set():
                    self.events.put(("stopped", n - 1))
                    return
                payload = build_submission(row, v["asset_uid"], v["column_map"],
                                           v["skip_columns"], v["repeats"], n)
                status, message = post_submission(v["kobo_url"], v["token"], payload)
                self.events.put(("row", (n, payload["submission"]["meta"]["instanceID"],
                                         status, message)))
                time.sleep(v["delay_ms"] / 1000)
            self.events.put(("done", len(rows)))

        def _poll_events(self):
            try:
                while True:
                    kind, data = self.events.get_nowait()
                    if kind == "row":
                        n, instance_id, status, message = data
                        self.results.append(data)
                        self._write(f"Row {n}: {status.upper()}  {message}", status)
                        self.progress.configure(value=n)
                        self.status.configure(text=self._summary())
                    else:
                        self._finish(stopped=(kind == "stopped"))
            except queue.Empty:
                pass
            self.after(100, self._poll_events)

        def _summary(self):
            counts = {}
            for _, _, status, _ in self.results:
                counts[status] = counts.get(status, 0) + 1
            return ", ".join(f"{k}: {c}" for k, c in sorted(counts.items())) or "Nothing sent"

        def _finish(self, stopped):
            for b in (self.send_btn, self.test_btn, self.save_btn):
                b.configure(state="normal")
            self.stop_btn.configure(state="disabled")
            text = ("Stopped. " if stopped else "Finished. ") + self._summary()
            self.status.configure(text=text)
            self._write("\n" + text)

        def _save_results(self):
            path = filedialog.asksaveasfilename(
                title="Save results", defaultextension=".csv",
                initialfile="kobo_import_results.csv", filetypes=[("CSV files", "*.csv")])
            if not path:
                return
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(["row", "instanceID", "status", "message"])
                w.writerows(self.results)
            self.status.configure(text=f"Results saved to {os.path.basename(path)}")

    App().mainloop()


if __name__ == "__main__":
    run_app()
