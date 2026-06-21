import discord
from discord.ext import tasks, commands
import datetime
import os
import asyncio
import threading
import subprocess
import stat
from dotenv import load_dotenv
from flask import Flask

# --- CẤU HÌNH BOT ---
load_dotenv()
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")


# --- TỰ CÀI DENO NẾU CHƯA CÓ (cần cho yt-dlp giải JS challenge của YouTube) ---
# Thay vì phải sửa Build Command / Start Command trên Render (có thể không linh
# hoạt tùy plan), code tự kiểm tra và cài deno ngay khi khởi động nếu chưa có.
# An toàn để gọi nhiều lần: nếu deno đã có trong PATH thì bỏ qua ngay, không cài lại.
def ensure_deno_installed():
    deno_home = os.path.expanduser("~/.deno/bin")
    deno_exe = os.path.join(deno_home, "deno")

    # Luôn đảm bảo PATH có chứa thư mục deno (nếu binary đã tồn tại từ lần chạy trước)
    if deno_home not in os.environ.get("PATH", ""):
        os.environ["PATH"] = deno_home + os.pathsep + os.environ.get("PATH", "")

    if os.path.exists(deno_exe):
        print("✅ Deno đã có sẵn, không cần cài lại.")
        return

    print("⬇️ Đang cài Deno (cần cho yt-dlp giải JS challenge của YouTube)...")
    try:
        install_script = subprocess.run(
            ["curl", "-fsSL", "https://deno.land/install.sh"],
            capture_output=True, text=True, timeout=30, check=True
        ).stdout
        subprocess.run(
            ["sh", "-s", "--", "-y"],
            input=install_script, text=True, timeout=120, check=True
        )
        if os.path.exists(deno_exe):
            os.chmod(deno_exe, os.stat(deno_exe).st_mode | stat.S_IEXEC)
            print("✅ Đã cài Deno thành công.")
        else:
            print("⚠️ Cài Deno xong nhưng không thấy binary, kiểm tra lại thủ công.")
    except Exception as e:
        # Không để lỗi cài deno làm sập cả bot — yt-dlp vẫn chạy được (chỉ thiếu
        # vài format/audio chất lượng cao), tốt hơn là bot không khởi động được luôn.
        print(f"⚠️ Lỗi khi tự cài Deno: {e}. Bot vẫn tiếp tục khởi động.")


ensure_deno_installed()

# DEBUG: kiểm tra import davey/nacl ngay khi khởi động để biết lỗi thật.
# Nếu lỗi xảy ra lúc import (thiếu .so hệ thống chẳng hạn), sẽ thấy traceback
# rõ ràng ngay trong log, thay vì chỉ thấy message rút gọn lúc connect voice.
try:
    import nacl
    print(f"🔍 PyNaCl OK, version: {nacl.__version__}")
except Exception as e:
    print(f"🔍 ❌ Lỗi import PyNaCl: {type(e).__name__}: {e}")

try:
    import davey
    print("🔍 davey OK")
except Exception as e:
    print(f"🔍 ❌ Lỗi import davey: {type(e).__name__}: {e}")

print(f"🔍 discord.py version: {discord.__version__}")


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
    print("🔍 [DEBUG] Đang load music...")
    try:
        await bot.load_extension('music')
        print(f"✅ Đã load music")
    except Exception as e:
        print(f"❌ Lỗi load music: {e}")

    print("🔍 [DEBUG] Đang load createvoice...")
    try:
        await bot.load_extension('createvoice')
        print(f"✅ Đã load createvoice")
    except Exception as e:
        print(f"❌ Lỗi load createvoice: {e}")

    print("🔍 [DEBUG] Đang load consult...")
    # Load consult với groq key
    try:
        from consult import setup as consult_setup
        await consult_setup(bot, GROQ_API_KEY)
        print(f"✅ Đã load consult")
    except Exception as e:
        print(f"❌ Lỗi load consult: {e}")
    print("🔍 [DEBUG] load_extensions() hoàn tất.")

async def main():
    async with bot:
        print("🚀 Đang khởi động bot...")
        print("🔍 [DEBUG] Bắt đầu load_extensions()...")
        await load_extensions()
        print("✅ Đã nạp xong module!")
        print("🔍 [DEBUG] Sắp gọi bot.start(TOKEN)...")
        print(f"🔍 [DEBUG] TOKEN có tồn tại không: {bool(TOKEN)}, độ dài: {len(TOKEN) if TOKEN else 0}")
        try:
            await bot.start(TOKEN)
        except Exception as e:
            print(f"🔍 ❌ [DEBUG] bot.start() raise lỗi: {type(e).__name__}: {e}")
            raise

if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    print(f"🌐 Flask server đang chạy ở port {os.getenv('PORT', 8080)}")

    asyncio.run(main())