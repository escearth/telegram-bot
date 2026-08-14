import time
from unittest.mock import MagicMock
import threading
import sys
import re

# Mock telebot module
class MockBot:
    def __init__(self):
        self.sleep_calls = 0
        self.messages_sent = 0

    def reply_to(self, message, text, **kwargs):
        pass

    def send_message(self, chat_id, text, **kwargs):
        self.messages_sent += 1

bot = MockBot()

# Mock objects
class MockMessage:
    def __init__(self, text):
        self.text = text
        self.chat = MagicMock()
        self.chat.id = 12345

message = MockMessage("Test broadcast message")
all_users = list(range(100)) # 100 users

def optimized_broadcast():
    broadcast_msg = message.text

    bot.reply_to(message, f"📢 Broadcasting to {len(all_users)} users in the background...")

    def broadcast_task(users, msg_text, chat_id):
        sent_count = 0
        failed_count = 0
        for target_user in users:
            for attempt in range(3):
                try:
                    bot.send_message(target_user, msg_text, parse_mode='HTML')
                    sent_count += 1
                    time.sleep(0.033)
                    break
                except Exception as e:
                    failed_count += 1
                    break

        bot.send_message(
            chat_id,
            f"✅ Broadcast complete!\n"
            f"📤 Sent: {sent_count}\n"
            f"❌ Failed: {failed_count}"
        )

    threading.Thread(target=broadcast_task, args=(all_users, broadcast_msg, message.chat.id), daemon=True).start()

start = time.time()
optimized_broadcast()
print(f"Optimized broadcast return time for 100 users took: {time.time() - start:.4f} seconds")
