import io

s = io.open(r"D:\智能桌宠\work\docx_dump.txt", encoding="utf-8").read()
i = s.find("01】")
seg = s[i - 4 : i + 4]
with io.open(r"D:\智能桌宠\work\codepoints.txt", "w", encoding="utf-8") as f:
    f.write(seg + "\n")
    f.write(" ".join(f"U+{ord(c):04X}" for c in seg) + "\n")
    j = s.find("01】", i + 1)
    seg2 = s[j - 4 : j + 4]
    f.write(seg2 + "\n")
    f.write(" ".join(f"U+{ord(c):04X}" for c in seg2) + "\n")
print("ok")
