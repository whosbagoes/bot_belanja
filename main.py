import logging
import os
import json
import re
import pytz
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, ConversationHandler, MessageHandler, filters
)
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ==================== KONFIGURASI ====================
BOT_TOKEN = os.environ.get("BOT_TOKEN_BELANJA")
SHEET_NAME = "Pengeluaran"
WIB = pytz.timezone("Asia/Jakarta")

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ==================== STATE ====================
TOKO, ITEM, JUMLAH, HARGA_SATUAN, TAMBAH_ITEM, PEMBAYARAN, KONFIRMASI = range(7)

# ==================== GOOGLE SHEETS ====================
def get_sheet():
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]
    creds_json = json.loads(os.environ.get("GOOGLE_CREDENTIALS"))
    spreadsheet_id = os.environ.get("SPREADSHEET_ID_BELANJA")
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_json, scope)
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(spreadsheet_id)
    try:
        sheet = spreadsheet.worksheet(SHEET_NAME)
    except gspread.WorksheetNotFound:
        sheet = spreadsheet.add_worksheet(title=SHEET_NAME, rows=1000, cols=9)
        sheet.append_row(["No", "Tanggal", "Waktu", "Nama Toko", "Item", "Jumlah", "Harga Satuan", "Subtotal", "Metode Bayar"])
    return sheet

def simpan_ke_sheet(data: dict):
    sheet = get_sheet()
    now = datetime.now(WIB)
    tanggal = now.strftime("%d/%m/%Y")
    waktu = now.strftime("%H:%M:%S")
    toko = data.get("toko", "-")
    pembayaran = data.get("pembayaran", "-")
    items = data.get("items", [])
    no_awal = len(sheet.get_all_values())
    for item in items:
        sheet.append_row([
            no_awal,
            tanggal,
            waktu,
            toko,
            item.get("nama", "-"),
            item.get("jumlah", 1),
            item.get("harga_satuan", 0),
            item.get("subtotal", 0),
            pembayaran,
        ])
        no_awal += 1

def fmt(angka):
    try:
        return f"{int(angka):,}".replace(",", ".")
    except:
        return str(angka)

def format_ringkasan(data: dict) -> str:
    teks = f"📋 *Ringkasan Belanja*\n\n"
    teks += f"🏪 Toko: {data.get('toko', '-')}\n"
    teks += f"💳 Pembayaran: {data.get('pembayaran', '-')}\n\n"
    teks += f"🛒 *Item:*\n"
    total = 0
    for item in data.get("items", []):
        subtotal = item.get("subtotal", 0)
        total += subtotal
        teks += f"  • {item['nama']}\n"
        teks += f"    {item['jumlah']} x Rp {fmt(item['harga_satuan'])} = Rp {fmt(subtotal)}\n"
    teks += f"\n💰 *Total: Rp {fmt(total)}*"
    return teks

# ==================== HANDLERS ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("✏️ Catat Pengeluaran", callback_data="catat")]]
    await update.message.reply_text(
        "🧾 *Bot Pengeluaran Bahan Baku*\n\nHalo! Siap mencatat pengeluaran belanja kamu.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def menu_utama(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    keyboard = [[InlineKeyboardButton("✏️ Catat Pengeluaran", callback_data="catat")]]
    await query.edit_message_text(
        "🧾 *Bot Pengeluaran Bahan Baku*\n\nMau catat pengeluaran baru?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def catat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    context.user_data["items"] = []
    await query.edit_message_text(
        "🏪 Beli di mana?\n\nKetik nama toko/tempat belanja:"
    )
    return TOKO

async def terima_toko(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["toko"] = update.message.text.strip()
    await update.message.reply_text(
        "🛒 Item apa yang dibeli?\n\nKetik nama item:"
    )
    return ITEM

async def terima_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["item_sementara"] = {"nama": update.message.text.strip()}
    await update.message.reply_text(
        f"📦 Berapa jumlah *{context.user_data['item_sementara']['nama']}* yang dibeli?\n\nKetik angka:",
        parse_mode="Markdown"
    )
    return JUMLAH

async def terima_jumlah(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        jumlah = int(update.message.text.strip())
        if jumlah <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Masukkan angka yang benar:")
        return JUMLAH
    context.user_data["item_sementara"]["jumlah"] = jumlah
    await update.message.reply_text(
        f"💰 Berapa harga satuan *{context.user_data['item_sementara']['nama']}*?\n\nKetik angka (tanpa Rp):",
        parse_mode="Markdown"
    )
    return HARGA_SATUAN

async def terima_harga_satuan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        harga = int(re.sub(r'[^\d]', '', update.message.text.strip()))
        if harga <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Masukkan angka yang benar:")
        return HARGA_SATUAN

    item = context.user_data["item_sementara"]
    item["harga_satuan"] = harga
    item["subtotal"] = item["jumlah"] * harga
    context.user_data["items"].append(item)
    context.user_data["item_sementara"] = {}

    keyboard = [
        [InlineKeyboardButton("➕ Tambah Item Lagi", callback_data="tambah_item")],
        [InlineKeyboardButton("✅ Selesai Input Item", callback_data="selesai_item")],
    ]
    await update.message.reply_text(
        f"✅ *{item['nama']}* ditambahkan!\n"
        f"   {item['jumlah']} x Rp {fmt(harga)} = Rp {fmt(item['subtotal'])}\n\n"
        f"Masih ada item lain?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return TAMBAH_ITEM

async def tambah_item_lagi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "🛒 Item apa lagi yang dibeli?\n\nKetik nama item:"
    )
    return ITEM

async def selesai_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [
        [InlineKeyboardButton("💵 Cash", callback_data="cash")],
        [InlineKeyboardButton("📱 QRIS", callback_data="qris")],
        [InlineKeyboardButton("💳 Transfer", callback_data="transfer")],
    ]
    await query.edit_message_text(
        "💳 Pembayaran pakai apa?",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return PEMBAYARAN

async def terima_pembayaran(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    metode = {"cash": "Cash 💵", "qris": "QRIS 📱", "transfer": "Transfer 💳"}
    context.user_data["pembayaran"] = metode[query.data]

    keyboard = [
        [InlineKeyboardButton("✅ Simpan", callback_data="simpan")],
        [InlineKeyboardButton("❌ Batal", callback_data="batal")],
    ]
    await query.edit_message_text(
        format_ringkasan(context.user_data) + "\n\nData sudah benar?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return KONFIRMASI

async def simpan_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        simpan_ke_sheet(context.user_data)
        keyboard = [
            [InlineKeyboardButton("✏️ Catat Lagi", callback_data="catat")],
            [InlineKeyboardButton("🏠 Menu Utama", callback_data="menu_utama")],
        ]
        await query.edit_message_text(
            "✅ *Pengeluaran berhasil dicatat!*\n\nData sudah masuk ke Google Sheets 📊",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error simpan: {e}")
        keyboard = [[InlineKeyboardButton("🏠 Menu Utama", callback_data="menu_utama")]]
        await query.edit_message_text(
            f"❌ Gagal menyimpan: {str(e)}",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    context.user_data.clear()
    return ConversationHandler.END

async def batal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    keyboard = [[InlineKeyboardButton("🏠 Menu Utama", callback_data="menu_utama")]]
    await query.edit_message_text("❌ Dibatalkan.", reply_markup=InlineKeyboardMarkup(keyboard))
    return ConversationHandler.END

# ==================== MAIN ====================
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(catat, pattern="^catat$")],
        states={
            TOKO:        [MessageHandler(filters.TEXT & ~filters.COMMAND, terima_toko)],
            ITEM:        [MessageHandler(filters.TEXT & ~filters.COMMAND, terima_item)],
            JUMLAH:      [MessageHandler(filters.TEXT & ~filters.COMMAND, terima_jumlah)],
            HARGA_SATUAN:[MessageHandler(filters.TEXT & ~filters.COMMAND, terima_harga_satuan)],
            TAMBAH_ITEM: [
                CallbackQueryHandler(tambah_item_lagi, pattern="^tambah_item$"),
                CallbackQueryHandler(selesai_item, pattern="^selesai_item$"),
            ],
            PEMBAYARAN:  [CallbackQueryHandler(terima_pembayaran, pattern="^(cash|qris|transfer)$")],
            KONFIRMASI:  [CallbackQueryHandler(simpan_data, pattern="^simpan$")],
        },
        fallbacks=[
            CallbackQueryHandler(batal, pattern="^batal$"),
            CallbackQueryHandler(menu_utama, pattern="^menu_utama$"),
            CommandHandler("start", start),
        ],
        per_message=False,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv_handler)
    app.add_handler(CallbackQueryHandler(menu_utama, pattern="^menu_utama$"))

    logger.info("🧾 Bot Pengeluaran berjalan...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
