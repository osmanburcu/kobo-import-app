# Kobo CSV Import app

A small desktop app that imports data into a KoboToolbox form:
pick a CSV, preview the JSON, and send every row as a submission.

Each column name becomes a field (`geo/p1` → `{"geo": {"p1": ...}}`), so it works
with any form, including repeat groups.

## Download

Get `Kobo-CSV-Import.exe` from the [latest release](../../releases/latest).
It runs on Windows without installing anything.

The exe is not code-signed. On first run Windows may show *Windows protected your PC*:
click **More info** → **Run anyway**.

## Run from source

Needs **Python 3** (from [python.org](https://www.python.org/downloads/); on Windows
tick *Add Python to PATH* during setup). Nothing else needs to be installed.

- **Windows:** double-click `Kobo CSV Import.bat`
- **Any system:** `python kobo_import_app.py`

## Use

1. **CSV file:** click *Browse…* and choose the file. A preview opens straight away.
   If the form has repeat groups, also choose the **Repeat CSV files**, see
   [Repeat groups](#repeat-groups).
2. Check the settings:

   | Field | Value |
   |---|---|
   | Kobo URL | `https://kf.kobotoolbox.org/{username}/submission?format=json`, with your Kobo username |
   | Form UID | UID of the form receiving the data, e.g. `aNAmpcbU9opH9xxxxtbL3x` |
   | API token | Your Kobo API token (Account settings → Security) |
   | Column map | Optional. Renames columns, e.g. `{"context/ccp_id": "geo/ccp_id"}`. Leave `{}` if names already match |
   | Skip columns | Optional. Comma-separated columns not to send, e.g. `start,end,deviceid` |
   | Delay between rows | Pause between rows in milliseconds. `200` is fine |

3. **Preview** lists which columns are sent, which are skipped, and the JSON for the first row.
4. **Send first row only** sends one row as a test. Check it in Kobo.
5. **Send to Kobo** sends all rows. Each row shows as OK (green), DUPLICATE (orange)
   or FAILED (red) with the reason. **Stop** halts after the current row.
6. **Save results…** saves a CSV with the outcome of every row.

The settings are remembered for next time, except the token, which you enter each time.

## CSV rules

- Column names must be the XML names with `/` between groups. When exporting from Kobo,
  choose *XML values and headers* and group separator `/`.
- `,`, `;` and tab separators all work. UTF-8 and Excel's Windows encoding both work.
- **Select multiple questions:** export them as *Single column*. If you create the column
  yourself, use the XML values separated by spaces in one cell, e.g. `option1 option2 option3`.
- Empty cells are not sent. `%20` in a value becomes a space.
- Kobo metadata columns (`_id`, `_submission_time`, `formhub/…`, `meta/…`) are not sent as answers.
- `_uuid` (or `meta/instanceID`) is used as the submission ID, so importing the same file
  twice gives DUPLICATE instead of a copy. Rows without it get a new ID.
## Repeat groups

Kobo puts repeat groups in separate sheets of its **Excel (XLS) export**: the first
sheet has the main submissions, and each repeat group has its own sheet. Give the app
each sheet as a CSV file:

1. In Kobo, export as **XLS** with *XML values and headers*, group separator `/`,
   and *Include groups in headers* on.
2. In Excel, save the **first sheet** as CSV. This is the main **CSV file**.
3. Save **every repeat sheet** as its own CSV, **named exactly like the sheet**
   (the repeat group's name), e.g. `members.csv`, `visits.csv`. Do not rename or
   delete the `_index`, `_parent_index` and `_parent_table_name` columns.
4. In the app, click *Browse…* next to **Repeat CSV files** and select all of them
   at once (hold Ctrl).

How they are linked:

- A repeat row belongs to the main row whose `_index` equals its `_parent_index`.
- **Nested repeats** (a repeat inside a repeat) work too: when `_parent_table_name` is the
  name of another repeat file, the row goes inside that repeat's entry instead.
- The JSON position comes from the column names, e.g. `hh/members/member_name` puts
  each entry in `"hh": {"members": [ {...}, {...} ]}`.

Preview shows each repeat group, how many entries it has, where it will be sent, and
a full JSON example of a row that has repeat entries. It warns about entries whose
`_parent_index` matches no main row; those are not sent.

**Test first** with *Send first row only* and open the submission in Kobo to check the
repeat entries are there.

## Examples

The `samples` folder has files to try the Preview with:

- `sample.csv`: a simple form without repeats (`;` separated)
- `repeat_example/`: a household survey with a `members` repeat, and a `visits`
  repeat inside each member. Choose `main.csv` as the CSV file and `members.csv` +
  `visits.csv` as the repeat files. One member row deliberately has no parent, to
  show the warning.

## Common errors

| Message | Fix |
|---|---|
| `HTTP 401 (check the token)` | Wrong or old token |
| `HTTP 403` | Your account cannot submit to this form |
| `HTTP 404` | Wrong Kobo URL or Form UID |
| `HTTP 400` | A value does not fit the target form's question, e.g. text in a number question |
| `Could not reach Kobo` | No internet, a proxy/VPN blocking it, or a typo in the URL |

## Make a single .exe (optional)

To share the app with people who do not have Python:

```bash
pip install pyinstaller
```

```bash
pyinstaller --onefile --windowed --name "Kobo CSV Import" kobo_import_app.py
```

The program is created in `dist/Kobo CSV Import.exe`.
