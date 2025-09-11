@echo off
REM FastAPI Backend Dependencies Installation Script for Windows
REM This script installs all required dependencies for the FastAPI backend

echo 🚀 Setting up FastAPI Backend Dependencies for Kite Bot...
echo =======================================================

REM Check if Python is installed
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo ❌ Python is not installed!
    echo 📥 Please install Python 3.8+ from: https://www.python.org/
    pause
    exit /b 1
)

REM Check Python version
echo ✅ Python version:
python --version

REM Check if pip is installed
pip --version >nul 2>&1
if %errorlevel% neq 0 (
    echo ❌ pip is not installed!
    echo 📥 Please install pip or reinstall Python with pip included
    pause
    exit /b 1
)

echo ✅ pip version:
pip --version
echo.

REM Create virtual environment if it doesn't exist
if not exist ".venv" (
    echo 🔧 Creating virtual environment...
    python -m venv .venv
    echo ✅ Virtual environment created: .venv
) else (
    echo ✅ Virtual environment already exists
)

REM Activate virtual environment
echo 🔄 Activating virtual environment...
call .venv\Scripts\activate.bat

REM Upgrade pip
echo ⬆️ Upgrading pip...
python -m pip install --upgrade pip

echo.
echo 📦 Installing Core FastAPI Dependencies...
echo =========================================

REM Core FastAPI and web framework
call pip install fastapi==0.104.1
call pip install uvicorn[standard]==0.24.0

REM Database ORM and migrations
call pip install sqlalchemy==2.0.23
call pip install alembic==1.12.1
call pip install psycopg2-binary==2.9.9

REM Authentication and security
call pip install python-jose[cryptography]==3.3.0
call pip install passlib[bcrypt]==1.7.4
call pip install python-multipart==0.0.6

REM Redis for caching and real-time data
call pip install redis==5.0.1
call pip install aioredis==2.0.1

REM HTTP client for external APIs
call pip install httpx==0.25.2
call pip install requests==2.31.0

REM Data validation and serialization
call pip install pydantic==2.4.2
call pip install pydantic-settings==2.0.3

REM Date and time handling
call pip install python-dateutil==2.8.2
call pip install pytz==2023.3

REM Configuration management
call pip install python-dotenv==1.0.0

REM Background task processing
call pip install celery==5.3.4
call pip install flower==2.0.1

REM WebSocket support
call pip install websockets==12.0

REM Data processing and analysis
call pip install pandas==2.1.3
call pip install numpy==1.24.4

REM Financial data and trading
call pip install kiteconnect==4.2.0
call pip install yfinance==0.2.28

REM Logging and monitoring
call pip install loguru==0.7.2
call pip install prometheus-client==0.19.0

REM Email notifications
call pip install aiosmtplib==3.0.1
call pip install jinja2==3.1.2

echo.
echo 🛠️ Installing Development Dependencies...
echo ========================================

REM Testing framework
call pip install pytest==7.4.3
call pip install pytest-asyncio==0.21.1
call pip install pytest-cov==4.1.0

REM Code quality and formatting
call pip install black==23.11.0
call pip install isort==5.12.0
call pip install flake8==6.1.0
call pip install mypy==1.7.1

REM Development server with auto-reload
call pip install watchdog==3.0.0

REM Database testing
call pip install pytest-postgresql==5.0.0
call pip install factory-boy==3.3.0

REM API documentation
call pip install mkdocs==1.5.3
call pip install mkdocs-material==9.4.8

echo.
echo ⚙️ Creating Configuration Files...
echo ==================================

REM Create requirements.txt
pip freeze > requirements.txt
echo 📄 Created requirements.txt

REM Create requirements-dev.txt
(
echo # Development dependencies
echo pytest==7.4.3
echo pytest-asyncio==0.21.1
echo pytest-cov==4.1.0
echo black==23.11.0
echo isort==5.12.0
echo flake8==6.1.0
echo mypy==1.7.1
echo watchdog==3.0.0
echo pytest-postgresql==5.0.0
echo factory-boy==3.3.0
echo mkdocs==1.5.3
echo mkdocs-material==9.4.8
) > requirements-dev.txt

REM Create .env template if it doesn't exist
if not exist ".env.example" (
(
echo # Database Configuration
echo DB_HOST=localhost
echo DB_PORT=5432
echo DB_NAME=kite_bot_db
echo DB_USER=postgres
echo DB_PASSWORD=your_password
echo.
echo # Redis Configuration
echo REDIS_HOST=localhost
echo REDIS_PORT=6379
echo REDIS_PASSWORD=
echo.
echo # Authentication
echo SECRET_KEY=your-super-secret-key-change-this-in-production
echo ALGORITHM=HS256
echo ACCESS_TOKEN_EXPIRE_MINUTES=30
echo.
echo # Kite Connect API
echo KITE_API_KEY=your_kite_api_key
echo KITE_API_SECRET=your_kite_api_secret
echo KITE_REDIRECT_URL=http://localhost:8000/auth/callback
echo.
echo # External APIs
echo PUSHOVER_TOKEN=your_pushover_token
echo PUSHOVER_USER=your_pushover_user
echo.
echo # Application Settings
echo DEBUG=True
echo LOG_LEVEL=INFO
echo CORS_ORIGINS=["http://localhost:3000", "http://127.0.0.1:3000"]
echo.
echo # Trading Configuration
echo PAPER_TRADING=True
echo DEFAULT_QUANTITY=1
echo RISK_MANAGEMENT_ENABLED=True
echo.
echo # Monitoring
echo ENABLE_METRICS=True
echo METRICS_PORT=8001
) > .env.example
echo 📄 Created .env.example
)

REM Create .env if it doesn't exist
if not exist ".env" (
    copy .env.example .env
    echo 📄 Created .env from template
    echo ⚠️  Please update .env with your actual configuration values
)

REM Create pyproject.toml
(
echo [build-system]
echo requires = ["setuptools>=45", "wheel"]
echo build-backend = "setuptools.build_meta"
echo.
echo [project]
echo name = "kite-bot-fastapi"
echo version = "1.0.0"
echo description = "Automated trading bot with FastAPI backend"
echo authors = [{name = "Kite Bot Team"}]
echo license = {text = "MIT"}
echo requires-python = ">=3.8"
echo.
echo [tool.black]
echo line-length = 88
echo target-version = ['py38']
echo include = '\.pyi?$'
echo extend-exclude = '''
echo /^(
echo   migrations
echo   ^| alembic
echo ^)/
echo '''
echo.
echo [tool.isort]
echo profile = "black"
echo multi_line_output = 3
echo line_length = 88
echo known_first_party = ["backend"]
echo.
echo [tool.mypy]
echo python_version = "3.8"
echo warn_return_any = true
echo warn_unused_configs = true
echo disallow_untyped_defs = true
echo ignore_missing_imports = true
echo.
echo [tool.pytest.ini_options]
echo testpaths = ["tests"]
echo asyncio_mode = "auto"
echo addopts = "--cov=backend --cov-report=html --cov-report=term-missing"
) > pyproject.toml

echo.
echo 📋 Creating Development Scripts...
echo =================================

REM Create development startup script
(
echo @echo off
echo REM Development startup script
echo.
echo echo 🚀 Starting Kite Bot FastAPI Development Server...
echo.
echo REM Activate virtual environment
echo call .venv\Scripts\activate.bat
echo.
echo REM Set development environment
echo set ENVIRONMENT=development
echo set DEBUG=True
echo.
echo REM Run database migrations
echo echo 📊 Running database migrations...
echo alembic upgrade head
echo.
echo REM Start the server
echo echo 🌐 Starting FastAPI server on http://localhost:8000
echo uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000
) > start_dev.bat

REM Create production startup script
(
echo @echo off
echo REM Production startup script
echo.
echo echo 🚀 Starting Kite Bot FastAPI Production Server...
echo.
echo REM Activate virtual environment
echo call .venv\Scripts\activate.bat
echo.
echo REM Set production environment
echo set ENVIRONMENT=production
echo set DEBUG=False
echo.
echo REM Run database migrations
echo echo 📊 Running database migrations...
echo alembic upgrade head
echo.
echo REM Start the server with production settings
echo echo 🌐 Starting FastAPI server...
echo uvicorn backend.main:app --host 0.0.0.0 --port 8000 --workers 4
) > start_prod.bat

echo.
echo ✅ Installation Summary
echo ======================
echo 📦 Core Dependencies Installed:
echo    - FastAPI 0.104.1 (Web framework)
echo    - SQLAlchemy 2.0.23 (Database ORM)
echo    - Redis 5.0.1 (Caching ^& real-time data)
echo    - Uvicorn 0.24.0 (ASGI server)
echo    - KiteConnect 4.2.0 (Trading API)
echo    - PostgreSQL support (psycopg2)
echo.
echo 🛠️ Development Tools Configured:
echo    - pytest (Testing framework)
echo    - black (Code formatting)
echo    - isort (Import sorting)
echo    - flake8 (Code linting)
echo    - mypy (Type checking)
echo.
echo ⚙️ Configuration Files Created:
echo    - requirements.txt (Production dependencies)
echo    - requirements-dev.txt (Development dependencies)
echo    - pyproject.toml (Project configuration)
echo    - .env.example (Environment template)
echo    - start_dev.bat (Development startup)
echo    - start_prod.bat (Production startup)
echo.
echo 🚀 Available Commands:
echo    pip install -r requirements.txt     - Install dependencies
echo    pip install -r requirements-dev.txt - Install dev dependencies
echo    pytest                              - Run tests
echo    black backend/                      - Format code
echo    uvicorn backend.main:app --reload   - Start development server
echo    start_dev.bat                       - Start with full dev setup
echo.
echo 📊 Database Setup:
echo    1. Set up PostgreSQL database
echo    2. Update .env with database credentials
echo    3. Run: alembic upgrade head
echo.
echo 🔧 Redis Setup:
echo    1. Install Redis (see install_redis_windows.md)
echo    2. Update .env with Redis configuration
echo.
echo 🎉 FastAPI backend setup complete!
echo 📂 Backend files are in: backend\
echo 🌐 Start development server with: start_dev.bat
echo 📚 API docs will be available at: http://localhost:8000/docs
echo.
pause
