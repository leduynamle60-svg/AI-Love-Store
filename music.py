import discord
from discord.ext import commands
import yt_dlp
import asyncio
from collections import deque
import json
import os
import shutil
from discord.ext import tasks
import signal
import time

try:
    import imageio_ffmpeg
    FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
except ImportError:
    # Fallback: nếu chưa cài imageio-ffmpeg, dùng "ffmpeg" có sẵn trong PATH hệ thống (nếu có)
    FFMPEG_PATH = "ffmpeg"


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
                self.delete_state(guild_id)
                self.idle_since.pop(guild_id, None)
                print(f"✅ Bot đã tự rời khỏi {vc.channel.name} vì không phát nhạc quá 3 phút.")

    @check_idle.before_loop
    async def before_check_idle(self):
        await self.bot.wait_until_ready()

    def __init__(self, bot):
        self.bot = bot
        self.music_queue = deque()
        self.is_resuming = False
        self.current_channel_id = {}  # guild_id -> channel_id (trước là biến đơn, gây lẫn dữ liệu giữa nhiều guild)
        self.current_song_position = {}  # guild_id -> vị trí (giây) của bài đang phát
        self.play_start_time = {}
        self.idle_since = {}
        self.last_text_channel = {}
        self._audio_cache = {}
        self._CACHE_TTL = 300
        self._shutdown_saved = False

        # FIX: cờ đánh dấu bài hiện tại của mỗi guild đã từng vc.play() thành công hay chưa.
        # Dùng để phân biệt callback after= được gọi vì "bài phát xong bình thường"
        # với callback after= được gọi vì "ffmpeg chết ngay lúc mới play" (lỗi link/codec).
        # Nếu không có cờ này, play_next_and_remove sẽ pop bài vừa thêm dù nó chưa
        # từng phát được 1 giây nào -> đây là nguyên nhân lỗi "vừa play xong báo hết hàng chờ".
        self.currently_playing = {}
        # Đếm số lần retry liên tiếp khi 1 bài chết ngay lúc play, tránh loop vô hạn
        self._dead_song_retries = {}

        self.check_idle.start()
        asyncio.create_task(self.auto_resume())
        try:
            signal.signal(signal.SIGINT, self._handle_shutdown)
        except (ValueError, OSError) as e:
            # signal.signal chỉ hoạt động ở main thread; nếu cog bị load ở thread khác
            # (hoặc bot chạy theo cách khiến đây không phải main thread) thì bỏ qua,
            # không cho exception này làm sập toàn bộ việc load cog.
            print(f"[INIT] ⚠️ Không đăng ký được signal handler: {e}")

    def cog_unload(self):
        self.check_idle.cancel()

    def _calc_current_position(self, guild_id):
        info = self.play_start_time.get(guild_id)
        if not info:
            return self.current_song_position.get(guild_id, 0)
        start_ts, seek_offset = info
        elapsed = time.time() - start_ts
        return int(seek_offset + max(elapsed, 0))

    def _handle_shutdown(self, signum, frame):
        if self._shutdown_saved:
            return
        self._shutdown_saved = True

        # Lưu state cho TẤT CẢ guild đang có channel active, không chỉ 1 guild như bản cũ
        # (bản cũ dùng biến đơn current_channel_id nên chỉ lưu được guild gần nhất, các guild
        # khác bị ghi đè mất dữ liệu khi bot chạy nhiều server cùng lúc).
        if self.current_channel_id:
            for guild_id, channel_id in list(self.current_channel_id.items()):
                position = self._calc_current_position(guild_id)
                self.current_song_position[guild_id] = position

                print(f"\n[SHUTDOWN] Lưu state guild {guild_id} trước khi tắt...")
                state = {
                    "queue": list(self.music_queue),
                    "channel_id": channel_id,
                    "current_position": position
                }
                try:
                    state_file = self._state_file_for_guild(guild_id)
                    with open(state_file, "w", encoding='utf-8') as f:
                        json.dump(state, f, indent=4)
                        f.flush()
                        os.fsync(f.fileno())
                    print(f"[SHUTDOWN] ✅ Guild {guild_id}: đã lưu {len(self.music_queue)} bài, position: {position}s")
                except Exception as e:
                    print(f"[SHUTDOWN] ❌ Lỗi save guild {guild_id}: {e}")
        asyncio.create_task(self.bot.close())

    def _state_file_for_guild(self, guild_id):
        """Mỗi guild có file state riêng, tránh nhiều guild ghi đè state của nhau."""
        return f"bot_state_{guild_id}.json"

    def _guild_id_from_channel(self, channel_id):
        channel = self.bot.get_channel(channel_id)
        return channel.guild.id if channel else None

    async def auto_resume(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(3)

        if self.is_resuming:
            return

        self.is_resuming = True

        # Quét tất cả file state đã lưu cho từng guild (bot_state_<guild_id>.json)
        # Bản cũ chỉ có 1 file chung -> chỉ resume được 1 guild. Giờ resume hết các guild có state.
        try:
            state_files = [f for f in os.listdir('.') if f.startswith('bot_state_') and f.endswith('.json')]
        except OSError as e:
            print(f"[AUTO-RESUME] ❌ Không đọc được thư mục hiện tại: {e}")
            self.is_resuming = False
            return

        if not state_files:
            print("[AUTO-RESUME] Không có state nào để khôi phục")
            self.is_resuming = False
            return

        for state_filename in state_files:
            try:
                guild_id_str = state_filename[len('bot_state_'):-len('.json')]
                guild_id = int(guild_id_str)
            except ValueError:
                continue

            await self._resume_one_guild(guild_id)

        self.is_resuming = False

    async def _resume_one_guild(self, guild_id):
        """Resume voice + queue cho 1 guild cụ thể, dùng file state riêng của guild đó."""
        state = self.load_state(guild_id)
        print(f"[AUTO-RESUME] Guild {guild_id}: channel_id={state['channel_id']}, queue={len(state['queue'])} bài, position={state.get('current_position', 0)}s")

        if not state["channel_id"] or not state["queue"]:
            print(f"[AUTO-RESUME] Guild {guild_id}: state không hợp lệ, bỏ qua")
            return

        max_retries = 3
        retry_count = 0

        while retry_count < max_retries:
            try:
                channel = self.bot.get_channel(state["channel_id"])
                if not channel:
                    print(f"[AUTO-RESUME] ❌ Channel {state['channel_id']} không tồn tại")
                    self.delete_state(guild_id)
                    return

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

                self.current_channel_id[guild_id] = channel.id
                self.current_song_position[guild_id] = state.get("current_position", 0)

                print(f"[AUTO-RESUME] ✅ Guild {guild_id}: khôi phục {len(self.music_queue)} bài")

                if self.music_queue:
                    song = self.music_queue[0]
                    position = self.current_song_position[guild_id]
                    print(f"[AUTO-RESUME] 🎵 Phát: {song['title']} (từ {position}s)")

                    try:
                        audio = await self.get_audio_url_async(song['url'])
                        seek_args = f"-ss {position}" if position > 0 else ""
                        before_options, ffmpeg_options = self.get_ffmpeg_options(seek_args)

                        source = discord.PCMVolumeTransformer(discord.FFmpegPCMAudio(
                            audio['url'],
                            executable=FFMPEG_PATH,
                            before_options=before_options,
                            options=ffmpeg_options
                        ))
                        self.play_start_time[guild_id] = (time.time(), position)
                        # FIX: đánh dấu chưa chắc chắn phát được, set True ngay sau khi vc.play() không raise
                        self.currently_playing[guild_id] = False
                        vc.play(source, after=lambda e: asyncio.run_coroutine_threadsafe(
                            self.play_next_and_remove(vc), self.bot.loop
                        ))
                        self.currently_playing[guild_id] = True
                        print(f"[AUTO-RESUME] ✅ Guild {guild_id}: resume thành công!")
                        return
                    except Exception as e:
                        print(f"[AUTO-RESUME] ❌ Guild {guild_id}: lỗi phát: {e}")
                        retry_count += 1
                        await asyncio.sleep(2)

            except Exception as e:
                print(f"[AUTO-RESUME] ❌ Guild {guild_id}: lỗi: {e}")
                retry_count += 1
                await asyncio.sleep(2)

        print(f"[AUTO-RESUME] ❌ Guild {guild_id}: thất bại")
        self.delete_state(guild_id)

    def save_state(self, queue_data, channel_id, guild_id):
        state_file = self._state_file_for_guild(guild_id)
        if os.path.exists(state_file):
            try:
                os.rename(state_file, state_file + ".bak")
            except OSError:
                pass

        state = {"queue": list(queue_data), "channel_id": channel_id, "current_position": self.current_song_position.get(guild_id, 0)}
        try:
            with open(state_file, "w", encoding='utf-8') as f:
                json.dump(state, f, indent=4)
                f.flush()
                os.fsync(f.fileno())
        except Exception as e:
            print(f"❌ Lỗi save state (guild {guild_id}): {e}")
            if os.path.exists(state_file + ".bak"):
                os.rename(state_file + ".bak", state_file)

    def load_state(self, guild_id):
        state_file = self._state_file_for_guild(guild_id)
        if os.path.exists(state_file):
            try:
                with open(state_file, "r", encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                return {"queue": [], "channel_id": None, "current_position": 0}
        return {"queue": [], "channel_id": None, "current_position": 0}

    def delete_state(self, guild_id):
        state_file = self._state_file_for_guild(guild_id)
        if os.path.exists(state_file):
            os.remove(state_file)

    def get_audio_url(self, query):
        cached = self._audio_cache.get(query)
        if cached and (time.time() - cached['ts']) < self._CACHE_TTL:
            return cached['data']

        search_query = f"ytsearch:{query}" if not query.startswith("http") else query
        ydl_opts = {
            # FIX: hạ xuống mức audio thấp hơn để giảm băng thông cần thiết khi mạng yếu.
            # 'bestaudio' có thể chọn bitrate cao (128-160kbps opus) gây giật nếu mạng
            # không đủ ổn định để stream liên tục. Ưu tiên định dạng audio nhẹ hơn
            # (~70-96kbps) nhưng vẫn đủ nghe ổn qua loa Discord.
            'format': 'worstaudio[abr>=48]/bestaudio[abr<=96]/bestaudio/best',
            'quiet': True,
            'no_warnings': True,
            'default_search': 'auto',
            'noplaylist': True,
            'extract_flat': False,
            # FIX: cho phép yt-dlp tự tải script giải JS challenge (signature) của YouTube.
            # Nếu không có, một số format audio bị thiếu hoặc link bị lỗi -> ffmpeg chết khi play.
            'remote_components': ['ejs:github'],
            # FIX: thêm 'web' làm fallback. Client 'android' đơn lẻ có lúc bị YouTube
            # trả về URL audio bị giới hạn/throttle hoặc lỗi 403, khiến ffmpeg chết ngay
            # khi vừa play() -> đây là một trong các nguyên nhân gây mất bài / báo "hết hàng chờ"
            # ngay sau khi tìm được nhạc.
            'extractor_args': {'youtube': {'player_client': ['android', 'web']}},
        }

        # FIX: trên server cloud (Render, v.v.) YouTube hay chặn IP datacenter với
        # lỗi "Sign in to confirm you're not a bot". Nếu có file cookies.txt (export
        # từ browser đã đăng nhập YouTube), dùng nó để vượt qua chặn này.
        # Đường dẫn lấy từ biến môi trường COOKIES_FILE để linh hoạt — nếu không set
        # hoặc file không tồn tại thì bỏ qua, không gây lỗi.
        #
        # FIX QUAN TRỌNG: yt-dlp cần GHI lại file cookies sau khi dùng (để lưu cookie
        # mới nhận từ session). Render's Secret Files (/etc/secrets/) là READ-ONLY,
        # nên phải copy file cookie ra một nơi ghi được (/tmp) trước khi đưa cho yt-dlp,
        # nếu không sẽ lỗi "[Errno 30] Read-only file system".
        cookies_source = os.getenv("COOKIES_FILE", "cookies.txt")
        if os.path.exists(cookies_source):
            writable_cookies_path = "/tmp/yt_dlp_cookies.txt"
            try:
                # Chỉ copy nếu chưa có bản /tmp, hoặc bản gốc mới hơn (tránh copy lại
                # mỗi lần gọi, vì hàm này có thể được gọi rất nhiều lần).
                if (not os.path.exists(writable_cookies_path)
                        or os.path.getmtime(cookies_source) > os.path.getmtime(writable_cookies_path)):
                    shutil.copyfile(cookies_source, writable_cookies_path)
                ydl_opts['cookiefile'] = writable_cookies_path
            except Exception as e:
                print(f"⚠️ Lỗi copy cookies sang /tmp: {e}. Bỏ qua cookies lần này.")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search_query, download=False)
            if 'entries' in info:
                # FIX: 'entries' có thể là list rỗng nếu search không ra kết quả nào
                # (từ khóa lạ, gõ sai, hoặc YouTube tạm thời chặn/giới hạn search từ IP này).
                # Trước đây code lấy entries[0] thẳng -> IndexError nếu rỗng.
                entries = list(info['entries'])
                if not entries:
                    raise ValueError(f"Không tìm thấy kết quả nào cho: {query}")
                info = entries[0]
            if not info:
                raise ValueError(f"Không tìm thấy kết quả nào cho: {query}")
            result = {'url': info.get('url') or info.get('webpage_url'), 'title': info.get('title', 'Unknown'), 'webpage_url': info.get('webpage_url')}

        self._audio_cache[query] = {'data': result, 'ts': time.time()}
        return result

    async def get_audio_url_async(self, query):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.get_audio_url, query)

    def get_ffmpeg_options(self, seek_args=""):
        """
        Trả về (before_options, options) cho FFmpegPCMAudio.
        - reconnect*: tự kết nối lại nếu mất kết nối stream giữa chừng.

        LƯU Ý: KHÔNG dùng '-vn' ở 'options' (after_options) — discord.py cần ffmpeg xuất
        ra đúng định dạng PCM (s16le, 48kHz, stereo).

        FIX: đã bỏ '-bufsize 5000k' khỏi before_options. Test tay bằng ffmpeg trực tiếp
        cho thấy link audio + codec hoàn toàn ổn (speed=354x, không lỗi), nhưng khi
        chạy qua discord.py/FFmpegPCMAudio thì process chết trong <1s. Nghi ngờ '-bufsize'
        đặt ở before_options (trước input) là flag thường dùng cho output/encoding,
        đặt sai vị trí có thể khiến ffmpeg nhận args không như mong đợi và xử lý sai
        khi chạy qua subprocess của discord.py (khác với khi gõ tay ở terminal).
        """
        reconnect = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"

        before_options = f"{seek_args} {reconnect}".strip() if seek_args else reconnect
        options = None  # để discord.py tự dùng default options đúng định dạng PCM
        return before_options, options

    async def _make_source(self, song):
        """Helper dùng chung: lấy audio URL + tạo FFmpegPCMAudio source cho 1 bài hát."""
        audio = await self.get_audio_url_async(song['url'])
        before_options, ffmpeg_options = self.get_ffmpeg_options()
        source = discord.PCMVolumeTransformer(discord.FFmpegPCMAudio(
            audio['url'], executable=FFMPEG_PATH,
            before_options=before_options,
            options=ffmpeg_options
        ))
        return source

    # Ngưỡng thời gian (giây): nếu callback after= được gọi sớm hơn ngưỡng này
    # tính từ lúc vc.play() được gọi, coi như ffmpeg "chết sớm" chứ không phải
    # "phát xong bình thường" (vì hầu như không có bài nhạc nào ngắn hơn vài giây).
    _DEAD_SONG_THRESHOLD_SECONDS = 3.0

    async def play_next(self, ctx):
        if not ctx.voice_client or not ctx.voice_client.is_connected():
            print("[PLAY_NEXT] ❌ Bot không còn trong voice, hủy.")
            return

        if not self.music_queue:
            return

        song = self.music_queue[0]
        guild_id = ctx.voice_client.channel.guild.id
        self.current_channel_id[guild_id] = ctx.voice_client.channel.id
        self.current_song_position[guild_id] = 0
        self.save_state(self.music_queue, ctx.voice_client.channel.id, guild_id)
        try:
            source = await self._make_source(song)

            # FIX: ghi mốc thời gian NGAY TRƯỚC khi gọi vc.play(). vc.play() không
            # raise dù ffmpeg chết ngay sau đó (process die bất đồng bộ), nên không thể
            # dựa vào "play() không lỗi" để coi là phát thành công. Thay vào đó, callback
            # after= sẽ tự so sánh thời gian thực tế đã trôi qua với ngưỡng để biết đây là
            # "chết sớm" hay "phát xong thật".
            self.play_start_time[guild_id] = (time.time(), 0)
            self.currently_playing[guild_id] = False

            ctx.voice_client.play(source, after=lambda e: asyncio.run_coroutine_threadsafe(
                self.play_next_and_remove(ctx.voice_client), self.bot.loop
            ))

            # vc.play() không raise -> ffmpeg process đã spawn thành công, coi như "đang phát"
            self.currently_playing[guild_id] = True
            self._dead_song_retries[guild_id] = 0

            await ctx.send(f"🎵 Đang phát: **{song['title']}**")
        except Exception as e:
            import traceback
            print(f"[PLAY_NEXT] ❌ Lỗi: {e}")
            traceback.print_exc()
            await ctx.send(f"❌ Lỗi: {e}")
            # So sánh theo nội dung bài hát (url + title) thay vì `is` (so sánh identity object,
            # không an toàn vì có thể có 2 dict khác object nhưng cùng nội dung bài hát).
            if self.music_queue and self.music_queue[0].get('url') == song.get('url'):
                self.music_queue.popleft()
            if self.music_queue:
                await self.play_next(ctx)
            else:
                self.delete_state(guild_id)

    async def _send_embed(self, guild_id, embed):
        channel = self.last_text_channel.get(guild_id)
        if channel:
            try:
                await channel.send(embed=embed)
            except Exception as e:
                print(f"[EMBED] ❌ Không gửi được embed: {e}")

    async def _retry_dead_song(self, vc, guild_id):
        """
        FIX (race condition chính): được gọi khi play_next_and_remove() phát hiện
        bài hiện tại bị ffmpeg "chết" ngay lúc mới play (chưa từng phát được giây nào),
        thay vì pop nó ra khỏi queue như callback after= thông thường vẫn làm.

        Thử lại tối đa 2 lần cho bài này; nếu vẫn lỗi liên tục mới chịu bỏ qua nó.
        """
        retry_count = self._dead_song_retries.get(guild_id, 0)
        print(f"[RETRY] ⚠️ Guild {guild_id}: bài chưa kịp phát thật (lần {retry_count + 1}), không xóa khỏi queue vội.")

        if retry_count >= 2:
            self._dead_song_retries[guild_id] = 0
            if self.music_queue:
                bad_song = self.music_queue.popleft()
                print(f"[RETRY] ❌ Guild {guild_id}: bỏ qua bài lỗi liên tục: {bad_song['title']}")
                embed = discord.Embed(
                    title="❌ Không Phát Được Bài Này",
                    description=f"**{bad_song['title']}** bị lỗi liên tục (có thể do link/định dạng), đang chuyển bài kế tiếp...",
                    color=discord.Color.red()
                )
                await self._send_embed(guild_id, embed)
        else:
            self._dead_song_retries[guild_id] = retry_count + 1
            await asyncio.sleep(1)

        if not self.music_queue:
            self.current_song_position[guild_id] = 0
            self.delete_state(guild_id)
            embed = discord.Embed(
                title="📭 Hết Nhạc Rồi!",
                description="Hàng đợi trống, dùng `!play [tên/link]` để thêm bài mới nhé!",
                color=discord.Color.dark_grey()
            )
            await self._send_embed(guild_id, embed)
            return

        song = self.music_queue[0]
        self.current_song_position[guild_id] = 0
        self.save_state(self.music_queue, vc.channel.id, guild_id)

        try:
            source = await self._make_source(song)
            self.play_start_time[guild_id] = (time.time(), 0)
            vc.play(source, after=lambda e: asyncio.run_coroutine_threadsafe(
                self.play_next_and_remove(vc), self.bot.loop
            ))
        except Exception as e:
            print(f"[RETRY] ❌ Guild {guild_id}: lỗi khi thử lại: {e}")
            # Gọi lại chính callback chính để nó tiếp tục xử lý theo logic dead-song
            await self.play_next_and_remove(vc)

    async def play_next_and_remove(self, vc):
        if not vc or not vc.is_connected():
            print("[PLAY_NEXT] ❌ Bot không còn trong voice, dừng auto-play.")
            return

        guild_id = vc.guild.id

        # FIX CHÍNH: vc.play() KHÔNG raise dù ffmpeg chết ngay sau khi spawn (process
        # die là sự kiện bất đồng bộ, callback after= mới là nơi báo lỗi). Vì vậy
        # không thể dùng "play() không lỗi" để coi là phát thành công.
        # Cách đáng tin cậy hơn: so sánh thời gian thực tế đã trôi qua từ lúc gọi
        # vc.play() tới lúc callback after= này được gọi. Nếu quá ngắn (dưới
        # _DEAD_SONG_THRESHOLD_SECONDS giây) thì coi là "chết sớm", không phải
        # "phát xong bình thường" -> KHÔNG pop bài khỏi queue, mà thử lại.
        # Đây chính là nguyên nhân lỗi "tìm được nhạc nhưng báo hết hàng chờ ngay,
        # không nghe được giây nào".
        start_info = self.play_start_time.get(guild_id)
        elapsed = (time.time() - start_info[0]) if start_info else 0

        if elapsed < self._DEAD_SONG_THRESHOLD_SECONDS:
            print(f"[PLAY_NEXT_AND_REMOVE] ⚠️ Guild {guild_id}: bài chỉ chạy được {elapsed:.1f}s -> coi là chết sớm.")
            await self._retry_dead_song(vc, guild_id)
            return

        # --- Từ đây là trường hợp bình thường: bài TRƯỚC đã thực sự phát xong ---
        self._dead_song_retries[guild_id] = 0

        finished_song = None
        if self.music_queue:
            finished_song = self.music_queue.popleft()
            print(f"[QUEUE] 🗑️ Đã xóa khỏi hàng đợi: {finished_song['title']}")

        if not self.music_queue:
            self.current_song_position[guild_id] = 0
            self.delete_state(guild_id)
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
        self.current_song_position[guild_id] = 0
        self.play_start_time[guild_id] = (time.time(), 0)
        self.save_state(self.music_queue, vc.channel.id, guild_id)

        try:
            source = await self._make_source(song)
            self.currently_playing[guild_id] = False
            vc.play(source, after=lambda e: asyncio.run_coroutine_threadsafe(
                self.play_next_and_remove(vc), self.bot.loop
            ))
            self.currently_playing[guild_id] = True

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
            import traceback
            print(f"[PLAY_NEXT_AND_REMOVE] ❌ Lỗi: {e}")
            traceback.print_exc()
            if self.music_queue and self.music_queue[0].get('url') == song.get('url'):
                self.music_queue.popleft()

            embed = discord.Embed(
                title="❌ Lỗi Phát Nhạc",
                description=f"Không phát được **{song['title']}**, đang chuyển bài kế tiếp...",
                color=discord.Color.red()
            )
            await self._send_embed(guild_id, embed)

            await self.play_next_and_remove(vc)

    @commands.command()
    async def play(self, ctx, *, query: str):
        if ctx.voice_client is None:
            if ctx.author.voice:
                try:
                    await ctx.author.voice.channel.connect()
                except Exception as e:
                    # Trước đây lỗi này bị "nuốt" mất, bot im lặng không phản hồi gì.
                    # Giờ báo rõ ra Discord để biết ngay nguyên nhân (thiếu ffmpeg/PyNaCl, timeout UDP...)
                    print(f"[PLAY] ❌ Lỗi connect voice: {e}")
                    return await ctx.send(f"❌ Không kết nối được vào voice channel: {e}")
            else:
                return await ctx.send("❌ Vào voice trước!")

        self.current_channel_id[ctx.guild.id] = ctx.voice_client.channel.id
        self.current_song_position[ctx.guild.id] = 0
        self.idle_since.pop(ctx.guild.id, None)
        self.last_text_channel[ctx.guild.id] = ctx.channel

        await ctx.send(f"🔍 Tìm kiếm: **{query}**...")
        try:
            audio = await self.get_audio_url_async(query)
        except Exception as e:
            import traceback
            print(f"[PLAY] ❌ Lỗi tìm bài: {e}")
            traceback.print_exc()
            return await ctx.send(f"❌ Không tìm được bài hát: {e}")

        self.music_queue.append({'url': audio['webpage_url'], 'title': audio['title']})

        try:
            self.save_state(self.music_queue, ctx.voice_client.channel.id, ctx.guild.id)
        except Exception as e:
            print(f"[PLAY] ❌ Lỗi lưu state: {e}")
            # Không return ở đây — lưu state lỗi không nên chặn việc phát nhạc

        if not ctx.voice_client.is_playing():
            await self.play_next(ctx)
        else:
            await ctx.send(f"➕ Đã thêm: **{audio['title']}**")

    @commands.command()
    async def stop(self, ctx):
        if ctx.voice_client:
            ctx.voice_client.stop()
            self.music_queue.clear()
            self.delete_state(ctx.guild.id)
            self.currently_playing.pop(ctx.guild.id, None)
            self._dead_song_retries.pop(ctx.guild.id, None)
            await ctx.send("⏹️ Đã dừng!")
        else:
            await ctx.send("❌ Bot không ở trong voice channel nào!")

    @commands.command()
    async def pause(self, ctx):
        if ctx.voice_client and ctx.voice_client.is_playing():
            ctx.voice_client.pause()
            await ctx.send("⏸️ Đã tạm dừng!")
        else:
            await ctx.send("❌ Không có bài nào đang phát để tạm dừng!")

    @commands.command()
    async def resume(self, ctx):
        if ctx.voice_client and ctx.voice_client.is_paused():
            ctx.voice_client.resume()
            await ctx.send("▶️ Tiếp tục phát!")
        else:
            await ctx.send("❌ Không có bài nào đang tạm dừng để tiếp tục!")

    @commands.command()
    async def volume(self, ctx, level: int):
        # FIX: trước đây nếu level ngoài 0-100 hoặc không có gì đang phát thì bot im lặng,
        # không phản hồi gì cả -> dễ gây hiểu lầm là bot bị treo.
        if not ctx.voice_client or not ctx.voice_client.source:
            await ctx.send("❌ Không có bài nào đang phát để chỉnh âm lượng!")
            return
        if not (0 <= level <= 100):
            await ctx.send("❌ Âm lượng phải trong khoảng 0-100!")
            return
        ctx.voice_client.source.volume = level / 100
        await ctx.send(f"🔊 Âm lượng: {level}%")

    @commands.command(name="skip")
    async def skip(self, ctx):
        voice_client = ctx.voice_client
        if voice_client and voice_client.is_playing():
            voice_client.stop()
            await ctx.send("⏭️ Đã bỏ qua bài hát hiện tại!")
        else:
            await ctx.send("❌ Không có bài nào đang phát để mà skip bạn ơi!")

    @commands.command()
    async def queue(self, ctx):
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
        state = self.load_state(ctx.guild.id)

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

            guild_id = channel.guild.id
            self.current_channel_id[guild_id] = channel.id
            self.current_song_position[guild_id] = state.get("current_position", 0)
            self.idle_since.pop(guild_id, None)
            self.last_text_channel[guild_id] = ctx.channel

            if self.music_queue:
                await ctx.send(f"✅ Đã kết nối lại! Đang phát {len(self.music_queue)} bài...")
                song = self.music_queue[0]
                position = self.current_song_position[guild_id]
                try:
                    audio = await self.get_audio_url_async(song['url'])
                    seek_args = f"-ss {position}" if position > 0 else ""
                    before_options, ffmpeg_options = self.get_ffmpeg_options(seek_args)

                    source = discord.PCMVolumeTransformer(discord.FFmpegPCMAudio(
                        audio['url'], executable=FFMPEG_PATH, before_options=before_options, options=ffmpeg_options
                    ))
                    self.play_start_time[guild_id] = (time.time(), position)
                    self.currently_playing[guild_id] = False
                    vc.play(source, after=lambda e: asyncio.run_coroutine_threadsafe(
                        self.play_next_and_remove(vc), self.bot.loop
                    ))
                    self.currently_playing[guild_id] = True
                except Exception as e:
                    import traceback
                    print(f"[KETNOILAI] ❌ Lỗi phát: {e}")
                    traceback.print_exc()
                    await ctx.send(f"❌ Lỗi: {e}")

        except Exception as e:
            import traceback
            print(f"[KETNOILAI] ❌ Lỗi: {e}")
            traceback.print_exc()
            await ctx.send(f"❌ Lỗi: {e}")

    @commands.command()
    async def leave(self, ctx):
        if ctx.voice_client:
            self.music_queue.clear()
            self.delete_state(ctx.guild.id)
            self.current_channel_id.pop(ctx.guild.id, None)
            self.current_song_position.pop(ctx.guild.id, None)
            self.idle_since.pop(ctx.guild.id, None)
            self.currently_playing.pop(ctx.guild.id, None)
            self._dead_song_retries.pop(ctx.guild.id, None)
            await ctx.voice_client.disconnect()
            await ctx.send("✅ Đã rời!")
        else:
            await ctx.send("❌ Bot không ở trong voice channel nào!")

    @commands.command(name="helpmusic")
    async def helpmusic(self, ctx):
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