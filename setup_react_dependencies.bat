@echo off
REM React Dependencies Installation Script for Windows
REM This script installs all required dependencies for the React frontend

echo 🚀 Setting up React Frontend Dependencies for Kite Bot...
echo ==================================================

REM Check if Node.js is installed
node --version >nul 2>&1
if %errorlevel% neq 0 (
    echo ❌ Node.js is not installed!
    echo 📥 Please install Node.js from: https://nodejs.org/
    echo    Recommended version: Node.js 18.x or higher
    pause
    exit /b 1
)

REM Check if npm is installed
npm --version >nul 2>&1
if %errorlevel% neq 0 (
    echo ❌ npm is not installed!
    echo 📥 npm usually comes with Node.js. Please reinstall Node.js.
    pause
    exit /b 1
)

echo ✅ Node.js version:
node --version
echo ✅ npm version:
npm --version
echo.

REM Navigate to frontend directory
if exist "frontend" (
    cd frontend
    echo 📁 Changed to frontend directory
) else (
    echo ❌ Frontend directory not found!
    echo 📁 Creating frontend directory...
    mkdir frontend
    cd frontend
)

REM Check if package.json exists
if not exist "package.json" (
    echo 📦 Initializing new React project...
    npx create-react-app . --template typescript
    echo ✅ React TypeScript project created
)

echo.
echo 📦 Installing Core React Dependencies...
echo =======================================

REM Core React dependencies
call npm install react@^18.2.0 react-dom@^18.2.0

REM React Router for navigation
call npm install react-router-dom@^6.8.0

REM State Management
call npm install @reduxjs/toolkit@^1.9.0 react-redux@^8.0.0

REM UI Component Libraries
call npm install @mui/material@^5.11.0 @emotion/react@^11.10.0 @emotion/styled@^11.10.0
call npm install @mui/icons-material@^5.11.0
call npm install @mui/x-date-pickers@^5.0.0

REM HTTP Client
call npm install axios@^1.3.0

REM WebSocket client for real-time data
call npm install socket.io-client@^4.6.0

REM Charts and Data Visualization
call npm install recharts@^2.5.0
call npm install @nivo/core@^0.80.0 @nivo/line@^0.80.0

REM Form Handling and Validation
call npm install react-hook-form@^7.43.0
call npm install yup@^1.0.0 @hookform/resolvers@^2.9.0

REM Date/Time Utilities
call npm install date-fns@^2.29.0

REM Notifications/Toasts
call npm install react-hot-toast@^2.4.0

REM Loading and Progress Indicators
call npm install nprogress@^0.2.0

REM Utility Libraries
call npm install lodash@^4.17.21
call npm install classnames@^2.3.0

echo.
echo 🛠️ Installing Development Dependencies...
echo ========================================

REM TypeScript types
call npm install --save-dev @types/react@^18.0.0 @types/react-dom@^18.0.0
call npm install --save-dev @types/node@^18.0.0
call npm install --save-dev @types/lodash@^4.14.0
call npm install --save-dev @types/nprogress@^0.2.0

REM Testing libraries
call npm install --save-dev @testing-library/react@^13.4.0
call npm install --save-dev @testing-library/jest-dom@^5.16.0
call npm install --save-dev @testing-library/user-event@^14.4.0

REM ESLint and Prettier for code quality
call npm install --save-dev eslint@^8.0.0
call npm install --save-dev prettier@^2.8.0
call npm install --save-dev eslint-config-prettier@^8.6.0
call npm install --save-dev eslint-plugin-prettier@^4.2.0

REM Additional ESLint plugins for React and TypeScript
call npm install --save-dev @typescript-eslint/eslint-plugin@^5.52.0
call npm install --save-dev @typescript-eslint/parser@^5.52.0
call npm install --save-dev eslint-plugin-react@^7.32.0
call npm install --save-dev eslint-plugin-react-hooks@^4.6.0

echo.
echo ⚙️ Creating Configuration Files...
echo ==================================

REM Create .eslintrc.json
(
echo {
echo   "extends": [
echo     "react-app",
echo     "react-app/jest",
echo     "@typescript-eslint/recommended",
echo     "prettier"
echo   ],
echo   "parser": "@typescript-eslint/parser",
echo   "plugins": ["@typescript-eslint", "prettier"],
echo   "rules": {
echo     "prettier/prettier": "error",
echo     "@typescript-eslint/no-unused-vars": "warn",
echo     "@typescript-eslint/explicit-function-return-type": "off",
echo     "react-hooks/exhaustive-deps": "warn"
echo   }
echo }
) > .eslintrc.json

REM Create .prettierrc
(
echo {
echo   "semi": true,
echo   "trailingComma": "es5",
echo   "singleQuote": true,
echo   "printWidth": 80,
echo   "tabWidth": 2,
echo   "useTabs": false
echo }
) > .prettierrc

REM Create environment variables template
(
echo # React App Configuration
echo REACT_APP_API_BASE_URL=http://localhost:8000
echo REACT_APP_WEBSOCKET_URL=ws://localhost:8000
echo REACT_APP_ENVIRONMENT=development
echo.
echo # Feature Flags
echo REACT_APP_ENABLE_NOTIFICATIONS=true
echo REACT_APP_ENABLE_CHARTS=true
echo REACT_APP_DEBUG_MODE=true
) > .env.example

REM Create .env.local if it doesn't exist
if not exist ".env.local" (
    copy .env.example .env.local
    echo 📄 Created .env.local from template
)

echo.
echo 🧹 Cleaning npm cache...
call npm cache clean --force

echo.
echo ✅ Installation Summary
echo ======================
echo 📦 Core Dependencies Installed:
echo    - React 18 with TypeScript
echo    - Material-UI for components
echo    - Redux Toolkit for state management
echo    - React Router for navigation
echo    - Axios for HTTP requests
echo    - Socket.IO for WebSocket connections
echo    - Recharts for data visualization
echo    - React Hook Form for form handling
echo.
echo 🛠️ Development Tools Configured:
echo    - ESLint for code linting
echo    - Prettier for code formatting
echo    - TypeScript for type safety
echo    - Testing libraries for unit tests
echo.
echo ⚙️ Configuration Files Created:
echo    - .eslintrc.json (ESLint configuration)
echo    - .prettierrc (Prettier configuration)
echo    - .env.example (Environment template)
echo    - .env.local (Local environment)
echo.
echo 🚀 Available Scripts:
echo    npm start          - Start development server
echo    npm run build      - Build for production
echo    npm run lint       - Check code quality
echo    npm run lint:fix   - Fix lint issues
echo    npm run format     - Format code with Prettier
echo    npm run type-check - Check TypeScript types
echo.
echo 🎉 React frontend setup complete!
echo 📂 Frontend files are in: %CD%
echo 🌐 Start development server with: npm start
echo 🔗 Make sure backend is running at: http://localhost:8000
echo.
pause
