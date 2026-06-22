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
import stat


def ensure_deno_installed():
    """
    QUAN TRỌNG: Trên Render, Build Command và Start Command (runtime) chạy
    trên 2 compute KHÁC NHAU. Mọi thứ Build Command cài vào filesystem (như
    'curl ... | sh' cài deno) KHÔNG được giữ lại khi chuyển sang compute chạy
    Start Command — đây là lý do dù build log báo "Deno installed successfully"
    nhưng runtime vẫn không thấy deno (shutil.which trả None).
    Phải tự cài deno NGAY TRONG runtime (script này), giống cách main.py làm.
    """
    deno_home = os.path.expanduser("~/.deno/bin")
    deno_exe = os.path.join(deno_home, "deno")

    if deno_home not in os.environ.get("PATH", ""):
        os.environ["PATH"] = deno_home + os.pathsep + os.environ.get("PATH", "")

    if os.path.exists(deno_exe):
        print("[TEST] ✅ Deno đã có sẵn ở runtime, không cần cài lại.")
        return deno_exe

    print("[TEST] ⬇️ Deno chưa có ở runtime, đang tự cài...")
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
            print("[TEST] ✅ Đã cài Deno thành công ở runtime.")
            return deno_exe
        else:
            print("[TEST] ⚠️ Cài xong nhưng không thấy binary.")
            return None
    except Exception as e:
        print(f"[TEST] ❌ Lỗi khi tự cài Deno: {e}")
        return None

try:
    import imageio_ffmpeg
    FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
except ImportError:
    FFMPEG_PATH = "ffmpeg"

import yt_dlp


def find_deno_path():
    """
    Tìm đường dẫn deno thực tế. Gọi ensure_deno_installed() trước để đảm bảo
    deno có ở compute runtime hiện tại (vì build-time deno không tồn tại ở đây).
    """
    installed_path = ensure_deno_installed()
    if installed_path:
        print(f"[TEST] 🔎 Dùng deno vừa cài/tìm thấy: {installed_path}")
        return installed_path

    deno_path = shutil.which("deno")
    print(f"[TEST] 🔎 shutil.which('deno') = {deno_path}")

    fallback_path = os.path.expanduser("~/.deno/bin/deno")
    print(f"[TEST] 🔎 Fallback path tồn tại? {os.path.exists(fallback_path)} ({fallback_path})")

    chosen = deno_path or (fallback_path if os.path.exists(fallback_path) else None)
    print(f"[TEST] 🔎 Sẽ dùng deno path: {chosen}")

    if chosen:
        try:
            result = subprocess.run([chosen, "--version"], capture_output=True, text=True, timeout=10)
            print(f"[TEST] 🔎 Gọi thử '{chosen} --version' -> return code {result.returncode}")
            print(f"[TEST] 🔎 stdout: {result.stdout.strip()}")
            if result.stderr:
                print(f"[TEST] 🔎 stderr: {result.stderr.strip()}")
        except Exception as e:
            print(f"[TEST] ❌ Lỗi khi gọi thử deno: {e}")

    return chosen


def get_audio_url(query):
    print(f"[TEST] 🔍 Đang tìm: {query}")
    search_query = f"ytsearch:{query}" if not query.startswith("http") else query

    deno_path = find_deno_path()

    ydl_opts = {
        'format': 'worstaudio[abr>=48]/bestaudio[abr<=96]/bestaudio/best',
        'quiet': False,       # bật log đầy đủ của yt-dlp để thấy rõ warning/error
        'no_warnings': False,
        'default_search': 'auto',
        'noplaylist': True,
        'extract_flat': False,
        # FIX: truyền rõ path tuyệt đối của deno thay vì để {} trống, vì subprocess
        # mà yt-dlp tạo ra để gọi deno có thể không kế thừa đúng PATH hiện tại,
        # dẫn tới không tìm thấy deno dù 'deno --version' chạy ổn ở chỗ khác.
        'js_runtimes': {'deno': {'path': deno_path}} if deno_path else {'deno': {}},
        'remote_components': ['ejs:github'],
        'extractor_args': {'youtube': {'player_client': ['tv', 'android', 'web']}},
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
        print("Cách dùng: python test_music.py \"tên bài hát hoặc link\"")
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