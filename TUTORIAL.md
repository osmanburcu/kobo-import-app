# Kobo CSV Import: Beginner's Guide

This guide shows you how to put rows from a spreadsheet into a KoboToolbox form, one submission per row, without writing any code. It takes about 15 minutes the first time.

## Before you start

You need these five things. Tick each one off as you get it.

- [ ] A Windows computer with an internet connection
- [ ] A KoboToolbox account that is allowed to submit to the form
- [ ] The form, already deployed in Kobo (this is the form that will receive the data)
- [ ] Your data in a spreadsheet, such as Excel
- [ ] About 15 minutes

You do not need to install Python or know any programming.

**Words used in this guide**

| Word | What it means |
| --- | --- |
| CSV file | A simple spreadsheet file that ends in .csv. Excel can save any sheet as one. |
| Column | A vertical line of cells in your spreadsheet, with a name in the first row. |
| Row | One horizontal line of cells. Each row becomes one submission in Kobo. |
| Form UID | A code of about 22 letters and numbers that identifies your form in Kobo. |
| API token | A long secret code that lets the app send data as you. Treat it like a password. |

## Step 1: Get the app

The app is a single file. You do not install it, you just open it.

1. Open the [download page for the latest version](https://github.com/osmanburcu/kobo-import-app/releases/latest) (v1.1.0 at the time of writing).
2. Under **Assets**, click **Kobo-CSV-Import.exe**. Your browser saves it, usually in your **Downloads** folder.
3. Move the file somewhere easy to find, such as your Desktop.
4. Double-click **Kobo-CSV-Import.exe** to open it.
5. The first time, Windows may show a blue box saying **Windows protected your PC**. This happens because the app is not signed by a company. Click **More info**, then **Run anyway**.

The **Kobo CSV Import** window opens. Leave it open: you will fill it in at Step 4.

If your computer blocks .exe files, ask your IT team to allow it, or ask someone technical to run it from source as the [README](README.md#run-from-source) describes.

## Step 2: Find your Kobo details

The app needs three pieces of information from Kobo. Open Notepad and paste each one there as you find it.

1. **Your username.** Sign in at [kf.kobotoolbox.org](https://kf.kobotoolbox.org). Click your account icon in the top-right corner: your username is shown there. It is the short name you sign in with, not your email.
2. **The Form UID.** Open the form that should receive the data. Look at the address bar of your browser. It looks like `https://kf.kobotoolbox.org/#/forms/aNAmpcbU9opH9xxxxtbL3x/summary`. The Form UID is the part between `forms/` and the next `/`. Copy it exactly, capital letters included.
3. **Your API token.** Click your account icon, then **Account settings**, then **Security**. Next to **API key**, click to show it and copy it.

Keep the token private. Anyone who has it can send data as you. The app never saves it, so you will paste it again each time you use the app.

## Step 3: Prepare your CSV file

The first row of your file must use the form's own question names, so Kobo knows which answer goes where. The easiest way to get them right is to let Kobo write that row for you.

1. If the form has no submissions yet, open it in Kobo, click **Open** under **Collect data** and send one test submission.
2. In Kobo, go to the form's **Data** tab, then **Downloads**.
3. Choose these options:
   - Export type: **CSV**
   - Value and header format: **XML values and headers**
   - Group separator: **/** (a forward slash)
4. Click **Export**, then download the file and open it in Excel.
5. Keep the first row (the column names). Delete the test rows below it and paste your own data under the right columns.
6. Save it: **File** → **Save As** → choose **CSV UTF-8 (Comma delimited) (\*.csv)** → **Save**. If Excel asks about keeping the format, click **Yes**.

The first row looks like this, with groups and questions joined by `/`:

```csv
geo/district;geo/village;household/head_name;household/size;_uuid
Sahinbey;Mahalle 1;Ayse;5;6b1f3c2e-1d4a-4c55-9a0b-1f2e3d4c5b6a
```

**Rules for the cells**

- **Choice questions:** type the choice's XML value (its short code, such as `yes` or `option_2`), not the label people see.
- **Select multiple questions:** put all chosen codes in one cell, separated by spaces: `water food shelter`.
- **Empty cells** are skipped, so you can leave unanswered questions blank.
- **Columns starting with `_`, `meta/` or `formhub/`** are Kobo's own information. The app does not send them as answers, so you can leave them in.
- **The `_uuid` column** gives each row an ID. If you send the same file twice, rows already in Kobo show as DUPLICATE instead of being copied again. Rows without it get a new ID.

## Step 4: Fill in the settings

Go back to the **Kobo CSV Import** window. Fill in the **Kobo connection** box at the top, then the **Import new data** page below it.

| Field | What to put there |
| --- | --- |
| Kobo URL | `https://kf.kobotoolbox.org/YOUR-USERNAME/submission?format=json` with **YOUR-USERNAME** replaced by the username from Step 2. |
| Form UID | The Form UID from Step 2. |
| API token | The token from Step 2. It shows as dots, which is normal. |
| Delay between rows (ms) | Leave it at `200`. This short pause between rows keeps Kobo from being overloaded. |
| CSV file | Click **Browse…** and choose the file you saved in Step 3. A preview appears below straight away. |
| Repeat CSV files (optional) | Leave empty, unless your form has repeat groups (see [Forms with repeat groups](#forms-with-repeat-groups)). |
| Column map (JSON) | Leave it as `{}`. You only change it if your column names differ from the form's names. |
| Skip columns | Leave empty. To leave some columns out, type their names separated by commas, such as `start,end,deviceid`. |

The Kobo URL box may already hold someone else's username from when the app was made. Always replace it with yours.

The app remembers everything except the token for next time.

## Step 5: Preview before sending

Click **Preview**. Nothing is sent to Kobo yet. The big box at the bottom shows:

- How many rows and columns your file has.
- **Sent as answers:** the columns that will go to Kobo.
- **Not sent:** the columns left out (Kobo's own columns and any you chose to skip).
- **Row 1 will be sent as:** exactly what the first submission will look like.

Check three things:

1. The number of rows matches your spreadsheet (not counting the first row of names).
2. Every question you want to fill is under **Sent as answers**.
3. The answers in the example look right: words are spelled correctly and are not shifted into the wrong question.

If something looks wrong, fix the file in Excel, save it again as CSV, and click **Preview** again.

## Step 6: Send one test row

Always test with one row before sending everything.

1. Click **Send first row only**.
2. Watch the bottom box. After a few seconds you see one line:
   - **OK** in green: it worked.
   - **DUPLICATE** in orange: this row is already in Kobo.
   - **FAILED** in red: it did not work. The message after it says why; look it up in [When something goes wrong](#when-something-goes-wrong).
3. In Kobo, open the form's **Data** tab and find the new submission. Open it and check every answer is in the right question.

If the test row is wrong, delete it in Kobo, fix your file, and test again.

## Step 7: Send everything and save the results

1. Click **Send to Kobo**, then **Yes** when asked to confirm.
2. Each row gets its own line, and the bar fills up as rows go out. Leave the window open until the bottom line says **Finished**.
3. To pause, click **Stop**. The app finishes the row it is on, then stops. Rows already sent stay in Kobo.
4. Click **Save results…** and save the file. It lists every row with OK, DUPLICATE or FAILED and the reason, so you have a record.

**If some rows failed:** open the results file to see which rows and why. Fix those rows in your CSV and click **Send to Kobo** again. Rows that already went in show as DUPLICATE and are not copied twice, as long as your file has the `_uuid` column.

## Forms with repeat groups

Skip this section if your form has no repeat groups. A repeat group is a set of questions asked several times in one submission, such as one set of questions per household member.

Repeat answers live in separate files, one per repeat group, linked to the main rows by number columns.

1. In Kobo, go to **Data** → **Downloads** and choose:
   - Export type: **XLS**
   - Value and header format: **XML values and headers**
   - Group separator: **/**
   - **Include groups in headers**: on
2. Open the downloaded file in Excel. The first sheet holds the main answers. Each repeat group has its own sheet, named after the group (for example `members`).
3. Click the first sheet and save it as CSV (as in Step 3). This is your main **CSV file**.
4. Click each repeat sheet and save it as its own CSV. Name each file exactly like its sheet, for example `members.csv` and `visits.csv`.
5. Fill in your data on each sheet. Do not rename or delete the columns `_index`, `_parent_index` and `_parent_table_name`: they link each repeat row to its main row.
6. In the app, click **Browse…** next to **Repeat CSV files**. Select all the repeat files at once by holding **Ctrl** while you click each one, then click **Open**.

**How rows are linked:** a repeat row belongs to the main row whose `_index` equals the repeat row's `_parent_index`. For example, a `members` row with `_parent_index` 2 goes into the second household.

Click **Preview** and check each repeat group shows the right number of entries. A red **WARNING** means some repeat rows point to a main row that does not exist; those rows are not sent. Then test with **Send first row only** and open the submission in Kobo to check the repeat entries are there.

To practise first, try the files in [samples/repeat_example](samples/repeat_example) with **Preview**.

## Change data that is already in Kobo

Use the **Update existing data** page to fix answers in submissions already in Kobo, for example to correct a misspelled village name everywhere. The old values are replaced and cannot be brought back from the app, so always preview first.

1. Fill in the **Kobo connection** box as in Step 4.
2. Click the **Update existing data** tab.
3. Choose how to change the data:
   - **Find and replace in one column:** click **Load columns** and pick the question from the **Column** list. In **Replace value**, type the answer to look for, such as `Merkez`. In **With new value**, type what it should become, such as `Center`. To change that question in **every** submission, leave **Replace value** empty.
   - **New values from a CSV file:** use this when each submission gets a different new value. Export the form's data from Kobo (as in Step 3), keep the `_id` column and the columns you want to change, and type the new values. Leave a cell empty to keep its current answer. Save it as CSV and choose it with **Browse…**.
4. Click **Preview changes**. The box at the bottom lists each submission that will change, with its old and new value. Nothing is changed yet.
5. If the list is right, click **Update in Kobo**, then **Yes**. Each submission shows as **OK** or **FAILED**.
6. Open a few submissions in Kobo to check, and click **Save results…** to keep a record.

Answers inside repeat groups cannot be changed this way.

## Copy data from one form to another

Use the **Copy from another form** page to move submissions from one form into another, for example from an old version of a survey into a new one. The two forms can be in different Kobo accounts.

1. In the **Kobo connection** box, fill in the form that should **receive** the data (as in Step 4).
2. Click the **Copy from another form** tab and fill in the form the data comes **from**:
   - **Source Kobo server:** leave `https://kf.kobotoolbox.org` unless the form is on another Kobo server.
   - **Source Form UID:** found the same way as in Step 2.
   - **Source API token:** the token of the account that owns that form. Leave it empty if both forms are in your account.
3. Click **Match columns**. The big box fills with one line per question, such as `hh/village = household/village`: the left side is the question in the source form, the right side is where it goes in the receiving form. The app pairs questions with the same name even when their groups are named differently.
4. Read the list. If a line ends with `=` and nothing after it, no matching question was found: type the right name after `=`, or leave it empty to skip that question.
5. Click **Preview**. The bottom box shows how many submissions will be copied, any questions left out, and how the first one will look.
6. Click **Copy first submission only** and check it in the receiving form in Kobo.
7. If it looks right, click **Copy all**, then **Yes**.

Copying the same submissions again shows **DUPLICATE** instead of making copies. Photos and other attached files are not copied.

## When something goes wrong

Find the message you see in the app, then follow the fix.

| Message | What it means | What to do |
| --- | --- | --- |
| Choose an existing CSV file. | The app cannot find the file. | Click **Browse…** and choose the file again. |
| Enter the Kobo URL, with {username} replaced… | The Kobo URL box is not filled in correctly. | Paste the URL from Step 4 with your own username. |
| Enter your Kobo API token. | The token box is empty. | Paste your token from Step 2. It is not saved between uses. |
| Column map must be JSON… | The Column map box has a typing mistake. | Change it back to `{}`. |
| HTTP 401 (check the token) | Kobo does not accept your token. | Copy the token again from Kobo and paste it with no extra spaces. When copying between accounts, check the source token belongs to the account that owns the source form. |
| HTTP 403 | Your account cannot submit to this form. | Ask the form's owner to give you permission to add submissions. |
| HTTP 404 | Kobo cannot find the address. | Check your username in the Kobo URL and the Form UID, letter by letter. |
| HTTP 400 | An answer does not fit its question. | Look for text in a number question, a choice label instead of its code, or a date in the wrong format. |
| Could not reach Kobo | No connection to Kobo. | Check your internet. If you use a VPN or office network, try another connection. |
| DUPLICATE | This row is already in Kobo. | Nothing to do: it was not copied twice. |
| WARNING: … \_parent\_index matches no row | Some repeat rows have no main row. | Fix the `_parent_index` numbers in the repeat file. |

Still stuck? Click **Save results…**, and send that file and a screenshot of the app to the person who manages your Kobo forms.
