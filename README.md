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
