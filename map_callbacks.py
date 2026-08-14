import re

with open('handle_callback_only.py', 'r') as f:
    lines = f.readlines()

for i, line in enumerate(lines):
    line = line.strip()
    if line.startswith('if data ==') or line.startswith('if data.startswith') or line.startswith('if data in'):
        print(f"{i+1}: {line}")
