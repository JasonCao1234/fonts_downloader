@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在启动 ass-font-fetch 可视化页面…
python gui_server.py %*
if errorlevel 1 (
  echo.
  echo 启动失败。请确认已安装 Python 3.9+，或在命令行里手动运行：
  echo     python "%~dp0gui_server.py"
  pause
)
