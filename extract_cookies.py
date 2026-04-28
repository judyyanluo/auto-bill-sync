"""
Cookie Extractor for T-Mobile Session
Run this script locally to extract cookies after manually logging in.
The output can be stored as a GitHub Actions secret.

Usage:
    1. Run: python extract_cookies.py
    2. A browser will open — log in to T-Mobile manually (handle 2FA)
    3. Once you're on the account dashboard, press Enter in the terminal
    4. The script outputs a base64-encoded cookie string
    5. Copy that string and save it as a GitHub secret named TMOBILE_COOKIES
"""

import json
import base64
from playwright.sync_api import sync_playwright


def main():
    with sync_playwright() as p:
        # Launch visible browser so you can log in manually
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
        )
        page = context.new_page()
        page.goto("https://account.t-mobile.com/")

        print("\n" + "=" * 60)
        print("A browser window has opened.")
        print("1. Log in to your T-Mobile account")
        print("2. Complete any 2FA verification")
        print("3. Wait until you see your account dashboard")
        print("4. Then come back here and press Enter")
        print("=" * 60)
        input("\nPress Enter after you've logged in successfully...")

        # Also visit the billing page to capture those cookies
        print("Visiting billing page to capture all cookies...")
        page.goto("https://www.t-mobile.com/bill/summary")
        page.wait_for_load_state("domcontentloaded", timeout=30_000)
        import time
        time.sleep(3)

        # Extract all cookies from the browser context
        all_cookies = context.cookies()
        print(f"\nExtracted {len(all_cookies)} total cookies")

        # Filter to only T-Mobile related cookies to stay under GitHub's 48KB limit
        tmobile_domains = ["t-mobile.com", ".t-mobile.com", "account.t-mobile.com", "www.t-mobile.com"]
        cookies = []
        for c in all_cookies:
            domain = c.get("domain", "")
            if "t-mobile" in domain:
                # Keep only essential fields to minimize size
                cookies.append({
                    "name": c["name"],
                    "value": c["value"],
                    "domain": c["domain"],
                    "path": c.get("path", "/"),
                    "secure": c.get("secure", False),
                    "httpOnly": c.get("httpOnly", False),
                })

        print(f"Filtered to {len(cookies)} T-Mobile cookies")

        # Encode as base64 JSON for safe storage as a GitHub secret
        cookies_json = json.dumps(cookies, separators=(",", ":"))  # compact JSON
        cookies_b64 = base64.b64encode(cookies_json.encode()).decode()
        size_kb = len(cookies_b64) / 1024
        print(f"Encoded size: {size_kb:.1f} KB (GitHub limit: 48 KB)")

        print("\n" + "=" * 60)
        print("COOKIE STRING (copy everything below this line):")
        print("=" * 60)
        print(cookies_b64)
        print("=" * 60)
        print("\nSave this as a GitHub secret named: TMOBILE_COOKIES")
        print("Go to: https://github.com/judyyanluo/auto-bill-sync/settings/secrets/actions")
        print("Click 'New repository secret', name it TMOBILE_COOKIES, paste the value above")
        print("\nNote: Cookies typically expire in 30-90 days.")
        print("Re-run this script when they expire.")

        browser.close()


if __name__ == "__main__":
    main()
