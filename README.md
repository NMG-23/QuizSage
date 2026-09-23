<div align="center">
  <h1>🧙‍♂️ QuizSage</h1>
  <p><strong>The Ultimate AI-Powered Google Forms Auto-Solver & Submitter</strong></p>
  <p>
    <i>Built with Playwright, NiceGUI, Groq, and Google Gemini.</i>
  </p>
</div>

---

**QuizSage** is a stealthy, fully automated desktop tool that effortlessly parses, solves, and submits Google Forms quizzes and assignments. Designed with a gorgeous NiceGUI web interface, it leverages lightning-fast LLM routing to save you countless hours of manual data entry and problem-solving.

---

## ✨ Key Features

- **Dual-LLM Cascade Routing**: 
  - **Text-Only Questions**: Handled by **Groq (`llama-3.1-70b-versatile`)** for instantaneous, zero-latency inference.
  - **Image & Multimodal Questions**: Automatically falls back to **Google Gemini (`gemini-3.6-flash`)** if embedded images are detected or if Groq fails.
- **Stealth Browser Automation**: Uses Playwright with anti-detection flags (`AutomationControlled` disabled, randomized typing delays, smooth scrolling) to avoid triggering CAPTCHAs.
- **Persistent Google Sessions**: No need to log in repeatedly! QuizSage maintains a secure, local persistent Chromium profile (`login.py`) so you can bypass restricted form locks seamlessly.
- **Auto-Fill Student Profile**: Detects Name, Roll Number, Branch, Section, and Email fields automatically.
- **Interactive UI Dashboard**: 
  - **Live Audit Table**: Highlights low-confidence AI answers in red so you can double-check the reasoning before submitting.
  - **Form History**: Automatically logs your runs (Submitted, Discarded, Blocked).
  - **Re-Solve Capability**: 1-click re-evaluation of draft forms from your history.
  - **Draft Preservation**: Intelligently leaves your existing manually selected answers untouched if the AI is uncertain or encounters an API error, ensuring safe re-evaluations.
  - **Safe UI Submission**: Thread-synchronized "Submit" and "Discard" buttons directly inside the dashboard.

---

## 🧠 How It Works (The Pipeline)

1. **Pre-Flight Check**: When you paste a URL and hit "Solve", QuizSage normalises the link and checks your local `solved_history.json`. If it's a duplicate, it warns you.
2. **Browser Boot-Up**: It launches a background Playwright worker thread and attaches your persistent Google profile.
3. **Guard Detection & Auto-Fill**: It skips "You've already responded" guard pages, then scans the DOM for standard student identifiers to auto-fill.
4. **Scrape & Solve Loop**:
   - Parses the DOM into `ParsedQuestion` objects (extracting titles, images, and widget types: Radio, Checkbox, Dropdown, Textbox).
   - Bundles the questions and injects your **Subject Context** (e.g. "DBMS").
   - Ships them to the LLM backend.
   - Applies the AI's exact text matches to the correct DOM locators in the browser.
5. **Pagination**: It clicks "Next" and recursively repeats the loop for multi-page forms until it finds the "Submit" button.
6. **Thread Handoff**: The background thread pauses securely for up to 10 minutes, passing control back to your UI to await your manual "Submit" or "Discard" confirmation.

---

## 📦 Installation & Setup

QuizSage requires **Python 3.10+**.

### 1. Clone the Repository
```bash
git clone https://github.com/NMG-23/QuizSage.git
cd QuizSage
```

### 2. Set Up a Virtual Environment (Recommended)
```bash
python -m venv .venv
# On Windows:
.\.venv\Scripts\activate
# On macOS/Linux:
source .venv/bin/activate
```

### 3. Install Dependencies
```bash
pip install -r requirements.txt
playwright install chromium
```

### 4. Configure API Keys
QuizSage uses `python-dotenv` to securely load API keys, protecting you from accidentally pushing secrets to GitHub.

Create a `.env` file in the root directory:
```bash
GROQ_API_KEY=your_groq_key_here
GEMINI_API_KEYS=your_gemini_key_1,your_gemini_key_2
```
> **Pro Tip:** You can supply a comma-separated list of Gemini API keys to enable round-robin rotation, dodging free-tier rate limits!

---

## 🚀 Usage Guide

### Step 1: Initial Google Login (One-Time Only)
To bypass forms restricted to specific domains (like your college email), you must authenticate once.
```bash
python login.py
```
A Chromium window will pop up. Log in to your Google Account. Once logged in, simply close the window. Your session cookies are now securely saved in your local directory!

### Step 2: Launch the Dashboard
```bash
python gui.py
```
A sleek web interface will open at `http://localhost:8080`.

### Step 3: Solving a Form
1. Configure your **Student Profile** on the left and click **Save Profile**.
2. Paste the Google Form URL.
3. Enter a **Subject Context** (e.g., "Physics Midterm") to give the AI domain awareness.
4. Toggle **Auto-Submit** OFF (recommended for safety).
5. Click **Solve with Sage**.
6. Review the AI's logic in the **Audit Table**. Use the **Submit** or **Discard** buttons at the bottom of the table to finalize your run!

### Step 4: History & Re-Solving
Scroll down to the **Form History** card to view your past runs. If you discarded a form or it timed out, simply click the purple **Re-solve** button to overwrite the draft and try again!

---

## 🛡️ License

This project is open-source and licensed under the [MIT License](LICENSE).

---
*Created with ❤️ by Yugank Bhende.*