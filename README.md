# Fitmeezo - E-Commerce Platform

> **SHOP SMART • LIVE BETTER**  
> Multi-category e-commerce brand platform for India (INR / ₹) built with Django, Django REST Framework, PostgreSQL, and planned Flutter Android/iOS mobile application.

---

## 🎨 Brand Identity

* **Brand Name**: Fitmeezo
* **Tagline**: SHOP SMART • LIVE BETTER
* **Market**: India
* **Currency**: Indian Rupee (`INR`, symbol `₹`)
* **Color Palette**:
  * **Primary Orange**: `#FF6B00`
  * **Jet Black**: `#121212`
  * **Pure White**: `#FFFFFF`
  * **Light Grey**: `#F5F5F7` / `#E5E7EB`

---

## 📁 Repository Structure

```text
Fitmeezo-website/
├── .env.example              # Template environment variables (never commit real credentials)
├── .gitignore                 # Excludes venv, .env, pycache, local databases, and logs
├── README.md                  # This document - setup and run instructions
├── requirements.txt           # Python dependencies (Django, DRF, psycopg, python-dotenv)
├── backend/                   # Django backend & API application
│   ├── .env.example           # Backend environment template
│   ├── manage.py              # Django management script
│   ├── requirements.txt       # Backend package dependencies
│   ├── db.sqlite3             # Local SQLite database (git-ignored, dev fallback)
│   ├── fitmeezo/              # Project settings & URL routing
│   │   ├── __init__.py
│   │   ├── asgi.py
│   │   ├── settings.py        # Environment-driven Django settings
│   │   ├── urls.py            # Global URL patterns (includes /health/)
│   │   └── wsgi.py
│   ├── core/                  # Core reusable application
│   │   ├── __init__.py
│   │   ├── admin.py
│   │   ├── apps.py            # Fitmeezo Core configuration
│   │   ├── context_processors.py # Brand context variables for templates
│   │   ├── models.py
│   │   ├── tests.py           # Automated tests for health-check endpoint
│   │   ├── urls.py            # Core app routing
│   │   └── views.py           # JSON health check view
│   ├── media/                 # User-uploaded assets (git-ignored)
│   ├── static/                # Static assets (CSS, JS, images)
│   └── templates/             # Django HTML templates
├── docs/                      # Architectural & project documentation
│   └── PROJECT_PLAN.md        # Comprehensive 20-phase roadmap + Flutter app phase
└── mobile_app/                # Reserved placeholder for future Flutter mobile app
    └── README.md
```

---

## 💻 Windows Setup & Run Guide (PowerShell)

### Prerequisites

Ensure the following are installed on Windows:
* **Python**: 3.12 or newer (tested with Python 3.14)
* **pip**: Python package manager
* **Git**: Version control

*Note on PostgreSQL*: PostgreSQL is the intended production database. If PostgreSQL is not installed or configured on your local machine, the application **automatically falls back to SQLite** for local development without manual intervention.

---

### Step 1: Open the Project Directory

Open Windows PowerShell and navigate to the project directory:

```powershell
cd C:\Fitmeezo-website
```

---

### Step 2: Create & Activate Python Virtual Environment

```powershell
# Create the virtual environment
python -m venv .venv

# If running scripts is restricted in PowerShell, run:
# Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

# Activate the virtual environment
.\.venv\Scripts\Activate.ps1
```

*(If using Command Prompt `cmd.exe` instead, use `.\.venv\Scripts\activate.bat`)*

---

### Step 3: Install Project Dependencies

```powershell
python -m pip install --upgrade pip
pip install -r requirements.txt
```

---

### Step 4: Configure Environment Variables

Create your local `.env` file from `.env.example`:

```powershell
Copy-Item .env.example .env
```

By default, `.env` uses `DB_ENGINE=sqlite` for instant local execution.

#### Connecting to PostgreSQL (Optional / Production):
When you have PostgreSQL installed and running:
1. Create a database in PostgreSQL: `CREATE DATABASE fitmeezo_db;`
2. Update `.env`:
   ```env
   DB_ENGINE=postgresql
   DB_NAME=fitmeezo_db
   DB_USER=postgres
   DB_PASSWORD=your_actual_password
   DB_HOST=127.0.0.1
   DB_PORT=5432
   ```

---

### Step 5: Run Database Migrations & System Check

```powershell
# Run Django system checks
python backend\manage.py check

# Apply database migrations
python backend\manage.py migrate
```

---

### Step 6: Run Automated Tests

Execute the automated test suite to verify the `/health/` endpoint and configuration:

```powershell
python backend\manage.py test core
```

---

### Step 7: Launch the Development Server

```powershell
python backend\manage.py runserver
```

Once running, access the following in your browser:
* **Health Check API**: [http://127.0.0.1:8000/health/](http://127.0.0.1:8000/health/)
* **Django Admin**: [http://127.0.0.1:8000/admin/](http://127.0.0.1:8000/admin/)

---

## 🩺 Health Check Endpoint

Request: `GET /health/`  
Response format (`application/json`):

```json
{
  "status": "ok",
  "app": "Fitmeezo",
  "tagline": "SHOP SMART • LIVE BETTER",
  "currency": "INR",
  "database_engine": "sqlite",
  "environment": "development",
  "timestamp": "2026-10-09T16:33:00.000000+00:00"
}
```

---

## 🗺️ Roadmap & Phases

See [docs/PROJECT_PLAN.md](file:///c:/Fitmeezo-website/docs/PROJECT_PLAN.md) for the complete 20-phase website implementation roadmap and the subsequent Flutter mobile application integration phase.
