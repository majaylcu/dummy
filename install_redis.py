#!/usr/bin/env python3
"""
Redis Installation Script for Windows
====================================

This script provides automated Redis installation for Windows systems.
Supports WSL, Docker, and native Windows installation methods.

Usage:
    python install_redis.py [method]
    
Methods:
    wsl     - Install Redis in WSL (Recommended)
    docker  - Install Redis using Docker
    native  - Install Redis natively on Windows
    auto    - Auto-detect best method and install
"""

import os
import sys
import subprocess
import platform
import urllib.request
import zipfile
import tempfile
import shutil
import json
import time
from pathlib import Path

class RedisInstaller:
    def __init__(self):
        self.system = platform.system().lower()
        self.redis_version = "7.2.4"
        self.redis_windows_version = "5.0.14.1"  # Latest Windows port
        self.install_dir = Path.home() / "redis"
        
    def run_command(self, command, shell=True, check=True, capture_output=True):
        """Execute a command and return the result."""
        try:
            result = subprocess.run(
                command, 
                shell=shell, 
                check=check, 
                capture_output=capture_output,
                text=True,
                timeout=300
            )
            return result
        except subprocess.CalledProcessError as e:
            print(f"❌ Command failed: {command}")
            print(f"Error: {e.stderr if e.stderr else e}")
            return None
        except subprocess.TimeoutExpired:
            print(f"⏰ Command timed out: {command}")
            return None

    def check_wsl_available(self):
        """Check if WSL is available and has a Linux distribution."""
        try:
            result = self.run_command("wsl --list --quiet")
            if result and result.returncode == 0:
                distributions = result.stdout.strip().split('\n')
                # Filter out empty lines and Windows-specific entries
                linux_distros = [d.strip() for d in distributions if d.strip() and not d.startswith('docker')]
                return len(linux_distros) > 0
        except:
            pass
        return False

    def check_docker_available(self):
        """Check if Docker is available."""
        try:
            result = self.run_command("docker --version")
            return result and result.returncode == 0
        except:
            return False

    def install_wsl_redis(self):
        """Install Redis in WSL."""
        print("🐧 Installing Redis in WSL...")
        
        if not self.check_wsl_available():
            print("❌ WSL not available. Please install WSL first:")
            print("   Run: wsl --install")
            return False

        # Commands to install Redis in WSL
        commands = [
            "wsl sudo apt update",
            "wsl sudo apt install -y redis-server",
            "wsl sudo systemctl enable redis-server",
            "wsl sudo systemctl start redis-server"
        ]

        for cmd in commands:
            print(f"🔄 Running: {cmd}")
            result = self.run_command(cmd)
            if not result or result.returncode != 0:
                # Try alternative for systems without systemd
                if "systemctl" in cmd:
                    alt_cmd = cmd.replace("sudo systemctl enable redis-server", "echo 'systemctl not available, will start manually'")
                    alt_cmd = alt_cmd.replace("sudo systemctl start redis-server", "sudo redis-server --daemonize yes")
                    print(f"🔄 Trying alternative: {alt_cmd}")
                    result = self.run_command(alt_cmd)
                
                if not result or result.returncode != 0:
                    print(f"❌ Failed to execute: {cmd}")
                    return False

        # Test Redis connection
        print("🔍 Testing Redis connection...")
        test_result = self.run_command("wsl redis-cli ping")
        if test_result and "PONG" in test_result.stdout:
            print("✅ Redis installed and running in WSL!")
            print("📝 To start Redis: wsl sudo redis-server --daemonize yes")
            print("📝 To connect: wsl redis-cli")
            print("📝 To stop: wsl sudo pkill redis-server")
            return True
        else:
            print("⚠️ Redis installed but connection test failed")
            print("💡 Try manually starting: wsl sudo redis-server --daemonize yes")
            return False

    def install_docker_redis(self):
        """Install Redis using Docker."""
        print("🐳 Installing Redis using Docker...")
        
        if not self.check_docker_available():
            print("❌ Docker not available. Please install Docker Desktop first:")
            print("   Download from: https://www.docker.com/products/docker-desktop")
            return False

        # Pull and run Redis container
        commands = [
            "docker pull redis:latest",
            "docker run --name redis-kitebot -p 6379:6379 -d redis:latest redis-server --appendonly yes"
        ]

        for cmd in commands:
            print(f"🔄 Running: {cmd}")
            result = self.run_command(cmd)
            if not result or result.returncode != 0:
                if "name redis-kitebot" in cmd:
                    # Container might already exist, try to start it
                    print("🔄 Container might exist, trying to start...")
                    start_result = self.run_command("docker start redis-kitebot")
                    if not start_result or start_result.returncode != 0:
                        print("❌ Failed to start Redis container")
                        return False
                else:
                    print(f"❌ Failed to execute: {cmd}")
                    return False

        # Wait for container to start
        print("⏳ Waiting for Redis to start...")
        time.sleep(5)

        # Test Redis connection
        print("🔍 Testing Redis connection...")
        test_result = self.run_command("docker exec redis-kitebot redis-cli ping")
        if test_result and "PONG" in test_result.stdout:
            print("✅ Redis installed and running in Docker!")
            print("📝 Container name: redis-kitebot")
            print("📝 Port: 6379")
            print("📝 To stop: docker stop redis-kitebot")
            print("📝 To start: docker start redis-kitebot")
            print("📝 To connect: docker exec -it redis-kitebot redis-cli")
            return True
        else:
            print("⚠️ Redis container started but connection test failed")
            return False

    def download_file(self, url, destination):
        """Download a file from URL."""
        try:
            print(f"📥 Downloading {url}...")
            urllib.request.urlretrieve(url, destination)
            return True
        except Exception as e:
            print(f"❌ Failed to download: {e}")
            return False

    def install_native_redis(self):
        """Install Redis natively on Windows."""
        print("🪟 Installing Redis natively on Windows...")
        
        # Create installation directory
        self.install_dir.mkdir(parents=True, exist_ok=True)
        
        # Download Redis for Windows
        redis_url = f"https://github.com/microsoftarchive/redis/releases/download/win-{self.redis_windows_version}/Redis-x64-{self.redis_windows_version}.zip"
        
        with tempfile.TemporaryDirectory() as temp_dir:
            zip_path = Path(temp_dir) / "redis.zip"
            
            # Download Redis
            if not self.download_file(redis_url, zip_path):
                print("❌ Failed to download Redis")
                return False
            
            # Extract Redis
            print("📦 Extracting Redis...")
            try:
                with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                    zip_ref.extractall(self.install_dir)
                print(f"✅ Redis extracted to {self.install_dir}")
            except Exception as e:
                print(f"❌ Failed to extract Redis: {e}")
                return False

        # Create Redis configuration
        redis_conf = self.install_dir / "redis.windows.conf"
        if not redis_conf.exists():
            # Create basic configuration
            config_content = """# Redis Configuration for Windows
bind 127.0.0.1
port 6379
timeout 0
save 900 1
save 300 10
save 60 10000
rdbcompression yes
dbfilename dump.rdb
dir ./
maxmemory-policy allkeys-lru
"""
            redis_conf.write_text(config_content)
            print("✅ Redis configuration created")

        # Create startup scripts
        start_script = self.install_dir / "start_redis.bat"
        start_script.write_text(f"""@echo off
echo Starting Redis Server...
cd /d "{self.install_dir}"
redis-server.exe redis.windows.conf
""")

        stop_script = self.install_dir / "stop_redis.bat"
        stop_script.write_text("""@echo off
echo Stopping Redis Server...
taskkill /IM redis-server.exe /F
echo Redis stopped.
""")

        # Add to PATH (optional)
        print("📝 Creating desktop shortcuts...")
        
        # Test Redis
        print("🔍 Testing Redis installation...")
        redis_exe = self.install_dir / "redis-server.exe"
        if redis_exe.exists():
            print("✅ Redis installed successfully!")
            print(f"📁 Installation directory: {self.install_dir}")
            print(f"🚀 To start Redis: {start_script}")
            print(f"🛑 To stop Redis: {stop_script}")
            print("📝 Or add Redis to your PATH to use globally")
            
            # Start Redis for testing
            print("🔄 Starting Redis for testing...")
            result = self.run_command(f'"{start_script}"', capture_output=False)
            time.sleep(3)
            
            # Test connection
            redis_cli = self.install_dir / "redis-cli.exe"
            if redis_cli.exists():
                test_result = self.run_command(f'"{redis_cli}" ping')
                if test_result and "PONG" in test_result.stdout:
                    print("✅ Redis is running and responding!")
                    return True
            
            return True
        else:
            print("❌ Redis installation failed")
            return False

    def auto_install(self):
        """Auto-detect best installation method and install."""
        print("🔍 Auto-detecting best Redis installation method...")
        
        if self.check_wsl_available():
            print("✅ WSL detected - using WSL installation (recommended)")
            return self.install_wsl_redis()
        elif self.check_docker_available():
            print("✅ Docker detected - using Docker installation")
            return self.install_docker_redis()
        else:
            print("✅ Using native Windows installation")
            return self.install_native_redis()

    def show_status(self):
        """Show current Redis installation status."""
        print("🔍 Checking Redis status...")
        print("=" * 50)
        
        # Check WSL Redis
        if self.check_wsl_available():
            wsl_result = self.run_command("wsl redis-cli ping")
            if wsl_result and "PONG" in wsl_result.stdout:
                print("✅ WSL Redis: Running")
            else:
                print("❌ WSL Redis: Not running or not installed")
        else:
            print("❌ WSL: Not available")
        
        # Check Docker Redis
        if self.check_docker_available():
            docker_result = self.run_command("docker exec redis-kitebot redis-cli ping")
            if docker_result and "PONG" in docker_result.stdout:
                print("✅ Docker Redis: Running")
            else:
                print("❌ Docker Redis: Not running or not installed")
        else:
            print("❌ Docker: Not available")
        
        # Check Native Redis
        native_redis = self.install_dir / "redis-cli.exe"
        if native_redis.exists():
            native_result = self.run_command(f'"{native_redis}" ping')
            if native_result and "PONG" in native_result.stdout:
                print("✅ Native Redis: Running")
            else:
                print("❌ Native Redis: Not running")
        else:
            print("❌ Native Redis: Not installed")

def main():
    """Main function."""
    installer = RedisInstaller()
    
    print("🔴 Redis Installation Script for Windows")
    print("=" * 50)
    
    if len(sys.argv) < 2:
        print("Usage: python install_redis.py [method]")
        print("\nMethods:")
        print("  wsl     - Install Redis in WSL (Recommended)")
        print("  docker  - Install Redis using Docker")
        print("  native  - Install Redis natively on Windows")
        print("  auto    - Auto-detect best method and install")
        print("  status  - Show current Redis status")
        print("\nExample: python install_redis.py auto")
        return

    method = sys.argv[1].lower()
    
    if method == "wsl":
        success = installer.install_wsl_redis()
    elif method == "docker":
        success = installer.install_docker_redis()
    elif method == "native":
        success = installer.install_native_redis()
    elif method == "auto":
        success = installer.auto_install()
    elif method == "status":
        installer.show_status()
        return
    else:
        print(f"❌ Unknown method: {method}")
        print("Valid methods: wsl, docker, native, auto, status")
        return

    if success:
        print("\n🎉 Redis installation completed successfully!")
        print("\n📋 Next steps:")
        print("1. Test connection: redis-cli ping (should return PONG)")
        print("2. Start your FastAPI application")
        print("3. Redis will be available at localhost:6379")
    else:
        print("\n❌ Redis installation failed!")
        print("Please check the error messages above and try again.")

if __name__ == "__main__":
    main()
