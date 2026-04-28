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

MAX_RETRIES      = int(os.environ.get("MAX_RETRIES", "3"))
RETRY_DELAY      = int(os.environ.get("RETRY_DELAY", "30"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    handlers=[
        logging.FileHandler(Path(__file__).parent / "pipeline.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


# Google Drive helpers

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


def _find_input_in_frames(page, selectors, timeout=8_000):
    """Search for an input field in the main page AND all iframes.

    T-Mobile login is often rendered inside an Okta iframe. The old code
    only searched the top-level page, which fails when the email/password
    fields live inside an embedded frame.

    Returns (frame_or_page, selector) on success, or (None, None).
    """
    for sel in selectors:
        try:
            page.wait_for_selector(sel, timeout=timeout, state="visible")
            return page, sel
        except PlaywrightTimeout:
            continue

    for frame in page.frames:
        if frame == page.main_frame:
            continue
        for sel in selectors:
            try:
                frame.wait_for_selector(sel, timeout=3_000, state="visible")
                log.info(f"Found input in iframe: {frame.url}")
                return frame, sel
            except PlaywrightTimeout:
                continue

    return None, None


def _click_in_frames(page, selectors, timeout=5_000):
    """Click the first matching element across main page + iframes."""
    for sel in selectors:
        try:
            page.click(sel, timeout=timeout)
            log.info(f"Clicked using: {sel}")
            return True
        except PlaywrightTimeout:
            continue

    for frame in page.frames:
        if frame == page.main_frame:
            continue
        for sel in selectors:
            try:
                el = frame.wait_for_selector(sel, timeout=3_000, state="visible")
                if el:
                    el.click()
                    log.info(f"Clicked in iframe using: {sel}")
                    return True
            except PlaywrightTimeout:
                continue

    return False


def login_tmobile(page, email, password):
    log.info("Navigating to T-Mobile login ...")
    page.goto("https://account.t-mobile.com/", timeout=60_000)

    try:
        page.click("button:has-text('Accept')", timeout=5_000)
        log.info("Accepted cookie banner")
    except PlaywrightTimeout:
        pass

    # Wait for DOM to be ready -- do NOT use "networkidle" here because
    # T-Mobile page keeps making background requests (analytics, etc.)
    # and will never reach networkidle in CI.
    page.wait_for_load_state("domcontentloaded", timeout=30_000)
    time.sleep(5)
    save_debug_screenshot(page, "01_landing")

    log.info("Page frames: %s", [f.url for f in page.frames])

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
        "input[placeholder*='ID' i]",
        "input[type='text']:visible",
    ]

    context, sel = _find_input_in_frames(page, EMAIL_SELECTORS, timeout=15_000)
    if context is None:
        save_debug_screenshot(page, "02_email_field_not_found")
        log.error("Page frames: %s", [f.url for f in page.frames])
        raise RuntimeError("Could not find email input. Check downloads/02_email_field_not_found.png")

    context.fill(sel, email)
    log.info(f"Filled email using selector: {sel}")
    save_debug_screenshot(page, "02_email_filled")

    NEXT_SELECTORS = [
        "input[type='submit']",
        "button[type='submit']",
        "button:has-text('Next')",
        "button:has-text('Continue')",
        "button:has-text('Sign in')",
        "#okta-signin-submit",
    ]
    _click_in_frames(page, NEXT_SELECTORS)
    page.wait_for_load_state("domcontentloaded", timeout=30_000)
    time.sleep(3)
    save_debug_screenshot(page, "03_after_email_submit")

    PASSWORD_SELECTORS = [
        "#okta-signin-password",
        "input[type='password']",
        "input[name='password']",
        "input[autocomplete='current-password']",
    ]

    context, sel = _find_input_in_frames(page, PASSWORD_SELECTORS, timeout=15_000)
    if context is None:
        save_debug_screenshot(page, "04_password_field_not_found")
        log.error("Page frames: %s", [f.url for f in page.frames])
        raise RuntimeError("Could not find password input. Check downloads/04_password_field_not_found.png")

    context.fill(sel, password)
    log.info(f"Filled password using selector: {sel}")
    save_debug_screenshot(page, "04_password_filled")

    _click_in_frames(page, NEXT_SELECTORS)
    page.wait_for_load_state("domcontentloaded", timeout=45_000)
    time.sleep(5)
    save_debug_screenshot(page, "05_after_login")


# Bill download

def download_bill(email, password):
    """Launch browser, log in, navigate to billing, and download the PDF."""
    DOWNLOAD_DIR.mkdir(exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            accept_downloads=True,
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )
        page = context.new_page()

        try:
            login_tmobile(page, email, password)

            log.info("Navigating to billing page ...")
            page.goto("https://account.t-mobile.com/billing", timeout=30_000)
            page.wait_for_load_state("domcontentloaded", timeout=30_000)
            time.sleep(5)
            save_debug_screenshot(page, "06_billing_page")

            BILL_SELECTORS = [
                "a[href*='pdf']",
                "a[href*='bill']",
                "a:has-text('Download')",
                "a:has-text('View PDF')",
                "a:has-text('View bill')",
                "button:has-text('Download')",
                "button:has-text('View PDF')",
                "a[href*='document']",
            ]

            download_link = None
            for sel in BILL_SELECTORS:
                try:
                    download_link = page.wait_for_selector(sel, timeout=5_000)
                    if download_link:
                        log.info(f"Found download link via selector: {sel}")
                        break
                except PlaywrightTimeout:
                    continue

            if not download_link:
                for frame in page.frames:
                    if frame == page.main_frame:
                        continue
                    for sel in BILL_SELECTORS:
                        try:
                            download_link = frame.wait_for_selector(sel, timeout=3_000)
                            if download_link:
                                log.info(f"Found download link in iframe: {sel}")
                                break
                        except PlaywrightTimeout:
                            continue
                    if download_link:
                        break

            if not download_link:
                screenshot_path = DOWNLOAD_DIR / "debug_screenshot.png"
                page.screenshot(path=str(screenshot_path))
                log.warning(f"Could not find PDF link. Screenshot saved to {screenshot_path}")
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


# Entry point with retry

def main():
    if not TMOBILE_EMAIL or not TMOBILE_PASSWORD:
        log.error("Set TMOBILE_EMAIL and TMOBILE_PASSWORD environment variables.")
        sys.exit(1)

    log.info("=== T-Mobile Bill Pipeline starting ===")

    pdf_path = None
    for attempt in range(1, MAX_RETRIES + 1):
        log.info(f"Attempt {attempt}/{MAX_RETRIES}")
        pdf_path = download_bill(TMOBILE_EMAIL, TMOBILE_PASSWORD)
        if pdf_path:
            break
        if attempt < MAX_RETRIES:
            log.warning(f"Retrying in {RETRY_DELAY}s ...")
            time.sleep(RETRY_DELAY)

    if not pdf_path:
        log.error("Pipeline aborted -- bill not downloaded after %d attempts.", MAX_RETRIES)
        sys.exit(1)

    if DRIVE_FOLDER_ID:
        log.info("Authenticating with Google Drive ...")
        service = get_drive_service()
        url = upload_to_drive(service, pdf_path, DRIVE_FOLDER_ID)
        log.info(f"=== Done! Bill available at: {url} ===")
    else:
        log.info("DRIVE_FOLDER_ID not set, skipping upload.")
        log.info(f"=== Done! Bill saved locally at: {pdf_path} ===")


if __name__ == "__main__":
    main()
