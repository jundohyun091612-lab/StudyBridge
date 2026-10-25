# StudyBridge

StudyBridge turns a high school student's own class materials into a study system for an upcoming exam.

Upload PDFs, slides, Word files, or photos of handwritten notes. StudyBridge sorts them into chapters, then for each chapter it writes a study guide, makes adaptive quizzes, keeps a spaced review schedule for missed questions, and builds a day-by-day plan that counts down to the exam date. Everything can be exported as a printable Study Book PDF or a Problem Set PDF.

The app and all study data stay on your own computer. Only the AI writing step calls the OpenAI API.

Congressional App Challenge 2026 entry, CA-47.

## Features

- Upload class files: PDF, PowerPoint, Word, text, and photos. Scanned pages are read with Tesseract OCR.
- Automatic sorting of files into chapters
- Study guide in eleven fixed sections per chapter, each tagged as "from your notes" or "AI-written"
- Ask AI follow-up questions on a study guide
- Adaptive quizzes (difficulty moves up above 80% and down below 40%)
- Weak spots: missed questions come back later, worded differently
- Spaced repetition review on a five-box schedule (1 to 30 days)
- Day-by-day study plan built backward from the exam date
- Study Book PDF and Problem Set PDF export

## Built with

Python, FastAPI, SQLite, Jinja2, plain JavaScript, HTML, CSS, PyMuPDF, Tesseract OCR, and the OpenAI API.

## Run it (Windows)

1. Install Python 3 from python.org and check "Add python.exe to PATH" during setup.
2. Optional: install Tesseract OCR for reading photos and scanned pages.
3. Create a file named `OpenAI_Key.txt` in this folder and paste your OpenAI API key into it.
4. Double-click `start.bat`. The first run installs the Python packages, then the app opens at http://127.0.0.1:8080.

On a new install the login page is pre-filled with a local demo account: `demo@studybridge.local` / `demo`.

## AI use

StudyBridge uses the OpenAI API (gpt-4o-mini and gpt-4o) inside the app to write study guides, quizzes, and plans. AI coding assistance was also used during development, as disclosed in the Congressional App Challenge submission.
