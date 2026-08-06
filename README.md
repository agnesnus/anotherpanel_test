# QC Studio

QC Studio is a Streamlit app for QC data import, target tracking, and charting.

This guide shows:
1. How to create a GitHub repository
2. How to upload this project to GitHub
3. How to run and deploy the app with Streamlit
4. How to customize the code for your own panel (including replacing "test")

---

## Prerequisites

1. GitHub account
2. Python installed (3.9+ recommended)
3. VS Code (recommended)

---

## A) Create a New GitHub Repository

### Option 1: Create repo on GitHub website

1. Go to GitHub and click New repository.
2. Enter a repo name (for example: qc-my-panel).
3. Choose Private or Public.
4. Click Create repository.

### Option 2: Create from local folder with Git

From the project folder, run:

```bash
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/<your-username>/<your-repo>.git
git push -u origin main
```

---

## B) Upload Existing Project Files to GitHub

If you created an empty repo on GitHub first, upload these files:

1. qc_unified_app.py
2. requirements.txt
3. README.md

### Upload using GitHub web UI

1. Open your new repository.
2. Click Add file > Upload files.
3. Drag and drop the files.
4. Commit changes.

### Upload using Git (recommended)

```bash
git add .
git commit -m "Upload QC Studio files"
git push
```

---

## C) Run Streamlit Locally

In your project folder:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run qc_unified_app.py
```

Then open the URL shown in terminal, usually:

http://localhost:8501

---

## D) Deploy from GitHub to Streamlit Community Cloud

1. Push latest code to GitHub.
2. Go to https://share.streamlit.io
3. Sign in with GitHub.
4. Click New app.
5. Select repository, branch, and main file: qc_unified_app.py
6. Click Deploy.

---

## E) Make It Specific to Your Panel

If your panel is not "test", rename labels and defaults to your panel name.

### Safe way to replace "test" across code

1. In VS Code, open global Search and Replace.
2. Search for:

```text
test
```

3. Replace with your panel name, for example:

```text
thyroid
```

4. Review each match before replacing (some words may be unrelated).

### Also update these likely fields

1. UI titles and captions
2. Any default DB file names
3. README project description
4. Any old query parameter names in URLs
5. Export labels (if they mention panel or hormone)

### Verify after customization

```bash
python -m py_compile qc_unified_app.py
streamlit run qc_unified_app.py
```

Check:
1. Data import works
2. Targets import works
3. Dashboard charts render correctly
4. Export and report still work

---

## F) Quick Update Workflow

After edits:

```bash
git add .
git commit -m "Panel customization updates"
git push
```

If deployed on Streamlit Cloud, it will auto-redeploy from GitHub.

---

## G) Download or Delete the Database

The app includes built-in database management in the Database page.

### Download database (.db)

1. Open the app.
2. Go to the Database module.
3. In Database File Management, click:

```text
Download current database (.db)
```

4. Save the file to your computer as a backup.

### Delete/reset database from app UI

1. Go to Database module.
2. Open Danger Zone.
3. Tick:

```text
I understand this cannot be undone.
```

4. Click:

```text
Delete database file and reset
```

This permanently removes all imported data from the local database file.

### Delete database from terminal (optional)

From the project folder:

```bash
rm -f test_panel.db
```

If you changed the DB path with QC_STUDIO_DB_PATH, delete that file path instead.

---

## H) What Files Can Be Uploaded?

The app has 2 upload areas in the Database module:

1. Import QC Data (builds database samples/results and charts)
2. Upload Mean/SD Targets File (stores QC mean and SD target lines)

### 1) Import QC Data (CSV or Excel)

Accepted file types:

1. .csv
2. .xls
3. .xlsx

#### CSV format (required fields)

Your CSV should include metadata columns:

1. Type
2. Level
3. Data File
4. Data Path
5. Acq. Date-Time (or equivalent acquisition datetime label)

And analyte result columns named like:

```text
<Analyte Name> Results
```

Notes:

1. Type should be QC for rows to be imported as QC samples.
2. Level should map to High/HQC or Low/LQC.

#### Excel format (supported patterns)

Pattern A: Workbook with one sheet per analyte.

Each analyte sheet should contain:

1. QC run table with Date/Run and RESULT columns for HQC and LQC values
2. HQC and LQC summary blocks with QC mean and SD information

Pattern B: Flat table sheet with columns such as:

1. analyte/compound/name
2. date (or run date)
3. either HQC/LQC value columns, or
4. qc level + concentration/value columns

This upload creates/updates:

1. runs
2. samples
3. analytes
4. results
5. (if present) qc_targets from summary tables

### 2) Upload Mean/SD Targets File (CSV or Excel)

Accepted file types:

1. .csv
2. .xls
3. .xlsx

Minimum required target fields:

1. analyte
2. qc_level (High/HQC or Low/LQC)
3. target_mean (QC mean)
4. target_sd (provided SD)

Optional target fields:

1. effective_from
2. effective_to
3. lot_number

For workbook-style Excel targets, each analyte sheet should have HQC/LQC summary tables where the app can read:

1. QC mean
2. SD (or values from which SD can be derived: ±2SD, ±3SD, or %CV)

### To get correct charts

1. Import QC data file first (for data points).
2. Import targets file (or workbook with summary tables) for mean/2SD/3SD lines.
3. If parser logic changed, re-import the targets file so stored target rows are refreshed.

---

## Troubleshooting

If something looks wrong:

1. Re-check uploaded target values in the app
2. Re-import files after parser changes
3. Run compile check:

```bash
python -m py_compile qc_unified_app.py
```

4. Confirm required libraries are installed:

```bash
pip install -r requirements.txt
```

---

## I) Improvement Ideas, Prompt Quality, Known Errors, and Summary

### 1) What Can Be Improved

#### Documentation improvements
1. Add a **sample input files** folder (`examples/`) with:
   - one valid CSV for QC data
   - one valid Excel workbook for QC data
   - one valid targets file  
2. Add a **data dictionary** table describing required/optional columns, accepted aliases, and examples.
3. Add **screenshots** of each module (Import, Targets, Dashboard, Database) so new users can follow visually.
4. Add a **versioned changelog** (`CHANGELOG.md`) to track parser and schema changes.
5. Add a **FAQ** section for common setup/import/deploy issues.

#### App/UX improvements
1. Add stronger file validation with clear, row-level error messages.
2. Add preview tables before import so users can confirm parsed columns.
3. Add progress bars and import summaries (rows accepted/rejected, analytes detected, target rows loaded).
4. Add one-click “Download template” files for QC and targets uploads.
5. Add confirmation dialogs for any destructive database actions.

#### Reliability improvements
1. Add automated tests for:
   - CSV parser
   - Excel parser (both supported patterns)
   - target extraction logic
2. Add CI checks (GitHub Actions) for:
   - syntax/compile
   - linting
   - tests
3. Add schema migration/version checks so database changes are safer over time.

---

### 2) How to Improve Prompts (for AI-assisted customization)

If you use AI tools (e.g., Copilot/ChatGPT) to modify the project, prompt quality matters a lot.

#### Prompt best practices
1. **Be specific about the file and function**
   - Example: “Update `qc_unified_app.py` function `parse_targets_file()` to accept `%CV` and calculate SD as `mean * (%CV/100)`.”
2. **State constraints clearly**
   - Keep backward compatibility.
   - Do not rename existing DB tables/columns.
   - Preserve Streamlit page layout.
3. **Define expected output format**
   - Request a diff-style response.
   - Ask for exact code block replacements.
4. **Provide acceptance criteria**
   - “Must still pass `python -m py_compile qc_unified_app.py`.”
   - “Must import both CSV and Excel targets without breaking current files.”
5. **Ask for edge-case handling**
   - Missing columns
   - blank cells
   - mixed HQC/LQC labels (`High`, `HQC`, `high`, etc.)
6. **Request tests with the change**
   - “Also generate unit tests for the new parsing branch.”
7. **Iterate in small steps**
   - One change per prompt is safer than many unrelated changes at once.

#### Example strong prompt
```text
In qc_unified_app.py, update target parsing so target_sd can also be derived from %CV when SD is missing.
Rules:
1) If target_sd exists, use it.
2) Else if percent_cv exists and target_mean exists, compute target_sd = target_mean * (percent_cv/100).
3) Preserve existing behavior for CSV and Excel inputs.
4) Add warnings (not crashes) for rows missing both SD and %CV.
5) Show me a minimal patch and include a quick test plan.
```

---

### 3) Existing / Common Errors to Watch For

> Note: These are common failure modes based on the current workflow described in this README.

1. **Environment activation mismatch**
   - `source .venv/bin/activate` works on macOS/Linux, not default Windows shell.
   - Windows alternative:
     - PowerShell: `.venv\Scripts\Activate.ps1`
     - CMD: `.venv\Scripts\activate.bat`

2. **Missing dependencies**
   - Import or runtime errors if `requirements.txt` packages are not installed.
   - Fix:
     ```bash
     pip install -r requirements.txt
     ```

3. **Wrong main file on Streamlit Cloud**
   - Deploy fails if main file is not set to `qc_unified_app.py`.

4. **Column naming mismatches in uploads**
   - CSV/Excel imports may silently fail or partially import if expected columns differ.
   - Fix: align headers to README requirements and standardize level labels.

5. **QC level mapping issues**
   - Inconsistent values (e.g., `HIGH`, `H`, `Level 1`) may not map cleanly to HQC/LQC logic.

6. **Target lines not showing on charts**
   - Usually happens when targets were not imported, imported with missing fields, or require re-import after parser updates.

7. **Database deletion risk**
   - “Delete database file and reset” is irreversible; accidental clicks can remove all local data.

8. **Database file path confusion**
   - If `QC_STUDIO_DB_PATH` is set, users may delete `test_panel.db` but actual DB is elsewhere.

---

### 4) Summary

QC Studio already provides a solid flow for:
1. importing QC data,
2. importing target mean/SD values,
3. charting and tracking results,
4. and managing the local database.

The highest-impact next steps are:
1. improve validation and import feedback,
2. add tests + CI for parser reliability,
3. strengthen docs with templates/examples/screenshots,
4. and use structured AI prompts for safer, faster customization.
