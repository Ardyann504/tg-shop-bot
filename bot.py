import os
import re
import sqlite3
import asyncio
from contextlib import closing

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

# ================== ENV ==================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CURRENCY = os.getenv("CURRENCY", "RUB")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))

if not BOT_TOKEN:
    raise SystemExit("❌ Не задан BOT_TOKEN в .env")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()

# === Реквизиты для перевода (ПОМЕНИ ВНИЗУ НА СВОИ!) ===
PAY_INSTRUCTIONS = (
    "💳 Оплата: переведите сумму на карту `1234 5678 9012 3456`\n"
    "или по телефону `+7 900 000-00-00`\n\n"
    "После оплаты отправьте сюда чек или скриншот."
)

# ================== DB ===================
DB_PATH = "db.sqlite3"

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with closing(db()) as conn, conn:
        conn.executescript("""
        PRAGMA journal_mode=WAL;

        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT,
            price_cents INTEGER NOT NULL,
            currency TEXT NOT NULL DEFAULT 'RUB',
            photo_url TEXT,
            is_active INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS carts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS cart_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cart_id INTEGER NOT NULL REFERENCES carts(id) ON DELETE CASCADE,
            product_id INTEGER NOT NULL REFERENCES products(id),
            qty INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            total_cents INTEGER NOT NULL,
            currency TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'placed',  -- placed, done, canceled
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            note TEXT
        );

        CREATE TABLE IF NOT EXISTS order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
            product_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            price_cents INTEGER NOT NULL,
            qty INTEGER NOT NULL
        );
                CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            city_code TEXT,
            city_name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
""")

        # Засеем 3 товара, если пусто
        c = conn.execute("SELECT COUNT(*) as c FROM products").fetchone()["c"]
        if c == 0:
            conn.executemany(
                "INSERT INTO products(title, description, price_cents, currency, photo_url) VALUES(?,?,?,?,?)",
                [
                    ("Футболка", "100% хлопок, унисекс", 19900, CURRENCY, "https://picsum.photos/seed/t1/800/500"),
                    ("Кружка", "Керамика, 350 мл", 9900, CURRENCY, "https://picsum.photos/seed/t2/800/500"),
                    ("Наклейки", "Набор 5 шт.", 4900, CURRENCY, "https://picsum.photos/seed/t3/800/500"),
                ]
            )

def list_products():
    with closing(db()) as conn:
        return conn.execute("SELECT * FROM products WHERE is_active=1 ORDER BY id").fetchall()

def get_user_city(user_id: int):
    with closing(db()) as conn:
        row = conn.execute(
            "SELECT city_code, city_name FROM users WHERE user_id=?",
            (user_id,)
        ).fetchone()
        return (row["city_code"], row["city_name"]) if row else (None, None)

def set_user_city(user_id: int, code: str, name: str):
    with closing(db()) as conn, conn:
        exists = conn.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,)).fetchone()
        if exists:
            conn.execute(
                "UPDATE users SET city_code=?, city_name=?, updated_at=CURRENT_TIMESTAMP WHERE user_id=?",
                (code, name, user_id)
            )
        else:
            conn.execute(
                "INSERT INTO users(user_id, city_code, city_name) VALUES(?,?,?)",
                (user_id, code, name)
            )

def get_or_create_cart(user_id: int) -> int:
    with closing(db()) as conn, conn:
        r = conn.execute("SELECT id FROM carts WHERE user_id=? AND status='active'", (user_id,)).fetchone()
        if r: return r["id"]
        cur = conn.execute("INSERT INTO carts(user_id) VALUES(?)", (user_id,))
        return cur.lastrowid

def cart_items(user_id: int):
    with closing(db()) as conn:
        cart_id = get_or_create_cart(user_id)
        return conn.execute("""
            SELECT ci.id, p.id AS product_id, p.title, p.price_cents, p.currency, ci.qty
            FROM cart_items ci JOIN products p ON p.id=ci.product_id
            WHERE ci.cart_id=?""", (cart_id,)).fetchall()

def add_to_cart(user_id: int, product_id: int, delta: int):
    with closing(db()) as conn, conn:
        cart_id = get_or_create_cart(user_id)
        row = conn.execute("SELECT id, qty FROM cart_items WHERE cart_id=? AND product_id=?",
                           (cart_id, product_id)).fetchone()
        if row:
            new_qty = row["qty"] + delta
            if new_qty <= 0:
                conn.execute("DELETE FROM cart_items WHERE id=?", (row["id"],))
            else:
                conn.execute("UPDATE cart_items SET qty=? WHERE id=?", (new_qty, row["id"]))
        elif delta > 0:
            conn.execute("INSERT INTO cart_items(cart_id, product_id, qty) VALUES(?,?,?)",
                         (cart_id, product_id, 1))
def add_to_cart_qty(user_id: int, product_id: int, qty: int):
    if qty <= 0:
        return
    with closing(db()) as conn, conn:
        cart_id = get_or_create_cart(user_id)
        row = conn.execute(
            "SELECT id, qty FROM cart_items WHERE cart_id=? AND product_id=?",
            (cart_id, product_id)
        ).fetchone()
        if row:
            new_qty = row["qty"] + qty
            conn.execute("UPDATE cart_items SET qty=? WHERE id=?", (new_qty, row["id"]))
        else:
            conn.execute("INSERT INTO cart_items(cart_id, product_id, qty) VALUES(?,?,?)",
                         (cart_id, product_id, qty))

def get_product_by_id(pid: int):
    with closing(db()) as conn:
        return conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()

def delete_from_cart(user_id: int, product_id: int):
    with closing(db()) as conn, conn:
        cart_id = get_or_create_cart(user_id)
        conn.execute("DELETE FROM cart_items WHERE cart_id=? AND product_id=?", (cart_id, product_id))

def clear_cart(user_id: int):
    with closing(db()) as conn, conn:
        cart_id = get_or_create_cart(user_id)
        conn.execute("DELETE FROM cart_items WHERE cart_id=?", (cart_id,))

def cart_total_cents(user_id: int) -> int:
    return sum(r["price_cents"] * r["qty"] for r in cart_items(user_id))

def last_order(user_id: int):
    with closing(db()) as conn:
        row = conn.execute(
            "SELECT id, total_cents, currency, created_at FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 1",
            (user_id,)
        ).fetchone()
        return row  # либо None

# ================ UI =================
def kb_main():
    kb = InlineKeyboardBuilder()
    kb.button(text="🛍 Каталог", callback_data="catalog")
    kb.button(text="🛒 Корзина", callback_data="cart")
    kb.button(text="✉️ Поддержка", callback_data="support")
    add_change_city_button(kb)  # ← ДОБАВЛЕНО
    kb.adjust(2, 2)  # 2 в ряд, последняя строка тоже уместится
    return kb.as_markup()

def product_list_kb():
    kb = InlineKeyboardBuilder()
    prods = list_products()
    # по кнопке на товар
    for p in prods:
        kb.button(text=p["title"], callback_data=f"prod:{p['id']}")
    # низ — навигация
    kb.button(text="🛒 Корзина", callback_data="cart")
    add_change_city_button(kb)
    kb.button(text="⬅️ Меню", callback_data="menu")
    # разложим сеткой (3 в ряд для товаров; последние 2–3 кнопки сами встанут ниже)
    kb.adjust(3)
    return kb.as_markup()

@dp.callback_query(F.data == "menu")
async def cb_menu(cq: CallbackQuery):
    await cq.message.answer("Главное меню:", reply_markup=kb_main())
    await cq.answer()

def add_change_city_button(kb: InlineKeyboardBuilder):
    kb.button(text="🌆 Сменить город", callback_data="change_city")

def cart_text(uid: int) -> str:
    items = cart_items(uid)
    if not items: return "Корзина пуста."
    lines = []; total = 0
    for r in items:
        s = r["price_cents"] * r["qty"]; total += s
        lines.append(f"{r['title']} × {r['qty']} — {s/100:.2f} {CURRENCY}")
    lines.append(f"\nИтого: *{total/100:.2f} {CURRENCY}*")
    return "\n".join(lines)

def cart_kb(uid: int):
    items = cart_items(uid)
    kb = InlineKeyboardBuilder()
    for r in items:
        pid = r["product_id"]
        kb.button(text=f"➖ {r['title']}", callback_data=f"dec:{pid}")
        kb.button(text=f"➕ {r['title']}", callback_data=f"inc:{pid}")
        kb.button(text=f"🗑 {r['title']}", callback_data=f"del:{pid}")
    if items:
        kb.button(text="🧾 Оформить заказ", callback_data="checkout")
        kb.button(text="Очистить", callback_data="clear")

    add_change_city_button(kb)        # ← ДОБАВЛЕНО
    kb.button(text="⬅️ Каталог", callback_data="catalog")
    kb.adjust(2)
    return kb.as_markup()

# 30 крупнейших городов РФ (сокращённые коды — удобны в БД)
CITIES = [
    ("msk", "Москва"),
    ("spb", "Санкт-Петербург"),
    ("nsk", "Новосибирск"),
    ("ekb", "Екатеринбург"),
    ("nnv", "Нижний Новгород"),
    ("kzn", "Казань"),
    ("chn", "Челябинск"),
    ("oms", "Омск"),
    ("sam", "Самара"),
    ("rst", "Ростов-на-Дону"),
    ("ufa", "Уфа"),
    ("krs", "Красноярск"),
    ("prm", "Пермь"),
    ("vrn", "Воронеж"),
    ("vol", "Волгоград"),
    ("kra", "Краснодар"),
    ("srg", "Саратов"),
    ("tyu", "Тюмень"),
    ("tol", "Тольятти"),
    ("izv", "Ижевск"),
    ("uly", "Ульяновск"),
    ("bar", "Барнаул"),
    ("irn", "Иркутск"),
    ("kbr", "Кемерово"),
    ("nnk", "Новокузнецк"),
    ("stl", "Ставрополь"),
    ("khn", "Хабаровск"),
    ("yar", "Ярославль"),
    ("vlr", "Владивосток"),
    ("mah", "Махачкала"),
]

def city_kb():
    kb = InlineKeyboardBuilder()
    for code, name in CITIES:
        kb.button(text=name, callback_data=f"city:{code}")
    kb.adjust(2)  # по 2 в ряд; можно 3: kb.adjust(3)
    return kb.as_markup()

def ensure_city_or_ask(user_id: int):
    code, name = get_user_city(user_id)
    return (code is not None), name

# ============== HANDLERS ==============
# 👋 Привет + меню, если город уже выбран
WELCOME_ANIM = "https://i.imgur.com/lP0ZJgb.gif"
WELCOME_TEXT = (
    "😼Главное не забывать: счастье — это когда ты нашёл, а тебя нет!😎\n\n"
    " ✨✨✨✨✨О нас✨✨✨✨✨\n\n"
    "✔️ ВСЕГДА ОТКРЫТО: стаж бесперебойной круглосуточной работы — свыше 7 лет;\n\n"
    "✔️ ВСЕГДА ОТКРЫТЫ: наш коллектив — зеркало покупателя, многие из нас когда-то впервые пришли в Abyss за покупками, потом зачастили, а следом и заступили на карьерную лестницу;\n\n"
    "✔️ КАЧЕСТВО ПРЕВЫШЕ АССОРТИМЕНТА: наша витрина сродни вкладке «Избранное» — пристально отфильтрованные и проверенные временем решения, если некогда пробовать и ошибаться;\n\n"
    "✔️ ЛЮДИ ПРЕЖДЕ ВСЕГО: клады находят и уходят, а взаимопонимание либо остаётся, либо нет. Общий язык — самый важный «НАХОД»;\n\n"
    "✔️ ЧЕСТНОЕ ПРЕДЛОЖЕНИЕ: рынок целиком держится на честном предложении, все здесь ищут именно его, и система работает ровно до тех пор, пока хотя бы некоторые его находят. Наши клиенты уже нашли;\n\n"
    "✔️ ЧАСТНОЕ ПРЕДЛОЖЕНИЕ: мы всегда знаем, чем помочь новичку, как удивить завсегдатая и что предложить амбициозному энтузиасту."
)

@dp.message(Command("start"))
async def start(m: Message):
    # 1) всегда шлём привет с гифкой
    try:
        await m.answer_animation(animation=WELCOME_ANIM, caption=WELCOME_TEXT)
    except Exception:
        await m.answer(WELCOME_TEXT)

    # 2) проверяем город
    has_city, city_name = ensure_city_or_ask(m.from_user.id)
    if not has_city:
        # города нет — сразу просим выбрать город
        await m.answer("Выберите город:", reply_markup=city_kb())
        return

    # 3) город уже выбран — следом показываем меню
    await m.answer(f"🏙 Ваш город: {city_name}")
    await m.answer("Выберите услугу:", reply_markup=kb_main())



@dp.callback_query(F.data.startswith("city:"))
async def choose_city(cq: CallbackQuery):
    code = cq.data.split(":")[1]
    # найдём имя по коду
    name = next((n for c, n in CITIES if c == code), None)
    if not name:
        await cq.answer("Неизвестный город", show_alert=True)
        return

    set_user_city(cq.from_user.id, code, name)
    await cq.message.answer(f"✅ Город выбран: {name}", reply_markup=kb_main())
    await cq.answer()

    # Уведомим админа
    if ADMIN_CHAT_ID:
        try:
            await bot.send_message(
                ADMIN_CHAT_ID,
                f"🏙 Пользователь @{cq.from_user.username or '—'} (id: {cq.from_user.id}) выбрал город: {name} ({code})"
            )
        except Exception:
            pass

@dp.message(Command("city"))
async def change_city(m: Message):
    await m.answer("Выберите город:", reply_markup=city_kb())

@dp.callback_query(F.data == "change_city")
async def cb_change_city(cq: CallbackQuery):
    await cq.message.answer("Выберите город:", reply_markup=city_kb())
    await cq.answer()

@dp.callback_query(F.data == "catalog")
async def show_catalog(cq: CallbackQuery):
    # блок: без города не пускаем
    has_city, _ = ensure_city_or_ask(cq.from_user.id)
    if not has_city:
        await cq.message.answer("Сначала выберите город:", reply_markup=city_kb())
        await cq.answer(); return

    prods = list_products()
    if not prods:
        await cq.message.answer("Каталог пуст.")
        await cq.answer(); return

    await cq.message.answer("Выберите товар:", reply_markup=product_list_kb())
    await cq.answer()

# Нажали на товар → спросим количество
# Нажали на товар → спросим количество (с фото товара)
@dp.callback_query(F.data.startswith("prod:"))
async def choose_product_qty(cq: CallbackQuery):
    pid = int(cq.data.split(":")[1])

    p = get_product_by_id(pid)
    if not p or not p["is_active"]:
        await cq.answer("Товар не найден", show_alert=True)
        return

    # клавиатура с количествами
    kb = InlineKeyboardBuilder()
    for q in (1, 2, 10, 20):
        kb.button(text=f"{q}", callback_data=f"addqty:{pid}:{q}")
    kb.button(text="⬅️ Каталог", callback_data="catalog")
    kb.adjust(4, 1)

    caption = f"Какая граммовка «{p['title']}»?\nЦена: {p['price_cents']/100:.2f} {p['currency']}"

    # пробуем показать фото; если нет или ссылка/file_id битые — пошлём просто текст
    photo = p["photo_url"]  # может быть HTTP(S) URL или telegram file_id
    try:
        if photo:
            await cq.message.answer_photo(
                photo=photo,
                caption=caption,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb.as_markup()
            )
        else:
            raise ValueError("no photo")
    except Exception:
        await cq.message.answer(
            caption,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb.as_markup()
        )

    await cq.answer()



# Выбрали количество → добавляем в корзину
@dp.callback_query(F.data.startswith("addqty:"))
async def add_with_qty(cq: CallbackQuery):
    _, pid_str, qty_str = cq.data.split(":")
    pid = int(pid_str); qty = int(qty_str)

    # добавим qty единиц
    add_to_cart_qty(cq.from_user.id, pid, qty)

    await cq.message.answer(f"✅ Добавлено в корзину: {qty} гр.")
    # можно сразу показать корзину или оставить меню — на твой вкус
    kb = InlineKeyboardBuilder()
    kb.button(text="🛒 Открыть корзину", callback_data="cart")
    kb.button(text="⬅️ Каталог", callback_data="catalog")
    kb.adjust(1, 1)
    await cq.message.answer("Продолжить покупки или перейти в корзину:", reply_markup=kb.as_markup())
    await cq.answer()


@dp.callback_query(F.data.startswith("add:"))
async def add(cq: CallbackQuery):
    pid = int(cq.data.split(":")[1])
    add_to_cart(cq.from_user.id, pid, +1)
    await cq.answer("Добавлено в корзину!")

@dp.callback_query(F.data == "cart")
async def cart(cq: CallbackQuery):
    has_city, _ = ensure_city_or_ask(cq.from_user.id)
    if not has_city:
        await cq.message.answer("Сначала выберите город:", reply_markup=city_kb())
        await cq.answer()
        return
    await cq.message.answer(cart_text(cq.from_user.id),
                            parse_mode=ParseMode.MARKDOWN,
                            reply_markup=cart_kb(cq.from_user.id))
    await cq.answer()

@dp.callback_query(F.data.startswith(("inc:", "dec:", "del:")))
async def qty(cq: CallbackQuery):
    action, pid = cq.data.split(":"); pid = int(pid)
    if action == "inc": add_to_cart(cq.from_user.id, pid, +1)
    elif action == "dec": add_to_cart(cq.from_user.id, pid, -1)
    elif action == "del": delete_from_cart(cq.from_user.id, pid)
    await cq.message.edit_text(cart_text(cq.from_user.id),
                               parse_mode=ParseMode.MARKDOWN,
                               reply_markup=cart_kb(cq.from_user.id))
    await cq.answer()

@dp.callback_query(F.data == "clear")
async def clear(cq: CallbackQuery):
    clear_cart(cq.from_user.id)
    await cq.message.edit_text("Корзина очищена.", reply_markup=kb_main())
    await cq.answer()

@dp.callback_query(F.data == "checkout")
async def checkout(cq: CallbackQuery):
    uid = cq.from_user.id
    items = cart_items(uid)
    if not items:
        await cq.answer("Корзина пуста", show_alert=True); return

    total = cart_total_cents(uid)

    # создаем заказ (без онлайн-оплаты)
    with closing(db()) as conn, conn:
        cur = conn.execute(
            "INSERT INTO orders(user_id, total_cents, currency, status) VALUES(?,?,?,?)",
            (uid, total, CURRENCY, "placed")
        )
        order_id = cur.lastrowid
        for r in items:
            conn.execute(
                "INSERT INTO order_items(order_id, product_id, title, price_cents, qty) VALUES(?,?,?,?,?)",
                (order_id, r["product_id"], r["title"], r["price_cents"], r["qty"])
            )
        # очистим корзину и закроем текущую
        cart_id = get_or_create_cart(uid)
        conn.execute("DELETE FROM cart_items WHERE cart_id=?", (cart_id,))
        conn.execute("UPDATE carts SET status='ordered' WHERE id=?", (cart_id,))

    text = (
        f"🧾 Заказ #{order_id} оформлен на сумму *{total/100:.2f} {CURRENCY}*\n\n"
        f"{PAY_INSTRUCTIONS}"
    )
    await cq.message.answer(text, parse_mode=ParseMode.MARKDOWN)
    await cq.answer()

    # уведомление админу
    if ADMIN_CHAT_ID:
        try:
            await bot.send_message(
                ADMIN_CHAT_ID,
                f"🆕 Заказ #{order_id} от @{cq.from_user.username or uid}\n"
                f"Сумма: {total/100:.2f} {CURRENCY}"
            )
        except Exception:
            pass

# -------- Поддержка --------
@dp.callback_query(F.data == "support")
async def support_btn(cq: CallbackQuery):
    await cq.message.answer("Напишите свой вопрос одной строкой: /support ваш_текст")
    await cq.answer()

@dp.message(Command("support"))
async def support_cmd(m: Message):
    text = m.text.partition(" ")[2].strip()
    if not text:
        await m.answer("Напишите: /support ваш_вопрос"); return
    await m.answer("📨 Спасибо! Мы получили ваш вопрос и ответим в этом чате.")
    if ADMIN_CHAT_ID:
        try:
            await bot.send_message(
                ADMIN_CHAT_ID,
                f"📩 Вопрос от @{m.from_user.username or '—'} (id: {m.from_user.id}):\n{text}"
            )
        except Exception:
            pass
# -------- Ответы админа пользователям (текст ИЛИ медиа) --------
@dp.message(Command("reply"))
async def admin_reply(m: Message):
    # Только админ
    if not (ADMIN_CHAT_ID and m.from_user.id == ADMIN_CHAT_ID):
        return await m.answer("Недостаточно прав.")

    # ---------- утилита отправки (умеет текст и медиа) ----------
    async def send_to_user(user_id: int, reply_text: str):
        # если админ прислал медиа — отправляем тем же типом
        if m.photo:
            # берём самое большое фото
            return await bot.send_photo(user_id, m.photo[-1].file_id, caption=reply_text or None)
        if m.document:
            return await bot.send_document(user_id, m.document.file_id, caption=reply_text or None)
        if m.video:
            return await bot.send_video(user_id, m.video.file_id, caption=reply_text or None)
        if m.animation:  # gif
            return await bot.send_animation(user_id, m.animation.file_id, caption=reply_text or None)
        if m.audio:
            return await bot.send_audio(user_id, m.audio.file_id, caption=reply_text or None)
        if m.voice:
            return await bot.send_voice(user_id, m.voice.file_id, caption=reply_text or None)
        # иначе обычный текст
        return await bot.send_message(user_id, f"✉️ Ответ поддержки:\n{reply_text}")

    # ---------- Вариант А: /reply как РЕПЛАЙ на уведомление бота ----------
    if m.reply_to_message:
        src = (m.reply_to_message.text or "") + "\n" + (m.reply_to_message.caption or "")
        import re
        id_match = re.search(r"id:\s*(\d+)", src)
        if id_match:
            user_id = int(id_match.group(1))
            # текст ответа — всё после /reply (в caption или в тексте сообщения админа)
            reply_text = m.text.partition(" ")[2].strip() if m.text else (m.caption or "")
            try:
                await send_to_user(user_id, reply_text)
                return await m.answer("✅ Отправлено пользователю.")
            except Exception as e:
                return await m.answer(f"❌ Не удалось отправить: {e}")
        # если не нашли id — упадём в вариант B

    # ---------- Вариант B: /reply <user_id|@username> <текст> ----------
    # В этом варианте команда и таргет находятся в тексте/подписи
    raw = (m.text or m.caption or "").strip()
    parts = raw.split(maxsplit=2)
    if len(parts) < 3:
        return await m.answer(
            "Формат:\n"
            "/reply <user_id или @username> <текст>\n"
            "ИЛИ: ответьте реплаем на сообщение бота про вопрос и напишите: /reply ваш_текст\n"
            "Можно прикреплять фото/док/видео."
        )

    target = parts[1]
    reply_text = parts[2].strip()

    # Определяем user_id
    user_id = None
    if target.isdigit():
        user_id = int(target)
    elif target.startswith("@"):
        try:
            ch = await bot.get_chat(target)
            user_id = ch.id
        except Exception:
            return await m.answer("Не удалось получить пользователя по username. Убедись, что он писал боту.")

    if not user_id:
        return await m.answer("Не понял, кому отвечаем. Укажи числовой id или @username.")

    try:
        await send_to_user(user_id, reply_text)
        await m.answer("✅ Отправлено пользователю.")
    except Exception as e:
        await m.answer(f"❌ Не удалось отправить: {e}")

# -------- Чеки / подтверждения оплаты --------
# Ловим медиа: фото, документ, видео, анимация — и шлём админу копию
@dp.message( F.photo | F.document | F.video | F.animation )
async def payment_proof_forwarder(m: Message):
    # соберём инфо о последнем заказе
    lo = last_order(m.from_user.id)
    if lo:
        order_info = f"последний заказ #{lo['id']} на {lo['total_cents']/100:.2f} {lo['currency']}"
    else:
        order_info = "заказы не найдены"

    header = (
        f"📥 Чек от @{m.from_user.username or '—'} (id: {m.from_user.id})\n"
        f"ℹ️ {order_info}"
    )

    # сообщим админу текстом и скопируем само сообщение с файлом
    if ADMIN_CHAT_ID:
        try:
            await bot.send_message(ADMIN_CHAT_ID, header)
            # copy_message сохраняет тип и вложение (фото/док и т.д.)
            await bot.copy_message(
                chat_id=ADMIN_CHAT_ID,
                from_chat_id=m.chat.id,
                message_id=m.message_id
            )
        except Exception:
            pass

    # ответ пользователю
    await m.answer("Спасибо! Чек отправлен в поддержку. Мы проверим и вернёмся с ответом.")

# ----- Простые админ-команды (только для ADMIN_CHAT_ID) -----
def is_admin(uid: int) -> bool:
    return ADMIN_CHAT_ID and uid == ADMIN_CHAT_ID

# Полное удаление
@dp.message(Command("delp"))
async def delete_product_cmd(m: Message):
    if not (ADMIN_CHAT_ID and m.from_user.id == ADMIN_CHAT_ID):
        return await m.answer("Недостаточно прав.")
    try:
        pid = int(m.text.split()[1])
    except Exception:
        return await m.answer("Используй: /delp <id>")
    with closing(db()) as conn, conn:
        conn.execute("DELETE FROM products WHERE id=?", (pid,))
    await m.answer(f"🗑️ Товар {pid} удалён.")

# /addp Название ; 199.00 ; Описание ; https://...
@dp.message(Command("addp"))
async def add_product(m: Message):
    if not is_admin(m.from_user.id):
        return await m.answer("Недостаточно прав.")
    raw = m.text.partition(" ")[2]
    parts = [p.strip() for p in raw.split(";")]
    if len(parts) < 2:
        return await m.answer('Формат: /addp Название ; 199.00 ; Описание ; https://photo\n(Описание и фото — опционально)')
    title = parts[0]
    try:
        price_cents = int(round(float(parts[1].replace(",", ".")) * 100))
    except Exception:
        return await m.answer("Цена должна быть числом (например 199.00)")
    desc = parts[2] if len(parts) > 2 else ""
    photo = parts[3] if len(parts) > 3 else None
    with closing(db()) as conn, conn:
        conn.execute(
            "INSERT INTO products(title, description, price_cents, currency, photo_url) VALUES(?,?,?,?,?)",
            (title, desc, price_cents, CURRENCY, photo)
        )
    await m.answer("✅ Товар добавлен. Откройте Каталог, чтобы увидеть его.")

# /hidep 3  (скрыть товар по id)
@dp.message(Command("hidep"))
async def hide_product(m: Message):
    if not is_admin(m.from_user.id):
        return await m.answer("Недостаточно прав.")
    pid = m.text.strip().split(" ")
    if len(pid) < 2 or not pid[1].isdigit():
        return await m.answer("Формат: /hidep ID")
    with closing(db()) as conn, conn:
        conn.execute("UPDATE products SET is_active=0 WHERE id=?", (int(pid[1]),))
    await m.answer("✅ Товар скрыт.")

@dp.message(Command("ls"))
async def list_products_cmd(m: Message):
    if not (ADMIN_CHAT_ID and m.from_user.id == ADMIN_CHAT_ID):
        return await m.answer("Недостаточно прав.")
    prods = list_products()
    if not prods:
        return await m.answer("Каталог пуст.")
    txt = "\n".join([f"{p['id']}: {p['title']} ({p['price_cents']/100:.2f} {p['currency']})"
                     for p in prods])
    await m.answer("📦 Товары:\n" + txt)


# /editp id ; Название ; 199.00 ; Описание ; https://ссылка_на_фото
@dp.message(Command("editp"))
async def edit_product(m: Message):
    if not (ADMIN_CHAT_ID and m.from_user.id == ADMIN_CHAT_ID):
        return await m.answer("Недостаточно прав.")
    raw = m.text.partition(" ")[2]
    parts = [p.strip() for p in raw.split(";")]
    if len(parts) < 2 or not parts[0].isdigit():
        return await m.answer(
            "Формат: /editp id ; Название ; 199.00 ; Описание ; https://ссылка\n"
            "(Название, цена, описание, картинка — опциональны, можно менять только нужное)"
        )

    pid = int(parts[0])
    updates = {}
    if len(parts) > 1 and parts[1]:
        updates["title"] = parts[1]
    if len(parts) > 2 and parts[2]:
        try:
            updates["price_cents"] = int(round(float(parts[2].replace(",", ".")) * 100))
        except Exception:
            return await m.answer("Цена должна быть числом (например 199.00)")
    if len(parts) > 3:
        updates["description"] = parts[3]
    if len(parts) > 4:
        updates["photo_url"] = parts[4]

    if not updates:
        return await m.answer("Нет данных для изменения.")

    q = "UPDATE products SET " + ", ".join(f"{k}=?" for k in updates.keys()) + " WHERE id=?"
    params = list(updates.values()) + [pid]

    with closing(db()) as conn, conn:
        conn.execute(q, params)

    await m.answer(f"✅ Товар #{pid} обновлён.")


# /adminhelp — список всех админских команд
@dp.message(Command("adminhelp"))
async def admin_help(m: Message):
    if not (ADMIN_CHAT_ID and m.from_user.id == ADMIN_CHAT_ID):
        return await m.answer("Недостаточно прав.")
    text = (
        "🛠 Админ-команды:\n\n"
        "/ls — список активных товаров\n"
        "/addp Название ; 199.00 ; Описание ; ссылка_на_фото — добавить товар\n"
        "/hidep id — скрыть товар\n"
        "/editp id ; Название ; 199.00 ; Описание ; ссылка_на_фото — изменить товар\n"
        "/reply <user_id/@username> текст — ответить пользователю на его вопрос\n"
        "/adminhelp — показать это меню\n"
    )
    await m.answer(text)


# =============== ENTRY ===============
async def main():
    init_db()
    print("✅ Бот запущен (ручные переводы). Нажмите Ctrl+C для остановки.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("🛑 Остановка бота.")