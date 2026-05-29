@echo off
echo Installing Ammo Price Alert dependencies...
pip install -r requirements.txt
echo.
echo Done! Now:
echo   1. Copy config.json.example to config.json
echo   2. Fill in your Gmail app password in config.json
echo   3. Run: python ammo_alert.py --test
echo   4. Or launch the web app: python app.py
pause
