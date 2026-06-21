import discord
from discord.ext import tasks, commands
import datetime
import os
import asyncio
from dotenv import load_dotenv

# --- CẤU HÌNH BOT ---
load_dotenv()
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

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
    asyncio.run(main())