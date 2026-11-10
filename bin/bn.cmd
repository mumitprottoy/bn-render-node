@echo off
rem bn status | bn pause | bn resume, from any PowerShell or cmd window once this folder is on PATH.
rem Only bn.cmd is in this folder, so PowerShell never finds bn.ps1 directly (its execution policy could block it).
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\bn.ps1" %*
exit /b %ERRORLEVEL%
