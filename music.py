import discord
from discord.ext import commands
import yt_dlp
import asyncio
from collections import deque
import json
import os
from discord.ext import tasks
import signal
import time


class Music(commands.Cog):
    # --- Tasks ---
    @tasks.loop(seconds=30.0)
    async def check_idle(self):
        """
        Mỗi 30s kiểm tra các voice client. Nếu một channel không có bài nào
        đang phát/pause LIÊN TỤC trong >= 3 phút (180s) thì bot tự rời.
        Khác với bản cũ: không phụ thuộc vào số người trong channel.
        """
        now = time.time()
        for vc in list(self.bot.voice_clients):
            guild_id = vc.guild.id

            if vc.is_playing() or vc.is_paused():
                # Đang phát/pause -> reset mốc thời gian rảnh
                self.idle_since.pop(guild_id, None)
                continue

            # Không phát gì cả -> bắt đầu/ tiếp tục tính thời gian rảnh
            if guild_id not in self.idle_since:
                self.idle_since[guild_id] = now
                continue

            if now - self.idle_since[guild_id] >= 180:
                try:
                    await vc.disconnect()
                except Exception as e:
                    print(f"[IDLE] ❌ Lỗi disconnect: {e}")
                self.music_queue.clear()
                self.delete_state()
                self.idle_since.pop(guild_id, None)
                print(f"✅ Bot đã tự rời khỏi {vc.channel.name} vì không phát nhạc quá 3 phút.")

    @check_idle.before_loop
    async def before_check_idle(self):
        await self.bot.wait_until_ready()

    def __init__(self, bot):
        self.bot = bot
        self.music_queue = deque()
        self.STATE_FILE = "bot_state.json"
        self.is_resuming = False
        self.current_channel_id = None
        self.current_song_position = 0
        self.play_start_time = {}  # guild_id -> (timestamp lúc bắt đầu phát, vị trí seek ban đầu)
        self.idle_since = {}  # guild_id -> timestamp bắt đầu rảnh
        self.last_text_channel = {}  # guild_id -> text channel để gửi embed thông báo
        self._audio_cache = {}  # query -> {'data': {...}, 'ts': timestamp} cache kết quả tìm bài
        self._CACHE_TTL = 300  # giây — cache hết hạn sau 5 phút (link stream YouTube có thời hạn)
        self._shutdown_saved = False  # chống lưu state + gọi close() nhiều lần
        self.check_idle.start()
        asyncio.create_task(self.auto_resume())
        signal.signal(signal.SIGINT, self._handle_shutdown)

    def cog_unload(self):
        self.check_idle.cancel()

    def _calc_current_position(self, guild_id):
        """Tính số giây đã phát thực tế của bài hiện tại, dựa trên thời điểm bắt đầu phát."""
        info = self.play_start_time.get(guild_id)
        if not info:
            return self.current_song_position
        start_ts, seek_offset = info
        elapsed = time.time() - start_ts
        return int(seek_offset + max(elapsed, 0))

    def _handle_shutdown(self, signum, frame):
        """Lưu state + position trước khi shutdown (chỉ chạy 1 lần)."""
        if self._shutdown_saved:
            return
        self._shutdown_saved = True

        if self.current_channel_id:
            # Cập nhật vị trí thật tại thời điểm tắt, không phải giá trị cũ = 0
            guild_id = self._guild_id_from_channel(self.current_channel_id)
            if guild_id is not None:
                self.current_song_position = self._calc_current_position(guild_id)

            print(f"\n[SHUTDOWN] Lưu state + position trước khi tắt...")
            state = {
                "queue": list(self.music_queue),
                "channel_id": self.current_channel_id,
                "current_position": self.current_song_position
            }
            try:
                with open(self.STATE_FILE, "w", encoding='utf-8') as f:
                    json.dump(state, f, indent=4)
                    f.flush()
                    os.fsync(f.fileno())
                print(f"[SHUTDOWN] ✅ Đã lưu {len(self.music_queue)} bài, position: {self.current_song_position}s")
            except Exception as e:
                print(f"[SHUTDOWN] ❌ Lỗi save: {e}")
        asyncio.create_task(self.bot.close())

    def _guild_id_from_channel(self, channel_id):
        """Lấy guild_id từ một voice channel_id, dùng cache của bot."""
        channel = self.bot.get_channel(channel_id)
        return channel.guild.id if channel else None

    async def auto_resume(self):
        """Tự động kết nối lại voice và phát nhạc khi bot restart"""
        await self.bot.wait_until_ready()
        await asyncio.sleep(3)

        if self.is_resuming:
            return

        self.is_resuming = True

        state = self.load_state()
        print(f"[AUTO-RESUME] Kiểm tra state: channel_id={state['channel_id']}, queue={len(state['queue'])} bài, position={state.get('current_position', 0)}s")

        if not state["channel_id"] or not state["queue"]:
            print("[AUTO-RESUME] State không hợp lệ, bỏ qua")
            self.is_resuming = False
            return

        max_retries = 3
        retry_count = 0

        while retry_count < max_retries:
            try:
                channel = self.bot.get_channel(state["channel_id"])
                if not channel:
                    print(f"[AUTO-RESUME] ❌ Channel {state['channel_id']} không tồn tại")
                    self.delete_state()
                    break

                print(f"[AUTO-RESUME] ✅ Tìm thấy channel: {channel.name}")

                existing_vc = None
                for vc in self.bot.voice_clients:
                    if vc.guild == channel.guild:
                        existing_vc = vc
                        break

                if existing_vc and existing_vc.channel != channel:
                    await existing_vc.disconnect()
                    await asyncio.sleep(1)

                if existing_vc and existing_vc.channel == channel:
                    vc = existing_vc
                else:
                    vc = await channel.connect()

                self.music_queue.clear()
                for song in state["queue"]:
                    self.music_queue.append(song)

                self.current_channel_id = channel.id
                self.current_song_position = state.get("current_position", 0)
                guild_id = channel.guild.id

                print(f"[AUTO-RESUME] ✅ Khôi phục {len(self.music_queue)} bài")

                if self.music_queue:
                    song = self.music_queue[0]
                    print(f"[AUTO-RESUME] 🎵 Phát: {song['title']} (từ {self.current_song_position}s)")

                    try:
                        audio = await self.get_audio_url_async(song['url'])
                        seek_args = f"-ss {self.current_song_position}" if self.current_song_position > 0 else ""
                        before_options = f"{seek_args} -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5" if seek_args else "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"

                        source = discord.PCMVolumeTransformer(discord.FFmpegPCMAudio(
                            audio['url'],
                            before_options=before_options
                        ))
                        # Lưu mốc thời gian bắt đầu + offset seek, để tính lại position chính xác nếu tắt giữa bài
                        self.play_start_time[guild_id] = (time.time(), self.current_song_position)
                        vc.play(source, after=lambda e: asyncio.run_coroutine_threadsafe(
                            self.play_next_and_remove(vc), self.bot.loop
                        ))
                        print(f"[AUTO-RESUME] ✅ Resume thành công!")
                        self.is_resuming = False
                        return
                    except Exception as e:
                        print(f"[AUTO-RESUME] ❌ Lỗi phát: {e}")
                        retry_count += 1
                        await asyncio.sleep(2)

            except Exception as e:
                print(f"[AUTO-RESUME] ❌ Lỗi: {e}")
                retry_count += 1
                await asyncio.sleep(2)

        print(f"[AUTO-RESUME] ❌ Thất bại")
        self.delete_state()
        self.is_resuming = False

    # --- Hàm hỗ trợ ---
    def save_state(self, queue_data, channel_id):
        if os.path.exists(self.STATE_FILE):
            try:
                os.rename(self.STATE_FILE, self.STATE_FILE + ".bak")
            except:
                pass

        state = {"queue": list(queue_data), "channel_id": channel_id, "current_position": self.current_song_position}
        try:
            with open(self.STATE_FILE, "w", encoding='utf-8') as f:
                json.dump(state, f, indent=4)
                f.flush()
                os.fsync(f.fileno())
        except Exception as e:
            print(f"❌ Lỗi save state: {e}")
            if os.path.exists(self.STATE_FILE + ".bak"):
                os.rename(self.STATE_FILE + ".bak", self.STATE_FILE)

    def load_state(self):
        if os.path.exists(self.STATE_FILE):
            try:
                with open(self.STATE_FILE, "r") as f:
                    return json.load(f)
            except:
                return {"queue": [], "channel_id": None, "current_position": 0}
        return {"queue": [], "channel_id": None, "current_position": 0}

    def delete_state(self):
        if os.path.exists(self.STATE_FILE):
            os.remove(self.STATE_FILE)

    def get_audio_url(self, query):
        """
        Tối ưu tốc độ so với bản cũ:
        - 'bestaudio/best' thay vì 'best' -> yt-dlp không cần quét/so sánh các format
          video nặng, chỉ cần audio, nên extract_info nhanh hơn rõ rệt.
        - Bỏ postprocessor FFmpegExtractAudio: postprocessor đó chỉ có ý nghĩa khi
          DOWNLOAD file thật (convert sau khi tải). Ở đây mình chỉ lấy URL stream
          (download=False) nên postprocessor này chạy vô ích, tốn thời gian xử lý.
        - extractor_args giảm số lượng client yt-dlp thử (chỉ dùng 'android'),
          tránh việc thử nhiều client tuần tự khi client đầu đã đủ dùng.
        """
        # Cache đơn giản: nếu vừa tra cứu link này trong vài phút trước, dùng lại luôn
        cached = self._audio_cache.get(query)
        if cached and (time.time() - cached['ts']) < self._CACHE_TTL:
            return cached['data']

        search_query = f"ytsearch:{query}" if not query.startswith("http") else query
        ydl_opts = {
            'format': 'bestaudio/best',
            'quiet': True,
            'no_warnings': True,
            'default_search': 'auto',
            'noplaylist': True,
            'extract_flat': False,
            'extractor_args': {'youtube': {'player_client': ['android']}},
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search_query, download=False)
            if 'entries' in info:
                info = info['entries'][0]
            result = {'url': info.get('url') or info.get('webpage_url'), 'title': info.get('title', 'Unknown'), 'webpage_url': info.get('webpage_url')}

        self._audio_cache[query] = {'data': result, 'ts': time.time()}
        return result

    async def get_audio_url_async(self, query):
        """Chạy get_audio_url (blocking, vì yt_dlp không hỗ trợ async) trong thread pool,
        để không làm đứng event loop của bot trong lúc đang tìm/extract."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.get_audio_url, query)

    async def play_next(self, ctx):
        """Phát bài đầu queue. KHÔNG xóa ở đây — xóa xảy ra sau khi bài hát kết thúc,
        trong play_next_and_remove, để tránh xóa nhầm khi vừa mới play()."""
        if not ctx.voice_client or not ctx.voice_client.is_connected():
            print("[PLAY_NEXT] ❌ Bot không còn trong voice, hủy.")
            return

        if not self.music_queue:
            return

        song = self.music_queue[0]
        guild_id = ctx.voice_client.channel.guild.id
        self.current_channel_id = ctx.voice_client.channel.id
        self.current_song_position = 0
        self.play_start_time[guild_id] = (time.time(), 0)
        self.save_state(self.music_queue, ctx.voice_client.channel.id)
        try:
            audio = await self.get_audio_url_async(song['url'])
            source = discord.PCMVolumeTransformer(discord.FFmpegPCMAudio(
                audio['url'], before_options="-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
            ))
            ctx.voice_client.play(source, after=lambda e: asyncio.run_coroutine_threadsafe(
                self.play_next_and_remove(ctx.voice_client), self.bot.loop
            ))
            await ctx.send(f"🎵 Đang phát: **{song['title']}**")
        except Exception as e:
            await ctx.send(f"❌ Lỗi: {e}")
            # Bài lỗi -> bỏ khỏi queue rồi thử bài kế, tránh lặp vô hạn nếu bài đó luôn lỗi
            if self.music_queue and self.music_queue[0] is song:
                self.music_queue.popleft()
            if self.music_queue:
                await self.play_next(ctx)
            else:
                self.delete_state()

    async def _send_embed(self, guild_id, embed):
        """Gửi embed vào text channel đã lưu lúc !play, nếu còn tồn tại."""
        channel = self.last_text_channel.get(guild_id)
        if channel:
            try:
                await channel.send(embed=embed)
            except Exception as e:
                print(f"[EMBED] ❌ Không gửi được embed: {e}")

    async def play_next_and_remove(self, vc):
        """
        Gọi sau khi 1 bài phát XONG (callback `after=`).
        Xóa bài vừa nghe khỏi queue, rồi phát bài kế tiếp (nếu còn).
        """
        if not vc or not vc.is_connected():
            print("[PLAY_NEXT] ❌ Bot không còn trong voice, dừng auto-play.")
            return

        guild_id = vc.guild.id

        # Bài vừa phát xong nằm ở đầu queue -> xóa nó
        finished_song = None
        if self.music_queue:
            finished_song = self.music_queue.popleft()
            print(f"[QUEUE] 🗑️ Đã xóa khỏi hàng đợi: {finished_song['title']}")

        if not self.music_queue:
            self.current_song_position = 0
            self.delete_state()
            print("[QUEUE] 📭 Hàng đợi trống, dừng phát.")

            embed = discord.Embed(
                title="📭 Hết Nhạc Rồi!",
                description=(
                    f"Đã phát xong **{finished_song['title']}**.\nHàng đợi trống, "
                    f"dùng `!play [tên/link]` để thêm bài mới nhé!"
                    if finished_song else
                    "Hàng đợi trống, dùng `!play [tên/link]` để thêm bài mới nhé!"
                ),
                color=discord.Color.dark_grey()
            )
            await self._send_embed(guild_id, embed)
            return

        song = self.music_queue[0]
        self.current_song_position = 0
        self.play_start_time[guild_id] = (time.time(), 0)
        self.save_state(self.music_queue, vc.channel.id)

        try:
            audio = await self.get_audio_url_async(song['url'])
            source = discord.PCMVolumeTransformer(discord.FFmpegPCMAudio(
                audio['url'],
                before_options="-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
            ))
            vc.play(source, after=lambda e: asyncio.run_coroutine_threadsafe(
                self.play_next_and_remove(vc), self.bot.loop
            ))

            embed = discord.Embed(
                title="⏭️ Chuyển Bài Tiếp Theo",
                color=discord.Color.green()
            )
            if finished_song:
                embed.add_field(name="✅ Vừa phát xong", value=finished_song['title'], inline=False)
            embed.add_field(name="🎵 Đang phát", value=f"**{song['title']}**", inline=False)
            embed.set_footer(text=f"Còn {len(self.music_queue) - 1} bài trong hàng đợi")
            await self._send_embed(guild_id, embed)

        except Exception as e:
            print(f"❌ Lỗi: {e}")
            # Bài lỗi -> bỏ khỏi queue rồi thử bài kế, tránh lặp vô hạn
            if self.music_queue and self.music_queue[0] is song:
                self.music_queue.popleft()

            embed = discord.Embed(
                title="❌ Lỗi Phát Nhạc",
                description=f"Không phát được **{song['title']}**, đang chuyển bài kế tiếp...",
                color=discord.Color.red()
            )
            await self._send_embed(guild_id, embed)

            await self.play_next_and_remove(vc)

    # --- Commands ---
    @commands.command()
    async def play(self, ctx, *, query: str):
        if ctx.voice_client is None:
            if ctx.author.voice:
                await ctx.author.voice.channel.connect()
            else:
                return await ctx.send("❌ Vào voice trước!")

        self.current_channel_id = ctx.voice_client.channel.id
        self.current_song_position = 0
        self.idle_since.pop(ctx.guild.id, None)
        self.last_text_channel[ctx.guild.id] = ctx.channel

        await ctx.send(f"🔍 Tìm kiếm: **{query}**...")
        audio = await self.get_audio_url_async(query)
        self.music_queue.append({'url': audio['webpage_url'], 'title': audio['title']})
        self.save_state(self.music_queue, ctx.voice_client.channel.id)
        if not ctx.voice_client.is_playing():
            await self.play_next(ctx)
        else:
            await ctx.send(f"➕ Đã thêm: **{audio['title']}**")

    @commands.command()
    async def stop(self, ctx):
        if ctx.voice_client:
            ctx.voice_client.stop()
            self.music_queue.clear()
            self.delete_state()
            await ctx.send("⏹️ Đã dừng!")

    @commands.command()
    async def pause(self, ctx):
        if ctx.voice_client and ctx.voice_client.is_playing():
            ctx.voice_client.pause()
            await ctx.send("⏸️ Đã tạm dừng!")

    @commands.command()
    async def resume(self, ctx):
        if ctx.voice_client and ctx.voice_client.is_paused():
            ctx.voice_client.resume()
            await ctx.send("▶️ Tiếp tục phát!")

    @commands.command()
    async def volume(self, ctx, level: int):
        if 0 <= level <= 100 and ctx.voice_client and ctx.voice_client.source:
            ctx.voice_client.source.volume = level / 100
            await ctx.send(f"🔊 Âm lượng: {level}%")

    @commands.command(name="skip")
    async def skip(self, ctx):
        voice_client = ctx.voice_client
        if voice_client and voice_client.is_playing():
            voice_client.stop()  # sẽ trigger callback after= -> play_next_and_remove
            await ctx.send("⏭️ Đã bỏ qua bài hát hiện tại!")
        else:
            await ctx.send("❌ Không có bài nào đang phát để mà skip bạn ơi!")

    @commands.command()
    async def queue(self, ctx):
        """Xem danh sách bài chờ"""
        if len(self.music_queue) == 0:
            await ctx.send("📭 Hàng đợi trống!")
            return

        embed = discord.Embed(
            title="🎵 Danh Sách Chờ",
            description=f"Có {len(self.music_queue)} bài chờ phát",
            color=discord.Color.blue()
        )

        for i, song in enumerate(self.music_queue, 1):
            embed.add_field(
                name=f"{i}. {song['title']}",
                value=song['url'],
                inline=False
            )

        await ctx.send(embed=embed)

    @commands.command(name="ketnoilai")
    async def ketnoilai(self, ctx):
        """Kết nối lại voice và phát lại danh sách chờ"""
        state = self.load_state()

        if not state["channel_id"]:
            await ctx.send("❌ Không có dữ liệu trước đó!")
            return

        if not state["queue"]:
            await ctx.send("❌ Danh sách chờ trống!")
            return

        try:
            if ctx.voice_client:
                await ctx.voice_client.disconnect()

            channel = self.bot.get_channel(state["channel_id"])
            if not channel:
                await ctx.send("❌ Không tìm thấy voice channel!")
                return

            vc = await channel.connect()
            self.music_queue.clear()
            for song in state["queue"]:
                self.music_queue.append(song)

            self.current_channel_id = channel.id
            self.current_song_position = state.get("current_position", 0)
            self.idle_since.pop(channel.guild.id, None)
            self.last_text_channel[channel.guild.id] = ctx.channel
            guild_id = channel.guild.id

            if self.music_queue:
                await ctx.send(f"✅ Đã kết nối lại! Đang phát {len(self.music_queue)} bài...")
                song = self.music_queue[0]
                try:
                    audio = await self.get_audio_url_async(song['url'])
                    seek_args = f"-ss {self.current_song_position}" if self.current_song_position > 0 else ""
                    before_options = f"{seek_args} -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5" if seek_args else "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"

                    source = discord.PCMVolumeTransformer(discord.FFmpegPCMAudio(
                        audio['url'], before_options=before_options
                    ))
                    # Lưu mốc thời gian bắt đầu + offset seek để tính lại position chính xác
                    self.play_start_time[guild_id] = (time.time(), self.current_song_position)
                    vc.play(source, after=lambda e: asyncio.run_coroutine_threadsafe(
                        self.play_next_and_remove(vc), self.bot.loop
                    ))
                except Exception as e:
                    await ctx.send(f"❌ Lỗi: {e}")

        except Exception as e:
            await ctx.send(f"❌ Lỗi: {e}")

    @commands.command()
    async def leave(self, ctx):
        if ctx.voice_client:
            self.music_queue.clear()
            self.delete_state()
            self.current_channel_id = None
            self.idle_since.pop(ctx.guild.id, None)
            await ctx.voice_client.disconnect()
            await ctx.send("✅ Đã rời!")

    @commands.command(name="helpmusic")
    async def helpmusic(self, ctx):
        """Hiển thị bảng hướng dẫn sử dụng bot nhạc"""
        embed = discord.Embed(
            title="🎵 Bảng Điều Khiển Âm Nhạc",
            description="Chào bạn, đây là danh sách các lệnh để bot phục vụ ông tận răng:",
            color=discord.Color.gold()
        )

        embed.add_field(
            name="📥 Phát Nhạc",
            value="`!play [tên/link]` - Tìm và phát bài hát ngay lập tức.",
            inline=False
        )
        embed.add_field(
            name="⏸️ Điều Khiển",
            value="`!pause` - Tạm dừng nhạc.\n`!resume` - Phát tiếp bài đang dừng.\n`!stop` - Dừng hẳn và xóa hết hàng đợi.\n`!skip` - Bỏ qua bài hiện tại.",
            inline=False
        )
        embed.add_field(
            name="🔊 Âm Thanh",
            value="`!volume [0-100]` - Chỉnh âm lượng bot (Ví dụ: `!volume 50`).",
            inline=False
        )
        embed.add_field(
            name="📜 Danh Sách & Kết Nối",
            value="`!queue` - Xem danh sách bài chờ.\n`!ketnoilai` - Kết nối lại và phát lại danh sách chờ.\n`!leave` - Đuổi bot ra khỏi kênh voice.",
            inline=False
        )

        embed.set_footer(text="Cần thêm tính năng gì cứ bảo tui nhé! 🎧")
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Music(bot))