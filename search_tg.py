import re

with open("telegram_handler.py", "r") as f:
    lines = f.readlines()

for i, line in enumerate(lines):
    if "def markdown_to_telegram_html" in line:
        start_idx = i
        end_idx = min(len(lines), i + 35)
        print(f"Line {i+1}:\n" + "".join(lines[start_idx:end_idx]) + "\n")
