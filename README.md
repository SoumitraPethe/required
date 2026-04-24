# CDS Vocab Master — Setup Guide
=======================================

## 📁 Project Structure

```
cds_app/
├── app.py              ← Flask backend (run this)
├── requirements.txt    ← Python dependencies
├── words.db            ← SQLite database (auto-created on first run)
└── templates/
    └── index.html      ← Frontend (served by Flask)
```

---

## 🚀 How to Run (Step by Step)

### Step 1 — Install Python
Make sure Python 3.8+ is installed.
Check: open terminal/cmd → type `python --version`

### Step 2 — Install Dependencies
Open terminal in the `cds_app` folder and run:

```bash
pip install -r requirements.txt
```

### Step 3 — Configure Environment Variables

Create a `.env` file in project root (or copy from `.env.example`) and set:

```env
FLASK_SECRET_KEY=replace-with-a-long-random-secret
HUGGINGFACE_API_KEY=your-huggingface-api-key-here
GROQ_API_KEY=your-groq-api-key-here
FLASK_DEBUG=true
FLASK_HOST=127.0.0.1
FLASK_PORT=5000
```

### Step 4 — Run the App

```bash
python app.py
```

You will see:
```
✅  CDS Vocab App running at http://localhost:5000
```

### Step 5 — Open in Browser
Go to: **http://localhost:5000**

That's it! Your words are saved in `words.db` (SQLite).
They persist across browser restarts, different browsers, and even different computers on the same network.

---

## 🌐 Access from Other Computers (Same Network / Office)

To access from another computer in the same office network, change the last line in `app.py`:

```python
# Change this:
app.run(debug=True, port=5000)

# To this (allows access from other machines):
app.run(host='0.0.0.0', debug=False, port=5000)
```

Then from other computers, open: **http://YOUR_PC_IP:5000**
(Find your IP: run `ipconfig` on Windows / `ifconfig` on Mac/Linux)

---

## 🔑 Features

| Feature | How it Works |
|---|---|
| Add words | Library tab → "+ Add Word" |
| Quiz on Synonyms | Quiz tab → 🔵 Synonyms mode |
| Quiz on Antonyms | Quiz tab → 🔴 Antonyms mode |
| Phrase fill-in-blank | Quiz tab → 📖 Phrases mode |
| AI Tutor | AI Tutor tab → needs internet |
| Daily streak heatmap | Quiz tab → GitHub-style green boxes for active quiz days |
| Edit / Delete words | Library tab → hover over any word card |
| Data storage | SQLite DB file (`words.db`) — persistent |

---

## ⚠️ Notes

- The AI Tutor tab needs an internet connection to call Hugging Face / Groq APIs.
- Quiz and Library work fully **offline** — only the SQLite DB is needed.
- Database file `words.db` is auto-created on first run. Back it up regularly.
