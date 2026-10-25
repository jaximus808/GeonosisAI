"""TDM-86: contact sheet of phone screenshots: python3 spikes/tdm-86/sheet.py out.png a.png b.png ..."""
import sys
from PIL import Image, ImageDraw

out, files = sys.argv[1], sys.argv[2:]
W = 300
ims = []
for f in files:
    im = Image.open(f).convert("RGB")
    im = im.resize((W, int(im.height * W / im.width)))
    ims.append((f.rsplit("/", 1)[-1][:-4], im))
H = max(im.height for _, im in ims) + 24
cols = min(len(ims), 4)
rows = (len(ims) + cols - 1) // cols
sheet = Image.new("RGB", (cols * (W + 10), rows * H), "white")
d = ImageDraw.Draw(sheet)
for i, (name, im) in enumerate(ims):
    x, y = (i % cols) * (W + 10), (i // cols) * H
    d.text((x + 4, y + 4), name, fill="black")
    sheet.paste(im, (x, y + 20))
sheet.save(out)
print(out)
