import os
import logging
import sqlite3
import uuid
from html import escape
from yookassa import Payment, Configuration
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackContext, CallbackQueryHandler, JobQueue
from telegram.constants import ParseMode
import ollama
from dotenv import load_dotenv


load_dotenv()


logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)


TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')


Configuration.account_id = os.getenv('YOOKASSA_SHOP_ID', '...')
Configuration.secret_key = os.getenv('YOOKASSA_SECRET_KEY', '...')

class YooKassaBot:
    def __init__(self, telegram_token: str):
        self.application = Application.builder().token(telegram_token).build()
        self.conn = sqlite3.connect('tokens.db', check_same_thread=False)
        self.init_db()
        
        self.COST_PER_REQUEST = 10 
        self.conversation_history = {}  
        
        self.token_packs = {
            'small': {'tokens': 1000, 'price': 100.00, 'label': '🔹 1,000 токенов'},
            'medium': {'tokens': 5000, 'price': 450.00, 'label': '🔸 5,000 токенов'},
            'large': {'tokens': 15000, 'price': 1200.00, 'label': '🔶 15,000 токенов'},
            'premium': {'tokens': 50000, 'price': 3500.00, 'label': '💎 50,000 токенов'}
        }
        
        self.ollama_available = self.check_ollama()

        self.setup_handlers()

        if self.application.job_queue:
            self.application.job_queue.run_repeating(
                self.check_pending_payments,
                interval=30,  
                first=10
            )

    def init_db(self):
        cursor = self.conn.cursor()

        cursor.execute('DROP TABLE IF EXISTS users')
        cursor.execute('DROP TABLE IF EXISTS payments')
        cursor.execute('DROP TABLE IF EXISTS orders')

        cursor.execute('''
            CREATE TABLE users (
                user_id TEXT PRIMARY KEY,
                tokens INTEGER DEFAULT 100,
                total_spent REAL DEFAULT 0,
                registered TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE TABLE payments (
                payment_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT,
                amount REAL,
                tokens_added INTEGER,
                yookassa_id TEXT,
                status TEXT DEFAULT 'pending',
                description TEXT,
                created TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE TABLE orders (
                order_id TEXT PRIMARY KEY,
                user_id TEXT,
                pack_id TEXT,
                tokens INTEGER,
                price REAL,
                yookassa_payment_id TEXT,
                status TEXT DEFAULT 'created',
                created TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        self.conn.commit()
        logger.info("База данных пересоздана с правильной структурой")

    def check_ollama(self):
        try:
            ollama.list()
            logger.info("Ollama доступен")
            return True
        except Exception as e:
            logger.error(f"Ollama недоступен: {e}")
            return False

    def setup_handlers(self):
        self.application.add_handler(CommandHandler("start", self.start))
        self.application.add_handler(CommandHandler("help", self.help_command))
        self.application.add_handler(CommandHandler("menu", self.show_menu))
        self.application.add_handler(CommandHandler("balance", self.show_balance))
        self.application.add_handler(CommandHandler("buy", self.buy_tokens))
        self.application.add_handler(CommandHandler("history", self.payment_history))
        self.application.add_handler(CommandHandler("clear", self.clear_history))

        self.application.add_handler(CallbackQueryHandler(self.button_handler))

        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))

    async def start(self, update: Update, context: CallbackContext):
        user = update.effective_user
        user_id = str(user.id)

        self.register_user(user_id)

        if user_id in self.conversation_history:
            del self.conversation_history[user_id]
        
        welcome_text = (
            f"👋 Привет, {user.first_name}!\n\n"
            "🤖 <b>Добро пожаловать в AI-бота с автоматической оплатой через YooKassa!</b>\n\n"
            f"🎁 <b>Бесплатный бонус:</b> 100 токенов\n"
            f"💸 <b>Стоимость запроса:</b> {self.COST_PER_REQUEST} токенов\n\n"
            "<b>Основные команды:</b>\n"
            "/buy - Купить токены (YooKassa)\n"
            "/balance - Проверить баланс\n"
            "/history - История платежей\n"
            "/clear - Очистить историю диалога\n"
            "/menu - Главное меню\n\n"
            "💡 Просто напишите сообщение для общения с ИИ!"
        )
        
        keyboard = [
            [InlineKeyboardButton("🛒 Купить токены", callback_data="buy")],
            [InlineKeyboardButton("💰 Баланс", callback_data="balance")],
            [InlineKeyboardButton("📋 Меню", callback_data="menu")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(welcome_text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)

    async def clear_history(self, update: Update, context: CallbackContext):
        user_id = str(update.effective_user.id)
        
        if user_id in self.conversation_history:
            del self.conversation_history[user_id]
            await update.message.reply_text("✅ История диалога очищена!")
        else:
            await update.message.reply_text("📭 История диалога уже пуста!")

    async def help_command(self, update: Update, context: CallbackContext):
        help_text = (
            "🤖 <b>Помощь по боту</b>\n\n"
            "<b>Как это работает:</b>\n"
            "1. У вас есть токены (начальный бонус: 100)\n"
            "2. Каждый запрос к ИИ стоит 10 токенов\n"
            "3. Пополняйте баланс через YooKassa\n\n"
            "<b>Команды:</b>\n"
            "/start - Начало работы\n"
            "/buy - Купить токены\n"
            "/balance - Баланс\n"
            "/history - История платежей\n"
            "/clear - Очистить историю диалога\n"
            "/menu - Главное меню\n\n"
            "<b>Оплата:</b>\n"
            "• Принимаем карты, Яндекс.Деньги, СБП\n"
            "• Мгновенное зачисление токенов\n"
            "• Безопасно через YooKassa"
        )
        
        await update.message.reply_text(help_text, parse_mode=ParseMode.HTML)

    async def show_menu(self, update: Update, context: CallbackContext):
        menu_text = "🏠 <b>Главное меню</b>\n\nВыберите действие:"
        
        keyboard = [
            [InlineKeyboardButton("🤖 Задать вопрос", callback_data="ask_question")],
            [InlineKeyboardButton("💰 Баланс", callback_data="balance")],
            [InlineKeyboardButton("🛒 Купить токены", callback_data="buy")],
            [InlineKeyboardButton("📜 История платежей", callback_data="history")],
            [InlineKeyboardButton("🗑️ Очистить историю", callback_data="clear_history")],
            [InlineKeyboardButton("❓ Помощь", callback_data="help")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        if update.message:
            await update.message.reply_text(menu_text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        elif update.callback_query:
            await update.callback_query.edit_message_text(menu_text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)

    async def show_balance(self, update: Update, context: CallbackContext):
        user_id = str(update.effective_user.id)
        user_info = self.get_user_info(user_id)
        
        if not user_info:
            if update.message:
                await update.message.reply_text("❌ Вы не зарегистрированы. Используйте /start")
            elif update.callback_query:
                await update.callback_query.answer("❌ Вы не зарегистрированы. Используйте /start")
            return
        
        balance_text = (
            f"💰 <b>Ваш баланс:</b>\n\n"
            f"🪙 <b>Токены:</b> {user_info['tokens']:,}\n"
            f"💵 <b>Всего потрачено:</b> {user_info['total_spent']:.2f} руб.\n\n"
            f"📊 <b>Статистика:</b>\n"
            f"• Доступно запросов: {user_info['tokens'] // self.COST_PER_REQUEST}\n"
            f"• Стоимость запроса: {self.COST_PER_REQUEST} токенов\n\n"
            f"🛒 <b>Пополнить:</b> /buy"
        )
        
        keyboard = [
            [InlineKeyboardButton("🛒 Купить токены", callback_data="buy")],
            [InlineKeyboardButton("📜 История платежей", callback_data="history")],
            [InlineKeyboardButton("📋 Меню", callback_data="menu")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        if update.message:
            await update.message.reply_text(balance_text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        elif update.callback_query:
            await update.callback_query.edit_message_text(balance_text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)

    async def buy_tokens(self, update: Update, context: CallbackContext):
        buy_text = (
            "🛒 <b>Покупка токенов через YooKassa</b>\n\n"
            "<b>Выберите пакет:</b>\n"
            "• Безопасная оплата картой, Яндекс.Деньги, СБП\n"
            "• Мгновенное зачисление токенов\n"
            "• Автоматическая проверка платежа\n"
        )
        
        keyboard = []
        for pack_id, pack in self.token_packs.items():
            keyboard.append([InlineKeyboardButton(
                f"{pack['label']} - {pack['price']:.0f}₽",
                callback_data=f"create_payment_{pack_id}"
            )])
        
        keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="menu")])
        reply_markup = InlineKeyboardMarkup(keyboard)

        if update.message:
            await update.message.reply_text(buy_text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        elif update.callback_query:
            await update.callback_query.edit_message_text(buy_text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)

    async def create_yookassa_payment(self, update: Update, pack_id: str):
        query = update.callback_query
        
        user = update.effective_user
        user_id = str(user.id)
        
        pack = self.token_packs.get(pack_id)
        if not pack:
            await query.answer("❌ Пакет не найден")
            return

        order_id = str(uuid.uuid4())
        
        try:
            payment = Payment.create({
                "amount": {
                    "value": f"{pack['price']:.2f}",
                    "currency": "RUB"
                },
                "confirmation": {
                    "type": "redirect",
                    "return_url": "https://t.me/"
                },
                "capture": True,
                "description": f"Покупка {pack['tokens']:,} токенов в AI боте",
                "metadata": {
                    "user_id": user_id,
                    "order_id": order_id,
                    "pack_id": pack_id,
                    "tokens": pack['tokens'],
                    "username": user.username or user.first_name
                }
            }, str(uuid.uuid4()))
            
            logger.info(f"Создан платеж YooKassa: {payment.id} для пользователя {user_id}")

            cursor = self.conn.cursor()
            cursor.execute('''
                INSERT INTO orders (order_id, user_id, pack_id, tokens, price, yookassa_payment_id, status)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                order_id,
                user_id,
                pack_id,
                pack['tokens'],
                pack['price'],
                payment.id,
                'created'
            ))

            cursor.execute('''
                INSERT INTO payments (user_id, amount, tokens_added, yookassa_id, status, description)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (
                user_id,
                pack['price'],
                pack['tokens'],
                payment.id,
                'pending',
                f"Покупка {pack['tokens']:,} токенов"
            ))
            
            self.conn.commit()

            payment_text = (
                f"💳 <b>Оплата {pack['label']}</b>\n\n"
                f"💰 <b>Сумма:</b> {pack['price']:.0f} руб.\n"
                f"🪙 <b>Вы получите:</b> {pack['tokens']:,} токенов\n\n"
                f"🆔 <b>Номер заказа:</b> <code>{order_id}</code>\n\n"
                "⏳ <b>Ссылка на оплату действует 24 часа</b>\n\n"
                "Нажмите кнопку ниже для оплаты:"
            )
            
            keyboard = [
                [InlineKeyboardButton("💳 Оплатить картой/СБП", url=payment.confirmation.confirmation_url)],
                [InlineKeyboardButton("🔄 Проверить оплату", callback_data=f"check_payment_{order_id}")],
                [InlineKeyboardButton("❌ Отменить заказ", callback_data=f"cancel_order_{order_id}")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                payment_text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML
            )
            
            await query.answer("✅ Платеж создан!")
            
        except Exception as e:
            logger.error(f"Ошибка создания платежа YooKassa: {e}", exc_info=True)
            await query.answer("❌ Ошибка создания платежа. Попробуйте позже.")

    async def check_payment_status(self, update: Update, order_id: str):
        query = update.callback_query
        await query.answer()
        
        cursor = self.conn.cursor()
        cursor.execute('SELECT yookassa_payment_id, status FROM orders WHERE order_id = ?', (order_id,))
        order = cursor.fetchone()
        
        if not order:
            await query.answer("❌ Заказ не найден")
            return
        
        payment_id, order_status = order
        
        if order_status == 'paid':
            await query.answer("✅ Платеж уже подтвержден")
            return
        
        try:
            payment = Payment.find_one(payment_id)
            
            if payment.status == 'succeeded':
                await self.process_successful_payment(payment_id, order_id)
                
                success_text = (
                    "🎉 <b>Оплата подтверждена!</b>\n\n"
                    "✅ <b>Токены зачислены на ваш баланс</b>\n\n"
                    "💰 Проверьте баланс: /balance\n"
                    "🤖 Задавайте вопросы!"
                )
                
                keyboard = [
                    [InlineKeyboardButton("📋 Главное меню", callback_data="menu")],
                    [InlineKeyboardButton("💰 Баланс", callback_data="balance")],
                    [InlineKeyboardButton("🤖 Задать вопрос", callback_data="ask_question")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await query.edit_message_text(
                    success_text,
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.HTML
                )
                
            elif payment.status == 'pending':
                await query.answer("⏳ Платеж еще не прошел. Попробуйте позже.")
            else:
                await query.answer("❌ Платеж не прошел или отменен")
                
        except Exception as e:
            logger.error(f"Ошибка проверки платежа: {e}")
            await query.answer("❌ Ошибка проверки платежа")

    async def process_successful_payment(self, payment_id: str, order_id: str):
        cursor = self.conn.cursor()

        cursor.execute('''
            SELECT o.user_id, o.tokens, o.price 
            FROM orders o 
            WHERE o.order_id = ? AND o.status != 'paid'
        ''', (order_id,))
        
        order = cursor.fetchone()
        
        if not order:
            return
        
        user_id, tokens, price = order

        cursor.execute('''
            UPDATE users 
            SET tokens = tokens + ?, total_spent = total_spent + ? 
            WHERE user_id = ?
        ''', (tokens, price, user_id))

        cursor.execute('''
            UPDATE orders 
            SET status = 'paid' 
            WHERE order_id = ?
        ''', (order_id,))

        cursor.execute('''
            UPDATE payments 
            SET status = 'completed', updated = CURRENT_TIMESTAMP 
            WHERE yookassa_id = ?
        ''', (payment_id,))
        
        self.conn.commit()

        try:
            user_info = self.get_user_info(user_id)
            if user_info:
                keyboard = [
                    [InlineKeyboardButton("📋 Главное меню", callback_data="menu")],
                    [InlineKeyboardButton("💰 Баланс", callback_data="balance")],
                    [InlineKeyboardButton("🤖 Задать вопрос", callback_data="ask_question")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await self.application.bot.send_message(
                    chat_id=int(user_id),
                    text=f"🎉 <b>Токены зачислены!</b>\n\n"
                         f"✅ Получено: {tokens:,} токенов\n"
                         f"💰 Ваш баланс: {user_info['tokens']:,} токенов\n\n"
                         f"Спасибо за покупку! 🛒",
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.HTML
                )
        except Exception as e:
            logger.error(f"Ошибка отправки уведомления: {e}")

    async def check_pending_payments(self, context: CallbackContext):
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT o.order_id, o.yookassa_payment_id 
            FROM orders o 
            WHERE o.status = 'created' 
            AND datetime(o.created) > datetime('now', '-1 day')
        ''')
        
        pending_orders = cursor.fetchall()
        
        for order_id, payment_id in pending_orders:
            try:
                payment = Payment.find_one(payment_id)
                
                if payment.status == 'succeeded':
                    await self.process_successful_payment(payment_id, order_id)
                    logger.info(f"Платеж {payment_id} подтвержден автоматически")
                elif payment.status in ['canceled', 'failed']:
                    cursor.execute('UPDATE orders SET status = ? WHERE order_id = ?', ('failed', order_id))
                    cursor.execute('UPDATE payments SET status = ? WHERE yookassa_id = ?', ('failed', payment_id))
                    self.conn.commit()
                    
            except Exception as e:
                logger.error(f"Ошибка проверки платежа {payment_id}: {e}")

        cursor.execute('''
            DELETE FROM orders 
            WHERE status IN ('failed', 'canceled') 
            AND datetime(created) < datetime('now', '-7 days')
        ''')
        self.conn.commit()

    async def payment_history(self, update: Update, context: CallbackContext):
        user_id = str(update.effective_user.id)
        
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT amount, tokens_added, status, created, description 
            FROM payments 
            WHERE user_id = ? 
            ORDER BY created DESC 
            LIMIT 10
        ''', (user_id,))
        
        payments = cursor.fetchall()
        
        if not payments:
            if update.message:
                await update.message.reply_text("📭 У вас пока нет платежей")
            elif update.callback_query:
                await update.callback_query.message.reply_text("📭 У вас пока нет платежей")
            return
        
        history_text = "📜 <b>История платежей:</b>\n\n"
        
        for i, (amount, tokens, status, created, description) in enumerate(payments, 1):
            status_emoji = "✅" if status == 'completed' else "⏳" if status == 'pending' else "❌"
            date_str = created[:16] if isinstance(created, str) else str(created)[:16]
            
            history_text += (
                f"{i}. <b>{description}</b>\n"
                f"   💰 Сумма: {amount:.2f} руб.\n"
                f"   🪙 Токены: {tokens:,}\n"
                f"   📅 Дата: {date_str}\n"
                f"   Статус: {status_emoji} {status}\n\n"
            )
        
        keyboard = [
            [InlineKeyboardButton("🛒 Купить токены", callback_data="buy")],
            [InlineKeyboardButton("📋 Главное меню", callback_data="menu")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        if update.message:
            await update.message.reply_text(history_text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        elif update.callback_query:
            await update.callback_query.edit_message_text(history_text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)

    async def handle_message(self, update: Update, context: CallbackContext):
        user_message = update.message.text
        user_id = str(update.effective_user.id)

        user_info = self.get_user_info(user_id)
        if not user_info or user_info['tokens'] < self.COST_PER_REQUEST:
            await update.message.reply_text(
                f"❌ <b>Недостаточно токенов!</b>\n\n"
                f"💰 Ваш баланс: {user_info['tokens'] if user_info else 0} токенов\n"
                f"💸 Нужно: {self.COST_PER_REQUEST} токенов\n\n"
                f"🛒 Пополнить баланс: /buy",
                parse_mode=ParseMode.HTML
            )
            return

        if not self.ollama_available:
            await update.message.reply_text("❌ ИИ временно недоступен. Попробуйте позже.")
            return

        await update.message.chat.send_action(action="typing")
        
        try:
            cursor = self.conn.cursor()
            cursor.execute('UPDATE users SET tokens = tokens - ? WHERE user_id = ?', (self.COST_PER_REQUEST, user_id))
            self.conn.commit()

            history = self.conversation_history.get(user_id, [])

            system_prompt = """Ты - исключительно русскоязычный ассистент. Твои правила:

1. ВСЕГДА отвечай ТОЛЬКО на русском языке
2. Если в запросе есть английские слова, переводи их на русский
3. НИКОГДА не смешивай языки в ответе
4. Если не знаешь ответа, честно скажи об этом
5. Будь полезным, вежливым и конкретным
6. Отвечай развернуто"""

            context = ""
            if history:
                context = "Контекст диалога:\n"
                for role, msg in history[-3:]:
                    speaker = "Пользователь" if role == 'user' else "Ассистент"
                    context += f"{speaker}: {msg}\n"
                context += "\n"

            full_prompt = f"""{system_prompt}

{context}Текущий запрос: {user_message}

Помни: отвечай ТОЛЬКО на русском языке! Ответ:"""
            
            answer = ""
            models_to_try = ['mistral', 'llama2', 'neural-chat', 'openchat']
            
            for model in models_to_try:
                try:
                    response = ollama.generate(
                        model=model,
                        prompt=full_prompt,
                        options={
                            'temperature': 0.3,
                            'num_predict': 1000,
                            'top_k': 40,
                            'top_p': 0.9
                        }
                    )
                    answer = response['response'].strip()

                    russian_chars = set('абвгдеёжзийклмнопрстуфхцчшщъыьэюя')
                    answer_chars = set(answer.lower())
                    russian_ratio = len(answer_chars & russian_chars) / max(1, len(answer_chars))
                    
                    if russian_ratio > 0.5:  
                        logger.info(f"✅ Успешно использована модель: {model}")
                        break
                    else:
                        logger.warning(f"⚠️ Модель {model} дала нерусский ответ, пробуем следующую...")
                        answer = self.filter_english_text(answer)  
                        if len(answer.strip()) > 50:  
                            break
                        continue
                        
                except Exception as e:
                    logger.error(f"Ошибка с моделью {model}: {e}")
                    continue

            if not answer or len(answer.strip()) < 20:
                answer = "Извините, в данный момент не могу дать качественный ответ на русском языке. Попробуйте перефразировать вопрос или обратитесь позже."

            if user_id not in self.conversation_history:
                self.conversation_history[user_id] = []
            
            self.conversation_history[user_id].append(('user', user_message))
            self.conversation_history[user_id].append(('assistant', answer))

            if len(self.conversation_history[user_id]) > 8:
                self.conversation_history[user_id] = self.conversation_history[user_id][-8:]

            user_info = self.get_user_info(user_id)

            keyboard = [
                [InlineKeyboardButton("💰 Баланс", callback_data="balance")],
                [InlineKeyboardButton("🛒 Купить токены", callback_data="buy")],
                [InlineKeyboardButton("🗑️ Очистить историю", callback_data="clear_history")],
                [InlineKeyboardButton("🤖 Новый вопрос", callback_data="ask_question")],
                [InlineKeyboardButton("📋 Главное меню", callback_data="menu")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await update.message.reply_text(
                f"{escape(answer)}\n\n"
                f"💸 <b>Списано:</b> {self.COST_PER_REQUEST} токенов\n"
                f"💰 <b>Баланс:</b> {user_info['tokens']:,} токенов",
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML
            )
            
        except Exception as e:
            logger.error(f"Ошибка обработки сообщения: {e}", exc_info=True)

            cursor = self.conn.cursor()
            cursor.execute('UPDATE users SET tokens = tokens + ? WHERE user_id = ?', (self.COST_PER_REQUEST, user_id))
            self.conn.commit()
            
            await update.message.reply_text(
                "❌ <b>Произошла ошибка</b>\n"
                "💰 Токены были возвращены\n"
                "Попробуйте еще раз или обратитесь в поддержку",
                parse_mode=ParseMode.HTML
            )

    def filter_english_text(self, text: str) -> str:
        if not text:
            return ""

        sentences = []
        current_sentence = ""
        
        for char in text + " ": 
            current_sentence += char
            if char in '.!?':
                russian_count = sum(1 for c in current_sentence if 'а' <= c.lower() <= 'я' or c == 'ё')
                english_count = sum(1 for c in current_sentence if 'a' <= c.lower() <= 'z')
                total_letters = russian_count + english_count
                
                if total_letters == 0 or russian_count / total_letters >= 0.7:  # 70% русских букв
                    sentences.append(current_sentence.strip())
                current_sentence = ""

        if current_sentence.strip():
            russian_count = sum(1 for c in current_sentence if 'а' <= c.lower() <= 'я' or c == 'ё')
            english_count = sum(1 for c in current_sentence if 'a' <= c.lower() <= 'z')
            total_letters = russian_count + english_count
            
            if total_letters == 0 or russian_count / total_letters >= 0.7:
                sentences.append(current_sentence.strip())
        
        result = ' '.join(sentences)

        if not result or len(result.strip()) < 20:
            return "К сожалению, ответ содержит слишком много английского текста. Попробуйте задать вопрос более конкретно на русском языке."
        
        return result

    async def button_handler(self, update: Update, context: CallbackContext):
        query = update.callback_query
        data = query.data
        
        try:
            if data == "menu":
                await query.answer()
                await self.show_menu(update, context)
            elif data == "balance":
                await query.answer()
                await self.show_balance(update, context)
            elif data == "buy":
                await query.answer()
                await self.buy_tokens(update, context)
            elif data == "history":
                await query.answer()
                await self.payment_history(update, context)
            elif data == "help":
                await query.answer()
                await self.help_command(update, context)
            elif data == "ask_question":
                await query.answer("Напишите ваш вопрос в чат!")
                await query.edit_message_text("💬 Напишите ваш вопрос в чат!")
            elif data == "clear_history":
                user_id = str(update.effective_user.id)
                if user_id in self.conversation_history:
                    del self.conversation_history[user_id]
                    await query.answer("✅ История диалога очищена!")
                    await query.edit_message_text("🗑️ История диалога очищена!")
                else:
                    await query.answer("📭 История диалога уже пуста!")
            elif data.startswith("create_payment_"):
                pack_id = data.split("_")[2]
                await self.create_yookassa_payment(update, pack_id)
            elif data.startswith("check_payment_"):
                order_id = data.split("_")[2]
                await self.check_payment_status(update, order_id)
            elif data.startswith("cancel_order_"):
                order_id = data.split("_")[2]
                await query.answer("Заказ отменен")
                await query.edit_message_text("❌ Заказ отменен")
            else:
                await query.answer(f"Неизвестная команда: {data}")
        except Exception as e:
            logger.error(f"Ошибка в обработчике кнопок: {e}")
            await query.answer("❌ Произошла ошибка, попробуйте позже")

    def register_user(self, user_id: str):
        cursor = self.conn.cursor()
        cursor.execute('INSERT OR IGNORE INTO users (user_id, tokens) VALUES (?, 100)', (user_id,))
        self.conn.commit()

    def get_user_info(self, user_id: str):
        cursor = self.conn.cursor()
        cursor.execute('SELECT tokens, total_spent FROM users WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        
        if result:
            return {
                'tokens': result[0],
                'total_spent': result[1]
            }
        return None

    def run(self):
        logger.info("Бот с YooKassa запущен!")
        logger.info(f"Пакеты токенов: {len(self.token_packs)}")
        logger.info(f"Ollama: {'Доступен' if self.ollama_available else 'Недоступен'}")

        
        self.application.run_polling()


def main():
    from pathlib import Path
    
    env_path = Path('.') / '.env'
    if not env_path.exists():
        print("Файл .env не найден")
    
    load_dotenv()
    
    TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN == "ваш_токен_бота":
        print("ELEGRAM_TOKEN не установлен или установлен по умолчанию")
        return
    
    if not os.getenv('YOOKASSA_SHOP_ID') or not os.getenv('YOOKASSA_SECRET_KEY'):
        print("Данные YooKassa не настроены")

    bot = YooKassaBot(TELEGRAM_TOKEN)
    bot.run()
    
if __name__ == "__main__":
    main()