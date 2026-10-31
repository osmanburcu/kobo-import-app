#!/usr/bin/env python3
"""Kobo CSV Import - a small desktop app.

Pick a CSV, check the settings, preview the JSON and send every row to a
KoboToolbox form. Each column name becomes a field ("geo/p1" -> geo.p1),
the same way the Postman collection works.

The "Update existing data" page changes answers in submissions that are
already in Kobo: find and replace in one column, or new values per
submission from a CSV.

The "Copy from another form" page pulls the submissions of one form (in any
account) and sends them to the form in the connection box, with a column
matching list for groups or questions that are named differently.

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
import urllib.parse
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
    "update_mode": "replace",
    "update_column": "",
    "update_find": "",
    "update_value": "",
    "update_csv": "",
    "copy_server": "https://kf.kobotoolbox.org",
    "copy_uid": "",
    "copy_mapping": "",
}
PATH_SEPARATOR = " | "  # between repeat file paths in the text box
HTTP_HINTS = {401: " (check the token)", 403: " (no permission for this form)",
              404: " (check the URL and form UID)"}
UPDATE_CHUNK = 100  # submissions changed per bulk request
NOT_QUESTIONS = {"note", "start", "end", "today", "deviceid", "username", "simserial",
                 "subscriberid", "phonenumber", "audit"}


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
            return "failed", f"HTTP {e.code}{HTTP_HINTS.get(e.code, '')}: {body}"
        except (urllib.error.URLError, OSError) as e:
            if attempt < retries:
                time.sleep(2 ** attempt)
                continue
            return "failed", f"Could not reach Kobo: {getattr(e, 'reason', e)}"


# --------------------------------------------------------------------------
# Updating existing submissions (Kobo API v2)
# --------------------------------------------------------------------------

class KoboError(Exception):
    """A Kobo API call failed; the message is shown to the user as it is."""


def server_url(kobo_url):
    """'https://kf.kobotoolbox.org/user/submission?...' -> 'https://kf.kobotoolbox.org'."""
    parts = urllib.parse.urlsplit(kobo_url)
    return f"{parts.scheme}://{parts.netloc}"


def kobo_api(method, url, token, payload=None, retries=3):
    """Call the Kobo API and return its JSON answer. Raises KoboError."""
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Authorization": f"Token {token}", "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            if (e.code == 429 or e.code >= 500) and attempt < retries:
                time.sleep(2 ** attempt)
                continue
            raise KoboError(f"HTTP {e.code}{HTTP_HINTS.get(e.code, '')}: {body}") from None
        except (urllib.error.URLError, OSError) as e:
            if attempt < retries:
                time.sleep(2 ** attempt)
                continue
            raise KoboError(f"Could not reach Kobo: {getattr(e, 'reason', e)}") from None
        try:
            return json.loads(body) if body.strip() else {}
        except ValueError:
            raise KoboError("Kobo sent an answer that is not JSON. "
                            "Check the Kobo URL.") from None


def fetch_form_columns(server, asset_uid, token, include_repeats=False):
    """The form's question names as data columns ("group/question").

    Questions inside repeat groups are left out (they cannot be updated), unless
    include_repeats is set: then each repeat group's own path is listed too.
    """
    asset = kobo_api("GET", f"{server}/api/v2/assets/{asset_uid}/?format=json", token)
    columns, groups = [], []
    for q in asset.get("content", {}).get("survey", []):
        kind = (q.get("type") or "").replace(" ", "_")
        name = q.get("name") or q.get("$autoname") or ""
        if kind in ("begin_group", "begin_repeat"):
            groups.append((name, kind == "begin_repeat"))
            if kind == "begin_repeat" and include_repeats:
                columns.append("/".join(g for g, _ in groups))
        elif kind in ("end_group", "end_repeat"):
            if groups:
                groups.pop()
        elif (name and kind not in NOT_QUESTIONS
              and (include_repeats or not any(r for _, r in groups))):
            columns.append(q.get("$xpath") or "/".join([g for g, _ in groups] + [name]))
    return columns


def fetch_submissions(server, asset_uid, token, fields=None):
    """All submissions of the form, with only the given fields (None = all)."""
    params = {"format": "json", "limit": 1000}
    if fields:
        params["fields"] = json.dumps(fields)
    url = f"{server}/api/v2/assets/{asset_uid}/data/?{urllib.parse.urlencode(params)}"
    results = []
    while url:
        page = kobo_api("GET", url, token)
        results.extend(page.get("results", []))
        url = page.get("next")
    return results


def plan_replace(server, asset_uid, token, column, find, new_value):
    """Find-and-replace in one column. An empty `find` changes every submission.

    Returns {"jobs": [(ids, data)], "lines": [...], "problems": [], "total": n}.
    """
    subs = fetch_submissions(server, asset_uid, token, ["_id", column])
    if find:
        subs = [s for s in subs if str(s.get(column, "")) == find]
    ids = [s["_id"] for s in subs]
    data = {column: new_value}
    lines = [f"_id {s['_id']}: {str(s.get(column, ''))!r}  →  {new_value!r}" for s in subs]
    return {"jobs": [(ids[i:i + UPDATE_CHUNK], data) for i in range(0, len(ids), UPDATE_CHUNK)],
            "lines": lines, "problems": [], "total": len(ids)}


def plan_csv_update(server, asset_uid, token, path):
    """New values per submission from a CSV with an _id (or _uuid) column.

    Every other column is a question to change; empty cells are left as they are.
    Same return shape as plan_replace.
    """
    columns, rows = read_csv(path)
    if "_id" not in columns and "_uuid" not in columns:
        raise KoboError("The update CSV needs an _id or _uuid column to know which "
                        "submission each row changes.")
    ids_by_uuid = {}
    if "_id" not in columns:
        ids_by_uuid = {str(s.get("_uuid", "")): s["_id"] for s in
                       fetch_submissions(server, asset_uid, token, ["_id", "_uuid"])}
    jobs, lines, problems = [], [], []
    for n, row in enumerate(rows, start=1):
        if "_id" in columns:
            sid = _norm_index(row.get("_id"))
        else:
            sid = str(ids_by_uuid.get(row.get("_uuid", "").strip(), ""))
        data = {c: v.replace("%20", " ") for c, v in row.items()
                if not is_metadata(c) and v.strip() != ""}
        if not sid.isdigit():
            problems.append(f"Row {n}: no submission found for this row, it is skipped")
        elif not data:
            problems.append(f"Row {n}: no values to change, it is skipped")
        else:
            jobs.append(([int(sid)], data))
            lines.append(f"_id {sid}: " + ", ".join(f"{c} = {v!r}" for c, v in data.items()))
    return {"jobs": jobs, "lines": lines, "problems": problems, "total": len(jobs)}


def update_submissions(server, asset_uid, token, ids, data):
    """Set the same answers on several submissions. Returns (status, message)."""
    payload = {"payload": {"submission_ids": list(ids), "data": data}}
    try:
        answer = kobo_api("PATCH", f"{server}/api/v2/assets/{asset_uid}/data/bulk/?format=json",
                          token, payload)
    except KoboError as e:
        return "failed", str(e)
    failures = answer.get("failures", 0) if isinstance(answer, dict) else 0
    if failures:
        details = json.dumps(answer.get("results", []), ensure_ascii=False)[:300]
        return "failed", f"{failures} of {len(ids)} not updated: {details}"
    return "ok", "updated"


# --------------------------------------------------------------------------
# Copying submissions from another form
# --------------------------------------------------------------------------

def match_columns(source, target):
    """Pair each source column with a target column: the same path, else the
    only target question with the same name (groups may be named differently).
    Returns [(source, target or "")]."""
    by_name = {}
    for t in target:
        by_name.setdefault(t.split("/")[-1], []).append(t)
    pairs = []
    for s in source:
        same_name = by_name.get(s.split("/")[-1], [])
        pairs.append((s, s if s in target else same_name[0] if len(same_name) == 1 else ""))
    return pairs


def format_mapping(pairs):
    return "\n".join(f"{s} = {t}" for s, t in pairs)


def parse_mapping(text):
    """'source = target' lines -> {source: target}. Lines without a target are left out."""
    mapping = {}
    for line in text.splitlines():
        source, _, target = line.partition("=")
        if source.strip() and target.strip() and not line.lstrip().startswith("#"):
            mapping[source.strip()] = target.strip()
    return mapping


def convert_submission(sub, mapping, parent=""):
    """Turn a submission from the Kobo data API into the answers for another form.

    Columns are renamed with `mapping`; unmapped columns are left out. A repeat
    group (a list of entries) is converted entry by entry, and `parent` is the
    target path of the repeat the entries go into.
    """
    node = {}
    for key, value in sub.items():
        target = mapping.get(key)
        if is_metadata(key) or not target:
            continue
        path = target[len(parent) + 1:] if parent and target.startswith(parent + "/") else target
        if isinstance(value, list):
            entries = [convert_submission(e, mapping, target) for e in value if isinstance(e, dict)]
            entries = [e for e in entries if e]
            if entries:
                _put(node, path, entries)
        elif value is not None and str(value) != "":
            _put(node, path, value if isinstance(value, str) else str(value))
    return node


def copy_payload(sub, target_uid, mapping):
    """The JSON to submit one pulled submission to the target form.

    The source _uuid is kept, so copying the same submission twice gives DUPLICATE.
    """
    submission = convert_submission(sub, mapping)
    submission["meta"] = {"instanceID": "uuid:" + (sub.get("_uuid") or str(uuid.uuid4()))}
    return {"id": target_uid, "submission": submission}


def unmapped_columns(subs, mapping):
    """Answer columns in the pulled data that have no target column."""
    found = set()
    for s in subs:
        for key, value in s.items():
            if is_metadata(key):
                continue
            if key not in mapping:
                found.add(key)
            if isinstance(value, list):
                found |= unmapped_columns([e for e in value if isinstance(e, dict)], mapping)
    return found


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
            self.geometry("820x860")
            self.minsize(640, 700)
            self.rows, self.columns, self.results = [], [], []
            self.events = queue.Queue()
            self.stop_flag = threading.Event()
            self.settings = self._load_settings()
            self._build()
            self.after(100, self._poll_events)

        # ---------- layout ----------
        def _build(self):
            pad = {"padx": 8, "pady": 4}
            self.vars = {}

            def field(parent, row, label, key, show=None, browse=None):
                ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", **pad)
                var = tk.StringVar(value=self.settings.get(key, ""))
                ttk.Entry(parent, textvariable=var, show=show).grid(
                    row=row, column=1, sticky="ew", **pad)
                if browse:
                    ttk.Button(parent, text="Browse…", command=browse).grid(
                        row=row, column=2, **pad)
                self.vars[key] = var

            def hint(parent, row, text):
                ttk.Label(parent, foreground="gray", text=text).grid(
                    row=row, column=0, columnspan=3, sticky="w", **pad)

            conn = ttk.LabelFrame(self, text="Kobo connection")
            conn.pack(fill="x", padx=10, pady=(10, 4))
            conn.columnconfigure(1, weight=1)
            field(conn, 0, "Kobo URL", "kobo_url")
            field(conn, 1, "Form UID", "asset_uid")
            field(conn, 2, "API token", "token", show="•")
            field(conn, 3, "Delay between rows (ms)", "delay_ms")
            hint(conn, 4, "The token is not saved.")

            pages = ttk.Notebook(self)
            pages.pack(fill="x", padx=10, pady=4)

            # Page 1: import new submissions
            imp = ttk.Frame(pages)
            imp.columnconfigure(1, weight=1)
            pages.add(imp, text="Import new data")
            field(imp, 0, "CSV file", "csv_path", browse=self._browse)
            field(imp, 1, "Repeat CSV files (optional)", "repeat_paths",
                  browse=self._browse_repeats)
            field(imp, 2, "Column map (JSON)", "column_map")
            field(imp, 3, "Skip columns", "skip_columns")
            hint(imp, 4, "Repeat files: one CSV per repeat group, named after the group "
                         "(e.g. members.csv).")
            buttons = ttk.Frame(imp)
            buttons.grid(row=5, column=0, columnspan=3, sticky="ew", **pad)
            ttk.Button(buttons, text="Preview", command=self._preview).pack(side="left")
            self.send_btn = ttk.Button(buttons, text="Send to Kobo", command=self._send)
            self.send_btn.pack(side="left", padx=6)
            self.test_btn = ttk.Button(buttons, text="Send first row only",
                                       command=lambda: self._send(limit=1))
            self.test_btn.pack(side="left")

            # Page 2: change answers in submissions already in Kobo
            upd = ttk.Frame(pages)
            upd.columnconfigure(1, weight=1)
            pages.add(upd, text="Update existing data")
            mode = tk.StringVar(value=self.settings.get("update_mode", "replace"))
            self.vars["update_mode"] = mode
            ttk.Radiobutton(upd, text="Find and replace in one column", value="replace",
                            variable=mode).grid(row=0, column=0, columnspan=3, sticky="w", **pad)
            ttk.Label(upd, text="Column").grid(row=1, column=0, sticky="w", **pad)
            self.vars["update_column"] = tk.StringVar(value=self.settings.get("update_column", ""))
            self.column_box = ttk.Combobox(upd, textvariable=self.vars["update_column"])
            self.column_box.grid(row=1, column=1, sticky="ew", **pad)
            ttk.Button(upd, text="Load columns", command=self._load_columns).grid(
                row=1, column=2, **pad)
            field(upd, 2, "Replace value", "update_find")
            field(upd, 3, "With new value", "update_value")
            hint(upd, 4, "Leave 'Replace value' empty to change this column in every submission.")
            ttk.Radiobutton(upd, text="New values from a CSV file (one row per submission)",
                            value="csv", variable=mode).grid(
                row=5, column=0, columnspan=3, sticky="w", **pad)
            field(upd, 6, "Update CSV file", "update_csv", browse=self._browse_update)
            hint(upd, 7, "Needs an _id or _uuid column. Every other column is a question to "
                         "change; empty cells are left as they are.")
            ubuttons = ttk.Frame(upd)
            ubuttons.grid(row=8, column=0, columnspan=3, sticky="ew", **pad)
            ttk.Button(ubuttons, text="Preview changes",
                       command=lambda: self._update(preview=True)).pack(side="left")
            self.update_btn = ttk.Button(ubuttons, text="Update in Kobo", command=self._update)
            self.update_btn.pack(side="left", padx=6)

            # Page 3: pull submissions from a form (any account) into this one
            cp = ttk.Frame(pages)
            cp.columnconfigure(1, weight=1)
            pages.add(cp, text="Copy from another form")
            hint(cp, 0, "Copies submissions FROM the source form below INTO the form in the "
                        "Kobo connection box.")
            field(cp, 1, "Source Kobo server", "copy_server")
            field(cp, 2, "Source Form UID", "copy_uid")
            field(cp, 3, "Source API token", "copy_token", show="•")
            hint(cp, 4, "Leave the source token empty if both forms are in the same account.")
            ttk.Button(cp, text="Match columns", command=self._match_columns).grid(
                row=5, column=0, sticky="w", **pad)
            ttk.Label(cp, foreground="gray",
                      text="One line per column: source = target. Clear the target to "
                           "leave a column out.").grid(row=5, column=1, columnspan=2,
                                                       sticky="w", **pad)
            self.mapping_text = ScrolledText(cp, height=7, font=("Consolas", 9))
            self.mapping_text.grid(row=6, column=0, columnspan=3, sticky="ew", **pad)
            self.mapping_text.insert("1.0", self.settings.get("copy_mapping", ""))
            cbuttons = ttk.Frame(cp)
            cbuttons.grid(row=7, column=0, columnspan=3, sticky="ew", **pad)
            ttk.Button(cbuttons, text="Preview",
                       command=lambda: self._copy(preview=True)).pack(side="left")
            self.copy_btn = ttk.Button(cbuttons, text="Copy all", command=self._copy)
            self.copy_btn.pack(side="left", padx=6)
            self.copy_test_btn = ttk.Button(cbuttons, text="Copy first submission only",
                                            command=lambda: self._copy(limit=1))
            self.copy_test_btn.pack(side="left")

            controls = ttk.Frame(self)
            controls.pack(fill="x", padx=10, pady=4)
            self.stop_btn = ttk.Button(controls, text="Stop", state="disabled",
                                       command=self.stop_flag.set)
            self.stop_btn.pack(side="left")
            self.save_btn = ttk.Button(controls, text="Save results…", state="disabled",
                                       command=self._save_results)
            self.save_btn.pack(side="right")

            self.progress = ttk.Progressbar(self, mode="determinate")
            self.progress.pack(fill="x", padx=10, pady=4)
            self.status = ttk.Label(self, text="Choose a CSV file to start.")
            self.status.pack(fill="x", padx=10)

            self.log = ScrolledText(self, height=10, font=("Consolas", 9), state="disabled")
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
            data = {k: v.get() for k, v in self.vars.items() if not k.endswith("token")}
            data["copy_mapping"] = self.mapping_text.get("1.0", "end").strip()
            try:
                with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
            except OSError:
                pass

        def _set_busy(self, busy, stoppable=False):
            for b in (self.send_btn, self.test_btn, self.update_btn,
                      self.copy_btn, self.copy_test_btn):
                b.configure(state="disabled" if busy else "normal")
            self.save_btn.configure(state="disabled" if busy or not self.results else "normal")
            self.stop_btn.configure(state="normal" if busy and stoppable else "disabled")

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

        def _browse_update(self):
            path = filedialog.askopenfilename(
                title="Choose the CSV with the new values",
                filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
            if path:
                self.vars["update_csv"].set(path)
                self.vars["update_mode"].set("csv")

        def _read_settings(self, need_token):
            """Validate the connection settings. Returns a dict or None after showing an error."""
            v = {k: var.get().strip() for k, var in self.vars.items()}
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
            v["server"] = server_url(v["kobo_url"])
            return v

        def _read_inputs(self, need_token):
            """Validate the import page. Returns a dict or None after showing an error."""
            v = self._read_settings(need_token)
            if not v:
                return None
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

        def _start(self, target, args, total, title):
            """Run a sending worker in the background with the progress bar."""
            self.results = []
            self.stop_flag.clear()
            self.progress.configure(maximum=total, value=0)
            self._set_busy(True, stoppable=True)
            self._write(title, clear=True)
            threading.Thread(target=target, args=args, daemon=True).start()

        # ---------- import page ----------
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
            payloads = [build_submission(row, v["asset_uid"], v["column_map"],
                                         v["skip_columns"], v["repeats"], n)
                        for n, row in enumerate(rows, start=1)]
            self._start(self._worker, (v, payloads), len(payloads),
                        f"Sending {len(rows)} row(s) to {v['kobo_url']}")

        def _worker(self, v, payloads):
            """Submit each payload to the form in the connection box."""
            for n, payload in enumerate(payloads, start=1):
                if self.stop_flag.is_set():
                    self.events.put(("stopped", n - 1))
                    return
                status, message = post_submission(v["kobo_url"], v["token"], payload)
                self.events.put(("row", (n, payload["submission"]["meta"]["instanceID"],
                                         status, message)))
                time.sleep(v["delay_ms"] / 1000)
            self.events.put(("done", len(payloads)))

        # ---------- update page ----------
        def _load_columns(self):
            v = self._read_settings(need_token=True)
            if not v:
                return
            self.status.configure(text="Loading the form's columns from Kobo…")

            def work():
                try:
                    columns = fetch_form_columns(v["server"], v["asset_uid"], v["token"])
                    self.events.put(("columns", columns))
                except KoboError as e:
                    self.events.put(("error", str(e)))

            threading.Thread(target=work, daemon=True).start()

        def _update(self, preview=False):
            v = self._read_settings(need_token=True)
            if not v:
                return
            if v["update_mode"] == "csv":
                if not os.path.isfile(v["update_csv"]):
                    messagebox.showerror("Kobo CSV Import",
                                         "Choose an existing CSV file with the new values.")
                    return
            elif not v["update_column"]:
                messagebox.showerror("Kobo CSV Import",
                                     "Choose the column to change. Click Load columns "
                                     "to list the form's columns.")
                return
            self._save_settings()
            self._set_busy(True)
            self._write("Reading the submissions from Kobo…", clear=True)
            threading.Thread(target=self._plan_worker, args=(v, preview), daemon=True).start()

        def _plan_worker(self, v, preview):
            try:
                if v["update_mode"] == "csv":
                    plan = plan_csv_update(v["server"], v["asset_uid"], v["token"],
                                           v["update_csv"])
                else:
                    plan = plan_replace(v["server"], v["asset_uid"], v["token"],
                                        v["update_column"], v["update_find"],
                                        v["update_value"])
            except (KoboError, OSError) as e:
                self.events.put(("error", str(e)))
                return
            self.events.put(("plan", (v, plan, preview)))

        def _show_plan(self, v, plan, preview):
            self._set_busy(False)
            total = plan["total"]
            if v["update_mode"] == "csv":
                head = f"{total} submission(s) will be changed from {os.path.basename(v['update_csv'])}"
            elif v["update_find"]:
                head = (f"{total} submission(s) have {v['update_column']} = "
                        f"{v['update_find']!r} and will get {v['update_value']!r}")
            else:
                head = (f"All {total} submission(s) will get "
                        f"{v['update_column']} = {v['update_value']!r}")
            self._write(head, clear=True)
            for problem in plan["problems"]:
                self._write("  WARNING: " + problem, "failed")
            if plan["lines"]:
                self._write("")
            for line in plan["lines"][:50]:
                self._write("  " + line)
            if len(plan["lines"]) > 50:
                self._write(f"  … and {len(plan['lines']) - 50} more")
            if not total:
                self.status.configure(text="Nothing to change.")
                return
            if preview:
                self.status.configure(text=f"Ready: {total} submission(s) to change.")
                return
            if not messagebox.askyesno(
                    "Kobo CSV Import",
                    f"Change {total} submission(s) in Kobo?\n\n"
                    "The old values are replaced. Check the preview first."):
                self.status.configure(text="Nothing changed.")
                return
            self._start(self._update_worker, (v, plan["jobs"]), total,
                        f"Updating {total} submission(s) in Kobo")

        def _update_worker(self, v, jobs):
            n = 0
            for ids, data in jobs:
                if self.stop_flag.is_set():
                    self.events.put(("stopped", n))
                    return
                status, message = update_submissions(v["server"], v["asset_uid"],
                                                     v["token"], ids, data)
                for sid in ids:
                    n += 1
                    self.events.put(("updated", (n, sid, status, message)))
                time.sleep(v["delay_ms"] / 1000)
            self.events.put(("done", n))

        # ---------- copy page ----------
        def _read_copy(self):
            """Validate the copy page. Returns a dict or None after showing an error."""
            v = self._read_settings(need_token=True)
            if not v:
                return None
            if not v["copy_server"].startswith("http"):
                messagebox.showerror("Kobo CSV Import", "Enter the source Kobo server, "
                                                        "e.g. https://kf.kobotoolbox.org")
                return None
            if not v["copy_uid"]:
                messagebox.showerror("Kobo CSV Import", "Enter the source Form UID.")
                return None
            v["copy_server"] = server_url(v["copy_server"])
            v["copy_token"] = v["copy_token"] or v["token"]
            v["mapping"] = parse_mapping(self.mapping_text.get("1.0", "end"))
            self._save_settings()
            return v

        def _match_columns(self):
            v = self._read_copy()
            if not v:
                return
            self._set_busy(True)
            self.status.configure(text="Reading both forms from Kobo…")

            def work():
                try:
                    source = fetch_form_columns(v["copy_server"], v["copy_uid"],
                                                v["copy_token"], include_repeats=True)
                    target = fetch_form_columns(v["server"], v["asset_uid"], v["token"],
                                                include_repeats=True)
                except KoboError as e:
                    self.events.put(("error", str(e)))
                    return
                self.events.put(("matched", match_columns(source, target)))

            threading.Thread(target=work, daemon=True).start()

        def _show_matches(self, pairs):
            self._set_busy(False)
            self.mapping_text.delete("1.0", "end")
            self.mapping_text.insert("1.0", format_mapping(pairs))
            missing = [s for s, t in pairs if not t]
            self._write(f"{len(pairs) - len(missing)} of {len(pairs)} source columns matched "
                        f"a target column.", clear=True)
            if missing:
                self._write("No match, will not be copied unless you type a target: "
                            + ", ".join(missing), "failed")
            self.status.configure(text="Check the column list, then click Preview.")

        def _copy(self, preview=False, limit=None):
            v = self._read_copy()
            if not v:
                return
            if not v["mapping"]:
                messagebox.showerror("Kobo CSV Import",
                                     "Click Match columns first, or type the column pairs.")
                return
            self._set_busy(True)
            self._write("Reading the submissions from the source form…", clear=True)
            threading.Thread(target=self._copy_plan_worker, args=(v, preview, limit),
                             daemon=True).start()

        def _copy_plan_worker(self, v, preview, limit):
            try:
                subs = fetch_submissions(v["copy_server"], v["copy_uid"], v["copy_token"])
            except KoboError as e:
                self.events.put(("error", str(e)))
                return
            if limit:
                subs = subs[:limit]
            payloads = [copy_payload(s, v["asset_uid"], v["mapping"]) for s in subs]
            left_out = sorted(unmapped_columns(subs, v["mapping"]))
            self.events.put(("copy_plan", (v, payloads, left_out, preview, limit)))

        def _show_copy_plan(self, v, payloads, left_out, preview, limit):
            self._set_busy(False)
            self._write(f"{len(payloads)} submission(s) to copy into form {v['asset_uid']}",
                        clear=True)
            if left_out:
                self._write("Not copied (no target column): " + ", ".join(left_out), "failed")
            if not payloads:
                self.status.configure(text="The source form has no submissions.")
                return
            self._write("\nThe first submission will be sent as:")
            self._write(json.dumps(payloads[0], indent=2, ensure_ascii=False))
            if preview:
                self.status.configure(text=f"Ready: {len(payloads)} submission(s) to copy.")
                return
            if not limit and not messagebox.askyesno(
                    "Kobo CSV Import",
                    f"Copy {len(payloads)} submission(s) into form {v['asset_uid']}?"):
                return
            self._start(self._worker, (v, payloads), len(payloads),
                        f"Copying {len(payloads)} submission(s) to {v['kobo_url']}")

        # ---------- events from the workers ----------
        def _poll_events(self):
            try:
                while True:
                    kind, data = self.events.get_nowait()
                    if kind in ("row", "updated"):
                        n, ident, status, message = data
                        self.results.append(data)
                        label = f"Row {n}" if kind == "row" else f"Submission {ident}"
                        self._write(f"{label}: {status.upper()}  {message}", status)
                        self.progress.configure(value=n)
                        self.status.configure(text=self._summary())
                    elif kind == "plan":
                        self._show_plan(*data)
                    elif kind == "matched":
                        self._show_matches(data)
                    elif kind == "copy_plan":
                        self._show_copy_plan(*data)
                    elif kind == "columns":
                        self.column_box.configure(values=data)
                        self.status.configure(
                            text=f"{len(data)} columns loaded. Pick one from the Column list."
                            if data else "The form has no columns that can be changed.")
                    elif kind == "error":
                        self._set_busy(False)
                        self.status.configure(text="Something went wrong.")
                        self._write(data, "failed")
                        messagebox.showerror("Kobo CSV Import", data)
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
            self._set_busy(False)
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
                w.writerow(["row", "submission", "status", "message"])
                w.writerows(self.results)
            self.status.configure(text=f"Results saved to {os.path.basename(path)}")

    App().mainloop()


if __name__ == "__main__":
    run_app()
