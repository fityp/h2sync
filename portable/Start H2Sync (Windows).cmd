@echo off
rem Starts H2Sync from its portable folder on Windows 10 and 11.
rem The Python runtime is unpacked into %TEMP% (once per version) and the app
rem runs from there, so the card can be ejected while H2Sync is open.
setlocal
set "HERE=%~dp0"
set "HERE=%HERE:~0,-1%"
set "T=windows-x86_64"
set /p V=<"%HERE%\runtime\VERSION"
set "BASE=%TEMP%\h2sync"
set "RT=%BASE%\runtime-%V%-%T%"
if exist "%RT%\python\pythonw.exe" goto app
echo Unpacking H2Sync, this happens once...
if exist "%RT%.part" rmdir /s /q "%RT%.part"
mkdir "%RT%.part" || goto fail
tar -xzf "%HERE%\runtime\python-%T%.tar.gz" -C "%RT%.part" || goto fail
if exist "%RT%" rmdir /s /q "%RT%"
move "%RT%.part" "%RT%" >nul || goto fail
:app
if exist "%BASE%\app" rmdir /s /q "%BASE%\app"
mkdir "%BASE%\app" || goto fail
copy /y "%HERE%\app\*.py" "%BASE%\app\" >nul || goto fail
cd /d "%BASE%"
start "" "%RT%\python\pythonw.exe" -E -s -B "%BASE%\app\h2sync.py" ui --home "%HERE%"
exit /b 0
:fail
echo H2Sync could not start. Windows 10 (1803 or later) or 11 is needed.
pause
exit /b 1
