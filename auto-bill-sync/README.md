# T-Mobile Bill Auto-Downloader

Automatically logs into T-Mobile, downloads your latest bill PDF,
and uploads it to a Google Drive folder — run monthly via cron or Task Scheduler.

---

## Setup (one-time)

### 1. Install dependencies

```bash
cd tmobile-bill-pipeline
pip install -r requirements.txt
playwright install chromium
```

---

### 2. Set your credentials as environment variables

**macOS / Linux** — add to `~/.zshrc` or `~/.bash_profile`:
```bash
export TMOBILE_EMAIL="you@example.com"
export TMOBILE_PASSWORD="your_password"
export DRIVE_FOLDER_ID="your_google_drive_folder_id"
```
Then run `source ~/.zshrc`.

**Windows** — open PowerShell and run:
```powershell
[System.Environment]::SetEnvironmentVariable("TMOBILE_EMAIL", "you@example.com", "User")
[System.Environment]::SetEnvironmentVariable("TMOBILE_PASSWORD", "your_password", "User")
[System.Environment]::SetEnvironmentVariable("DRIVE_FOLDER_ID", "your_folder_id", "User")
```

> **How to find your Drive folder ID:**
> Open the folder in Google Drive, copy the URL.
> The ID is the long string after `/folders/`:
> `https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz`
>                                          ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

---

### 3. Set up Google Drive API credentials

1. Go to https://console.cloud.google.com/
2. Create a new project (or select an existing one)
3. Go to **APIs & Services → Library** and enable **Google Drive API**
4. Go to **APIs & Services → Credentials → Create Credentials → OAuth client ID**
5. Application type: **Desktop app** — give it any name
6. Download the JSON file and save it as **`google_credentials.json`** in this folder

The first time you run the script, a browser window will open asking you to
authorize the app. After that, a `token.pickle` file stores your auth token
so you won't need to log in again.

---

## Running manually

```bash
python download_bill.py
```

The bill is saved to `./downloads/tmobile_bill_YYYY-MM.pdf` and uploaded to Drive.
Check `pipeline.log` if anything goes wrong.

---

## Schedule to run monthly (automatic)

### macOS / Linux — cron

Open the cron editor:
```bash
crontab -e
```

Add this line to run at 9 AM on the 5th of every month
(T-Mobile typically sends bills around the 1st–3rd):
```
0 9 5 * * cd /full/path/to/tmobile-bill-pipeline && /usr/bin/python3 download_bill.py >> pipeline.log 2>&1
```

Replace `/full/path/to/` with the actual path. Find it with `pwd` inside the folder.

---

### Windows — Task Scheduler

1. Open **Task Scheduler** → **Create Basic Task**
2. Name it "T-Mobile Bill Download"
3. Trigger: **Monthly**, day **5**, time **9:00 AM**
4. Action: **Start a program**
   - Program: `python`
   - Arguments: `C:\full\path\to\download_bill.py`
   - Start in: `C:\full\path\to\tmobile-bill-pipeline\`
5. Finish — the task will run automatically each month

---

## Troubleshooting

| Problem | Fix |
|---|---|
| "Could not find PDF link" | T-Mobile updated their UI. Check `downloads/debug_screenshot.png` to see what the page looks like, then update the selectors in `download_bill.py` |
| Google auth browser doesn't open | Run the script interactively (not headless) the first time |
| "google_credentials.json not found" | Follow Step 3 above |
| Two-factor auth (2FA) prompt | Set `headless=False` in the script, complete 2FA manually the first run, then switch back |

---

## Notes

- Bills are saved as `downloads/tmobile_bill_YYYY-MM.pdf` locally AND uploaded to Drive
- The script never deletes local copies
- All activity is logged to `pipeline.log`
- Your password is never stored in the script — it's read from environment variables
