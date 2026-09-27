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

## 🚀 Key Features

- **Intelligent LLM Cascade Routing**: 
  - **Primary**: **Groq** for instantaneous, zero-latency inference on text questions.
  - **Secondary**: Automatically retries on **OpenRouter** if Groq fails or rate-limits.
  - **Tertiary**: Falls back to your **Custom OpenAI-compatible providers**.
  - **Quaternary & Multimodal**: Seamlessly falls back to a **Google Gemini** key-pool for image-based questions or if all other text providers are unavailable.
- **API-Key Health Tracking & Quota Meter**: Built-in automatic failover and live UI tracking for your LLM API usage limits.
- **Answer Cache**: Caches responses to identical questions to save API quota and speed up runs.
- **Markdown Study Notes**: Automatically exports per-quiz markdown study notes containing the questions, AI answers, and confidence scores.
- **Multi-Format Batch Upload**: Upload your quiz queue as `.txt`, `.csv`, or `.xlsx` — QuizSage auto-detects URL, subject, and title columns with case-insensitive header matching and falls back to regex URL extraction for unstructured files.
- **Solved URL Tracking (`solved.json`)**: Every successfully submitted form is atomically saved to a local `solved.json` after each quiz — not at the end of a batch. If your API quota runs out mid-batch or the app crashes, all progress is preserved.
- **Smart Upload Filtering**: On upload, already-solved URLs are auto-skipped and duplicates are removed, with clear notifications for each.
- **Quota Exhaustion Handling**: Detects fatal `429` / `RESOURCE_EXHAUSTED` errors and gracefully stops the batch, preserving all progress. Re-run later to continue from where you left off.
- **Stealth Browser Automation**: Uses Playwright with anti-detection flags (`AutomationControlled` disabled, randomized typing delays, smooth scrolling) to avoid triggering CAPTCHAs.
- **Persistent Google Sessions**: No need to log in repeatedly! QuizSage maintains a secure, local persistent Chromium profile (`login.py`) so you can bypass restricted form locks seamlessly.
- **Robust Student Auto-Fill**: Intelligently detects and clicks matching **Radio buttons, Checkboxes, and Textboxes** for Name, Roll Number, Branch, Section, and Email fields, tagging them so the AI never hallucinates over them.
- **Interactive UI Dashboard**: 
  - **Low-Confidence Retries**: Answers below the confidence warn threshold (0.70, adjustable in Settings) are automatically retried with Gemini, bypass the local answer cache, and are tagged with their confidence scores in the exported Markdown study notes (e.g. `### 1. <question> (confidence 0.65)`).
  - **Form History**: Automatically logs your runs (Submitted, Discarded, Blocked) and includes a Score column to track your success rate.
  - **Re-Solve Capability**: 1-click re-evaluation of draft forms from your history.
  - **Draft Preservation**: Intelligently leaves your existing manually selected answers untouched if the AI is uncertain or encounters an API error, ensuring safe re-evaluations.
  - **Safe UI Submission**: Thread-synchronized "Submit" and "Discard" buttons directly inside the dashboard.

---

## ⚙️ How It Works (The Pipeline)

1. **Upload & Filter**: Upload a `.txt`, `.csv`, or `.xlsx` queue file (or paste URLs directly). QuizSage normalises all URLs, skips those already in `solved.json`, and deduplicates the queue.
2. **Pre-Flight Check**: For each form, QuizSage checks your local `solved_history.json`. If it's a duplicate, it warns you with a dialog.
3. **Browser Boot-Up**: It launches a background Playwright worker thread and attaches your persistent Google profile.
4. **Guard Detection & Auto-Fill**: It skips "You've already responded" guard pages, then scans the DOM for standard student identifiers to auto-fill.
5. **Scrape & Solve Loop**:
   - Parses the DOM into `ParsedQuestion` objects (extracting titles, images, and widget types: Radio, Checkbox, Dropdown, Textbox).
   - Bundles the questions and injects your **Subject Context** (e.g. "DBMS").
   - Ships them to the LLM backend via the intelligent cascade pipeline.
   - Applies the AI's exact text matches to the correct DOM locators in the browser.
6. **Pagination**: It clicks "Next" and recursively repeats the loop for multi-page forms until it finds the "Submit" button.
7. **Thread Handoff**: The background thread pauses securely for up to 10 minutes, passing control back to your UI to await your manual "Submit" or "Discard" confirmation.
8. **Atomic Save**: On confirmed submission, the URL is immediately written to `solved.json` (atomic write via temp file + `os.replace`). If quota runs out mid-batch, all prior progress is safe.
9. **Post-Batch Prune**: After the batch finishes (or stops due to quota), the in-memory queue is pruned against `solved.json` so re-running picks up exactly where you left off.


---

## 🛠️ Installation & Setup

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
The primary way to manage API keys is directly within the QuizSage **Settings UI**. Keys are stored securely in `QuizSage_Data/settings.json` and are masked by default with a show/hide toggle. 

For local development only, QuizSage also supports a `.env` file as a fallback. (Note: The frozen `.exe` build ignores the `.env` file entirely.)
> **Pro Tip:** You can supply multiple Gemini API keys in the UI to enable round-robin rotation, dodging free-tier rate limits!

## 🖥️ Desktop Build (Windows)

QuizSage can be built into a standalone `.exe` using PyInstaller:
```bash
python build_exe.py
```
This produces a single `dist/QuizSage.exe`. All application state (profiles, history, cache, API keys) lives in a portable `QuizSage_Data` folder (located in the repo root during dev, or beside the `.exe` when frozen). Chromium is not bundled to save space — instead, it is automatically installed into `QuizSage_Data/browsers` on first launch. If the default port 8080 is taken, the app automatically moves to the next free port and prints the actual URL to the console.

---

## 📖 Usage Guide

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

### Step 3: Solving Forms
1. Configure your **Student Profile** on the left and click **Save Profile**.
2. Upload a queue file (`.txt`, `.csv`, or `.xlsx`) or paste form URLs directly.
   - `.txt`: Subject headings followed by URLs (same format as before).
   - `.csv` / `.xlsx`: Columns are auto-detected — use headers like `url`/`form_url`/`link`, `subject`/`class`, and `title`.
3. Already-solved URLs are automatically skipped; duplicates are removed.
4. Enter a **Subject Context** (e.g., "Physics Midterm") as a fallback for forms without a subject heading.
5. Toggle **Auto-Submit** OFF (recommended for safety).
6. Click **Solve with Sage**.
7. Review the run and use the **Submit** or **Discard** buttons in the dashboard to finalize your form!
8. If quota runs out mid-batch, QuizSage stops gracefully — all submitted forms are saved. Re-upload and re-run to continue.

### Step 4: History & Re-Solving
Scroll down to the **Form History** card to view your past runs. If you discarded a form or it timed out, simply click the purple **Re-solve** button to overwrite the draft and try again!

---

## ⚖️ License

This project is open-source and licensed under the [MIT License](LICENSE).

*Created with ❤️ by QuizSage Contributors.*