import discord
from discord.ext import commands
import json
import os

class CreateVoice(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db_file = "user_rooms.json"
        self.created_rooms_file = "created_rooms.json"
        self.created_rooms = self.load_created_rooms()

    def load_created_rooms(self):
        """Load danh sách channel_id do bot tạo ra (qua Join to Create)."""
        if os.path.exists(self.created_rooms_file):
            try:
                with open(self.created_rooms_file, "r") as f:
                    return set(json.load(f))
            except:
                return set()
        return set()

    def save_created_rooms(self):
        try:
            with open(self.created_rooms_file, "w") as f:
                json.dump(list(self.created_rooms), f, indent=4)
        except Exception as e:
            print(f"❌ Lỗi lưu created_rooms: {e}")

    def mark_room_created(self, channel_id):
        self.created_rooms.add(channel_id)
        self.save_created_rooms()

    def unmark_room(self, channel_id):
        self.created_rooms.discard(channel_id)
        self.save_created_rooms()

    def save_user_room_name(self, user_id, name):
        data = {}
        if os.path.exists(self.db_file):
            with open(self.db_file, "r") as f:
                try: data = json.load(f)
                except: pass
        data[str(user_id)] = name
        with open(self.db_file, "w") as f: json.dump(data, f, indent=4)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        # 1. Tạo phòng
        if after.channel and after.channel.name == "Join to Create":
            custom_name = None
            if os.path.exists(self.db_file):
                with open(self.db_file, "r") as f:
                    try:
                        data = json.load(f)
                        custom_name = data.get(str(member.id))
                    except: pass

            final_name = custom_name if custom_name else f"Phòng của {member.display_name}"

            new_channel = await member.guild.create_voice_channel(
                name=final_name,
                category=after.channel.category
            )
            await member.move_to(new_channel)

            
            self.mark_room_created(new_channel.id)

            embed = discord.Embed(
                title="🎮 Bảng điều khiển phòng",
                description=f"Chào {member.mention}, đây là phòng của bạn!",
                color=discord.Color.green()
            )
            await new_channel.send(embed=embed, view=VoiceControlView(new_channel, member.id))


        if before.channel and before.channel.id in self.created_rooms:
            if len(before.channel.members) == 0:
                try:
                    await before.channel.delete()
                    self.unmark_room(before.channel.id)
                    print(f"✅ Đã xóa kênh {before.channel.name} thành công!")
                except Exception as e:
                    print(f"❌ Lỗi xóa kênh: {e}")

class VoiceControlView(discord.ui.View):
    def __init__(self, channel, owner_id):
        super().__init__(timeout=None)
        self.channel = channel
        self.owner_id = owner_id

    @discord.ui.button(label="🔒 Khóa/Mở", style=discord.ButtonStyle.red)
    async def lock(self, interaction: discord.Interaction, button: discord.ui.Button):
        perms = self.channel.overwrites_for(interaction.guild.default_role)
        perms.connect = not perms.connect
        await self.channel.set_permissions(interaction.guild.default_role, connect=perms.connect)
        await interaction.response.send_message("✅ Đã cập nhật trạng thái khóa!", ephemeral=True)

    @discord.ui.button(label="✏️ Đổi tên", style=discord.ButtonStyle.primary)
    async def rename(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RenameModal(self.channel, self.owner_id))

    @discord.ui.button(label="👥 Giới hạn", style=discord.ButtonStyle.secondary)
    async def limit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(LimitModal(self.channel))

class RenameModal(discord.ui.Modal, title="Đổi tên phòng"):
    name = discord.ui.TextInput(label="Tên mới", style=discord.TextStyle.short)
    def __init__(self, channel, owner_id):
        super().__init__(); self.channel = channel; self.owner_id = owner_id

    async def on_submit(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("CreateVoice")
        cog.save_user_room_name(self.owner_id, self.name.value)
        await self.channel.edit(name=self.name.value)
        await interaction.response.send_message(f"✅ Đã đổi và lưu tên: {self.name.value}", ephemeral=True)

class LimitModal(discord.ui.Modal, title="Giới hạn người"):
    count = discord.ui.TextInput(label="Số người tối đa (0-99)", style=discord.TextStyle.short)
    def __init__(self, channel): super().__init__(); self.channel = channel
    async def on_submit(self, interaction: discord.Interaction):
        await self.channel.edit(user_limit=int(self.count.value))
        await interaction.response.send_message(f"✅ Đã đặt giới hạn {self.count.value} người!", ephemeral=True)

async def setup(bot):
    await bot.add_cog(CreateVoice(bot))