import re
import ast

with open('bot.py', 'r') as f:
    content = f.read()

# See what variables are global
try:
    tree = ast.parse(content)
    globals_list = [node.id for node in tree.body if isinstance(node, ast.Assign) for target in node.targets if isinstance(target, ast.Name)]
    print(f"Top-level assignments: {globals_list[:20]}...")
except SyntaxError as e:
    print(f"Syntax error in bot.py: {e}")
