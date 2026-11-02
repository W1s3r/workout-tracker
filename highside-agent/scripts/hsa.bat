@echo off
REM Launcher: put this folder on PATH (or copy this file somewhere on PATH and edit HSA_DIR).
REM Uses the Anaconda python on PATH; set HSA_PYTHON to a specific python.exe if needed.
set HSA_DIR=%~dp0..
if "%HSA_PYTHON%"=="" set HSA_PYTHON=python
set PYTHONPATH=%HSA_DIR%;%PYTHONPATH%
"%HSA_PYTHON%" -m hsa %*
