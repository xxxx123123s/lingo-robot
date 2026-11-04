import sys
from docx import Document

path = sys.argv[1]
out = sys.argv[2]
doc = Document(path)
lines = []
lines.append("=== PARAGRAPHS ===")
for i, p in enumerate(doc.paragraphs):
    t = p.text.strip()
    if t:
        lines.append(f"[{i}] ({p.style.name}) {t}")
lines.append("=== TABLES ===")
for ti, tb in enumerate(doc.tables):
    lines.append(f"--- table {ti}: {len(tb.rows)}x{len(tb.columns)} ---")
    for r in tb.rows:
        cells = [c.text.replace("\n", " / ").strip() for c in r.cells]
        lines.append(" | ".join(cells))
with open(out, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))
print("written", out, len(lines), "lines")
