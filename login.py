import time
from playwright.sync_api import sync_playwright
import config

print("Launching browser with the 'Yugank - College' profile...")
print("Please log in to your Google account in the browser window.")
print("Close the browser window or press Ctrl+C in this terminal when you are done.")

with sync_playwright() as pw:
    context = pw.chromium.launch_persistent_context(
        user_data_dir=config.BROWSER_PROFILE_DIR,
        headless=False,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
        ],
        viewport={"width": 1280, "height": 900},
        locale="en-US",
    )
    
    page = context.pages[0] if context.pages else context.new_page()
    page.goto("https://accounts.google.com/signin")
    
    try:
        # Keep the script running until the user closes the browser context
        context.wait_for_event("close", timeout=0)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        # Playwright might raise an error when the browser is manually closed, which is fine here.
        pass

print("Browser closed. Your session is now saved!")
