import re

with open('handle_callback_only.py', 'r') as f:
    lines = f.readlines()

def print_block_summary():
    current_block = []

    for i, line in enumerate(lines):
        if line.strip().startswith('if data ==') or line.strip().startswith('if data.startswith') or line.strip().startswith('if data in'):
            print(f"Line {i+1}: {line.strip()}")

print_block_summary()
