import asyncio
import logging
import os
import hmac
import hashlib
import json
import time
from datetime import datetime

from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, PreCheckoutQuery,
    LabeledPrice
)
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
import aiohttp
from database import db
from config import config
from cashfree_utils import create_payment_order, verify_payment_signature

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

# ─── States ───────────────────────────────────────────────────────────────────

class OrderStates(StatesGroup):
    waiting_service_id = State()
    waiting_link = State()
    waiting_quantity = State()
    confirming = State()

class RechargeStates(StatesGroup):
    waiting_amount = State()
    waiting_upi_screenshot = State()

class AdminStates(StatesGroup):
    waiting_user_id = State()
    waiting_balance = State()
    waiting_broadcast = State()

# ─── Helpers ──────────────────────────────────────────────────────────────────

def main_menu_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🛒 New Order"), KeyboardButton(text="📋 My Orders")],
        [KeyboardButton(text="💰 Balance"), KeyboardButton(text="➕ Add Funds")],
        [KeyboardButton(text="📊 Services"), KeyboardButton(text="👤 Profile")],
        [KeyboardButton(text="❓ Help")]
    ], resize_keyboard=True)

def admin_menu_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🛒 New Order"), KeyboardButton(text="📋 My Orders")],
        [KeyboardButton(text="💰 Balance"), KeyboardButton(text="➕ Add Funds")],
        [KeyboardButton(text="📊 Services"), KeyboardButton(text="👤 Profile")],
        [KeyboardButton(text="🔧 Admin Panel"), KeyboardButton(text="❓ Help")]
    ], resize_keyboard=True)

def get_kb(user_id):
    if user_id in config.ADMIN_IDS:
        return admin_menu_kb()
    return main_menu_kb()

async def smm_api(action: str, params: dict = {}):
    """Call smmpanelone.com API"""
    payload = {"key": config.SMM_API_KEY, "action": action, **params}
    async with aiohttp.ClientSession() as session:
        async with session.post(config.SMM_API_URL, data=payload) as resp:
            try:
                return await resp.json(content_type=None)
            except Exception:
                text = await resp.text()
                logger.error(f"SMM API error: {text}")
                return {"error": text}

def cashfree_charge(amount: float) -> float:
    """Add Cashfree gateway charge on top of amount"""
    # 2% charge borne by user
    return round(amount * 1.02, 2)

# ─── /start ───────────────────────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(msg: Message):
    user = await db.get_or_create_user(msg.from_user.id, msg.from_user.full_name, msg.from_user.username)
    welcome = (
        f"👋 Welcome to <b>{config.BOT_NAME}</b>, {msg.from_user.first_name}!\n\n"
        f"💎 Your Balance: <b>₹{user['balance']:.2f}</b>\n\n"
        "Use the menu below to get started."
    )
    await msg.answer(welcome, parse_mode="HTML", reply_markup=get_kb(msg.from_user.id))

# ─── Profile ──────────────────────────────────────────────────────────────────

@router.message(F.text == "👤 Profile")
async def profile(msg: Message):
    user = await db.get_or_create_user(msg.from_user.id, msg.from_user.full_name, msg.from_user.username)
    total_orders = await db.count_user_orders(msg.from_user.id)
    total_spent = await db.total_spent(msg.from_user.id)
    text = (
        f"👤 <b>Your Profile</b>\n\n"
        f"🆔 ID: <code>{msg.from_user.id}</code>\n"
        f"📛 Name: {msg.from_user.full_name}\n"
        f"💰 Balance: <b>₹{user['balance']:.2f}</b>\n"
        f"📦 Total Orders: {total_orders}\n"
        f"💸 Total Spent: ₹{total_spent:.2f}\n"
        f"📅 Joined: {user['created_at'][:10]}"
    )
    await msg.answer(text, parse_mode="HTML")

# ─── Balance ──────────────────────────────────────────────────────────────────

@router.message(F.text == "💰 Balance")
async def balance(msg: Message):
    user = await db.get_or_create_user(msg.from_user.id, msg.from_user.full_name, msg.from_user.username)
    await msg.answer(
        f"💰 Your current balance: <b>₹{user['balance']:.2f}</b>",
        parse_mode="HTML"
    )

# ─── Add Funds ────────────────────────────────────────────────────────────────

@router.message(F.text == "➕ Add Funds")
async def add_funds_start(msg: Message, state: FSMContext):
    await msg.answer(
        "💳 <b>Add Funds</b>\n\n"
        "Enter amount to add (Min ₹10, Max ₹50000):\n"
        "⚠️ Note: 2% payment gateway charge will be added.\n\n"
        "Example: <code>100</code>",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove()
    )
    await state.set_state(RechargeStates.waiting_amount)

@router.message(RechargeStates.waiting_amount)
async def process_recharge_amount(msg: Message, state: FSMContext):
    try:
        amount = float(msg.text.strip())
        if amount < 10 or amount > 50000:
            await msg.answer("❌ Amount must be between ₹10 and ₹50000")
            return
    except ValueError:
        await msg.answer("❌ Please enter a valid number")
        return

    charge = cashfree_charge(amount)
    gateway_fee = round(charge - amount, 2)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💳 Card/Net Banking ₹{charge} (Cashfree)", callback_data=f"pay_cashfree_{amount}")],
        [InlineKeyboardButton(text=f"📱 UPI ₹{amount} (Manual)", callback_data=f"pay_upi_manual_{amount}")],
        [InlineKeyboardButton(text="❌ Cancel", callback_data="pay_cancel")]
    ])

    await msg.answer(
        f"💳 <b>Payment Summary</b>\n\n"
        f"💰 Amount to add: ₹{amount:.2f}\n\n"
        f"<b>Card/Net Banking:</b> Pay ₹{charge:.2f} (includes 2% gateway fee)\n"
        f"<b>UPI (Manual):</b> Pay ₹{amount:.2f} — no extra charge, admin verifies\n\n"
        f"Select payment method:",
        parse_mode="HTML",
        reply_markup=kb
    )
    await state.clear()
    await state.update_data(pending_amount=amount)

@router.callback_query(F.data.startswith("pay_cashfree_"))
async def cashfree_payment(cb: CallbackQuery):
    amount = float(cb.data.split("_")[2])
    charge = cashfree_charge(amount)

    await cb.answer("Creating payment link...")

    user = await db.get_or_create_user(cb.from_user.id, cb.from_user.full_name, cb.from_user.username)
    order_id = f"TG{cb.from_user.id}{int(time.time())}"

    result = await create_payment_order(
        order_id=order_id,
        amount=charge,
        customer_id=str(cb.from_user.id),
        customer_name=cb.from_user.full_name or "User",
        customer_phone=user.get("phone", "9999999999")
    )

    if result.get("payment_link"):
        await db.create_recharge(cb.from_user.id, order_id, amount, charge, "cashfree")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Pay Now", url=result["payment_link"])],
            [InlineKeyboardButton(text="✅ I've Paid - Verify", callback_data=f"verify_{order_id}")]
        ])
        await cb.message.edit_text(
            f"✅ Payment link created!\n\n"
            f"Amount: ₹{charge:.2f}\n"
            f"Order ID: <code>{order_id}</code>\n\n"
            f"Click below to pay, then press Verify.",
            parse_mode="HTML",
            reply_markup=kb
        )
    else:
        await cb.message.edit_text("❌ Could not create payment link. Try again later.")

@router.callback_query(F.data.startswith("pay_upi_manual_"))
async def upi_manual_payment(cb: CallbackQuery, state: FSMContext):
    amount = float(cb.data.split("_")[3])
    await state.update_data(upi_amount=amount)
    await state.set_state(RechargeStates.waiting_upi_screenshot)

    await cb.message.edit_text(
        f"📱 <b>UPI Manual Payment</b>\n\n"
        f"Amount: <b>₹{amount:.2f}</b>\n\n"
        f"UPI ID: <code>{config.UPI_ID}</code>\n"
        f"Name: {config.UPI_NAME}\n\n"
        f"Steps:\n"
        f"1. Pay ₹{amount:.2f} to above UPI ID\n"
        f"2. Take screenshot of payment\n"
        f"3. Send screenshot here\n\n"
        f"⏳ Admin will verify and credit your balance (usually within 5-15 min)",
        parse_mode="HTML"
    )

@router.message(RechargeStates.waiting_upi_screenshot, F.photo)
async def upi_screenshot_received(msg: Message, state: FSMContext):
    data = await state.get_data()
    amount = data.get("upi_amount", 0)
    await state.clear()

    order_id = f"UPI{msg.from_user.id}{int(time.time())}"
    await db.create_recharge(msg.from_user.id, order_id, amount, amount, "upi_manual")

    # Notify all admins
    kb_admin = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Approve", callback_data=f"upi_approve_{order_id}_{msg.from_user.id}_{amount}"),
            InlineKeyboardButton(text="❌ Reject", callback_data=f"upi_reject_{order_id}_{msg.from_user.id}")
        ]
    ])

    for admin_id in config.ADMIN_IDS:
        try:
            await bot.send_photo(
                admin_id,
                photo=msg.photo[-1].file_id,
                caption=(
                    f"💰 <b>UPI Recharge Request</b>\n\n"
                    f"👤 User: {msg.from_user.full_name} (@{msg.from_user.username or 'N/A'})\n"
                    f"🆔 ID: <code>{msg.from_user.id}</code>\n"
                    f"💵 Amount: ₹{amount:.2f}\n"
                    f"🔖 Order: <code>{order_id}</code>"
                ),
                parse_mode="HTML",
                reply_markup=kb_admin
            )
        except Exception:
            pass

    await msg.answer(
        f"✅ <b>Screenshot received!</b>\n\n"
        f"Amount: ₹{amount:.2f}\n"
        f"Order ID: <code>{order_id}</code>\n\n"
        f"⏳ Admin will verify and credit your balance soon.",
        parse_mode="HTML",
        reply_markup=get_kb(msg.from_user.id)
    )

@router.message(RechargeStates.waiting_upi_screenshot)
async def upi_screenshot_wrong(msg: Message):
    await msg.answer("📸 Please send a <b>screenshot/photo</b> of your UPI payment.", parse_mode="HTML")

# ─── Admin UPI Approve/Reject ─────────────────────────────────────────────────

@router.callback_query(F.data.startswith("upi_approve_"))
async def admin_upi_approve(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    parts = cb.data.split("_")
    # upi_approve_ORDERID_USERID_AMOUNT
    order_id = parts[2]
    user_id = int(parts[3])
    amount = float(parts[4])

    recharge = await db.get_recharge(order_id)
    if not recharge or recharge["status"] == "completed":
        await cb.answer("Already processed!", show_alert=True)
        return

    await db.complete_recharge(order_id, user_id, amount)
    user = await db.get_or_create_user(user_id, "", "")

    await cb.message.edit_caption(
        cb.message.caption + f"\n\n✅ <b>APPROVED by @{cb.from_user.username}</b>",
        parse_mode="HTML"
    )

    try:
        await bot.send_message(
            user_id,
            f"✅ <b>UPI Payment Approved!</b>\n\n"
            f"₹{amount:.2f} added to your wallet.\n"
            f"New Balance: <b>₹{user['balance']:.2f}</b>",
            parse_mode="HTML"
        )
    except Exception:
        pass

    await cb.answer("✅ Approved & balance credited!")

@router.callback_query(F.data.startswith("upi_reject_"))
async def admin_upi_reject(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    parts = cb.data.split("_")
    order_id = parts[2]
    user_id = int(parts[3])

    await db.reject_recharge(order_id)

    await cb.message.edit_caption(
        cb.message.caption + f"\n\n❌ <b>REJECTED by @{cb.from_user.username}</b>",
        parse_mode="HTML"
    )

    try:
        await bot.send_message(
            user_id,
            "❌ <b>UPI Payment Rejected</b>\n\nYour payment could not be verified. "
            "Please contact support if you believe this is an error.",
            parse_mode="HTML"
        )
    except Exception:
        pass

    await cb.answer("❌ Rejected!")


@router.callback_query(F.data.startswith("verify_"))
async def verify_payment(cb: CallbackQuery):
    order_id = cb.data.replace("verify_", "")
    await cb.answer("Checking payment...")

    recharge = await db.get_recharge(order_id)
    if not recharge:
        await cb.answer("❌ Order not found", show_alert=True)
        return

    if recharge["status"] == "completed":
        await cb.answer("✅ Already credited!", show_alert=True)
        return

    # Check with Cashfree
    async with aiohttp.ClientSession() as session:
        headers = {
            "x-client-id": config.CASHFREE_APP_ID,
            "x-client-secret": config.CASHFREE_SECRET_KEY,
            "x-api-version": "2023-08-01"
        }
        url = f"{config.CASHFREE_BASE_URL}/orders/{order_id}/payments"
        async with session.get(url, headers=headers) as resp:
            data = await resp.json()

    # Look for successful payment
    paid = False
    if isinstance(data, list):
        for p in data:
            if p.get("payment_status") == "SUCCESS":
                paid = True
                break
    elif isinstance(data, dict) and data.get("payment_status") == "SUCCESS":
        paid = True

    if paid:
        await db.complete_recharge(order_id, recharge["user_id"], recharge["amount"])
        user = await db.get_or_create_user(cb.from_user.id, cb.from_user.full_name, cb.from_user.username)
        await cb.message.edit_text(
            f"✅ <b>Payment Verified!</b>\n\n"
            f"₹{recharge['amount']:.2f} added to your wallet.\n"
            f"New Balance: <b>₹{user['balance']:.2f}</b>",
            parse_mode="HTML"
        )
        await bot.send_message(cb.from_user.id, "💰 Wallet recharged!", reply_markup=get_kb(cb.from_user.id))
    else:
        await cb.answer("❌ Payment not found yet. Try after completing payment.", show_alert=True)

@router.callback_query(F.data == "pay_cancel")
async def pay_cancel(cb: CallbackQuery):
    await cb.message.edit_text("❌ Payment cancelled.")
    await bot.send_message(cb.from_user.id, "Cancelled.", reply_markup=get_kb(cb.from_user.id))

# ─── Services ─────────────────────────────────────────────────────────────────

@router.message(F.text == "📊 Services")
async def services_list(msg: Message):
    await msg.answer("⏳ Fetching services from panel...")
    result = await smm_api("services")

    if isinstance(result, list) and len(result) > 0:
        # Group by category
        categories = {}
        for svc in result:
            cat = svc.get("category", "Other")
            if cat not in categories:
                categories[cat] = []
            categories[cat].append(svc)

        # Show first 5 categories as buttons
        cat_list = list(categories.keys())[:10]
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"📁 {cat}", callback_data=f"cat_{i}")]
            for i, cat in enumerate(cat_list)
        ] + [[InlineKeyboardButton(text="🔍 Search by ID", callback_data="search_service")]])

        # Store categories in a simple way
        cats_text = "\n".join([f"{i}. {cat} ({len(categories[cat])} services)" for i, cat in enumerate(cat_list)])
        await msg.answer(
            f"📊 <b>Service Categories</b>\n\n{cats_text}\n\nSelect a category:",
            parse_mode="HTML",
            reply_markup=kb
        )
    else:
        await msg.answer("❌ Could not fetch services. Try again later.")

# ─── New Order ────────────────────────────────────────────────────────────────

@router.message(F.text == "🛒 New Order")
async def new_order_start(msg: Message, state: FSMContext):
    await msg.answer(
        "🛒 <b>Place New Order</b>\n\n"
        "Enter the <b>Service ID</b> from our panel.\n"
        "Use 📊 Services to browse available services.",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove()
    )
    await state.set_state(OrderStates.waiting_service_id)

@router.message(OrderStates.waiting_service_id)
async def order_service_id(msg: Message, state: FSMContext):
    service_id = msg.text.strip()
    if not service_id.isdigit():
        await msg.answer("❌ Enter a valid numeric Service ID")
        return

    # Fetch service details
    result = await smm_api("services")
    service = None
    if isinstance(result, list):
        for svc in result:
            if str(svc.get("service")) == service_id:
                service = svc
                break

    if not service:
        await msg.answer(f"❌ Service ID {service_id} not found. Please check and try again.")
        return

    await state.update_data(service=service)
    await msg.answer(
        f"✅ <b>Service Found:</b>\n\n"
        f"📦 {service['name']}\n"
        f"💰 Rate: ₹{service['rate']} per 1000\n"
        f"📊 Min: {service['min']} | Max: {service['max']}\n\n"
        f"Now send the <b>link/URL</b> for this order:",
        parse_mode="HTML"
    )
    await state.set_state(OrderStates.waiting_link)

@router.message(OrderStates.waiting_link)
async def order_link(msg: Message, state: FSMContext):
    link = msg.text.strip()
    await state.update_data(link=link)
    data = await state.get_data()
    service = data["service"]

    await msg.answer(
        f"📊 Enter <b>quantity</b>:\n"
        f"Min: {service['min']} | Max: {service['max']}",
        parse_mode="HTML"
    )
    await state.set_state(OrderStates.waiting_quantity)

@router.message(OrderStates.waiting_quantity)
async def order_quantity(msg: Message, state: FSMContext):
    try:
        qty = int(msg.text.strip())
    except ValueError:
        await msg.answer("❌ Enter a valid number")
        return

    data = await state.get_data()
    service = data["service"]

    if qty < int(service["min"]) or qty > int(service["max"]):
        await msg.answer(f"❌ Quantity must be between {service['min']} and {service['max']}")
        return

    cost = round((float(service["rate"]) * qty) / 1000, 2)
    user = await db.get_or_create_user(msg.from_user.id, msg.from_user.full_name, msg.from_user.username)

    await state.update_data(quantity=qty, cost=cost)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Confirm Order", callback_data="confirm_order")],
        [InlineKeyboardButton(text="❌ Cancel", callback_data="cancel_order")]
    ])

    await msg.answer(
        f"📋 <b>Order Summary</b>\n\n"
        f"Service: {service['name']}\n"
        f"Link: {data['link']}\n"
        f"Quantity: {qty:,}\n"
        f"💰 Cost: ₹{cost}\n"
        f"Your Balance: ₹{user['balance']:.2f}\n\n"
        f"{'✅ Sufficient balance' if user['balance'] >= cost else '❌ Insufficient balance'}",
        parse_mode="HTML",
        reply_markup=kb
    )
    await state.set_state(OrderStates.confirming)

@router.callback_query(F.data == "confirm_order")
async def confirm_order(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    service = data["service"]
    user = await db.get_or_create_user(cb.from_user.id, cb.from_user.full_name, cb.from_user.username)

    if user["balance"] < data["cost"]:
        await cb.answer("❌ Insufficient balance! Please add funds.", show_alert=True)
        await state.clear()
        return

    # Place order on SMM panel
    await cb.answer("Placing order...")
    result = await smm_api("add", {
        "service": service["service"],
        "link": data["link"],
        "quantity": data["quantity"]
    })

    if result.get("order"):
        smm_order_id = result["order"]
        await db.deduct_balance(cb.from_user.id, data["cost"])
        await db.create_order(
            user_id=cb.from_user.id,
            smm_order_id=smm_order_id,
            service_id=service["service"],
            service_name=service["name"],
            link=data["link"],
            quantity=data["quantity"],
            cost=data["cost"]
        )
        user = await db.get_or_create_user(cb.from_user.id, cb.from_user.full_name, cb.from_user.username)
        await cb.message.edit_text(
            f"✅ <b>Order Placed!</b>\n\n"
            f"📦 Order ID: <code>{smm_order_id}</code>\n"
            f"Service: {service['name']}\n"
            f"Quantity: {data['quantity']:,}\n"
            f"Cost: ₹{data['cost']}\n"
            f"Remaining Balance: ₹{user['balance']:.2f}",
            parse_mode="HTML"
        )
        await bot.send_message(cb.from_user.id, "✅ Order placed successfully!", reply_markup=get_kb(cb.from_user.id))
    else:
        error = result.get("error", "Unknown error")
        await cb.message.edit_text(f"❌ Order failed: {error}")
        await bot.send_message(cb.from_user.id, "❌ Order failed.", reply_markup=get_kb(cb.from_user.id))

    await state.clear()

@router.callback_query(F.data == "cancel_order")
async def cancel_order(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("❌ Order cancelled.")
    await bot.send_message(cb.from_user.id, "Cancelled.", reply_markup=get_kb(cb.from_user.id))

# ─── My Orders ────────────────────────────────────────────────────────────────

@router.message(F.text == "📋 My Orders")
async def my_orders(msg: Message):
    orders = await db.get_user_orders(msg.from_user.id, limit=10)
    if not orders:
        await msg.answer("📭 No orders yet. Place your first order!")
        return

    text = "📋 <b>Your Recent Orders</b>\n\n"
    for o in orders:
        status_emoji = {"pending": "⏳", "processing": "🔄", "completed": "✅", "partial": "⚠️", "cancelled": "❌"}.get(o["status"], "❓")
        text += (
            f"{status_emoji} <b>Order #{o['smm_order_id']}</b>\n"
            f"   {o['service_name'][:40]}\n"
            f"   Qty: {o['quantity']:,} | ₹{o['cost']}\n"
            f"   Status: {o['status'].title()}\n\n"
        )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Refresh Status", callback_data="refresh_orders")]
    ])
    await msg.answer(text, parse_mode="HTML", reply_markup=kb)

@router.callback_query(F.data == "refresh_orders")
async def refresh_orders(cb: CallbackQuery):
    orders = await db.get_user_orders(cb.from_user.id, limit=5)
    if not orders:
        await cb.answer("No orders found", show_alert=True)
        return

    # Check status from SMM panel
    await cb.answer("Checking statuses...")
    for o in orders:
        if o["status"] not in ["completed", "cancelled"]:
            result = await smm_api("status", {"order": o["smm_order_id"]})
            if result.get("status"):
                new_status = result["status"].lower()
                start_count = result.get("start_count", 0)
                remains = result.get("remains", 0)
                await db.update_order_status(o["smm_order_id"], new_status, start_count, remains)

    await my_orders(cb.message)

# ─── Help ─────────────────────────────────────────────────────────────────────

@router.message(F.text == "❓ Help")
async def help_cmd(msg: Message):
    await msg.answer(
        f"ℹ️ <b>{config.BOT_NAME} Help</b>\n\n"
        f"🛒 <b>New Order</b> - Place a new SMM order\n"
        f"📋 <b>My Orders</b> - View your order history\n"
        f"💰 <b>Balance</b> - Check your wallet balance\n"
        f"➕ <b>Add Funds</b> - Recharge via Cashfree/UPI\n"
        f"📊 <b>Services</b> - Browse available services\n"
        f"👤 <b>Profile</b> - Your account details\n\n"
        f"📞 Support: @{config.SUPPORT_USERNAME}",
        parse_mode="HTML"
    )

# ─── Admin Panel ──────────────────────────────────────────────────────────────

def is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS

@router.message(F.text == "🔧 Admin Panel")
async def admin_panel(msg: Message):
    if not is_admin(msg.from_user.id):
        await msg.answer("❌ Access denied.")
        return

    stats = await db.get_stats()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Users", callback_data="admin_users"),
         InlineKeyboardButton(text="📦 Orders", callback_data="admin_orders")],
        [InlineKeyboardButton(text="➕ Add Balance", callback_data="admin_add_balance"),
         InlineKeyboardButton(text="📢 Broadcast", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="💰 Recharges", callback_data="admin_recharges")]
    ])

    await msg.answer(
        f"🔧 <b>Admin Panel</b>\n\n"
        f"👥 Total Users: {stats['users']}\n"
        f"📦 Total Orders: {stats['orders']}\n"
        f"💰 Total Revenue: ₹{stats['revenue']:.2f}\n"
        f"📈 Today Orders: {stats['today_orders']}\n",
        parse_mode="HTML",
        reply_markup=kb
    )

@router.callback_query(F.data == "admin_add_balance")
async def admin_add_balance_start(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return
    await cb.message.edit_text("Send User ID to add balance:")
    await state.set_state(AdminStates.waiting_user_id)

@router.message(AdminStates.waiting_user_id)
async def admin_get_user_id(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    if not msg.text.strip().lstrip('-').isdigit():
        await msg.answer("❌ Invalid User ID")
        return
    await state.update_data(target_user_id=int(msg.text.strip()))
    await msg.answer("Enter amount to add (use negative to deduct):")
    await state.set_state(AdminStates.waiting_balance)

@router.message(AdminStates.waiting_balance)
async def admin_add_balance_amount(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    try:
        amount = float(msg.text.strip())
    except ValueError:
        await msg.answer("❌ Invalid amount")
        return

    data = await state.get_data()
    target_id = data["target_user_id"]

    user = await db.admin_update_balance(target_id, amount)
    if user:
        await msg.answer(
            f"✅ Balance updated!\nUser: {target_id}\nAdded: ₹{amount}\nNew Balance: ₹{user['balance']:.2f}",
            reply_markup=get_kb(msg.from_user.id)
        )
        try:
            await bot.send_message(
                target_id,
                f"💰 Your balance has been {'added' if amount > 0 else 'adjusted'}: ₹{amount:+.2f}\nNew Balance: ₹{user['balance']:.2f}"
            )
        except Exception:
            pass
    else:
        await msg.answer("❌ User not found")

    await state.clear()

@router.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_start(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return
    await cb.message.edit_text("✉️ Send broadcast message (supports HTML):")
    await state.set_state(AdminStates.waiting_broadcast)

@router.message(AdminStates.waiting_broadcast)
async def admin_broadcast_send(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    users = await db.get_all_user_ids()
    sent, failed = 0, 0
    await msg.answer(f"📢 Broadcasting to {len(users)} users...")
    for uid in users:
        try:
            await bot.send_message(uid, msg.text, parse_mode="HTML")
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1
    await msg.answer(f"✅ Broadcast done!\nSent: {sent} | Failed: {failed}", reply_markup=get_kb(msg.from_user.id))
    await state.clear()

@router.callback_query(F.data == "admin_users")
async def admin_users(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    users = await db.get_recent_users(10)
    text = "👥 <b>Recent Users</b>\n\n"
    for u in users:
        text += f"• <code>{u['user_id']}</code> - {u['name']} - ₹{u['balance']:.2f}\n"
    await cb.message.edit_text(text, parse_mode="HTML")

@router.callback_query(F.data == "admin_orders")
async def admin_orders(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    orders = await db.get_recent_orders(10)
    text = "📦 <b>Recent Orders</b>\n\n"
    for o in orders:
        text += f"• #{o['smm_order_id']} | {o['service_name'][:25]} | ₹{o['cost']} | {o['status']}\n"
    await cb.message.edit_text(text, parse_mode="HTML")

# ─── Cashfree Webhook ─────────────────────────────────────────────────────────

async def cashfree_webhook(request: web.Request):
    data = await request.json()
    logger.info(f"Cashfree webhook: {data}")

    # Verify signature
    ts = request.headers.get("x-webhook-timestamp", "")
    sig = request.headers.get("x-webhook-signature", "")
    body = await request.read()

    expected = hmac.new(
        config.CASHFREE_SECRET_KEY.encode(),
        f"{ts}{body.decode()}".encode(),
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected, sig):
        return web.Response(status=400, text="Invalid signature")

    event = data.get("type", "")
    if event == "PAYMENT_SUCCESS_WEBHOOK":
        order_id = data["data"]["order"]["order_id"]
        recharge = await db.get_recharge(order_id)
        if recharge and recharge["status"] != "completed":
            await db.complete_recharge(order_id, recharge["user_id"], recharge["amount"])
            user = await db.get_or_create_user(recharge["user_id"], "", "")
            try:
                await bot.send_message(
                    recharge["user_id"],
                    f"✅ Payment confirmed!\n₹{recharge['amount']:.2f} added.\nNew Balance: ₹{user['balance']:.2f}"
                )
            except Exception:
                pass

    return web.Response(text="OK")

# ─── Main ─────────────────────────────────────────────────────────────────────

async def main():
    await db.init()
    logger.info("Bot starting...")

    if config.WEBHOOK_URL:
        # Webhook mode
        app = web.Application()
        app.router.add_post("/cashfree/webhook", cashfree_webhook)

        webhook_handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
        webhook_handler.register(app, path=f"/webhook/{config.BOT_TOKEN}")
        setup_application(app, dp, bot=bot)

        await bot.set_webhook(f"{config.WEBHOOK_URL}/webhook/{config.BOT_TOKEN}")
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", config.PORT)
        await site.start()
        logger.info(f"Webhook running on port {config.PORT}")
        await asyncio.Event().wait()
    else:
        # Polling mode
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
