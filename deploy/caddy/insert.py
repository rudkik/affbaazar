"""Вставляет или обновляет блок affbazaar в чужом Caddyfile между маркерами, остальное не трогает.

    insert.py <Caddyfile> <файл-со-сниппетом>
"""
import re
import sys

path, snippet_path = sys.argv[1], sys.argv[2]
snippet = open(snippet_path, encoding="utf-8").read().rstrip("\n") + "\n"
text = open(path, encoding="utf-8").read()
pattern = re.compile(r"# --- affbazaar begin ---.*?# --- affbazaar end ---\n?", re.S)
if pattern.search(text):
    text = pattern.sub(lambda _: snippet, text)
else:
    text = text.rstrip("\n") + "\n\n" + snippet
open(path, "w", encoding="utf-8").write(text)
