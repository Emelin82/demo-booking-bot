# -*- coding: utf-8 -*-
"""
ДЕМО-БОТ ЗАПИСИ (проект для портфолио)
======================================
Выдуманная «Студия Пример», выдуманные мастера — никаких реальных данных.

Что умеет (это и есть пункты для портфолио):
  - запись клиента: услуга -> мастер -> день -> время -> имя/телефон
  - слоты по 30 минут, длительность зависит от услуги
  - у каждого мастера своё расписание по дням недели
  - «Мои записи» с самоотменой клиентом
  - админка по паролю: просмотр записей, отмена с уведомлением клиента
  - напоминание клиенту вечером накануне визита
  - антиспам: лимит активных записей + пауза между записями
  - keep-alive веб-сервер для бесплатного хостинга Render

Запуск локально (Windows):
  pip install -r requirements.txt
  вставить токен в BOT_TOKEN (или задать переменную окружения)
  python demo_booking_bot.py
"""

import asyncio
import json
import logging
import os
import threading
import time as tm
from datetime import date, datetime, time, timedelta
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("demo_bot")

# ---------------------- НАСТРОЙКИ ----------------------

# Токен задаётся переменной окружения BOT_TOKEN (на Render: раздел Environment).
BOT_TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬ_СЮДА_ТОКЕН_ОТ_BOTFATHER")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "demo2026")

BUSINESS_NAME = "Студия «Пример»"          # выдуманное название
SLOT_MINUTES = 30                          # шаг слотов
BUFFER_MINUTES = 30                        # ближе чем через 30 мин записаться нельзя
MAX_ACTIVE_BOOKINGS = 3                    # антиспам: активных записей на клиента
BOOKING_COOLDOWN_MINUTES = 3               # антиспам: пауза между записями
REMIND_HOUR = 19                           # напоминание накануне, после 19:00

DB_FILE = Path(__file__).with_name("demo_bookings.json")

# Выдуманные мастера: дни — номера дней недели (0=пн ... 6=вс)
MASTERS = [
    {"id": "anna",  "name": "Анна",   "role": "Стилист-парикмахер",
     "days": [0, 1, 2, 3, 4], "start": 9,  "end": 18},
    {"id": "olga",  "name": "Ольга",  "role": "Мастер маникюра",
     "days": [1, 2, 3, 4, 5], "start": 10, "end": 19},
    {"id": "maxim", "name": "Максим", "role": "Барбер",
     "days": [2, 3, 4, 5, 6], "start": 10, "end": 20},
]

# Выдуманные услуги: slots = сколько 30-минутных слотов занимает услуга
SERVICES = [
    {"id": "haircut", "title": "Стрижка",          "price": "от 30 BYN", "slots": 2},
    {"id": "color",   "title": "Окрашивание",      "price": "от 90 BYN", "slots": 4},
    {"id": "mani",    "title": "Маникюр",          "price": "от 35 BYN", "slots": 3},
    {"id": "beard",   "title": "Стрижка + борода", "price": "от 40 BYN", "slots": 2},
]

# ---------------------- ХРАНИЛИЩЕ (JSON) ----------------------

_lock = threading.Lock()


def load_db() -> dict:
    with _lock:
        db = {}
        if DB_FILE.exists():
            try:
                db = json.loads(DB_FILE.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                log.warning("JSON повреждён, начинаю с чистой базы")
        db.setdefault("bookings", [])
        db.setdefault("cooldowns", {})
        db.setdefault("admins", [])
        return db


def save_db(db: dict) -> None:
    with _lock:
        DB_FILE.write_text(
            json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def find_master(master_id: str) -> dict | None:
    return next((m for m in MASTERS if m["id"] == master_id), None)


def find_service(service_id: str) -> dict | None:
    return next((s for s in SERVICES if s["id"] == service_id), None)


def next_booking_id(db: dict) -> int:
    return max([b.get("id", 0) for b in db["bookings"]], default=0) + 1

# ---------------------- ДВИЖОК СЛОТОВ ----------------------


def free_slots(master: dict, service: dict, day: date) -> list[str]:
    """Свободные стартовые времена для мастера/услуги/дня (список 'ЧЧ:ММ')."""
    if day.weekday() not in master["days"]:
        return []

    db = load_db()
    taken = [
        (b["start"], b["end"])
        for b in db["bookings"]
        if b["master_id"] == master["id"] and b["date"] == day.isoformat()
    ]

    dur = timedelta(minutes=SLOT_MINUTES * service["slots"])
    cur = datetime.combine(day, time(master["start"], 0))
    work_end = datetime.combine(day, time(master["end"], 0))
    now = datetime.now()

    out = []
    while cur + dur <= work_end:
        s, e = cur, cur + dur
        ok = True
        # сегодня — отсекаем прошедшее и ближний буфер
        if day == now.date() and s < now + timedelta(minutes=BUFFER_MINUTES):
            ok = False
        for ts, te in taken:
            bs = datetime.combine(day, datetime.strptime(ts, "%H:%M").time())
            be = datetime.combine(day, datetime.strptime(te, "%H:%M").time())
            if s < be and e > bs:          # пересечение интервалов
                ok = False
                break
        if ok:
            out.append(s.strftime("%H:%M"))
        cur += timedelta(minutes=SLOT_MINUTES)
    return out


def count_active(db: dict, tgid: int) -> int:
    today = date.today().isoformat()
    return sum(
        1 for b in db["bookings"] if b["tgid"] == tgid and b["date"] >= today
    )


def fmt_booking(b: dict) -> str:
    m = find_master(b["master_id"]) or {"name": "?"}
    s = find_service(b["service_id"]) or {"title": "?", "price": ""}
    d = datetime.strptime(b["date"], "%Y-%m-%d").strftime("%d.%m")
    return (
        f"#{b['id']} · {d} {b['start']}–{b['end']}\n"
        f"💇 {s['title']} ({s['price']}) · мастер {m['name']}\n"
        f"👤 {b['name']} · {b['phone']}"
    )

# ---------------------- КЛАВИАТУРЫ ----------------------

router = Router()


def main_menu_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="📅 Записаться", callback_data="book")
    kb.button(text="🗓 Мои записи", callback_data="my")
    kb.button(text="💇 Услуги и цены", callback_data="services")
    kb.button(text="ℹ️ О студии", callback_data="about")
    kb.adjust(1)
    return kb.as_markup()


def services_kb():
    kb = InlineKeyboardBuilder()
    for s in SERVICES:
        kb.button(text=f"{s['title']} — {s['price']}", callback_data=f"srv_{s['id']}")
    kb.button(text="❌ Отмена", callback_data="cancel_flow")
    kb.adjust(1)
    return kb.as_markup()


def masters_kb():
    kb = InlineKeyboardBuilder()
    for m in MASTERS:
        kb.button(text=f"{m['name']} ({m['role']})", callback_data=f"mst_{m['id']}")
    kb.button(text="❌ Отмена", callback_data="cancel_flow")
    kb.adjust(1)
    return kb.as_markup()


def days_kb(master: dict):
    kb = InlineKeyboardBuilder()
    today = date.today()
    for i in range(7):
        d = today + timedelta(days=i)
        if d.weekday() in master["days"]:
            label = d.strftime("%d.%m (%a)").replace("Mon", "пн").replace("Tue", "вт") \
                .replace("Wed", "ср").replace("Thu", "чт").replace("Fri", "пт") \
                .replace("Sat", "сб").replace("Sun", "вс")
            if i == 0:
                label = "Сегодня, " + label
            elif i == 1:
                label = "Завтра, " + label
            kb.button(text=label, callback_data=f"day_{d.isoformat()}")
    kb.button(text="❌ Отмена", callback_data="cancel_flow")
    kb.adjust(2)
    return kb.as_markup()


def slots_kb(slots: list[str]):
    kb = InlineKeyboardBuilder()
    for s in slots:
        kb.button(text=s, callback_data=f"slot_{s}")
    kb.button(text="❌ Отмена", callback_data="cancel_flow")
    kb.adjust(4)
    return kb.as_markup()


def confirm_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Подтвердить", callback_data="cf_yes")
    kb.button(text="❌ Отмена", callback_data="cancel_flow")
    kb.adjust(2)
    return kb.as_markup()


def cancel_input_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data="cancel_flow")
    return kb.as_markup()


def my_bookings_kb(bookings: list[dict]):
    kb = InlineKeyboardBuilder()
    for b in bookings:
        d = datetime.strptime(b["date"], "%Y-%m-%d").strftime("%d.%m")
        kb.button(text=f"❌ {d} {b['start']}", callback_data=f"myc_{b['id']}")
    kb.button(text="🏠 В меню", callback_data="to_menu")
    kb.adjust(1)
    return kb.as_markup()


def admin_menu_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Записи на сегодня", callback_data="adm_today")
    kb.button(text="🗓 Все предстоящие", callback_data="adm_all")
    kb.button(text="❌ Отменить запись", callback_data="adm_cancel")
    kb.button(text="🚪 Выйти из админки", callback_data="to_menu")
    kb.adjust(1)
    return kb.as_markup()


def admin_cancel_kb(bookings: list[dict]):
    kb = InlineKeyboardBuilder()
    for b in bookings:
        d = datetime.strptime(b["date"], "%Y-%m-%d").strftime("%d.%m")
        kb.button(
            text=f"❌ #{b['id']} {d} {b['start']} {b['name']}",
            callback_data=f"adc_{b['id']}",
        )
    kb.button(text="⬅️ Назад", callback_data="adm_back")
    kb.adjust(1)
    return kb.as_markup()

# ---------------------- СОСТОЯНИЯ ----------------------


class Book(StatesGroup):
    service = State()
    master = State()
    day = State()
    slot = State()
    name = State()
    phone = State()
    confirm = State()


class AdminAuth(StatesGroup):
    password = State()

# ---------------------- ВСПОМОГАТЕЛЬНОЕ ----------------------


async def notify_admins(bot: Bot, text: str):
    db = load_db()
    for admin_id in db["admins"]:
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            pass

# ---------------------- КЛИЕНТ: СТАРТ И МЕНЮ ----------------------


@router.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer(
        f"👋 Привет! Это демо-бот записи — {BUSINESS_NAME}.\n\n"
        "Выберите действие:",
        reply_markup=main_menu_kb(),
    )


@router.callback_query(F.data == "to_menu")
async def cb_to_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("Главное меню:", reply_markup=main_menu_kb())
    await cb.answer()


@router.callback_query(F.data == "cancel_flow")
async def cb_cancel_flow(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text(
        "Запись отменена. Главное меню:", reply_markup=main_menu_kb()
    )
    await cb.answer()


@router.callback_query(F.data == "services")
async def cb_services(cb: CallbackQuery):
    lines = ["💇 <b>Услуги и цены</b>\n"]
    for s in SERVICES:
        mins = s["slots"] * SLOT_MINUTES
        lines.append(f"• {s['title']} — {s['price']} (~{mins} мин)")
    lines.append("\n<i>Цены демонстрационные.</i>")
    kb = InlineKeyboardBuilder()
    kb.button(text="🏠 В меню", callback_data="to_menu")
    await cb.message.edit_text("\n".join(lines), reply_markup=kb.as_markup())
    await cb.answer()


@router.callback_query(F.data == "about")
async def cb_about(cb: CallbackQuery):
    kb = InlineKeyboardBuilder()
    kb.button(text="🏠 В меню", callback_data="to_menu")
    await cb.message.edit_text(
        f"ℹ️ <b>{BUSINESS_NAME}</b>\n\n"
        "Это демонстрационный бот-портфолио.\n"
        "Все мастера, услуги и цены выдуманы.\n\n"
        "Бот умеет: запись по мастерам и расписанию, "
        "напоминания, самоотмену, админку для персонала.",
        reply_markup=kb.as_markup(),
    )
    await cb.answer()

# ---------------------- КЛИЕНТ: ЗАПИСЬ ----------------------


@router.callback_query(F.data == "book")
async def cb_book(cb: CallbackQuery, state: FSMContext):
    db = load_db()

    # антиспам: лимит активных записей
    if count_active(db, cb.from_user.id) >= MAX_ACTIVE_BOOKINGS:
        await cb.message.edit_text(
            f"⚠️ У вас уже {MAX_ACTIVE_BOOKINGS} активных записи. "
            "Отмените одну из них в разделе «Мои записи».",
            reply_markup=main_menu_kb(),
        )
        await cb.answer()
        return

    # антиспам: пауза между записями
    last = db["cooldowns"].get(str(cb.from_user.id))
    if last:
        elapsed = datetime.now() - datetime.fromisoformat(last)
        if elapsed < timedelta(minutes=BOOKING_COOLDOWN_MINUTES):
            wait = BOOKING_COOLDOWN_MINUTES - int(elapsed.total_seconds() // 60)
            await cb.answer(
                f"Пауза между записями. Попробуйте через {wait} мин.",
                show_alert=True,
            )
            return

    await state.set_state(Book.service)
    await cb.message.edit_text(
        "Шаг 1/4. Выберите услугу:", reply_markup=services_kb()
    )
    await cb.answer()


@router.callback_query(Book.service, F.data.startswith("srv_"))
async def cb_service(cb: CallbackQuery, state: FSMContext):
    service = find_service(cb.data[4:])
    if not service:
        await cb.answer("Услуга не найдена", show_alert=True)
        return
    await state.update_data(service_id=service["id"])
    await state.set_state(Book.master)
    await cb.message.edit_text(
        f"Услуга: <b>{service['title']}</b>\n\nШаг 2/4. Выберите мастера:",
        reply_markup=masters_kb(),
    )
    await cb.answer()


@router.callback_query(Book.master, F.data.startswith("mst_"))
async def cb_master(cb: CallbackQuery, state: FSMContext):
    master = find_master(cb.data[4:])
    if not master:
        await cb.answer("Мастер не найден", show_alert=True)
        return
    await state.update_data(master_id=master["id"])
    await state.set_state(Book.day)
    await cb.message.edit_text(
        f"Мастер: <b>{master['name']}</b> ({master['role']})\n\n"
        "Шаг 3/4. Выберите день:",
        reply_markup=days_kb(master),
    )
    await cb.answer()


@router.callback_query(Book.day, F.data.startswith("day_"))
async def cb_day(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    master = find_master(data["master_id"])
    service = find_service(data["service_id"])
    day = date.fromisoformat(cb.data[4:])

    slots = free_slots(master, service, day)
    if not slots:
        await cb.answer("На этот день свободных окон нет", show_alert=True)
        return

    await state.update_data(day=day.isoformat())
    await state.set_state(Book.slot)
    await cb.message.edit_text(
        f"День: <b>{day.strftime('%d.%m')}</b>\n\nШаг 4/4. Выберите время:",
        reply_markup=slots_kb(slots),
    )
    await cb.answer()


@router.callback_query(Book.slot, F.data.startswith("slot_"))
async def cb_slot(cb: CallbackQuery, state: FSMContext):
    await state.update_data(start=cb.data[5:])
    await state.set_state(Book.name)
    await cb.message.edit_text(
        "Как вас зовут? (имя для записи)", reply_markup=cancel_input_kb()
    )
    await cb.answer()


@router.message(Book.name)
async def msg_name(msg: Message, state: FSMContext):
    name = msg.text.strip()
    if not (2 <= len(name) <= 40):
        await msg.answer("Имя от 2 до 40 символов. Попробуйте ещё раз:",
                         reply_markup=cancel_input_kb())
        return
    await state.update_data(name=name)
    await state.set_state(Book.phone)
    await msg.answer(
        "Ваш телефон для связи (например, +375291234567):",
        reply_markup=cancel_input_kb(),
    )


@router.message(Book.phone)
async def msg_phone(msg: Message, state: FSMContext):
    phone = msg.text.strip()
    digits = sum(ch.isdigit() for ch in phone)
    if digits < 7 or len(phone) > 20:
        await msg.answer("Похоже, это не номер. Попробуйте ещё раз:",
                         reply_markup=cancel_input_kb())
        return
    await state.update_data(phone=phone)

    data = await state.get_data()
    master = find_master(data["master_id"])
    service = find_service(data["service_id"])
    d = datetime.strptime(data["day"], "%Y-%m-%d").strftime("%d.%m")

    await state.set_state(Book.confirm)
    await msg.answer(
        "Проверьте запись:\n\n"
        f"💇 Услуга: {service['title']} ({service['price']})\n"
        f"👤 Мастер: {master['name']}\n"
        f"🗓 Дата: {d}\n"
        f"⏰ Время: {data['start']}\n"
        f"🙍 Имя: {data['name']}\n"
        f"📞 Телефон: {phone}",
        reply_markup=confirm_kb(),
    )


@router.callback_query(Book.confirm, F.data == "cf_yes")
async def cb_confirm(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    master = find_master(data["master_id"])
    service = find_service(data["service_id"])
    day = date.fromisoformat(data["day"])

    # повторная проверка: слот могли занять, пока клиент заполнял анкету
    if data["start"] not in free_slots(master, service, day):
        await state.clear()
        await cb.message.edit_text(
            "😔 Это время только что заняли. Попробуйте записаться снова:",
            reply_markup=main_menu_kb(),
        )
        await cb.answer()
        return

    start_dt = datetime.strptime(f"{data['day']} {data['start']}", "%Y-%m-%d %H:%M")
    end_dt = start_dt + timedelta(minutes=SLOT_MINUTES * service["slots"])

    db = load_db()
    booking = {
        "id": next_booking_id(db),
        "tgid": cb.from_user.id,
        "username": cb.from_user.username or "",
        "master_id": master["id"],
        "service_id": service["id"],
        "date": data["day"],
        "start": data["start"],
        "end": end_dt.strftime("%H:%M"),
        "name": data["name"],
        "phone": data["phone"],
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "reminded": False,
    }
    db["bookings"].append(booking)
    db["cooldowns"][str(cb.from_user.id)] = datetime.now().isoformat()
    save_db(db)
    await state.clear()

    d = day.strftime("%d.%m")
    await cb.message.edit_text(
        f"✅ <b>Вы записаны!</b>\n\n"
        f"💇 {service['title']} · мастер {master['name']}\n"
        f"🗓 {d} в {booking['start']}\n\n"
        "Напомним накануне вечером. Отменить можно в «Мои записи».",
        reply_markup=main_menu_kb(),
    )
    await cb.answer()

    await notify_admins(
        cb.bot,
        "🆕 <b>Новая запись</b>\n\n" + fmt_booking(booking),
    )

# ---------------------- КЛИЕНТ: МОИ ЗАПИСИ ----------------------


@router.callback_query(F.data == "my")
async def cb_my(cb: CallbackQuery):
    db = load_db()
    today = date.today().isoformat()
    mine = [
        b for b in db["bookings"]
        if b["tgid"] == cb.from_user.id and b["date"] >= today
    ]
    if not mine:
        await cb.message.edit_text(
            "У вас нет активных записей.", reply_markup=main_menu_kb()
        )
        await cb.answer()
        return

    text = "🗓 <b>Ваши записи</b> (нажмите, чтобы отменить):\n\n"
    text += "\n\n".join(fmt_booking(b) for b in mine)
    await cb.message.edit_text(text, reply_markup=my_bookings_kb(mine))
    await cb.answer()


@router.callback_query(F.data.startswith("myc_"))
async def cb_my_cancel(cb: CallbackQuery):
    bid = int(cb.data[4:])
    db = load_db()
    booking = next((b for b in db["bookings"] if b["id"] == bid), None)

    # чужую запись отменить нельзя
    if not booking or booking["tgid"] != cb.from_user.id:
        await cb.answer("Запись не найдена", show_alert=True)
        return

    db["bookings"] = [b for b in db["bookings"] if b["id"] != bid]
    save_db(db)

    await cb.message.edit_text(
        "❌ Запись отменена. Будем ждать вас снова!",
        reply_markup=main_menu_kb(),
    )
    await cb.answer()
    await notify_admins(cb.bot, "🚫 <b>Клиент отменил запись</b>\n\n" + fmt_booking(booking))

# ---------------------- АДМИНКА ----------------------


@router.message(Command("admin"))
async def cmd_admin(msg: Message, state: FSMContext):
    await state.set_state(AdminAuth.password)
    await msg.answer("🔐 Введите пароль администратора:")


@router.message(AdminAuth.password)
async def msg_admin_password(msg: Message, state: FSMContext):
    if msg.text.strip() != ADMIN_PASSWORD:
        await state.clear()
        await msg.answer("Неверный пароль.", reply_markup=main_menu_kb())
        return

    db = load_db()
    if msg.from_user.id not in db["admins"]:
        db["admins"].append(msg.from_user.id)
        save_db(db)

    await state.clear()
    await msg.answer("✅ Вы вошли как администратор.",
                     reply_markup=admin_menu_kb())


@router.callback_query(F.data == "adm_back")
async def cb_adm_back(cb: CallbackQuery):
    await cb.message.edit_text("Админка:", reply_markup=admin_menu_kb())
    await cb.answer()


@router.callback_query(F.data.in_({"adm_today", "adm_all"}))
async def cb_adm_list(cb: CallbackQuery):
    if cb.from_user.id not in load_db()["admins"]:
        await cb.answer("Нет доступа", show_alert=True)
        return

    db = load_db()
    today = date.today().isoformat()
    if cb.data == "adm_today":
        items = [b for b in db["bookings"] if b["date"] == today]
        title = "📋 Записи на сегодня"
    else:
        items = [b for b in db["bookings"] if b["date"] >= today]
        items.sort(key=lambda b: (b["date"], b["start"]))
        title = "🗓 Все предстоящие записи"

    if not items:
        text = title + "\n\nПока пусто."
    else:
        text = title + "\n\n" + "\n\n".join(fmt_booking(b) for b in items)

    # телеграм режет сообщения длиннее ~4096 символов
    await cb.message.edit_text(
        text[:4000], reply_markup=admin_menu_kb()
    )
    await cb.answer()


@router.callback_query(F.data == "adm_cancel")
async def cb_adm_cancel_list(cb: CallbackQuery):
    db = load_db()
    if cb.from_user.id not in db["admins"]:
        await cb.answer("Нет доступа", show_alert=True)
        return

    today = date.today().isoformat()
    items = [b for b in db["bookings"] if b["date"] >= today]
    items.sort(key=lambda b: (b["date"], b["start"]))

    if not items:
        await cb.answer("Активных записей нет", show_alert=True)
        return

    await cb.message.edit_text(
        "Нажмите на запись, чтобы отменить (клиент получит уведомление):",
        reply_markup=admin_cancel_kb(items),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("adc_"))
async def cb_adm_cancel(cb: CallbackQuery):
    db = load_db()
    if cb.from_user.id not in db["admins"]:
        await cb.answer("Нет доступа", show_alert=True)
        return

    bid = int(cb.data[4:])
    booking = next((b for b in db["bookings"] if b["id"] == bid), None)
    if not booking:
        await cb.answer("Уже отменена", show_alert=True)
        return

    db["bookings"] = [b for b in db["bookings"] if b["id"] != bid]
    save_db(db)

    try:
        await cb.bot.send_message(
            booking["tgid"],
            "😔 К сожалению, ваша запись отменена студией:\n\n"
            + fmt_booking(booking)
            + "\n\nЗапишитесь на другое время через /start",
        )
    except Exception:
        pass

    await cb.message.edit_text(
        f"❌ Запись #{bid} отменена, клиент уведомлён.",
        reply_markup=admin_menu_kb(),
    )
    await cb.answer()

# ---------------------- НАПОМИНАНИЯ ----------------------


async def send_due_reminders(bot: Bot):
    """Напоминание вечером накануне визита. Флаг reminded — чтобы не слать дважды."""
    db = load_db()
    now = datetime.now()
    tomorrow = (now + timedelta(days=1)).date().isoformat()
    changed = False

    for b in db["bookings"]:
        if b["date"] == tomorrow and not b.get("reminded") and now.hour >= REMIND_HOUR:
            try:
                await bot.send_message(
                    b["tgid"],
                    "⏰ <b>Напоминание о записи</b>\n\nЗавтра:\n"
                    + fmt_booking(b),
                )
                b["reminded"] = True
                changed = True
            except Exception:
                pass  # клиент заблокировал бота — не критично

    if changed:
        save_db(db)


async def reminder_loop(bot: Bot):
    while True:
        try:
            await send_due_reminders(bot)
        except Exception:
            log.exception("ошибка в reminder_loop")
        await asyncio.sleep(1800)  # раз в 30 минут

# ---------------------- KEEP-ALIVE ДЛЯ БЕСПЛАТНОГО ХОСТИНГА ----------------------


def run_keepalive_server():
    """Мини-сервер: Render требует открытый порт, а пингер (UptimeRobot)
    будет дёргать этот адрес, чтобы бесплатный сервис не засыпал."""
    from flask import Flask

    app = Flask("keepalive")

    @app.get("/")
    def index():
        return "Demo booking bot is alive"

    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)

# ---------------------- ЗАПУСК ----------------------


async def main():
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    asyncio.create_task(reminder_loop(bot))
    await bot.delete_webhook(drop_pending_updates=True)
    log.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    if BOT_TOKEN.startswith("ВСТАВЬ"):
        raise SystemExit("Сначала вставьте токен бота в BOT_TOKEN!")

    # на Render переменная PORT задана -> поднимаем keep-alive сервер
    if os.getenv("PORT"):
        threading.Thread(target=run_keepalive_server, daemon=True).start()

    while True:  # самовосстановление после сбоев сети
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            break
        except Exception:
            log.exception("Бот упал, перезапуск через 5 секунд")
            tm.sleep(5)
