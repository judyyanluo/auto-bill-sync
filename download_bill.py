"""
T-Mobile Bill Downloader
Downloads the latest bill PDF and uploads it to Google Drive.
Run manually or schedule with cron / Task Scheduler.
"""

import os
import sys
import time
import json
import base64
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
TMOBILE_COOKIES  = os.environ.get("TMOBILE_COOKIES", "")  # base64-encoded cookies
DRIVE_FOLDER_ID  = os.environ.get("DRIVE_FOLDER_ID", "")
DOWNLOAD_DIR     = Path(__file__).parent / "downloads"
CREDENTIALS_FILE = Path(__file__).parent / "google_credentials.json"
TOKEN_FILE       = Path(__file__).parent / "token.pickle"
SCOPES = ["https://www.googleapis.com/auth/drive.file"]

MAX_RETRIES      = int(os.environ.get("MAX_RETRIES", "3"))
RETRY_DELAY      = int(os.environ.get("RETRY_DELAY", "30"))  # seconds

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    handlers=[
        logging.FileHandler(Path(__file__).parent / "pipeline.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


# ─── Google Drive helpers ─────────────────────────────────────────────────────

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


# ─── T-Mobile login helpers ──────────────────────────────────────────────────

def save_debug_screenshot(page, name="debug"):
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    path = DOWNLOAD_DIR / f"{name}.png"
    page.screenshot(path=str(path), full_page=True)
    log.info(f"Debug screenshot saved: {path}")


def _find_input_in_frames(page, selectors, timeout=8_000):
    """Search for an input field in the main page AND all iframes.

    T-Mobile's login is often rendered inside an Okta iframe.  The old code
    only searched the top-level page, which fails when the email/password
    fields live inside an embedded frame.

    Returns (frame_or_page, selector) on success, or (None, None).
    """
    # 1. Try the top-level page first
    for sel in selectors:
        try:
            page.wait_for_selector(sel, timeout=timeout, state="visible")
            return page, sel
        except PlaywrightTimeout:
            continue

    # 2. Walk every iframe on the page
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

    # Dismiss cookie banner if present
    try:
        page.click("button:has-text('Accept')", timeout=5_000)
        log.info("Accepted cookie banner")
    except PlaywrightTimeout:
        pass

    # Wait for DOM to be ready — do NOT use "networkidle" here because
    # T-Mobile's page keeps making background requests (analytics, etc.)
    # and will never reach networkidle in CI.
    page.wait_for_load_state("domcontentloaded", timeout=30_000)
    time.sleep(5)  # give JS frameworks / iframes extra time to render
    save_debug_screenshot(page, "01_landing")

    # Log all frames for debugging
    log.info("Page frames: %s", [f.url for f in page.frames])

    # ── Email ─────────────────────────────────────────────────────────────
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
        # Broader fallback: any visible text input
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

    # ── Submit email ──────────────────────────────────────────────────────
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

    # ── Handle "Log in with Face ID/Fingerprint" interstitial ──────────
    #    T-Mobile may show a biometric prompt after email. We need to
    #    click "Log in with password" to get to the password field.
    PASSWORD_LINK_SELECTORS = [
        "a:has-text('Log in with password')",
        "button:has-text('Log in with password')",
        "a:has-text('Use password')",
        "button:has-text('Use password')",
        "[data-testid='password-login']",
    ]
    if _click_in_frames(page, PASSWORD_LINK_SELECTORS):
        log.info("Clicked 'Log in with password' link")
        page.wait_for_load_state("domcontentloaded", timeout=30_000)
        time.sleep(3)
        save_debug_screenshot(page, "03b_after_password_link_click")
    else:
        log.info("No biometric interstitial found, proceeding to password field")

    # ── Password ──────────────────────────────────────────────────────────
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

    LOGIN_SELECTORS = [
        "button:has-text('Log in')",
        "input[type='submit']",
        "button[type='submit']",
        "button:has-text('Sign in')",
        "button:has-text('Submit')",
        "#okta-signin-submit",
    ]
    _click_in_frames(page, LOGIN_SELECTORS)
    page.wait_for_load_state("domcontentloaded", timeout=45_000)
    time.sleep(5)
    save_debug_screenshot(page, "05_after_login")


# ─── Bill download ────────────────────────────────────────────────────────────

def download_bill(email, password):
    """Launch browser, log in, navigate to billing, and download the PDF."""
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    LOCAL_COOKIES_FILE = Path(__file__).parent / ".tmobile_cookies.json"

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

        # ── Load saved cookies (env var OR local file) ────────────────
        use_cookies = False

        # Priority 1: env var (base64-encoded, for CI)
        if TMOBILE_COOKIES:
            try:
                cookies_json = base64.b64decode(TMOBILE_COOKIES).decode()
                cookies = json.loads(cookies_json)
                context.add_cookies(cookies)
                log.info(f"Injected {len(cookies)} cookies from env var — skipping login")
                use_cookies = True
            except Exception as e:
                log.warning(f"Failed to load cookies from env var: {e}")

        # Priority 2: local cookie file (for local cron runs)
        if not use_cookies and LOCAL_COOKIES_FILE.exists():
            try:
                cookies = json.loads(LOCAL_COOKIES_FILE.read_text())
                context.add_cookies(cookies)
                log.info(f"Injected {len(cookies)} cookies from local file — skipping login")
                use_cookies = True
            except Exception as e:
                log.warning(f"Failed to load local cookies: {e}")

        page = context.new_page()

        try:
            if not use_cookies:
                login_tmobile(page, email, password)

                # Save cookies locally after successful login for future runs
                try:
                    new_cookies = context.cookies()
                    tmobile_cookies = [c for c in new_cookies if "t-mobile" in c.get("domain", "")]
                    LOCAL_COOKIES_FILE.write_text(json.dumps(tmobile_cookies, indent=2))
                    log.info(f"Saved {len(tmobile_cookies)} cookies to {LOCAL_COOKIES_FILE}")
                except Exception as e:
                    log.warning(f"Could not save cookies: {e}")

            # Navigate to billing page — use the correct URL
            log.info("Navigating to billing page ...")
            page.goto("https://www.t-mobile.com/bill/summary", timeout=30_000)
            page.wait_for_load_state("domcontentloaded", timeout=30_000)
            time.sleep(3)
            save_debug_screenshot(page, "06_billing_page")

            # Check if we got redirected back to login (cookies expired)
            current_url = page.url
            log.info(f"Current URL after navigation: {current_url}")
            if "login" in current_url or "signin" in current_url:
                log.error("Session expired — redirected to login page: %s", current_url)
                if use_cookies:
                    log.error("Cookies are stale. Re-run extract_cookies.py to refresh.")
                raise RuntimeError("Not authenticated — landed on login page instead of billing")

            # Look for the "Download" link on the bill summary page
            # Use short timeouts (2s each) to avoid session expiry
            BILL_SELECTORS = [
                "a:has-text('Download summary bill')",
                "a:has-text('Download bill')",
                "a:has-text('Download')",
                "button:has-text('Download summary bill')",
                "button:has-text('Download bill')",
                "button:has-text('Download')",
                "a[href*='pdf']",
                "a[href*='bill'][href*='download']",
                "a[href*='document']",
            ]

            download_link = None
            for sel in BILL_SELECTORS:
                try:
                    download_link = page.wait_for_selector(sel, timeout=2_000)
                    if download_link:
                        log.info(f"Found download link via selector: {sel}")
                        break
                except PlaywrightTimeout:
                    continue

            # Try iframes if not found on main page
            if not download_link:
                for frame in page.frames:
                    if frame == page.main_frame:
                        continue
                    for sel in BILL_SELECTORS:
                        try:
                            download_link = frame.wait_for_selector(sel, timeout=2_000)
                            if download_link:
                                log.info(f"Found download link in iframe: {sel}")
                                break
                        except PlaywrightTimeout:
                            continue
                    if download_link:
                        break

            if not download_link:
                save_debug_screenshot(page, "debug_screenshot")
                log.warning("Could not find PDF link. Screenshot saved.")
                return None

            # Download the file
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


# ─── Entry point with retry ──────────────────────────────────────────────────

def main():
    if not TMOBILE_EMAIL or not TMOBILE_PASSWORD:
        log.error("Set TMOBILE_EMAIL and TMOBILE_PASSWORD environment variables.")
        sys.exit(1)

    log.info("=== T-Mobile Bill Pipeline starting ===")

    # Retry loop
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
        log.error("Pipeline aborted — bill not downloaded after %d attempts.", MAX_RETRIES)
        sys.exit(1)

    # Upload to Drive
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
