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

# ================ UI =================
def kb_main():
    kb = InlineKeyboardBuilder()
    kb.button(text="🛍 Каталог", callback_data="catalog")
    kb.button(text="🛒 Корзина", callback_data="cart")
    kb.button(text="✉️ Поддержка", callback_data="support")
    kb.adjust(2,1)
    return kb.as_markup()

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
    kb.button(text="⬅️ Каталог", callback_data="catalog")
    kb.adjust(2)
    return kb.as_markup()

# ============== HANDLERS ==============
@dp.message(Command("start"))
async def start(m: Message):
    await m.answer(
        "Добро пожаловать в магазин! Добавляйте товары в корзину и оформляйте заказ.\n"
        "Оплата переводом по реквизитам после оформления.",
        reply_markup=kb_main()
    )

@dp.callback_query(F.data == "catalog")
async def show_catalog(cq: CallbackQuery):
    prods = list_products()
    if not prods:
        await cq.message.answer("Каталог пуст."); await cq.answer(); return
    for p in prods:
        kb = InlineKeyboardBuilder()
        kb.button(text="➕ В корзину", callback_data=f"add:{p['id']}")
        kb.button(text="🛒 Корзина", callback_data="cart")
        kb.adjust(1,1)
        caption = f"*{p['title']}* — {p['price_cents']/100:.2f} {p['currency']}\n{p['description'] or ''}"
        await cq.message.answer_photo(
            photo=p["photo_url"] or "https://picsum.photos/seed/none/800/500",
            caption=caption,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb.as_markup()
        )
    await cq.answer()

@dp.callback_query(F.data.startswith("add:"))
async def add(cq: CallbackQuery):
    pid = int(cq.data.split(":")[1])
    add_to_cart(cq.from_user.id, pid, +1)
    await cq.answer("Добавлено в корзину!")

@dp.callback_query(F.data == "cart")
async def cart(cq: CallbackQuery):
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
# -------- Ответы админа пользователям --------
@dp.message(Command("reply"))
async def admin_reply(m: Message):
    # Только админ
    if not (ADMIN_CHAT_ID and m.from_user.id == ADMIN_CHAT_ID):
        return await m.answer("Недостаточно прав.")

    # 1) Удобный сценарий: админ ответил РЕПЛАЕМ на уведомление бота
    if m.reply_to_message:
        src = (m.reply_to_message.text or "") + "\n" + (m.reply_to_message.caption or "")
        # Ищем (id: 123456789)
        import re
        id_match = re.search(r"id:\s*(\d+)", src)
        if id_match:
            user_id = int(id_match.group(1))
            reply_text = m.text.partition(" ")[2].strip()  # всё после /reply
            if not reply_text:
                return await m.answer("Напиши текст ответа после /reply")
            try:
                await bot.send_message(user_id, f"✉️ Ответ поддержки:\n{reply_text}")
                return await m.answer("✅ Отправлено пользователю.")
            except Exception as e:
                return await m.answer(f"❌ Не удалось отправить: {e}")
        else:
            # если id не нашли, попробуем вытащить @username из уведомления
            uname = re.search(r"@([A-Za-z0-9_]{5,})", src)
            if uname:
                try:
                    ch = await bot.get_chat("@"+uname.group(1))
                    user_id = ch.id
                    reply_text = m.text.partition(" ")[2].strip()
                    if not reply_text:
                        return await m.answer("Напиши текст ответа после /reply")
                    await bot.send_message(user_id, f"✉️ Ответ поддержки:\n{reply_text}")
                    return await m.answer("✅ Отправлено пользователю.")
                except Exception:
                    pass  # упадём в вариант B

    # 2) Явный синтаксис: /reply <user_id|@username> <текст>
    parts = m.text.split(maxsplit=2)
    if len(parts) < 3:
        return await m.answer(
            "Формат:\n"
            "/reply <user_id или @username> <текст>\n"
            "ИЛИ: ответьте реплаем на сообщение бота про вопрос и напишите: /reply ваш_текст"
        )

    target = parts[1]
    reply_text = parts[2].strip()
    if not reply_text:
        return await m.answer("Пустой текст ответа.")

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
        await bot.send_message(user_id, f"✉️ Ответ поддержки:\n{reply_text}")
        await m.answer("✅ Отправлено пользователю.")
    except Exception as e:
        await m.answer(f"❌ Не удалось отправить: {e}")

# ----- Простые админ-команды (только для ADMIN_CHAT_ID) -----
def is_admin(uid: int) -> bool:
    return ADMIN_CHAT_ID and uid == ADMIN_CHAT_ID

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

# /ls   (список товаров)
@dp.message(Command("ls"))
async def listp(m: Message):
    if not is_admin(m.from_user.id):
        return await m.answer("Недостаточно прав.")
    prods = list_products()
    if not prods: return await m.answer("Каталог пуст.")
    text = "Товары:\n" + "\n".join([f"{p['id']}. {p['title']} — {p['price_cents']/100:.2f} {p['currency']}" for p in prods])
    await m.answer(text)


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
