import discord
from discord.ext import commands
from groq import Groq

class ConsultCog(commands.Cog):
    def __init__(self, bot, groq_api_key):
        self.bot = bot
        self.groq_client = Groq(api_key=groq_api_key)
        self.CONSULT_CHANNEL_ID = 1517781027349725295  # Thay ID kênh của bạn
        
        # Đọc thông tin dịch vụ từ file txt
        with open('services.txt', 'r', encoding='utf-8') as f:
            self.services_info = f.read()
        
        # Tạo system prompt
        self.SYSTEM_PROMPT = f"""Bạn là nhân viên bán hàng lịch sự, thân thiện.
Trả lời ngắn gọn tự nhiên bằng tiếng Việt.

THÔNG TIN DỊCH VỤ:
{self.services_info}

HƯỚNG DẪN:
- Trả lời dựa trên thông tin phía trên
- Khi khách muốn chốt đơn: "Vui lòng nhắn admin để hoàn tất đơn hàng"
- Giải thích rõ ràng, trả lời tự nhiên"""

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author == self.bot.user:
            return

        if message.channel.id == self.CONSULT_CHANNEL_ID:
            await self.process_consult(message)
        elif self.bot.user in message.mentions:
            await self.process_consult(message)

    async def process_consult(self, message):
        question = message.content
        for mention in message.mentions:
            question = question.replace(f"<@{mention.id}>", "").strip()
        
        if not question:
            return

        async with message.channel.typing():
            try:
                response = self.groq_client.chat.completions.create(
                    model="openai/gpt-oss-20b",
                    messages=[
                        {"role": "system", "content": self.SYSTEM_PROMPT},
                        {"role": "user", "content": question}
                    ]
                )
                answer = response.choices[0].message.content
                await message.reply(f"💬 {answer}", mention_author=False)

            except Exception as e:
                await message.reply(f"❌ Lỗi: {e}", mention_author=False)

async def setup(bot, groq_api_key):
    await bot.add_cog(ConsultCog(bot, groq_api_key))