import sqlite3
import os
import asyncio
from datetime import datetime

# Импортируем бота и настройки из твоего проекта
from aiogram import Bot
from aiogram.types import FSInputFile
from backend.config import TELEGRAM_BOT_TOKEN, ADMIN_ID

DB_PATH = "backend/music_bot.db"
BACKUP_DIR = "backups"

async def send_backup_to_telegram(backup_path):
    """Отправляет файл бэкапа админу в Telegram."""
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    try:
        await bot.send_document(
            chat_id=ADMIN_ID,
            document=FSInputFile(backup_path),
            caption=f"🗄 <b>Резервная копия базы данных</b>\n📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        print("📤 Бэкап успешно отправлен в Telegram.")
    except Exception as e:
        print(f"❌ Не удалось отправить бэкап в Telegram: {e}")
    finally:
        await bot.session.close()

def make_backup():
    if not os.path.exists(BACKUP_DIR):
        os.makedirs(BACKUP_DIR)

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    backup_path = os.path.join(BACKUP_DIR, f"music_bot_{timestamp}.db")

    try:
        # Создаем локальный бэкап
        source = sqlite3.connect(DB_PATH)
        dest = sqlite3.connect(backup_path)
        
        with dest:
            source.backup(dest)
            
        source.close()
        dest.close()
        
        print(f"✅ Бэкап создан: {backup_path}")
        
        # Отправляем в Telegram
        asyncio.run(send_backup_to_telegram(backup_path))
        
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
