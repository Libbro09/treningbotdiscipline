# ⚡ Быстрый старт

## 1️⃣ Локально (Windows)

```powershell
cd "C:\Users\user\Desktop\playerok exp\discipline-bot"
.\.venv\Scripts\python.exe bot.py
```

Откройся в Telegram: **@treneeeeer_bot** → `/start`

## 2️⃣ Проверка Google Gemini API

```
/test_api
```

Если видишь ответ — всё работает! ✅

Если ошибка — смотри [README.md](README.md) → раздел "🤖 Google Gemini".

---

## 3️⃣ Деплой на Railway (легче всего)

1. Залей папку на GitHub: `discipline-bot`
2. Зайди https://railway.app → **New Project → Deploy from GitHub**
3. Выбери репозиторий `discipline-bot`
4. **Variables** → добавь:
   ```
   BOT_TOKEN = твой_токен_от_BotFather
   GOOGLE_API_KEY = твой_ключ_от_Google_Gemini
   TZ = Europe/Moscow
   ```
5. **Deploy** → жди пару минут
6. Готово! Бот работает 24/7 🚀

---

## 📋 Функионал

- ⏰ **7:00** → Доброе утро + план дня
- ✅ Кнопки «Сделал(а)» для каждой привычки
- 📊 Статистика: серия 🔥, % за неделю, график
- 🤖 Gemini отвечает как живой тренер
- 🌙 **00:00** → Вечерний отчёт + анализ
- ⚙️ Настройка времени подъёма и отчёта

---

## 🆘 Если что-то не работает

1. Запусти локально и смотри консоль → там все ошибки
2. На Railway смотри **Logs** вкладку
3. Проверь `/test_api` — работает ли Gemini?

Готово! Удачи с привычками 💪
