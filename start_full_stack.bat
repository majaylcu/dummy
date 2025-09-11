@echo off
REM Unified Development Server Startup Script for Windows
REM This script starts both FastAPI backend and React frontend simultaneously

title Kite Bot Full Stack Development Environment

echo 🚀 Starting Kite Bot Full Stack Development Environment
echo ======================================================

REM Function to check if a command exists
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo ❌ Python is not installed. Please install Python 3.8+
    pause
    exit /b 1
)

where node >nul 2>&1
if %errorlevel% neq 0 (
    echo ❌ Node.js is not installed. Please install Node.js 16+
    pause
    exit /b 1
)

where npm >nul 2>&1
if %errorlevel% neq 0 (
    echo ❌ npm is not installed. Please install npm
    pause
    exit /b 1
)

echo ✅ Python version:
python --version
echo ✅ Node.js version:
node --version
echo ✅ npm version:
npm --version
echo.

REM Check if virtual environment exists
if not exist ".venv" (
    echo ⚠️ Virtual environment not found. Creating one...
    python -m venv .venv
    echo ✅ Virtual environment created
)

REM Check if frontend directory exists
if not exist "frontend" (
    echo ❌ Frontend directory not found. Please run setup_react_dependencies.bat first
    pause
    exit /b 1
)

echo 📋 Setting up environment variables...

REM Load environment variables if .env exists
if exist ".env" (
    echo ✅ Environment variables file found
) else (
    echo ⚠️ .env file not found. Using defaults
)

REM Create log directory
if not exist "logs" mkdir logs

echo.
echo 🚀 Starting services...
echo =====================

REM Kill any existing processes on our ports (if needed)
echo 🔧 Checking for existing processes...
netstat -ano | findstr ":8000" >nul 2>&1
if %errorlevel% equ 0 (
    echo ⚠️ Port 8000 is in use. You may need to stop existing processes manually.
)

netstat -ano | findstr ":3000" >nul 2>&1
if %errorlevel% equ 0 (
    echo ⚠️ Port 3000 is in use. You may need to stop existing processes manually.
)

REM Start Redis check
echo 🔍 Checking Redis connection...
redis-cli ping >nul 2>&1
if %errorlevel% neq 0 (
    echo ⚠️ Redis not accessible. Please ensure Redis is running.
    echo 💡 Tip: See install_redis_windows.md for setup instructions
) else (
    echo ✅ Redis is running
)

echo.

REM Start Backend
echo 🚀 Starting FastAPI backend...
echo ============================

REM Activate virtual environment for backend
call .venv\Scripts\activate.bat

REM Check if dependencies are installed
if not exist ".venv\dependencies_installed" (
    echo 📦 Installing backend dependencies...
    pip install -r requirements.txt
    echo. > .venv\dependencies_installed
)

REM Run database migrations
echo 📊 Running database migrations...
alembic upgrade head 2>nul || echo ⚠️ Migration failed - database might not be configured

REM Start backend server in background
echo 🌐 Starting FastAPI server on http://localhost:8000
start "Kite Bot Backend" /min cmd /c "uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000 > logs\backend.log 2>&1"

REM Wait a bit for backend to start
timeout /t 5 /nobreak >nul

REM Test backend
echo 🔍 Testing backend connection...
powershell -Command "try { Invoke-RestMethod -Uri 'http://localhost:8000/health' -TimeoutSec 5 | Out-Null; Write-Host '✅ Backend is ready!' } catch { Write-Host '⚠️ Backend might still be starting...' }"

echo.

REM Start Frontend
echo ⚛️ Starting React frontend...
echo ============================

cd frontend

REM Check if dependencies are installed
if not exist "node_modules\.dependencies_installed" (
    echo 📦 Installing frontend dependencies...
    call npm install
    echo. > node_modules\.dependencies_installed
)

REM Start frontend server in background
echo 🌐 Starting React development server on http://localhost:3000
start "Kite Bot Frontend" /min cmd /c "npm start > ..\logs\frontend.log 2>&1"

cd ..

REM Wait a bit for frontend to start
timeout /t 10 /nobreak >nul

echo.
echo ✅ 🎉 Kite Bot Development Environment is ready!
echo ==============================================
echo.
echo 📱 Frontend (React):     http://localhost:3000
echo 🚀 Backend (FastAPI):    http://localhost:8000
echo 📚 API Documentation:    http://localhost:8000/docs
echo 📊 Interactive API:      http://localhost:8000/redoc
echo.
echo 📝 Logs:
echo    Backend:  logs\backend.log
echo    Frontend: logs\frontend.log
echo.
echo 🛑 To stop all services:
echo    Run: stop_dev_servers.bat
echo.

REM Create stop script
(
echo @echo off
echo echo 🛑 Stopping Kite Bot Development Servers...
echo.
echo REM Kill processes by window title
echo taskkill /FI "WINDOWTITLE:Kite Bot Backend*" /F >nul 2>&1
echo taskkill /FI "WINDOWTITLE:Kite Bot Frontend*" /F >nul 2>&1
echo.
echo REM Kill processes on specific ports
echo for /f "tokens=5" %%%%a in ('netstat -ano ^| findstr ":8000"') do taskkill /PID %%%%a /F >nul 2>&1
echo for /f "tokens=5" %%%%a in ('netstat -ano ^| findstr ":3000"') do taskkill /PID %%%%a /F >nul 2>&1
echo.
echo echo ✅ All services stopped!
echo pause
) > stop_dev_servers.bat

REM Open browser windows
echo 🌐 Opening browser windows...
timeout /t 3 /nobreak >nul
start http://localhost:3000
start http://localhost:8000/docs

echo.
echo 💡 Services are running in background windows.
echo 📖 Check the minimized windows for server output.
echo 🛑 Run 'stop_dev_servers.bat' to stop all services.
echo.
echo ⌨️ Press any key to exit this script (services will continue running)...
pause >nul
