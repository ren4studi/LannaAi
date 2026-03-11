import asyncio
import logging
from typing import List, Dict, Optional
import time
import hashlib
import urllib.parse
from datetime import datetime, date, timedelta
import aiohttp
from bs4 import BeautifulSoup
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import (
    Message, 
    InlineKeyboardMarkup, 
    InlineKeyboardButton, 
    CallbackQuery,
    LabeledPrice,
    PreCheckoutQuery
)
from aiogram.filters import Command
import os
import json
from pathlib import Path
import ssl
import certifi
from aiohttp import web

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==================== КЛАСС ДЛЯ РАБОТЫ С ПОДПИСКАМИ ====================

class SubscriptionManager:
    """
    Менеджер подписок пользователей
    """
    def __init__(self, db_file="subscriptions.json"):
        self.db_file = db_file
        self.subscriptions = self.load_subscriptions()
        
        # Настройки лимитов
        self.free_daily_requests = 2  # 2 бесплатных запроса в день
        self.premium_monthly_price = 25  # 25 Telegram Stars
        self.premium_requests = 999999
    
    def load_subscriptions(self):
        if Path(self.db_file).exists():
            try:
                with open(self.db_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except:
                return {}
        return {}
    
    def save_subscriptions(self):
        with open(self.db_file, 'w', encoding='utf-8') as f:
            json.dump(self.subscriptions, f, ensure_ascii=False, indent=2)
    
    def check_subscription(self, user_id: str) -> bool:
        if user_id in self.subscriptions:
            expiry = datetime.fromisoformat(self.subscriptions[user_id]['expiry'])
            if expiry > datetime.now():
                return True
            else:
                del self.subscriptions[user_id]
                self.save_subscriptions()
        return False
    
    def add_subscription(self, user_id: str, days: int = 30):
        expiry = datetime.now() + timedelta(days=days)
        self.subscriptions[user_id] = {
            'expiry': expiry.isoformat(),
            'purchased_at': datetime.now().isoformat()
        }
        self.save_subscriptions()
    
    def get_remaining_free_requests(self, user_id: str) -> int:
        today = date.today().isoformat()
        
        if user_id not in self.subscriptions:
            user_file = f"user_stats_{user_id}.json"
            if Path(user_file).exists():
                with open(user_file, 'r') as f:
                    stats = json.load(f)
                    if stats.get('date') == today:
                        return max(0, self.free_daily_requests - stats.get('requests', 0))
            return self.free_daily_requests
        return self.premium_requests
    
    def increment_request(self, user_id: str):
        today = date.today().isoformat()
        user_file = f"user_stats_{user_id}.json"
        
        stats = {'date': today, 'requests': 1}
        if Path(user_file).exists():
            with open(user_file, 'r') as f:
                stats = json.load(f)
                if stats.get('date') == today:
                    stats['requests'] = stats.get('requests', 0) + 1
                else:
                    stats = {'date': today, 'requests': 1}
        
        with open(user_file, 'w') as f:
            json.dump(stats, f)

# ==================== КЛАСС ДЛЯ СТАТИСТИКИ ПЛАТЕЖЕЙ ====================

class PaymentStats:
    def __init__(self, stats_file="payment_stats.json"):
        self.stats_file = stats_file
        self.stats = self.load_stats()
    
    def load_stats(self):
        if Path(self.stats_file).exists():
            try:
                with open(self.stats_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except:
                return {"total_earned": 0, "payments": []}
        return {"total_earned": 0, "payments": []}
    
    def save_stats(self):
        with open(self.stats_file, 'w', encoding='utf-8') as f:
            json.dump(self.stats, f, ensure_ascii=False, indent=2)
    
    def add_payment(self, user_id, amount, username):
        self.stats["total_earned"] += amount
        self.stats["payments"].append({
            "user_id": user_id,
            "username": username,
            "amount": amount,
            "date": datetime.now().isoformat()
        })
        self.save_stats()
    
    def get_stats(self):
        total = self.stats["total_earned"]
        payments_count = len(self.stats["payments"])
        
        current_month = datetime.now().month
        month_payments = [p for p in self.stats["payments"] 
                         if datetime.fromisoformat(p["date"]).month == current_month]
        month_total = sum(p["amount"] for p in month_payments)
        
        return {
            "total_earned": total,
            "total_payments": payments_count,
            "month_earned": month_total,
            "month_payments": len(month_payments)
        }

# ==================== КЛАСС ПОИСКОВОЙ СИСТЕМЫ ====================

class SearchEngine:
    """
    Поисковая система с OpenAI
    """
    
    def __init__(self):
        self.cache = {}
        self.openai_key = "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"
        self.cache_ttl = 3600
        
        # Создаем SSL контекст для корректной работы
        self.ssl_context = ssl.create_default_context(cafile=certifi.where())
    
    async def search_duckduckgo_html(self, query: str) -> List[Dict]:
        url = "https://html.duckduckgo.com/html/"
        params = {"q": query, "kl": "ru-ru"}
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        
        try:
            conn = aiohttp.TCPConnector(ssl=self.ssl_context)
            async with aiohttp.ClientSession(connector=conn) as session:
                async with session.post(url, data=params, headers=headers, timeout=15) as response:
                    if response.status != 200:
                        return await self.search_google_fallback(query)
                    
                    html = await response.text()
                    soup = BeautifulSoup(html, 'html.parser')
                    
                    results = []
                    for result in soup.find_all('div', class_='result')[:5]:
                        title_elem = result.find('a', class_='result__a')
                        if not title_elem:
                            continue
                            
                        title = title_elem.get_text(strip=True)
                        title = ' '.join(title.split())
                        
                        url_elem = result.find('a', class_='result__url')
                        url = url_elem.get('href') if url_elem else '#'
                        if url.startswith('/'):
                            url = 'https://duckduckgo.com' + url
                        
                        snippet_elem = result.find('a', class_='result__snippet')
                        snippet = snippet_elem.get_text(strip=True) if snippet_elem else "Описание отсутствует"
                        snippet = ' '.join(snippet.split())
                        
                        results.append({
                            'title': title,
                            'url': url,
                            'snippet': snippet
                        })
                    
                    return results if results else await self.search_google_fallback(query)
                        
        except Exception as e:
            logger.error(f"Ошибка DuckDuckGo: {e}")
            return await self.search_google_fallback(query)
    
    async def search_google_fallback(self, query: str) -> List[Dict]:
        url = "https://www.google.com/search"
        params = {"q": query, "hl": "ru"}
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        
        try:
            conn = aiohttp.TCPConnector(ssl=self.ssl_context)
            async with aiohttp.ClientSession(connector=conn) as session:
                async with session.get(url, params=params, headers=headers, timeout=15) as response:
                    if response.status != 200:
                        return self.get_demo_results(query)
                    
                    html = await response.text()
                    soup = BeautifulSoup(html, 'html.parser')
                    
                    results = []
                    for result in soup.find_all('div', class_='g')[:5]:
                        title_elem = result.find('h3')
                        if not title_elem:
                            continue
                            
                        title = title_elem.get_text(strip=True)
                        title = ' '.join(title.split())
                        
                        url_elem = result.find('a')
                        url = url_elem.get('href', '#')
                        if url.startswith('/url?q='):
                            url = url.split('/url?q=')[1].split('&')[0]
                        
                        snippet_elem = result.find('div', class_='VwiC3b')
                        snippet = snippet_elem.get_text(strip=True) if snippet_elem else "Описание отсутствует"
                        snippet = ' '.join(snippet.split())
                        
                        results.append({
                            'title': title,
                            'url': url,
                            'snippet': snippet
                        })
                    
                    return results if results else self.get_demo_results(query)
                    
        except Exception as e:
            logger.error(f"Ошибка Google: {e}")
            return self.get_demo_results(query)
    
    def get_demo_results(self, query: str) -> List[Dict]:
        return [
            {
                'title': 'Джеффри Эпштейн - Википедия',
                'url': 'https://ru.wikipedia.org/wiki/Эпштейн,_Джеффри',
                'snippet': 'Джеффри Эпштейн (1953-2019) - американский финансист и осужденный сексуальный преступник.'
            },
            {
                'title': 'Кто такой Джеффри Эпштейн?',
                'url': 'https://www.bbc.com/russian/news-48919302',
                'snippet': 'Джеффри Эпштейн был миллиардером, который вращался в кругах элиты.'
            }
        ]
    
    async def generate_with_openai(self, query: str, context: str) -> str:
        url = "https://api.openai.com/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.openai_key}",
            "Content-Type": "application/json"
        }
        
        if len(context) > 3000:
            context = context[:3000] + "..."
        
        prompt = f"""На основе результатов поиска дай ответ на вопрос.

Результаты поиска:
{context}

Вопрос: {query}

Ответ напиши на русском языке:"""

        data = {
            "model": "gpt-3.5-turbo",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 1000,
            "temperature": 0.7
        }
        
        try:
            conn = aiohttp.TCPConnector(ssl=self.ssl_context)
            async with aiohttp.ClientSession(connector=conn) as session:
                async with session.post(url, headers=headers, json=data, timeout=30) as response:
                    if response.status != 200:
                        return self.generate_local_answer(query, context)
                    
                    result = await response.json()
                    return result['choices'][0]['message']['content']
        except Exception as e:
            logger.error(f"Ошибка OpenAI: {e}")
            return self.generate_local_answer(query, context)
    
    def generate_local_answer(self, query: str, context: str) -> str:
        sources = context.split('\n\n')
        answer = f"По запросу \"{query}\" найдена информация:\n\n"
        
        if sources and len(sources) > 0:
            for i, source in enumerate(sources[:3], 1):
                if source.strip():
                    clean_source = source.replace('[', '').replace(']', '').replace('*', '')
                    clean_source = ' '.join(clean_source.split())
                    if len(clean_source) > 300:
                        clean_source = clean_source[:300] + "..."
                    answer += f"{i}. {clean_source}\n\n"
        else:
            answer += "Информация не найдена.\n\n"
        
        return answer
    
    def get_from_cache(self, key: str) -> Optional[Dict]:
        if key in self.cache:
            data, timestamp = self.cache[key]
            if time.time() - timestamp < self.cache_ttl:
                return data
            else:
                del self.cache[key]
        return None
    
    def save_to_cache(self, key: str, data: dict):
        self.cache[key] = (data, time.time())

# ==================== ТЕЛЕГРАМ БОТ ====================

# Токен бота
BOT_TOKEN = "8769273391:AAEKyZ2ZvMU3rQDeBqaiY4nAriNXWPchTX4"

# ID администратора (замените на ваш)
ADMIN_IDS = [823985747]  # ⚠️ ЗАМЕНИТЕ НА ВАШ ID

# Инициализация
search_engine = SearchEngine()
subscription_manager = SubscriptionManager()
payment_stats = PaymentStats()

# Инициализация бота и диспетчера
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

def get_main_keyboard(user_id: str):
    """Главная клавиатура"""
    is_premium = subscription_manager.check_subscription(user_id)
    
    buttons = []
    
    if is_premium:
        buttons.append([InlineKeyboardButton(text="🌟 Премиум режим", callback_data="premium_info")])
    else:
        remaining = subscription_manager.get_remaining_free_requests(user_id)
        buttons.append([InlineKeyboardButton(text=f"🆓 Бесплатных запросов: {remaining}/2", callback_data="free_info")])
        buttons.append([InlineKeyboardButton(text="💎 Купить Premium (25 ⭐)", callback_data="buy_premium")])
    
    buttons.append([InlineKeyboardButton(text="🔍 Новый поиск", callback_data="new_search")])
    buttons.append([InlineKeyboardButton(text="ℹ️ О боте", callback_data="about")])
    buttons.append([InlineKeyboardButton(text="📊 Статистика", callback_data="stats")])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_back_keyboard():
    """Клавиатура возврата"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад в меню", callback_data="back_to_menu")]
    ])

def get_admin_keyboard():
    """Админ клавиатура"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Баланс Stars", callback_data="admin_balance")],
        [InlineKeyboardButton(text="📊 Статистика платежей", callback_data="admin_stats")],
        [InlineKeyboardButton(text="💎 Инструкция по выводу", callback_data="admin_withdraw")],
        [InlineKeyboardButton(text="👥 Пользователи", callback_data="admin_users")],
        [InlineKeyboardButton(text="◀️ Выход", callback_data="back_to_menu")]
    ])

# ==================== ФУНКЦИЯ ДЛЯ ПОЛУЧЕНИЯ БАЛАНСА ====================

async def get_stars_balance():
    """Получение баланса Stars через правильный метод API"""
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/getStarBalance"
        
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        conn = aiohttp.TCPConnector(ssl=ssl_context)
        
        async with aiohttp.ClientSession(connector=conn) as session:
            async with session.get(url, timeout=10) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get('ok'):
                        return data.get('result', {}).get('balance', 0)
                    else:
                        logger.error(f"Ошибка API: {data}")
                        return None
                else:
                    logger.error(f"Ошибка HTTP: {response.status}")
                    return None
    except Exception as e:
        logger.error(f"Ошибка при запросе баланса: {e}")
        return None

# ==================== ОБРАБОТЧИКИ КОМАНД ====================

@dp.message(Command("start"))
async def cmd_start(message: Message):
    user_id = str(message.from_user.id)
    user_name = message.from_user.first_name
    
    is_premium = subscription_manager.check_subscription(user_id)
    remaining = subscription_manager.get_remaining_free_requests(user_id)
    
    welcome_text = f"""
🎯 Привет, {user_name}!

Я - ИИ поисковая система с доступом к интернету.
Помогу тебе с школьными проектами и любыми вопросами!

📊 Твой статус:
{'🌟 Премиум (безлимитно)' if is_premium else f'🆓 Бесплатно: {remaining}/2 запросов в день'}

🔍 Отправь мне любой вопрос, и я найду ответ!

💰 Премиум (25 ⭐ в месяц):
• Безлимитные запросы
• Более подробные ответы
• Без ожидания
"""
    await message.answer(welcome_text, reply_markup=get_main_keyboard(user_id))

@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    """Админ панель"""
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("❌ Доступ запрещен")
        return
    
    await message.answer(
        "🔧 **Админ панель**\n\nВыберите действие:",
        parse_mode="Markdown",
        reply_markup=get_admin_keyboard()
    )

@dp.message(Command("balance"))
async def cmd_balance(message: Message):
    """Проверка баланса бота (только для админа)"""
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("❌ Доступ запрещен")
        return
    
    status_msg = await message.answer("🔄 Получаю баланс...")
    
    try:
        balance = await get_stars_balance()
        
        if balance is not None:
            await status_msg.edit_text(
                f"💰 **Баланс бота:**\n\n"
                f"• Доступно звезд: {balance} ⭐\n"
                f"• 1 ⭐ ≈ 1.7 руб\n"
                f"• В рублях: ~{balance * 1.7:.0f} руб\n\n"
                f"📤 Для вывода используйте @send\n"
                f"💡 Команда: /withdraw для инструкции",
                parse_mode="Markdown"
            )
        else:
            await status_msg.edit_text(
                "❌ Не удалось получить баланс через API.\n\n"
                "📊 Используйте @getmybot для проверки баланса.",
                parse_mode="Markdown"
            )
    except Exception as e:
        logger.error(f"Ошибка в /balance: {e}")
        await status_msg.edit_text(
            f"❌ Ошибка при получении баланса.\n\n"
            f"Используйте @getmybot для проверки баланса."
        )

@dp.message(Command("withdraw"))
async def cmd_withdraw(message: Message):
    """Инструкция по выводу"""
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("❌ Доступ запрещен")
        return
    
    text = """
💎 **Инструкция по выводу Stars**

**Шаг 1: Проверьте баланс**
• Через @getmybot
• Или через /balance

**Шаг 2: Выведите через @send**
1. Откройте @send
2. Нажмите "Start"
3. Выберите "Wallets" → "Withdraw"
4. Выберите TON (мин. 10⭐)
5. Введите адрес TON кошелька

**Шаг 3: Конвертируйте TON в рубли**
• Используйте биржи: Bybit, OKX, Binance
• Или обменники: BestChange

**Советы:**
• Tonkeeper - лучший кошелек
• Мин. вывод: 10⭐
• Комиссия: 1-3%
"""
    await message.answer(text, parse_mode="Markdown")

# ==================== ОБРАБОТЧИКИ CALLBACK ====================

@dp.callback_query()
async def process_callbacks(callback: CallbackQuery):
    """Обработка всех callback запросов"""
    data = callback.data
    user_id = str(callback.from_user.id)
    
    await callback.answer()
    
    # ===== ОБЩИЕ КНОПКИ =====
    
    if data == "premium_info":
        expiry = subscription_manager.subscriptions.get(user_id, {}).get('expiry', 'Неизвестно')
        if expiry != 'Неизвестно':
            expiry_date = datetime.fromisoformat(expiry).strftime('%d.%m.%Y')
        else:
            expiry_date = 'Неизвестно'
            
        await callback.message.edit_text(
            f"🌟 У вас активна премиум подписка!\n\n"
            f"✅ Действует до: {expiry_date}\n"
            f"✅ Безлимитные запросы\n\n"
            f"Спасибо за поддержку! ❤️",
            reply_markup=get_back_keyboard()
        )
    
    elif data == "free_info":
        remaining = subscription_manager.get_remaining_free_requests(user_id)
        await callback.message.edit_text(
            f"🆓 Бесплатный режим\n\n"
            f"У вас осталось: {remaining}/2 запросов на сегодня\n\n"
            f"💎 Купите премиум за 25 ⭐ для безлимитных запросов!",
            reply_markup=get_back_keyboard()
        )
    
    elif data == "buy_premium":
        try:
            prices = [LabeledPrice(label="Премиум подписка на 30 дней", amount=25)]
            
            await bot.send_invoice(
                chat_id=callback.from_user.id,
                title="💎 Премиум подписка Lanna AI Search",
                description="30 дней безлимитного доступа к ИИ поиску",
                payload="premium_30_days",
                provider_token="",
                currency="XTR",
                prices=prices,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="💎 Оплатить 25 ⭐", pay=True)],
                    [InlineKeyboardButton(text="◀️ Отмена", callback_data="back_to_menu")]
                ])
            )
        except Exception as e:
            logger.error(f"Ошибка при создании счета: {e}")
            await callback.message.edit_text(
                "❌ Ошибка при создании счета. Попробуйте позже.",
                reply_markup=get_back_keyboard()
            )
    
    elif data == "new_search":
        await callback.message.edit_text(
            "🔍 Отправьте ваш вопрос:",
            reply_markup=get_back_keyboard()
        )
    
    elif data == "about":
        await callback.message.edit_text(
            "ℹ️ **Lanna AI Search Bot**\n\n"
            "🔍 Умный поиск с искусственным интеллектом\n"
            "Помогаю с школьными проектами и любыми вопросами!\n\n"
            "📌 **Возможности:**\n"
            "• Поиск в интернете в реальном времени\n"
            "• Ответы от ИИ (OpenAI GPT-3.5)\n"
            "• Ссылки на источники\n"
            "• 2 бесплатных запроса в день\n"
            "• Премиум безлимит за 25 ⭐/мес\n\n"
            "👨‍💻 **Для связи:** @Lanna_support",
            parse_mode="Markdown",
            reply_markup=get_back_keyboard()
        )
    
    elif data == "stats":
        is_premium = subscription_manager.check_subscription(user_id)
        remaining = subscription_manager.get_remaining_free_requests(user_id)
        
        await callback.message.edit_text(
            f"📊 **Статистика**\n\n"
            f"👤 Ваш аккаунт:\n"
            f"• Статус: {'🌟 Премиум' if is_premium else '🆓 Бесплатный'}\n"
            f"• Осталось запросов: {'♾️ Безлимит' if is_premium else f'{remaining}/2'}\n"
            f"• В кэше бота: {len(search_engine.cache)} ответов",
            parse_mode="Markdown",
            reply_markup=get_back_keyboard()
        )
    
    elif data == "back_to_menu":
        is_premium = subscription_manager.check_subscription(user_id)
        remaining = subscription_manager.get_remaining_free_requests(user_id)
        
        await callback.message.edit_text(
            f"🔍 Главное меню\n\n"
            f"{'🌟 Премиум' if is_premium else f'🆓 Осталось: {remaining}/2'}",
            reply_markup=get_main_keyboard(user_id)
        )
    
    # ===== АДМИН КНОПКИ =====
    
    elif data.startswith("admin_"):
        if callback.from_user.id not in ADMIN_IDS:
            await callback.message.edit_text("❌ Доступ запрещен")
            return
        
        if data == "admin_balance":
            status_msg = await callback.message.edit_text("🔄 Получаю баланс...")
            
            balance = await get_stars_balance()
            
            if balance is not None:
                await status_msg.edit_text(
                    f"💰 **Баланс Stars**\n\n"
                    f"• Всего звезд: {balance} ⭐\n"
                    f"• 1 ⭐ ≈ 1.7 руб\n"
                    f"• В рублях: ~{balance * 1.7:.0f} руб\n\n"
                    f"📤 **Для вывода:**\n"
                    f"1. Напишите @send\n"
                    f"2. Нажмите Withdraw\n"
                    f"3. Выберите TON кошелек",
                    parse_mode="Markdown",
                    reply_markup=get_admin_keyboard()
                )
            else:
                await status_msg.edit_text(
                    "❌ Не удалось получить баланс через API.\n\n"
                    "📊 Используйте @getmybot для проверки баланса.",
                    reply_markup=get_admin_keyboard()
                )
        
        elif data == "admin_stats":
            stats = payment_stats.get_stats()
            
            text = f"""
📊 **Статистика платежей**

**Всего:**
• Заработано: {stats['total_earned']} ⭐
• Продаж: {stats['total_payments']}

**За текущий месяц:**
• Заработано: {stats['month_earned']} ⭐
• Продаж: {stats['month_payments']}

**Последние платежи:**
"""
            for payment in payment_stats.stats['payments'][-5:]:
                date = datetime.fromisoformat(payment['date']).strftime('%d.%m')
                text += f"\n• {date}: {payment['amount']} ⭐ - {payment['username']}"
            
            await callback.message.edit_text(
                text,
                parse_mode="Markdown",
                reply_markup=get_admin_keyboard()
            )
        
        elif data == "admin_withdraw":
            text = """
💎 **Инструкция по выводу Stars**

**Способ 1: Через @send**
1. Откройте @send в Telegram
2. Нажмите "Start"
3. Выберите "Wallets" → "Withdraw"
4. Выберите TON (мин. 10⭐)
5. Введите адрес TON кошелька

**Способ 2: Через Fragment**
1. Перейдите на fragment.com
2. Войдите через Telegram
3. Подключите TON кошелек
4. Продайте Stars за TON

**Способ 3: Проверка баланса**
• @getmybot - баланс всех ботов
• /balance - баланс этого бота

**Советы:**
• Tonkeeper - лучший кошелек
• Мин. вывод: 10⭐
• Комиссия: 1-3%
"""
            await callback.message.edit_text(
                text,
                parse_mode="Markdown",
                reply_markup=get_admin_keyboard()
            )
        
        elif data == "admin_users":
            total_users = len(subscription_manager.subscriptions)
            premium_users = sum(1 for _ in subscription_manager.subscriptions.values())
            
            text = f"""
👥 **Пользователи**

• Всего пользователей: {total_users}
• Премиум подписок: {premium_users}

📈 **Доход:**
• Средний чек: 25 ⭐
• За месяц: {premium_users * 25} ⭐
"""
            await callback.message.edit_text(
                text,
                parse_mode="Markdown",
                reply_markup=get_admin_keyboard()
            )

# ==================== ОБРАБОТЧИКИ ПЛАТЕЖЕЙ ====================

@dp.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery):
    """Проверка перед оплатой"""
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@dp.message(F.successful_payment)
async def process_successful_payment(message: Message):
    """Обработка успешной оплаты"""
    user_id = str(message.from_user.id)
    payment_info = message.successful_payment
    
    # Активируем подписку
    subscription_manager.add_subscription(user_id)
    
    # Сохраняем статистику
    payment_stats.add_payment(
        user_id=user_id,
        amount=payment_info.total_amount,
        username=message.from_user.username or "No username"
    )
    
    # Уведомление пользователю
    await message.answer(
        f"🎉 Поздравляю!\n\n"
        f"✅ Оплачено: {payment_info.total_amount} ⭐\n"
        f"✅ Премиум подписка активирована на 30 дней!\n\n"
        f"Спасибо за поддержку! ❤️",
        reply_markup=get_main_keyboard(user_id)
    )
    
    # Уведомление админам
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"💰 **Новый платеж!**\n\n"
                f"• Пользователь: {message.from_user.full_name}\n"
                f"• Username: @{message.from_user.username}\n"
                f"• ID: {user_id}\n"
                f"• Сумма: {payment_info.total_amount} ⭐\n"
                f"• Время: {datetime.now().strftime('%d.%m.%Y %H:%M')}",
                parse_mode="Markdown"
            )
        except:
            pass

# ==================== ОБРАБОТЧИК ТЕКСТОВЫХ СООБЩЕНИЙ ====================

@dp.message()
async def handle_search(message: Message):
    """Обработка поисковых запросов"""
    if message.text.startswith('/'):
        return
    
    user_id = str(message.from_user.id)
    query = message.text.strip()
    
    logger.info(f"Запрос от {user_id}: {query}")
    
    # Проверка подписки
    is_premium = subscription_manager.check_subscription(user_id)
    remaining = subscription_manager.get_remaining_free_requests(user_id)
    
    if not is_premium and remaining <= 0:
        await message.answer(
            "❌ Лимит бесплатных запросов исчерпан!\n\n"
            "💎 Купите премиум за 25 ⭐ для безлимитного доступа!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💎 Купить Premium", callback_data="buy_premium")],
                [InlineKeyboardButton(text="◀️ Меню", callback_data="back_to_menu")]
            ])
        )
        return
    
    status_msg = await message.answer("🔍 Ищу информацию...")
    
    try:
        if not is_premium:
            subscription_manager.increment_request(user_id)
        
        cache_key = f"search:{hashlib.md5(query.encode()).hexdigest()}"
        cached_result = search_engine.get_from_cache(cache_key)
        
        if cached_result:
            await status_msg.delete()
            await send_search_result(message, cached_result, cached=True, is_premium=is_premium)
            return
        
        search_results = await search_engine.search_duckduckgo_html(query)
        
        if not search_results:
            search_results = search_engine.get_demo_results(query)
        
        context = ""
        sources = []
        
        for i, result in enumerate(search_results[:5], 1):
            sources.append({
                'number': i,
                'title': result['title'],
                'url': result['url'],
                'snippet': result['snippet']
            })
            context += f"[{i}] {result['title']}\n{result['snippet']}\n\n"
        
        await status_msg.edit_text(f"📚 Нашел {len(sources)} источников. Генерирую ответ...")
        
        answer = await search_engine.generate_with_openai(query, context)
        
        result_data = {
            'answer': answer,
            'sources': sources,
            'timestamp': datetime.now().isoformat()
        }
        search_engine.save_to_cache(cache_key, result_data)
        
        await status_msg.delete()
        await send_search_result(message, result_data, is_premium=is_premium)
        
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        await status_msg.edit_text("❌ Произошла ошибка. Попробуйте позже.")

async def send_search_result(message: Message, result_data: dict, cached: bool = False, is_premium: bool = False):
    """Отправка результата поиска"""
    answer = result_data['answer']
    sources = result_data['sources']
    
    text = f"{answer}\n\n📚 **Источники:**\n\n"
    
    for source in sources:
        text += f"[{source['number']}] {source['title']}\n🔗 {source['url']}\n\n"
    
    if cached:
        text += f"\n⚡ Ответ из кэша"
    
    if is_premium:
        text += f"\n🌟 Премиум режим"
    else:
        remaining = subscription_manager.get_remaining_free_requests(str(message.from_user.id))
        text += f"\n🆓 Осталось запросов: {remaining}/2"
    
    await message.answer(
        text,
        parse_mode="Markdown",
        disable_web_page_preview=True,
        reply_markup=get_main_keyboard(str(message.from_user.id))
    )

# ==================== ВЕБ-СЕРВЕР ДЛЯ RENDER ====================

async def handle_health(request):
    """Эндпоинт для проверки здоровья"""
    return web.Response(text="Bot is running!")

async def handle_root(request):
    """Корневой эндпоинт"""
    return web.Response(text="Lanna AI Search Bot is running!")

async def start_bot():
    """Запуск бота"""
    await dp.start_polling(bot)

async def start_web_server():
    """Запуск веб-сервера"""
    app = web.Application()
    app.router.add_get('/', handle_root)
    app.router.add_get('/health', handle_health)
    
    # Получаем порт из переменных окружения Render
    port = int(os.environ.get('PORT', 8000))
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logger.info(f"Веб-сервер запущен на порту {port}")

async def main():
    """Главная функция запуска"""
    print("="*60)
    print("🤖 LANNA AI SEARCH BOT v2.0")
    print("="*60)
    print(f"\n✅ Бот токен: {BOT_TOKEN[:10]}...")
    print("🆓 Бесплатно: 2 запроса в день")
    print("💎 Премиум: 25 ⭐ в месяц")
    print("\n🚀 Бот запускается на Render...")
    print("="*60)
    
    # Запускаем веб-сервер и бота параллельно
    await asyncio.gather(
        start_web_server(),
        start_bot()
    )

if __name__ == "__main__":
    # Устанавливаем certifi для корректной работы SSL
    import certifi
    import ssl
    
    # Для Windows может потребоваться дополнительная настройка
    if os.name == 'nt':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен")
