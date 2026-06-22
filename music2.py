"""
Script test ĐỘC LẬP, không cần Discord/main.py — chỉ test 2 việc:
1. yt-dlp tìm bài hát (dùng đúng ydl_opts giống music.py, kể cả cookies)
2. ffmpeg có decode được link audio đó không

Cách dùng trên Render (qua Shell) hoặc local:
    python test_music.py "tên bài hát hoặc link"

In log rõ từng bước để biết chính xác lỗi nằm ở yt-dlp hay ffmpeg.
"""
import sys
import os
import shutil
import subprocess
import time

try:
    import imageio_ffmpeg
    FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
except ImportError:
    FFMPEG_PATH = "ffmpeg"

import yt_dlp


def get_audio_url(query):
    print(f"[TEST] 🔍 Đang tìm: {query}")
    search_query = f"ytsearch:{query}" if not query.startswith("http") else query

    ydl_opts = {
        'format': 'worstaudio[abr>=48]/bestaudio[abr<=96]/bestaudio/best',
        'quiet': False,       # bật log đầy đủ của yt-dlp để thấy rõ warning/error
        'no_warnings': False,
        'default_search': 'auto',
        'noplaylist': True,
        'extract_flat': False,
        'remote_components': ['ejs:github'],
        'extractor_args': {'youtube': {'player_client': ['android', 'web']}},
    }

    cookies_source = os.getenv("COOKIES_FILE", "cookies.txt")
    print(f"[TEST] 📄 COOKIES_FILE env = {cookies_source}")
    print(f"[TEST] 📄 File cookies tồn tại? {os.path.exists(cookies_source)}")

    if os.path.exists(cookies_source):
        writable_cookies_path = "/tmp/yt_dlp_cookies.txt"
        try:
            if (not os.path.exists(writable_cookies_path)
                    or os.path.getmtime(cookies_source) > os.path.getmtime(writable_cookies_path)):
                shutil.copyfile(cookies_source, writable_cookies_path)
                print(f"[TEST] ✅ Đã copy cookies sang {writable_cookies_path}")
            ydl_opts['cookiefile'] = writable_cookies_path
            print(f"[TEST] 📦 cookiefile size: {os.path.getsize(writable_cookies_path)} bytes")
        except Exception as e:
            print(f"[TEST] ⚠️ Lỗi copy cookies: {e}")
    else:
        print("[TEST] ⚠️ Không có file cookies, chạy không có cookie.")

    t0 = time.time()
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(search_query, download=False)
        if 'entries' in info:
            entries = list(info['entries'])
            if not entries:
                raise ValueError(f"Không tìm thấy kết quả nào cho: {query}")
            info = entries[0]
        if not info:
            raise ValueError(f"Không tìm thấy kết quả nào cho: {query}")
        result = {
            'url': info.get('url') or info.get('webpage_url'),
            'title': info.get('title', 'Unknown'),
            'webpage_url': info.get('webpage_url'),
        }
    print(f"[TEST] ✅ Tìm xong trong {time.time() - t0:.1f}s")
    print(f"[TEST] 🎵 Title: {result['title']}")
    print(f"[TEST] 🔗 Audio URL (100 ký tự đầu): {result['url'][:100] if result['url'] else None}...")
    return result


def test_ffmpeg(audio_url, duration_seconds=10):
    """
    Chạy ffmpeg trực tiếp (không qua discord.py) để decode link audio trong
    `duration_seconds` giây, in toàn bộ output ffmpeg ra màn hình. Nếu ffmpeg
    chết sớm hơn duration_seconds, log ffmpeg sẽ cho biết lý do chính xác
    (network error, codec error, 403, v.v.) — không bị discord.py nuốt mất lỗi.
    """
    print(f"\n[TEST] 🎬 Đang test ffmpeg với audio URL trong tối đa {duration_seconds}s...")
    print(f"[TEST] 🛠️ FFMPEG_PATH = {FFMPEG_PATH}")

    cmd = [
        FFMPEG_PATH,
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",
        "-i", audio_url,
        "-t", str(duration_seconds),   # chỉ decode N giây đầu để test nhanh
        "-f", "null",
        "-",
    ]

    t0 = time.time()
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    elapsed = time.time() - t0

    print(f"[TEST] ⏱️ ffmpeg chạy trong {elapsed:.1f}s, return code = {result.returncode}")
    print("[TEST] ---- ffmpeg stderr (chứa log/lỗi) ----")
    print(result.stderr[-3000:])  # in 3000 ký tự cuối, đủ để thấy lỗi quan trọng
    print("[TEST] ---- hết ffmpeg stderr ----")

    if result.returncode == 0:
        print("[TEST] ✅ ffmpeg decode thành công, không có lỗi.")
    else:
        print(f"[TEST] ❌ ffmpeg lỗi, return code = {result.returncode}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Cách dùng: python test_music2.py \"tên bài hát hoặc link\"")
        sys.exit(1)

    query = " ".join(sys.argv[1:])

    try:
        audio = get_audio_url(query)
    except Exception as e:
        import traceback
        print(f"[TEST] ❌ Lỗi khi tìm bài hát: {type(e).__name__}: {e}")
        traceback.print_exc()
        sys.exit(1)

    if not audio.get('url'):
        print("[TEST] ❌ Không lấy được audio URL, dừng test ffmpeg.")
        sys.exit(1)

    test_ffmpeg(audio['url'], duration_seconds=10)