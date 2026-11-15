@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 把临时目录放到自己旁边，避免某些机器上用户 TEMP 不能建目录
set "TEMP=%~dp0_tmp"
set "TMP=%~dp0_tmp"
if not exist "%TEMP%" mkdir "%TEMP%" >nul 2>&1
echo 正在启动 ass-font-fetch…
echo （这个黑窗口不能关，关掉就等于停止服务）
echo.
"%~dp0ass-font-fetch.exe" %*
if errorlevel 1 (
  echo.
  echo 启动失败。如果提示 "Could not create temporary directory"，请手动运行：
  echo     set TEMP=%~dp0_tmp
  echo     ass-font-fetch.exe
  pause
)
