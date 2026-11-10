import sqlite3
import os
from datetime import datetime

DB_PATH = "backend/music_bot.db"
BACKUP_DIR = "backups"

def make_backup():
    if not os.path.exists(BACKUP_DIR):
        os.makedirs(BACKUP_DIR)

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    backup_path = os.path.join(BACKUP_DIR, f"music_bot_{timestamp}.db")

    try:
        source = sqlite3.connect(DB_PATH)
        dest = sqlite3.connect(backup_path)
        
        with dest:
            source.backup(dest)
            
        source.close()
        dest.close()
        
        print(f"✅ Бэкап создан: {backup_path}")
        
        # Очистка старых бэкапов (оставляем только 10 последних)
        backups = sorted([f for f in os.listdir(BACKUP_DIR) if f.endswith('.db')])
        if len(backups) > 10:
            for old_file in backups[:-10]:
                os.remove(os.path.join(BACKUP_DIR, old_file))
                print(f"🗑 Удален старый бэкап: {old_file}")

    except Exception as e:
        print(f"❌ Ошибка: {e}")

if __name__ == "__main__":
    make_backup()