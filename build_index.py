"""Rebuild posts/index.json from the front matter of posts/*.md."""
import json, os, glob

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
posts = []
for path in glob.glob(os.path.join(ROOT, "posts", "*.md")):
    meta = {}
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    if lines and lines[0].strip() == "---":
        for line in lines[1:]:
            if line.strip() == "---":
                break
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip().strip('"')
    slug = os.path.splitext(os.path.basename(path))[0]
    posts.append({"slug": slug, "title": meta.get("title", slug), "date": meta.get("date", slug[:10]),
                  "kind": meta.get("kind", "daily"), "summary": meta.get("summary", "")})
posts.sort(key=lambda p: (p["date"], p["slug"]), reverse=True)
with open(os.path.join(ROOT, "posts", "index.json"), "w", encoding="utf-8") as f:
    json.dump(posts, f, indent=1, ensure_ascii=False)
print(f"{len(posts)} posts indexed")
