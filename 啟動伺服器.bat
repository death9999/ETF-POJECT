@echo off
chcp 65001 >nul
title ETF追蹤器後端

echo ================================================
echo   ETF 追蹤器後端 - 啟動程式
echo ================================================
echo.

:: 檢查 Python 是否安裝
python --version >nul 2>&1
if errorlevel 1 (
    echo [錯誤] 找不到 Python！
    echo.
    echo 請先到以下網址下載並安裝 Python：
    echo https://www.python.org/downloads/
    echo.
    echo 安裝時請勾選 "Add Python to PATH"
    echo.
    pause
    exit /b 1
)

echo [✓] Python 已安裝
python --version
echo.

:: 安裝套件
echo [安裝] 正在安裝必要套件（約1~2分鐘）...
pip install -r requirements.txt -q
if errorlevel 1 (
    echo [錯誤] 套件安裝失敗，請確認網路連線
    pause
    exit /b 1
)
echo [✓] 套件安裝完成
echo.

:: 啟動伺服器
echo [啟動] 後端伺服器啟動中...
echo.
python app.py

pause