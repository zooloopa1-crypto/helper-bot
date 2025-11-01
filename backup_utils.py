# backup_utils.py
# Автоматичне створення резервної копії Google Sheets у Excel

import datetime
import gspread
import pandas as pd
from oauth2client.service_account import ServiceAccountCredentials
from config import GOOGLE_CREDS_FILE, SPREADSHEET_ID

def backup_to_excel():
    try:
        scope = ['https://spreadsheets.google.com/feeds','https://www.googleapis.com/auth/drive']
        creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDS_FILE, scope)
        client = gspread.authorize(creds)
        ss = client.open_by_key(SPREADSHEET_ID)
        
        # Отримуємо аркуш Reports
        worksheet = ss.worksheet('Reports')
        data = worksheet.get_all_records()

        if not data:
            print('⚠️ Дані відсутні, резервне копіювання пропущено.')
            return

        # Конвертуємо в DataFrame
        df = pd.DataFrame(data)

        # Додаємо час резервного копіювання
        timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        df['Резервна копія створена'] = timestamp

        # Зберігаємо у файл
        backup_filename = f'backup_reports_{datetime.date.today()}.xlsx'
        df.to_excel(backup_filename, index=False)

        print(f'✅ Резервна копія створена: {backup_filename}')
    except Exception as e:
        print('❌ Помилка резервного копіювання:', e)
