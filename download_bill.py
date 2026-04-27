"""
T-Mobile Bill Downloader
Downloads the latest bill PDF and uploads it to Google Drive.
Run manually or schedule with cron / Task Scheduler.
"""

import os
import sys
import time
import logging
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
import pickle

# Configuration
TMOBILE_EMAIL    = os.environ.get("TMOBILE_EMAIL", "")
TMOBILE_PASSWORD = os.environ.get("TMOBILE_PASSWORD", "")
DRIVE_FOLDER_ID  = os.environ.get("DRIVE_FOLDER_ID", "")
DOWNLOAD_DIR     = Path(__file__).parent / "downloads"
CREDENTIALS_FILE = Path(__file__).parent / "google_credentials.json"
TOKEN_FILE       = Path(__file__).parent / "token.pickle"
SCOPES = ["https://www.googleapis.com/auth/drive.file"]

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    handlers=[
        logging.FileHandler("pipeline.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# Google Drive
def get_drive_service():
    creds = None
    if TOKEN_FILE.exists():
        with open(TOKEN_FILE, "rb") as f:
            creds = pickle.load(f)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "wb") as f:
            pickle.dump(creds, f)
    return build("drive", "v3", credentials=creds)

def file_exists_in_drive(service, folder_id, filename):
    query = f"name='{filename}' and '{folder_id}' in parents and trashed=false"
    results = service.files().list(q=query, fields="files(id, name)").execute()
    files = results.get("files", [])
    if files:
        log.info(f"File '{filename}' already exists in Drive, skipping upload.")
        return True
    return False

def upload_to_drive(service, file_path, folder_id):
    filename = file_path.name
    if folder_id and file_exists_in_drive(service, folder_id, filename):
        return "(already exists in Drive folder)"
    meta = {"name": filename}
    if folder_id:
        meta["parents"] = [folder_id]
    media = MediaFileUpload(str(file_path), mimetype="application/pdf")
    f = service.files().create(body=meta, media_body=media, fields="id,webViewLink").execute()
    log.info(f"Uploaded to Drive: {f.get('webViewLink')}")
    return f.get("webViewLink", "")

# T-Mobile login helpers
def save_debug_screenshot(page, name="debug"):
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    path = DOWNLOAD_DIR / f"{name}.png"
    page.screenshot(path=str(path), full_page=True)
    log.info(f"Debug screenshot saved: {path}")

def login_tmobile(page, email, password):
    log.info("Navigating to T-Mobile login ...")
    page.goto("https://account.t-mobile.com/", timeout=60_000)
    try:
        page.click("button:has-text('Accept')", timeout=5_000)
        log.info("Accepted cookie banner")
    except PlaywrightTimeout:
        pass
    save_debug_screenshot(page, "01_landing")

    EMAIL_SELECTORS = [
        "#okta-signin-username",
        "input[name='identifier']",
        "input[autocomplete='username']",
        "input[type='email']",
        "input[name='email']",
        "input[id*='email']",
        "input[id*='username']",
        "input[placeholder*='email' i]",
        "input[placeholder*='phone' i]",
    ]
    email_filled = False
    for sel in EMAIL_SELECTORS:
        try:
            page.wait_for_selector(sel, timeout=8_000, state="visible")
            page.fill(sel, email)
            log.info(f"Filled email using selector: {sel}")
            email_filled = True
            break
        except PlaywrightTimeout:
            continue
    if not email_filled:
        save_debug_screenshot(page, "02_email_field_not_found")
        raise RuntimeError("Could not find email input. Check downloads/02_email_field_not_found.png")

    save_debug_screenshot(page, "02_email_filled")
    NEXT_SELECTORS = [
        "input[type='submit']",
        "button[type='submit']",
        "button:has-text('Next')",
        "button:has-text('Continue')",
        "button:has-text('Sign in')",
        "#okta-signin-submit",
    ]
    for sel in NEXT_SELECTORS:
        try:
            page.click(sel, timeout=5_000)
            log.info(f"Clicked next/submit using: {sel}")
            break
        except PlaywrightTimeout:
            continue

    page.wait_for_load_state("networkidle", timeout=30_000)
    save_debug_screenshot(page, "03_after_email_submit")

    PASSWORD_SELECTORS = [
        "#okta-signin-password",
        "input[type='password']",
        "input[name='password']",
        "input[autocomplete='current-password']",
    ]
    password_filled = False
    for sel in PASSWORD_SELECTORS:
        try:
            page.wait_for_selector(sel, timeout=10_000, state="visible")
            page.fill(sel, password)
            log.info(f"Filled password using selector: {sel}")
            password_filled = True
            break
        except PlaywrightTimeout:
            continue
    if not password_filled:
        save_debug_screenshot(page, "04_password_field_not_found")
        raise RuntimeError("Could not find password input. Check downloads/04_password_field_not_found.png")

    save_debug_screenshot(page, "04_password_filled")
    for sel in NEXT_SELECTORS:
        try:
            page.click(sel, timeout=5_000)
            log.info(f"Clicked sign-in using: {sel}")
            break
        except PlaywrightTimeout:
            continue

    page.wait_for_load_state("networkidle", timeout=45_000)
    save_debug_screenshot(page, "05_after_login")
    log.info("Login flow completed")

def find_bill_pdf(page):
    PDF_SELECTORS = [
        "a[href*='.pdf']",
        "a:has-text('Download PDF')",
        "a:has-text('Download bill')",
        "a:has-text('View bill')",
        "button:has-text('Download PDF')",
        "button:has-text('Download bill')",
        "[data-testid*='bill'] a",
        "[class*='bill'] a[href*='pdf']",
        "a[href*='bill'][href*='pdf']",
    ]
    BILLING_URLS = [
        "https://account.t-mobile.com/isp/billing/history",
        "https://account.t-mobile.com/isp/billing",
        "https://account.t-mobile.com/billing",
    ]
    for url in BILLING_URLS:
        try:
            log.info(f"Trying billing URL: {url}")
            page.goto(url, timeout=30_000)
            page.wait_for_load_state("networkidle", timeout=20_000)
            save_debug_screenshot(page, f"06_billing_{url.split('/')[-1]}")
            for sel in PDF_SELECTORS:
                try:
                    locator = page.locator(sel).first
                    locator.wait_for(timeout=5_000, state="visible")
                    log.info(f"Found PDF link via: {sel}")
                    return locator
                except PlaywrightTimeout:
                    continue
        except PlaywrightTimeout:
            log.warning(f"Timed out loading {url}")
            continue
    return None

def download_bill(email, password):
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()
        try:
            login_tmobile(page, email, password)
            download_link = find_bill_pdf(page)
            if not download_link:
                save_debug_screenshot(page, "07_pdf_not_found")
                log.warning("Could not find PDF link. Check downloads/07_pdf_not_found.png")
                return None
            month_tag = datetime.now().strftime("%Y-%m")
            dest = DOWNLOAD_DIR / f"tmobile_bill_{month_tag}.pdf"
            with page.expect_download() as dl_info:
                download_link.click()
            download = dl_info.value
            download.save_as(str(dest))
            log.info(f"Bill saved to {dest}")
            return dest
        except Exception as e:
            log.error(f"Download failed: {e}", exc_info=True)
            try:
                save_debug_screenshot(page, "error_state")
            except Exception:
                pass
            return None
        finally:
            browser.close()

def main():
    if not TMOBILE_EMAIL or not TMOBILE_PASSWORD:
        log.error("Set TMOBILE_EMAIL and TMOBILE_PASSWORD environment variables.")
        sys.exit(1)
    log.info("=== T-Mobile Bill Pipeline starting ===")
    pdf_path = download_bill(TMOBILE_EMAIL, TMOBILE_PASSWORD)
    if not pdf_path:
        log.error("Pipeline aborted — bill not downloaded.")
        sys.exit(1)
    log.info("Authenticating with Google Drive ...")
    service = get_drive_service()
    url = upload_to_drive(service, pdf_path, DRIVE_FOLDER_ID)
    log.info(f"=== Done! Bill available at: {url} ===")

if __name__ == "__main__":
    main()
