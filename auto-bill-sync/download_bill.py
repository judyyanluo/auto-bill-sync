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

# ─── Configuration ────────────────────────────────────────────────────────────

TMOBILE_EMAIL    = os.environ.get("TMOBILE_EMAIL", "")
TMOBILE_PASSWORD = os.environ.get("TMOBILE_PASSWORD", "")

DRIVE_FOLDER_ID  = os.environ.get("DRIVE_FOLDER_ID", "")   # Google Drive folder ID
DOWNLOAD_DIR     = Path(__file__).parent / "downloads"
CREDENTIALS_FILE = Path(__file__).parent / "google_credentials.json"
TOKEN_FILE       = Path(__file__).parent / "token.pickle"

SCOPES = ["https://www.googleapis.com/auth/drive.file"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(Path(__file__).parent / "pipeline.log"),
    ],
)
log = logging.getLogger(__name__)

# ─── Google Drive helpers ──────────────────────────────────────────────────────

def get_drive_service():
    """Authenticate and return a Google Drive service object."""
    creds = None

    if TOKEN_FILE.exists():
        with open(TOKEN_FILE, "rb") as f:
            creds = pickle.load(f)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                log.error("google_credentials.json not found — see README for setup steps.")
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "wb") as f:
            pickle.dump(creds, f)

    return build("drive", "v3", credentials=creds)


def upload_to_drive(service, file_path: Path, folder_id: str) -> str:
    """Upload a file to Google Drive and return its shareable URL."""
    file_metadata = {
        "name": file_path.name,
        "parents": [folder_id] if folder_id else [],
    }
    media = MediaFileUpload(str(file_path), mimetype="application/pdf", resumable=True)
    result = service.files().create(body=file_metadata, media_body=media, fields="id,webViewLink").execute()
    log.info(f"Uploaded to Drive → {result['webViewLink']}")
    return result["webViewLink"]


# ─── T-Mobile download ─────────────────────────────────────────────────────────

def download_bill(email: str, password: str) -> Path | None:
    """
    Log in to T-Mobile, navigate to billing, and download the latest PDF.
    Returns the path of the downloaded file or None on failure.
    """
    DOWNLOAD_DIR.mkdir(exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        try:
            # 1. Log in
            log.info("Navigating to T-Mobile login …")
            page.goto("https://account.t-mobile.com/", timeout=30_000)
            page.wait_for_load_state("networkidle")

            # Accept cookies if banner appears
            try:
                page.click("button:has-text('Accept')", timeout=4_000)
            except PlaywrightTimeout:
                pass

            page.fill("input[type='email'], input[name='email'], input[id*='email']", email)
            page.click("button[type='submit'], button:has-text('Next'), button:has-text('Continue')")
            page.wait_for_load_state("networkidle")

            page.fill("input[type='password'], input[name='password'], input[id*='pass']", password)
            page.click("button[type='submit'], button:has-text('Sign in'), button:has-text('Log in')")
            page.wait_for_load_state("networkidle")
            time.sleep(3)

            log.info("Logged in — navigating to billing …")

            # 2. Go to billing page
            page.goto("https://account.t-mobile.com/account/bill-pay", timeout=30_000)
            page.wait_for_load_state("networkidle")
            time.sleep(2)

            # 3. Find and click the PDF / View bill button
            pdf_selectors = [
                "a:has-text('View PDF')",
                "a:has-text('Download PDF')",
                "a:has-text('View bill')",
                "button:has-text('View PDF')",
                "a[href*='.pdf']",
            ]

            download_link = None
            for sel in pdf_selectors:
                try:
                    download_link = page.wait_for_selector(sel, timeout=5_000)
                    if download_link:
                        log.info(f"Found download link via selector: {sel}")
                        break
                except PlaywrightTimeout:
                    continue

            if not download_link:
                # Fallback: screenshot for manual inspection
                screenshot_path = DOWNLOAD_DIR / "debug_screenshot.png"
                page.screenshot(path=str(screenshot_path))
                log.warning(f"Could not find PDF link. Screenshot saved to {screenshot_path}")
                return None

            # 4. Download the file
            month_tag = datetime.now().strftime("%Y-%m")
            with page.expect_download() as dl_info:
                download_link.click()
            download = dl_info.value

            dest = DOWNLOAD_DIR / f"tmobile_bill_{month_tag}.pdf"
            download.save_as(str(dest))
            log.info(f"Bill saved to {dest}")
            return dest

        except Exception as e:
            log.error(f"Download failed: {e}", exc_info=True)
            return None
        finally:
            browser.close()


# ─── Entry point ───────────────────────────────────────────────────────────────

def main():
    if not TMOBILE_EMAIL or not TMOBILE_PASSWORD:
        log.error("Set TMOBILE_EMAIL and TMOBILE_PASSWORD environment variables.")
        sys.exit(1)

    log.info("=== T-Mobile Bill Pipeline starting ===")

    # Step 1: Download
    pdf_path = download_bill(TMOBILE_EMAIL, TMOBILE_PASSWORD)
    if not pdf_path:
        log.error("Pipeline aborted — bill not downloaded.")
        sys.exit(1)

    # Step 2: Upload to Drive
    log.info("Authenticating with Google Drive …")
    service = get_drive_service()
    url = upload_to_drive(service, pdf_path, DRIVE_FOLDER_ID)

    log.info(f"=== Done! Bill available at: {url} ===")


if __name__ == "__main__":
    main()
