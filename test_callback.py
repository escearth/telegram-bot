import re

with open('bot.py', 'r') as f:
    content = f.read()

# Find the start of handle_callback
match = re.search(r'def handle_callback\(call\):', content)
if match:
    start_pos = match.start()

    # We'll just look for large blocks of if/elif statements
    callback_func = content[start_pos:]
    end_match = re.search(r'\n@bot.message_handler', callback_func)
    if end_match:
        callback_func = callback_func[:end_match.start()]

    with open('handle_callback_only.py', 'w') as f:
        f.write(callback_func)
    print(f"Extracted handle_callback to handle_callback_only.py. Length: {len(callback_func)}")
