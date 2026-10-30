"""Build a portable, printable guide from the versioned beginner Markdown."""
from html import escape
from pathlib import Path
import re

ROOT=Path(__file__).resolve().parents[1]
source=(ROOT/"docs"/"BEGINNER_GUIDE.md").read_text(encoding="utf-8")


def inline(text):
    text=escape(text)
    text=re.sub(r"`([^`]+)`",r"<code>\1</code>",text)
    text=re.sub(r"\*\*([^*]+)\*\*",r"<strong>\1</strong>",text)
    return re.sub(r"\[([^\]]+)\]\((https://[^)\s]+)\)",r'<a href="\2">\1</a>',text)


blocks=[]
toc=[]
paragraph=[]
list_kind=None
code=None


def flush():
    global list_kind
    if paragraph:
        blocks.append("<p>"+inline(" ".join(paragraph))+"</p>")
        paragraph.clear()
    if list_kind:
        blocks.append(f"</{list_kind}>")
        list_kind=None


for line in source.splitlines():
    if line.startswith("```"):
        if code is None:
            flush()
            code=[]
        else:
            blocks.append("<pre><code>"+escape("\n".join(code))+"</code></pre>")
            code=None
        continue
    if code is not None:
        code.append(line)
        continue
    if not line.strip():
        flush()
    elif line.startswith("# "):
        flush()
        blocks.append("<h1>"+inline(line[2:])+"</h1><p class='intro'>A practical guide for server owners, moderators, and members</p>")
    elif line.startswith("## "):
        flush()
        name=line[3:]
        anchor=re.sub(r"[^a-z0-9]+","-",name.lower()).strip("-")
        toc.append(f'<a href="#{anchor}">{escape(name)}</a>')
        blocks.append(f'<h2 id="{anchor}">{inline(name)}</h2>')
    elif re.match(r"^(\d+\. |- )",line):
        kind="ul" if line.startswith("- ") else "ol"
        if list_kind!=kind:
            flush()
            blocks.append(f"<{kind}>")
            list_kind=kind
        blocks.append("<li>"+inline(re.sub(r"^(\d+\. |- )","",line))+"</li>")
    else:
        paragraph.append(line)
flush()
html='''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Kuro Bot Beginner Guide</title>
<style>
:root{color-scheme:light;--ink:#212333;--accent:#6541a5}*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:#f5f3f9;color:var(--ink);font:17px/1.7 "Segoe UI",Arial,sans-serif}main{max-width:900px;margin:40px auto;background:white;padding:56px 64px;border-radius:18px;box-shadow:0 8px 40px #2720390c}h1{font-size:40px;line-height:1.15;letter-spacing:-1px;margin:0;color:#171922}h2{font-size:25px;line-height:1.3;margin:48px 0 14px;scroll-margin-top:24px}p{margin:0 0 18px}.intro{color:#66547e;margin:16px 0 30px}code{font-family:Consolas,monospace;font-size:.88em;background:#f0ecf7;border-radius:5px;padding:2px 5px;overflow-wrap:anywhere}pre{background:#f0ecf7;padding:20px;border-radius:10px;white-space:pre-wrap;line-height:1.5}pre code{padding:0;background:none}li{padding-left:4px;margin-bottom:12px}a{color:var(--accent)}nav{border:1px solid #e3dbed;background:#faf8fd;border-radius:12px;padding:24px;margin:30px 0}nav strong{display:block;margin-bottom:10px}nav a{display:block;padding:5px 0;text-decoration:none}nav a:hover{text-decoration:underline}a:focus-visible{outline:3px solid #9368d2;outline-offset:3px}button{font:inherit;border:0;background:var(--accent);color:white;padding:10px 18px;border-radius:8px;cursor:pointer;margin:12px 0}footer{margin-top:44px;font-size:14px;color:#625d6a}@media(max-width:650px){main{margin:0;padding:28px 22px;border-radius:0}h1{font-size:32px}body{font-size:16px}}@media print{body{background:white;font-size:11pt;line-height:1.5}main{box-shadow:none;margin:0;padding:0;max-width:none}nav,button{display:none}h1{font-size:26pt}h2{font-size:17pt;break-after:avoid;margin-top:24pt}p,li{orphans:3;widows:3}pre{break-inside:avoid}a{color:inherit}footer{font-size:9pt}@page{margin:20mm}}
</style></head><body><main>'''
first=blocks[:2]
rest=blocks[2:]
html+='\n'.join(first)+'<button type="button" onclick="window.print()">Print or save as PDF</button><nav aria-label="Contents"><strong>Find what you need</strong>'+"".join(toc)+"</nav>"+'\n'.join(rest)
html+='<footer>Kuro Bot guide · Keep this file with your bot documentation. Setup commands do not run from this page.</footer></main></body></html>'
target=ROOT/"docs"/"Kuro Beginner Guide.html"
target.write_text(html,encoding="utf-8",newline="\n")
print(f"Guide built: {len(toc)} sections")
