"""
California Water Service Bill Downloader
Downloads the latest bill PDF and uploads it to OneDrive.
Run manually or schedule with cron / Task Scheduler.

Flow: login → "View Bills" → Transactions page → "View Current Bill" → PDF
"""

import os
import sys
import time
import json
import base64
import logging
from datetime import datetime
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# ─── Configuration ────────────────────────────────────────────────────────────

CALWATER_EMAIL          = os.environ.get("CALWATER_EMAIL", "")
CALWATER_PASSWORD       = os.environ.get("CALWATER_PASSWORD", "")
CALWATER_COOKIES        = os.environ.get("CALWATER_COOKIES", "")  # base64-encoded cookies
ONEDRIVE_CLIENT_ID      = os.environ.get("ONEDRIVE_CLIENT_ID", "")
ONEDRIVE_REFRESH_TOKEN  = os.environ.get("ONEDRIVE_REFRESH_TOKEN", "")
DOWNLOAD_DIR            = Path(__file__).parent / "downloads"


def onedrive_folder_path():
    """Dynamic folder path: tax/<current_year>/Home Office/Water Bill."""
    return f"tax/{datetime.now().year}/Home Office/Water Bill"

MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "3"))
RETRY_DELAY = int(os.environ.get("RETRY_DELAY", "30"))  # seconds

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    handlers=[
        logging.FileHandler(Path(__file__).parent / "pipeline.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


# ─── OneDrive helpers (Microsoft Graph API) ──────────────────────────────────

GRAPH_ROOT = "https://graph.microsoft.com/v1.0/me/drive/root"


def get_onedrive_access_token(client_id, refresh_token):
    """Exchange a refresh token for a short-lived access token."""
    resp = requests.post(
        "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        data={
            "client_id": client_id,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
            "scope": "Files.ReadWrite offline_access",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def ensure_onedrive_folder(access_token, folder_path):
    """Create folder hierarchy in OneDrive if any segment doesn't exist."""
    headers = {"Authorization": f"Bearer {access_token}"}
    parts = [p for p in folder_path.strip("/").split("/") if p]
    parent = ""
    for part in parts:
        path_so_far = f"{parent}/{part}" if parent else part
        check = requests.get(f"{GRAPH_ROOT}:/{path_so_far}", headers=headers, timeout=30)
        if check.status_code == 404:
            create_url = f"{GRAPH_ROOT}:/{parent}:/children" if parent else f"{GRAPH_ROOT}/children"
            resp = requests.post(
                create_url,
                headers={**headers, "Content-Type": "application/json"},
                json={"name": part, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"},
                timeout=30,
            )
            resp.raise_for_status()
            log.info(f"Created OneDrive folder: {path_so_far}")
        elif not check.ok:
            check.raise_for_status()
        parent = path_so_far


def bill_already_uploaded(access_token, folder_path, year, month):
    """Return True if any file matching `<year>-<month>-* Water.pdf` exists in folder."""
    headers = {"Authorization": f"Bearer {access_token}"}
    resp = requests.get(f"{GRAPH_ROOT}:/{folder_path}:/children", headers=headers, timeout=30)
    if resp.status_code == 404:
        return False
    resp.raise_for_status()
    prefix = f"{year}-{month:02d}-"
    for item in resp.json().get("value", []):
        name = item.get("name", "")
        if name.startswith(prefix) and name.endswith(" Water.pdf"):
            log.info(f"Bill already in OneDrive: {name}")
            return True
    return False


def upload_to_onedrive(file_path, access_token, folder_path):
    """Upload a file to OneDrive at /<folder_path>/<filename>."""
    ensure_onedrive_folder(access_token, folder_path)
    filename = file_path.name
    upload_path = f"{folder_path.strip('/')}/{filename}"
    url = f"{GRAPH_ROOT}:/{upload_path}:/content"

    with open(file_path, "rb") as f:
        resp = requests.put(
            url,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/pdf",
            },
            data=f.read(),
            timeout=60,
        )
    resp.raise_for_status()
    web_url = resp.json().get("webUrl", "")
    log.info(f"Uploaded to OneDrive: {web_url}")
    return web_url


# ─── Bill date extraction ────────────────────────────────────────────────────

import re
from datetime import date

_DATE_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),\s*(\d{4})",
    re.IGNORECASE,
)
_MONTHS = {m: i + 1 for i, m in enumerate([
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
])}


def _extract_bill_date(link_locator):
    """Walk up from the View Current Bill link to its row and extract a date.

    Returns a `date` object or None if it can't find one.
    """
    try:
        # Get the text of the closest row/article ancestor that has more text
        row_text = link_locator.evaluate(
            "el => { let n = el; while (n && !['TR','LI','ARTICLE'].includes(n.tagName) && n.parentElement) n = n.parentElement; return n ? n.innerText : ''; }"
        )
    except Exception as e:
        log.warning(f"Failed to read row text: {e}")
        return None

    if not row_text:
        return None
    m = _DATE_RE.search(row_text)
    if not m:
        return None
    month = _MONTHS[m.group(1).lower()]
    return date(int(m.group(3)), month, int(m.group(2)))


# ─── Cal Water login helpers ──────────────────────────────────────────────────

def save_debug_screenshot(page, name="debug"):
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    path = DOWNLOAD_DIR / f"calwater_{name}.png"
    page.screenshot(path=str(path), full_page=True)
    log.info(f"Debug screenshot saved: {path}")


def _find_input_in_frames(page, selectors, timeout=8_000):
    """Search for an input field in the main page AND all iframes."""
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


def login_calwater(page, email, password):
    log.info("Navigating to Cal Water login ...")
    page.goto("https://myaccount.calwater.com/", timeout=60_000)
    page.wait_for_load_state("domcontentloaded", timeout=30_000)
    time.sleep(3)
    save_debug_screenshot(page, "01_landing")

    log.info("Page frames: %s", [f.url for f in page.frames])

    # ── Email / Username ──────────────────────────────────────────────────
    EMAIL_SELECTORS = [
        "input[type='email']",
        "input[name='email']",
        "input[name='username']",
        "input[id*='email']",
        "input[id*='username']",
        "input[placeholder*='email' i]",
        "input[placeholder*='username' i]",
        "input[type='text']:visible",
    ]

    context, sel = _find_input_in_frames(page, EMAIL_SELECTORS, timeout=15_000)
    if context is None:
        save_debug_screenshot(page, "02_email_field_not_found")
        raise RuntimeError("Could not find email input. Check downloads/calwater_02_email_field_not_found.png")

    context.fill(sel, email)
    log.info(f"Filled email using selector: {sel}")
    save_debug_screenshot(page, "02_email_filled")

    # ── Password ──────────────────────────────────────────────────────────
    PASSWORD_SELECTORS = [
        "input[type='password']",
        "input[name='password']",
        "input[autocomplete='current-password']",
        "input[id*='password']",
    ]

    context, sel = _find_input_in_frames(page, PASSWORD_SELECTORS, timeout=10_000)
    if context is None:
        save_debug_screenshot(page, "03_password_field_not_found")
        raise RuntimeError("Could not find password input. Check downloads/calwater_03_password_field_not_found.png")

    context.fill(sel, password)
    log.info(f"Filled password using selector: {sel}")
    save_debug_screenshot(page, "03_password_filled")

    # ── Submit ────────────────────────────────────────────────────────────
    # The "Log In" button is disabled until both fields are filled.
    # Press Enter on the password field — most reliable way to submit.
    context.press(sel, "Enter")
    log.info("Pressed Enter to submit login form")
    page.wait_for_load_state("domcontentloaded", timeout=45_000)
    time.sleep(5)
    save_debug_screenshot(page, "04_after_login")


# ─── Bill download ────────────────────────────────────────────────────────────

def download_water_bill(email, password):
    """Log in to Cal Water, navigate to the latest bill, and download the PDF."""
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    LOCAL_COOKIES_FILE = Path(__file__).parent / ".calwater_cookies.json"

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

        if CALWATER_COOKIES:
            try:
                cookies_json = base64.b64decode(CALWATER_COOKIES).decode()
                cookies = json.loads(cookies_json)
                context.add_cookies(cookies)
                log.info(f"Injected {len(cookies)} cookies from env var — skipping login")
                use_cookies = True
            except Exception as e:
                log.warning(f"Failed to load cookies from env var: {e}")

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
                login_calwater(page, email, password)

                try:
                    new_cookies = context.cookies()
                    calwater_cookies = [c for c in new_cookies if "calwater" in c.get("domain", "")]
                    LOCAL_COOKIES_FILE.write_text(json.dumps(calwater_cookies, indent=2))
                    log.info(f"Saved {len(calwater_cookies)} cookies to {LOCAL_COOKIES_FILE}")
                except Exception as e:
                    log.warning(f"Could not save cookies: {e}")

            # ── Navigate directly to Billing & Payments page ─────────
            # /app/billing is the href of the "Billing & Payments" nav item.
            # Navigating directly avoids relying on JS-rendered dashboard links.
            log.info("Navigating to Billing & Payments page ...")
            page.goto("https://myaccount.calwater.com/app/billing", timeout=30_000)
            page.wait_for_load_state("domcontentloaded", timeout=30_000)
            time.sleep(3)
            save_debug_screenshot(page, "05_billing_page")

            current_url = page.url
            log.info(f"Current URL after navigation: {current_url}")
            # Cal Water shows the login form inline on /app/billing when unauthenticated
            # (URL does NOT change), so check page content too
            page_text = page.inner_text("body")
            if ("login" in current_url or "signin" in current_url or "sign-in" in current_url
                    or "Please login to continue" in page_text
                    or "Log Into Your Account" in page_text):
                log.error("Not authenticated — login form visible on billing page")
                if use_cookies:
                    log.error("Cookies are stale. Delete .calwater_cookies.json and re-run.")
                raise RuntimeError("Not authenticated — login form detected on billing page")

            # ── Click "View Current Bill" (first/latest bill entry) ───
            VIEW_BILL_SELECTORS = [
                "a:has-text('View Current Bill')",
                "a:has-text('View current bill')",
                "a:has-text('View Bill')",
                "a:has-text('View bill')",
            ]

            # Wait for the transactions table to load
            try:
                page.wait_for_selector("table, [class*='transaction']", timeout=10_000)
            except PlaywrightTimeout:
                log.warning("Transactions table did not appear within timeout, proceeding anyway")

            # Find and click the first (most recent) bill link
            download_link = None
            for sel in VIEW_BILL_SELECTORS:
                try:
                    # Use first() to get the most recent bill when multiple exist
                    el = page.locator(sel).first
                    el.wait_for(timeout=3_000, state="visible")
                    download_link = el
                    log.info(f"Found bill link via selector: {sel}")
                    break
                except PlaywrightTimeout:
                    continue

            if not download_link:
                # Walk iframes as fallback
                for frame in page.frames:
                    if frame == page.main_frame:
                        continue
                    for sel in VIEW_BILL_SELECTORS:
                        try:
                            el = frame.wait_for_selector(sel, timeout=2_000, state="visible")
                            if el:
                                download_link = el
                                log.info(f"Found bill link in iframe: {sel}")
                                break
                        except PlaywrightTimeout:
                            continue
                    if download_link:
                        break

            if not download_link:
                save_debug_screenshot(page, "07_bill_link_not_found")
                log.warning("Could not find 'View Current Bill' link. Screenshot saved.")
                return None

            # ── Extract the bill issue date from the same row ─────────
            # Row layout (per screenshot): Type | Date | Account | Details | Amount
            # The link lives in Details, so walk up to the row and read the date cell.
            bill_date = _extract_bill_date(download_link)
            if bill_date:
                log.info(f"Parsed bill issue date: {bill_date.isoformat()}")
                filename = f"{bill_date.isoformat()} Water.pdf"
            else:
                # Fallback: today's date
                fallback = datetime.now().date()
                log.warning(f"Could not parse bill date, using today: {fallback}")
                filename = f"{fallback.isoformat()} Water.pdf"

            dest = DOWNLOAD_DIR / filename
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
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-retry", action="store_true", help="Disable retries (useful for test runs)")
    args = parser.parse_args()

    if not CALWATER_EMAIL or not CALWATER_PASSWORD:
        log.error("Set CALWATER_EMAIL and CALWATER_PASSWORD environment variables.")
        sys.exit(1)

    log.info("=== Cal Water Bill Pipeline starting ===")

    folder_path = onedrive_folder_path()
    log.info(f"Target OneDrive folder: {folder_path}")

    # ── Skip everything if this month's bill is already in OneDrive ──
    onedrive_token = None
    if ONEDRIVE_CLIENT_ID and ONEDRIVE_REFRESH_TOKEN:
        log.info("Authenticating with OneDrive (pre-check) ...")
        onedrive_token = get_onedrive_access_token(ONEDRIVE_CLIENT_ID, ONEDRIVE_REFRESH_TOKEN)
        now = datetime.now()
        if bill_already_uploaded(onedrive_token, folder_path, now.year, now.month):
            log.info("=== Skipping: this month's bill already uploaded ===")
            return
    else:
        log.warning("ONEDRIVE_CLIENT_ID/ONEDRIVE_REFRESH_TOKEN not set; cannot check or upload.")

    retries = 1 if args.no_retry else MAX_RETRIES
    pdf_path = None
    for attempt in range(1, retries + 1):
        log.info(f"Attempt {attempt}/{retries}")
        pdf_path = download_water_bill(CALWATER_EMAIL, CALWATER_PASSWORD)
        if pdf_path:
            break
        if attempt < retries:
            log.warning(f"Retrying in {RETRY_DELAY}s ...")
            time.sleep(RETRY_DELAY)

    if not pdf_path:
        log.error("Pipeline aborted — bill not downloaded after %d attempts.", MAX_RETRIES)
        sys.exit(1)

    if onedrive_token:
        # Refresh token in case the pre-check happened a while ago
        onedrive_token = get_onedrive_access_token(ONEDRIVE_CLIENT_ID, ONEDRIVE_REFRESH_TOKEN)
        url = upload_to_onedrive(pdf_path, onedrive_token, folder_path)
        log.info(f"=== Done! Bill available at: {url} ===")
    else:
        log.info(f"=== Done! Bill saved locally at: {pdf_path} ===")


if __name__ == "__main__":
    main()
