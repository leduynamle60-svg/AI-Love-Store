import discord
from discord.ext import tasks, commands
import datetime
import os
import asyncio
import threading
from dotenv import load_dotenv
from flask import Flask

# --- CẤU HÌNH BOT ---
load_dotenv()
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# --- FLASK SERVER (giữ bot online trên Render + UptimeRobot ping) ---
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is alive!"

@app.route("/health")
def health():
    """Endpoint riêng cho UptimeRobot, trả JSON nhẹ hơn để ping nhanh."""
    return {"status": "ok", "bot_ready": bot.is_ready()}

def run_flask():
    port = int(os.getenv("PORT", 8080))  # Render tự cấp PORT qua biến môi trường
    app.run(host="0.0.0.0", port=port)

# --- STATUS TASK ---
start_time = datetime.datetime.now(datetime.timezone.utc)

@tasks.loop(minutes=1)
async def update_bot_status():
    uptime = datetime.datetime.now(datetime.timezone.utc) - start_time
    hours, remainder = divmod(int(uptime.total_seconds()), 3600)
    minutes, _ = divmod(remainder, 60)
    activity = discord.Game(name=f"💖 Love Store | {hours}h {minutes}m")
    await bot.change_presence(activity=activity)

# --- SỰ KIỆN KHI BOT ONLINE ---
@bot.event
async def on_ready():
    print(f"🎉 Bot đã đăng nhập: {bot.user}")
    if not update_bot_status.is_running():
        update_bot_status.start()

async def load_extensions():
    """Load các cog từ các file riêng"""
    try:
        await bot.load_extension('music')
        print(f"✅ Đã load music")
    except Exception as e:
        print(f"❌ Lỗi load music: {e}")

    try:
        await bot.load_extension('createvoice')
        print(f"✅ Đã load createvoice")
    except Exception as e:
        print(f"❌ Lỗi load createvoice: {e}")

    # Load consult với groq key
    try:
        from consult import setup as consult_setup
        await consult_setup(bot, GROQ_API_KEY)
        print(f"✅ Đã load consult")
    except Exception as e:
        print(f"❌ Lỗi load consult: {e}")

async def main():
    async with bot:
        print("🚀 Đang khởi động bot...")
        await load_extensions()
        print("✅ Đã nạp xong module!")
        await bot.start(TOKEN)

if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    print(f"🌐 Flask server đang chạy ở port {os.getenv('PORT', 8080)}")

    asyncio.run(main())