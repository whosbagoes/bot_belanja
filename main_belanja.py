import logging
import os
import json
import re
import pytz
import base64
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, ConversationHandler, MessageHandler, filters
)
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import google.generativeai as genai

# ==================== KONFIGURASI ====================
BOT_TOKEN = os.environ.get("BOT_TOKEN_BELANJA")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
SHEET_NAME = "Pengeluaran"
WIB = pytz.timezone("Asia/Jakarta")

genai.configure(api_key=GEMINI_API_KEY)

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ==================== STATE ====================
TUNGGU_INPUT = 1
KONFIRMASI = 2

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
        sheet = spreadsheet.add_worksheet(title=SHEET_NAME, rows=1000, cols=10)
        sheet.append_row(["No", "Tanggal", "Waktu", "Nama Toko", "Item", "Harga Satuan", "Jumlah", "Subtotal", "Total Belanja", "Sumber"])
    return sheet

def simpan_ke_sheet(data: dict):
    sheet = get_sheet()
    now = datetime.now(WIB)
    tanggal = data.get("tanggal", now.strftime("%d/%m/%Y"))
    waktu = now.strftime("%H:%M:%S")
    toko = data.get("toko", "-")
    total = data.get("total", 0)
    sumber = data.get("sumber", "manual")
    items = data.get("items", [])
    no_awal = len(sheet.get_all_values())
    for item in items:
        sheet.append_row([
            no_awal,
            tanggal,
            waktu,
            toko,
            item.get("nama", "-"),
            item.get("harga_satuan", 0),
            item.get("jumlah", 1),
            item.get("subtotal", 0),
            total,
            sumber,
        ])
        no_awal += 1

def fmt(angka):
    try:
        return f"{int(angka):,}".replace(",", ".")
    except:
        return str(angka)

# ==================== GEMINI ====================
async def baca_struk_gemini(foto_bytes: bytes) -> dict:
    model = genai.GenerativeModel("gemini-1.5-flash")
    foto_b64 = base64.b64encode(foto_bytes).decode("utf-8")
    prompt = """Kamu adalah asisten yang membaca struk belanja.
Ekstrak informasi dari struk ini dan kembalikan HANYA dalam format JSON seperti ini:
{
  "toko": "nama toko",
  "tanggal": "dd/mm/yyyy",
  "items": [
    {"nama": "nama item", "jumlah": 1, "harga_satuan": 10000, "subtotal": 10000}
  ],
  "total": 50000
}
Aturan:
- Jika tanggal tidak ada, isi dengan "hari ini"
- Jika nama toko tidak ada, isi dengan "Tidak diketahui"
- jumlah dan harga dalam angka bulat tanpa Rp atau pemisah ribuan
- Jika tidak bisa membaca struk, kembalikan {"error": "tidak bisa membaca struk"}
- Kembalikan HANYA JSON, tanpa teks lain"""
    response = model.generate_content([
        {"mime_type": "image/jpeg", "data": foto_b64},
        prompt
    ])
    text = response.text.strip()
    text = re.sub(r"```json|```", "", text).strip()
    return json.loads(text)

# ==================== PARSE INPUT MANUAL ====================
def parse_input_manual(teks: str) -> dict:
    """
    Format: Nama Toko | Item jumlah harga, Item jumlah harga
    Contoh: Pasar Minggu | Tepung Terigu 2 15000, Gula Pasir 1 12000
    """
    try:
        if "|" in teks:
            bagian = teks.split("|", 1)
            toko = bagian[0].strip()
            item_str = bagian[1].strip()
        else:
            toko = "Tidak diketahui"
            item_str = teks.strip()

        items = []
        total = 0
        for item_raw in item_str.split(","):
            item_raw = item_raw.strip()
            if not item_raw:
                continue
            angka = re.findall(r'\d+', item_raw)
            teks_nama = re.sub(r'\d+', '', item_raw).strip()

            if len(angka) >= 2:
                jumlah = int(angka[-2])
                harga_satuan = int(angka[-1])
                if jumlah > 100:
                    jumlah = 1
                    harga_satuan = int(angka[-1])
            elif len(angka) == 1:
                jumlah = 1
                harga_satuan = int(angka[0])
            else:
                continue

            subtotal = jumlah * harga_satuan
            total += subtotal
            items.append({
                "nama": teks_nama if teks_nama else "Item",
                "jumlah": jumlah,
                "harga_satuan": harga_satuan,
                "subtotal": subtotal,
            })

        now = datetime.now(WIB)
        return {
            "toko": toko,
            "tanggal": now.strftime("%d/%m/%Y"),
            "items": items,
            "total": total,
            "sumber": "manual",
        }
    except Exception as e:
        logger.error(f"Error parse manual: {e}")
        return None

def format_ringkasan(data: dict) -> str:
    teks = f"📋 *Ringkasan Belanja*\n\n"
    teks += f"🏪 Toko: {data.get('toko', '-')}\n"
    teks += f"📅 Tanggal: {data.get('tanggal', '-')}\n\n"
    teks += f"🛒 *Item:*\n"
    for item in data.get("items", []):
        teks += f"  • {item['nama']}\n"
        teks += f"    {item['jumlah']} x Rp {fmt(item['harga_satuan'])} = Rp {fmt(item['subtotal'])}\n"
    teks += f"\n💰 *Total: Rp {fmt(data.get('total', 0))}*"
    return teks

# ==================== HANDLERS ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📸 Upload Struk", callback_data="struk")],
        [InlineKeyboardButton("✏️ Input Manual", callback_data="manual")],
    ]
    await update.message.reply_text(
        "🧾 *Bot Pengeluaran Bahan Baku*\n\nHalo! Mau catat pengeluaran gimana?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def menu_utama(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    keyboard = [
        [InlineKeyboardButton("📸 Upload Struk", callback_data="struk")],
        [InlineKeyboardButton("✏️ Input Manual", callback_data="manual")],
    ]
    await query.edit_message_text(
        "🧾 *Bot Pengeluaran Bahan Baku*\n\nMau catat pengeluaran gimana?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def pilih_struk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["mode"] = "struk"
    await query.edit_message_text(
        "📸 Silakan kirim *foto struk* belanja kamu.",
        parse_mode="Markdown"
    )
    return TUNGGU_INPUT

async def pilih_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["mode"] = "manual"
    await query.edit_message_text(
        "✏️ Ketik pengeluaran dengan format:\n\n"
        "*Nama Toko | Item jumlah harga, Item jumlah harga*\n\n"
        "Contoh:\n"
        "`Pasar Minggu | Tepung Terigu 2 15000, Gula Pasir 1 12000`\n\n"
        "Tanpa jumlah (otomatis 1):\n"
        "`Alfamart | Mentega 8500, Telur 10 25000`",
        parse_mode="Markdown"
    )
    return TUNGGU_INPUT

async def terima_foto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Sedang membaca struk, tunggu sebentar...")
    try:
        foto = update.message.photo[-1]
        file = await foto.get_file()
        foto_bytes = await file.download_as_bytearray()
        data = await baca_struk_gemini(bytes(foto_bytes))

        if "error" in data:
            keyboard = [
                [InlineKeyboardButton("✏️ Input Manual", callback_data="manual")],
                [InlineKeyboardButton("🏠 Menu Utama", callback_data="menu_utama")],
            ]
            await update.message.reply_text(
                "❌ Tidak bisa membaca struk. Coba input manual.",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return ConversationHandler.END

        if data.get("tanggal") == "hari ini":
            data["tanggal"] = datetime.now(WIB).strftime("%d/%m/%Y")

        data["sumber"] = "struk"
        context.user_data["data_belanja"] = data

        keyboard = [
            [InlineKeyboardButton("✅ Simpan", callback_data="simpan")],
            [InlineKeyboardButton("❌ Batal", callback_data="batal")],
        ]
        await update.message.reply_text(
            format_ringkasan(data) + "\n\nData sudah benar?",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return KONFIRMASI

    except Exception as e:
        logger.error(f"Error baca struk: {e}")
        keyboard = [
            [InlineKeyboardButton("✏️ Input Manual", callback_data="manual")],
            [InlineKeyboardButton("🏠 Menu Utama", callback_data="menu_utama")],
        ]
        await update.message.reply_text(
            f"❌ Gagal membaca struk.\n\nCoba input manual.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return ConversationHandler.END

async def terima_teks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    teks = update.message.text.strip()
    data = parse_input_manual(teks)

    if not data or not data.get("items"):
        await update.message.reply_text(
            "❌ Format tidak dikenali. Coba lagi:\n\n"
            "`Nama Toko | Item jumlah harga, Item jumlah harga`\n\n"
            "Contoh:\n`Pasar | Tepung 2 15000, Gula 1 12000`",
            parse_mode="Markdown"
        )
        return TUNGGU_INPUT

    context.user_data["data_belanja"] = data
    keyboard = [
        [InlineKeyboardButton("✅ Simpan", callback_data="simpan")],
        [InlineKeyboardButton("❌ Batal", callback_data="batal")],
    ]
    await update.message.reply_text(
        format_ringkasan(data) + "\n\nData sudah benar?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return KONFIRMASI

async def simpan_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = context.user_data.get("data_belanja")
    if not data:
        await query.edit_message_text("❌ Data tidak ditemukan. Mulai ulang.")
        return ConversationHandler.END
    try:
        simpan_ke_sheet(data)
        keyboard = [
            [InlineKeyboardButton("📸 Catat Lagi (Struk)", callback_data="struk")],
            [InlineKeyboardButton("✏️ Catat Lagi (Manual)", callback_data="manual")],
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
        entry_points=[
            CallbackQueryHandler(pilih_struk, pattern="^struk$"),
            CallbackQueryHandler(pilih_manual, pattern="^manual$"),
        ],
        states={
            TUNGGU_INPUT: [
                MessageHandler(filters.PHOTO, terima_foto),
                MessageHandler(filters.TEXT & ~filters.COMMAND, terima_teks),
            ],
            KONFIRMASI: [
                CallbackQueryHandler(simpan_data, pattern="^simpan$"),
            ],
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
