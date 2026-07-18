@echo off
rem ==========================================================================
rem Proxyshop Web - one-command NAS refresh from Windows
rem
rem SSHes into the NAS, runs nas-update.sh there, streams output back.
rem Edit NAS_HOST once. Pass a different script path as the first argument
rem to refresh another app:  nas-refresh.bat ~/other-app/nas-update.sh
rem
rem Requires the Windows OpenSSH client ("where ssh" to check; add via
rem Settings > Apps > Optional Features if missing). Add an SSH key to the
rem NAS for a passwordless run (see docs/web-service-architecture.md).
rem ==========================================================================

set NAS_HOST=user@nas-host
set NAS_PATH=~/proxyshop-web/nas-update.sh

set SCRIPT=%NAS_PATH%
if not "%~1"=="" set SCRIPT=%~1

echo Refreshing via %NAS_HOST% : %SCRIPT%
ssh %NAS_HOST% "sh %SCRIPT%"
if errorlevel 1 (
  echo.
  echo Update FAILED - see output above.
  exit /b 1
)
echo.
echo Update complete.
