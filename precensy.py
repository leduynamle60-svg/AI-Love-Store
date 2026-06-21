from pypresence import Presence
import time

client_id = "1515688908472451094" 
RPC = Presence(client_id)
RPC.connect()

start_time = time.time()

print("✅ Rich Presence đã chạy!")

while True:
    RPC.update(
        state="💖 Love Store | Sản phẩm thử nghiệm",
        details="Sốp bán đồ 💖",
        large_image="https://cdn.discordapp.com/emojis/1516028466347380786.webp?size=128", # Hoặc để trống nếu không có ảnh
        large_text="Love Store",
        start=start_time 
    )
    time.sleep(15) # Discord yêu cầu update tối đa 15s/lần