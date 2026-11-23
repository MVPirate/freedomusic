#!/bin/bash

# Переходим в папку проекта
cd /root/music-bot || exit

echo "🔍 Проверяю статус Git..."
git status --short

# ПРОВЕРКА НА СЕКРЕТНЫЕ ФАЙЛЫ
if git status --short | grep -qE "\.env|\.db|uploads/"; then
    echo "🛑 ВНИМАНИЕ! В коммит пытаются попасть секретные файлы (.env, база данных или uploads)."
    echo "Проверь .gitignore. Коммит отменен."
    exit 1
fi

echo "✅ Секретных файлов в списке нет."
echo ""

# Запрос названия коммита
read -p "📝 Введите название коммита: " commit_msg

if [ -z "$commit_msg" ]; then
    echo "❌ Название коммита не может быть пустым. Отмена."
    exit 1
fi

# Добавляем, коммитим, пушим
git add .
git commit -m "$commit_msg"
git push origin main

echo "🚀 Готово! Изменения отправлены на GitHub."
