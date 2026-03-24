#!/usr/bin/env python3
"""
書影獲得: Image が空の Book ノートに書影URLを記入する。
承認不要・一括実行。プロセス: 書籍特定 → URL候補取得 → HEADで検証(Content-Type画像 & Content-Length>=100) → 記入。
"""
import os
import re
import json
import time
import urllib.request
import urllib.parse
from datetime import datetime
from pathlib import Path


MIN_CONTENT_LENGTH = 100
IMAGE_TYPES = ("image/jpeg", "image/png", "image/gif", "image/webp")
REQUEST_DELAY = 0.5       # サーバー負荷軽減
GOOGLE_DELAY = 2.0        # Google Books は連続呼び出しで429になりやすい
RAKUTEN_DELAY = 1.0       # 楽天APIは1秒以上の間隔が推奨
RETRY_WAIT = 5.0          # 429 発生時の待機秒数
IGNORE_EXPIRE_DAYS = 30   # ignore list の有効期限（日数）


def resolve_vault_root(vault_arg):
    """ボルトルート: --vault-root が優先、なければ環境変数 OBSIDIAN_VAULT_ROOT。"""
    if vault_arg:
        return Path(vault_arg).expanduser().resolve()
    env = os.environ.get("OBSIDIAN_VAULT_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    raise SystemExit(
        "エラー: Obsidian ボルトのルートが未設定です。\n"
        "  環境変数 OBSIDIAN_VAULT_ROOT を設定するか、--vault-root を指定してください。\n"
        "  例: export OBSIDIAN_VAULT_ROOT=\"$HOME/Documents/MyVault\""
    )


def book_paths(vault: Path):
    """Book ノートディレクトリと ignore ファイルのパス。"""
    sub = os.environ.get("BOOK_NOTES_SUBPATH", "10_Zettelkasten/LiteratureNote/Book")
    book_dir = vault / sub
    ignore_file = book_dir / ".book_cover_ignore.json"
    return book_dir, ignore_file


def load_ignore_list(ignore_file: Path):
    """30日以内に失敗したタイトルのセットを読み込む。旧形式(list)にも対応。"""
    if not ignore_file.exists():
        return set()
    try:
        with open(ignore_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        cutoff = datetime.now().timestamp() - IGNORE_EXPIRE_DAYS * 86400
        if isinstance(data, list):
            return set()
        return {k for k, v in data.items() if isinstance(v, (int, float)) and v > cutoff}
    except Exception:
        return set()


def save_ignore_list(ignore_file: Path, new_failures: dict):
    """失敗タイトルのタイムスタンプ辞書をマージして保存。"""
    existing = {}
    if ignore_file.exists():
        try:
            with open(ignore_file, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                existing = raw
        except Exception:
            pass
    existing.update(new_failures)
    try:
        ignore_file.parent.mkdir(parents=True, exist_ok=True)
        with open(ignore_file, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def get_frontmatter_and_body(path):
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    if not content.startswith("---"):
        return None, content
    parts = content.split("---", 2)
    if len(parts) < 3:
        return None, content
    return parts[1].strip(), parts[2]


def get_author(frontmatter):
    """Author を取得。YAMLリスト形式（Author:\n  - 名前）にも対応。余分なスペースを正規化。"""
    m = re.search(r"^Author:\s*(.+)$", frontmatter, re.MULTILINE)
    if m:
        s = m.group(1).strip()
        if s and not s.startswith("-"):
            return re.sub(r"\s+", " ", s).strip()
    m2 = re.search(r"^Author:\s*\n\s+-\s*([^\n]+)", frontmatter, re.MULTILINE)
    if m2:
        return re.sub(r"\s+", " ", m2.group(1)).strip()
    return ""


def has_empty_image(frontmatter):
    return bool(re.search(r"^Image:\s*$", frontmatter, re.MULTILINE))


def set_image_in_frontmatter(frontmatter, url):
    return re.sub(r"^Image:\s*$", f"Image: {url}", frontmatter, count=1, flags=re.MULTILINE)


def _isbn10_to_isbn13(isbn10):
    """10桁ISBNを13桁に変換（978接頭辞＋チェックディジット計算）。"""
    if len(isbn10) != 10:
        return None
    prefix = "978" + isbn10[:9]
    total = sum((1 if i % 2 == 0 else 3) * int(c) for i, c in enumerate(prefix))
    check = (10 - total % 10) % 10
    return prefix + str(check)


def _isbn13_to_isbn10(isbn13):
    """13桁ISBNを10桁に変換（チェックディジット再計算）。978以外は None。"""
    if len(isbn13) != 13 or not isbn13.startswith("978"):
        return None
    base = isbn13[3:12]
    if not base.isdigit():
        return None
    total = sum((10 - i) * int(d) for i, d in enumerate(base))
    check = (11 - total % 11) % 11
    return base + ("X" if check == 10 else str(check))


def _fetch_with_retry(url, headers=None, timeout=15):
    """GETリクエスト。429の場合は RETRY_WAIT 秒待ってリトライする。"""
    if headers is None:
        headers = {"User-Agent": "BookCoverBot/1.0"}
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt == 0:
                time.sleep(RETRY_WAIT)
                continue
            return None
        except Exception:
            return None
    return None


def openlibrary_search(title, author):
    """Open Library search API. Returns list of (isbn10, isbn13) tuples."""
    q = f"{title} {author}".strip()
    if not q:
        return []
    url = "https://openlibrary.org/search.json?" + urllib.parse.urlencode({"q": q, "limit": 5})
    raw = _fetch_with_retry(url)
    try:
        data = json.loads(raw.decode("utf-8")) if raw else {}
    except Exception:
        return []
    seen = set()
    result = []
    for doc in data.get("docs", [])[:5]:
        for i in doc.get("isbn", []) or []:
            s = re.sub(r"\D", "", str(i))
            if len(s) == 10 and s not in seen:
                seen.add(s)
                result.append((s, _isbn10_to_isbn13(s)))
                break
            if len(s) == 13 and s.startswith("978") and s not in seen:
                seen.add(s)
                i10 = _isbn13_to_isbn10(s)
                result.append((i10, s))
                break
    return result[:3]


def head_ok(url, referer=None):
    """HEAD で Content-Type が画像かつ Content-Length >= MIN_CONTENT_LENGTH なら True。"""
    headers = {"User-Agent": "Mozilla/5.0 (compatible; BookCoverBot/1.0)"}
    if referer:
        headers["Referer"] = referer
    try:
        req = urllib.request.Request(url, method="HEAD", headers=headers)
        with urllib.request.urlopen(req, timeout=12) as r:
            ct = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if not any(ct.startswith(t) for t in IMAGE_TYPES):
                return False
            cl = r.headers.get("Content-Length")
            if cl is not None:
                try:
                    if int(cl) < MIN_CONTENT_LENGTH:
                        return False
                except ValueError:
                    pass
            return True
    except Exception:
        return False


def ndl_search(title, author):
    """国立国会図書館 (NDL) APIを利用してISBNを取得します。"""
    if not title.strip():
        return []
    params = {"title": title.strip()}
    if author.strip():
        params["creator"] = author.strip()
    url = "https://ndlsearch.ndl.go.jp/api/opensearch?" + urllib.parse.urlencode(params)
    raw = _fetch_with_retry(url, timeout=15)
    if not raw:
        return []
    try:
        content = raw.decode("utf-8")
    except Exception:
        return []
    isbn_matches = re.findall(r'<dc:identifier[^>]*>([0-9X-]{10,})<\/dc:identifier>', content)
    seen = set()
    result = []
    for match in isbn_matches:
        s = re.sub(r"\D", "", match)
        if len(s) == 10 and s not in seen:
            seen.add(s)
            result.append((s, _isbn10_to_isbn13(s)))
        elif len(s) == 13 and s.startswith("978") and s not in seen:
            seen.add(s)
            i10 = _isbn13_to_isbn10(s)
            result.append((i10, s))
        if len(result) >= 3:
            break
    return result


def rakuten_books_cover(title, author, fallback=False):
    """楽天ブックスAPIで書影URLを取得。環境変数 RAKUTEN_APP_ID が必要（RAKUTEN_ACCESS_KEY は任意）。"""
    app_id = os.environ.get("RAKUTEN_APP_ID") or os.environ.get("RAKUTEN_APPID")
    if not app_id:
        return []
    if not title.strip() and not author.strip():
        return []
    params = {
        "applicationId": app_id,
        "format": "json",
        "formatVersion": 2,
        "hits": 5,
        "outOfStockFlag": 1,
    }
    access_key = os.environ.get("RAKUTEN_ACCESS_KEY") or os.environ.get("RAKUTEN_ACCESSKEY")
    if access_key:
        params["accessKey"] = access_key
    if not fallback:
        if title.strip():
            params["title"] = title.strip()
        if author.strip():
            params["author"] = author.strip()
    else:
        params["title"] = f"{title.strip()} {author.strip()}".strip()
    url = "https://app.rakuten.co.jp/services/api/BooksBook/Search/20170404?" + urllib.parse.urlencode(params)
    raw = _fetch_with_retry(url, timeout=12)
    try:
        data = json.loads(raw.decode("utf-8")) if raw else {}
    except Exception:
        return []
    items = data.get("Items") or data.get("items") or []
    out = []
    for raw in items[:3]:
        item = raw.get("item", raw) if isinstance(raw, dict) else raw
        if not isinstance(item, dict):
            continue
        for key in ("largeImageUrl", "mediumImageUrl", "smallImageUrl"):
            u = item.get(key)
            if u and u.startswith("http"):
                out.append(("rakuten", u))
                break
    return out


def google_books_cover_and_isbns(title, author, fallback=False):
    """Google Books API で表紙URLとISBNのリストを取得。langRestrict なしで幅広く検索。"""
    if not fallback:
        q = f"intitle:{title}"
        if author:
            q += f" inauthor:{author}"
    else:
        q = f"{title} {author}".strip()
    url = "https://www.googleapis.com/books/v1/volumes?" + urllib.parse.urlencode({"q": q, "maxResults": 10})
    raw = _fetch_with_retry(url, timeout=12)
    try:
        data = json.loads(raw.decode("utf-8")) if raw else {}
    except Exception:
        return [], []
    out_urls = []
    out_isbns = []
    for item in data.get("items", [])[:10]:
        info = item.get("volumeInfo", {})
        identifiers = info.get("industryIdentifiers", [])
        i10, i13 = None, None
        for id_obj in identifiers:
            t = id_obj.get("type", "")
            i = id_obj.get("identifier", "")
            if t == "ISBN_13" and len(i) == 13:
                i13 = i
            elif t == "ISBN_10" and len(i) == 10:
                i10 = i
        if i13 and not i10:
            i10 = _isbn13_to_isbn10(i13)
        if i10 and not i13:
            i13 = _isbn10_to_isbn13(i10)
        if i10 and i13:
            out_isbns.append((i10, i13))
        links = info.get("imageLinks", {}) or {}
        for key in ("thumbnail", "small", "smallThumbnail", "medium", "large"):
            u = links.get(key)
            if u and u.startswith("http"):
                u = u.replace("http://", "https://")
                out_urls.append(("google", u))
                break
    return out_urls, out_isbns


def openbd_cover_bulk(isbn13_list):
    """openBD API で複数ISBNの書影URLを一括取得。"""
    isbn13_list = [x for x in isbn13_list if x and len(x) == 13]
    if not isbn13_list:
        return []
    url = "https://api.openbd.jp/v1/get?isbn=" + ",".join(isbn13_list)
    raw = _fetch_with_retry(url, timeout=15)
    try:
        data = json.loads(raw.decode("utf-8")) if raw else []
    except Exception:
        return []
    if not data or not isinstance(data, list):
        return []
    out = []
    for rec in data:
        if rec is None:
            continue
        cover = (rec.get("summary") or {}).get("cover")
        if cover and cover.startswith("http"):
            out.append(("openbd", cover))
        collateral = (rec.get("onix") or {}).get("CollateralDetail") or {}
        for res in (collateral.get("SupportingResource") or []):
            if isinstance(res, dict):
                link = res.get("ResourceLink")
                if link and link.startswith("http"):
                    out.append(("openbd", link))
    return out


def get_cover_url_candidates(title, author):
    """書籍特定 → URL候補リスト。段階的フォールバックで取得率を最大化。"""
    candidates = []
    isbn_pairs = []

    urls, isbns = google_books_cover_and_isbns(title, author)
    candidates.extend(urls)
    isbn_pairs.extend(isbns)
    time.sleep(GOOGLE_DELAY)
    candidates.extend(rakuten_books_cover(title, author))
    time.sleep(RAKUTEN_DELAY)

    if not candidates and not isbn_pairs:
        if author:
            urls_to, isbns_to = google_books_cover_and_isbns(title, "")
            candidates.extend(urls_to)
            isbn_pairs.extend(isbns_to)
            time.sleep(GOOGLE_DELAY)
            candidates.extend(rakuten_books_cover(title, ""))
            time.sleep(RAKUTEN_DELAY)

    if not candidates and not isbn_pairs:
        urls_fb, isbns_fb = google_books_cover_and_isbns(title, author, fallback=True)
        candidates.extend(urls_fb)
        isbn_pairs.extend(isbns_fb)
        time.sleep(GOOGLE_DELAY)
        candidates.extend(rakuten_books_cover(title, author, fallback=True))
        time.sleep(RAKUTEN_DELAY)

    ndl_isbns = ndl_search(title, author)
    if ndl_isbns:
        isbn_pairs.extend(ndl_isbns)
        time.sleep(REQUEST_DELAY)
    ol_isbns = openlibrary_search(title, author)
    if ol_isbns:
        isbn_pairs.extend(ol_isbns)
        time.sleep(REQUEST_DELAY)

    seen_isbn_13 = set()
    unique_isbn_pairs = []
    for pair in isbn_pairs:
        if pair[1] not in seen_isbn_13:
            seen_isbn_13.add(pair[1])
            unique_isbn_pairs.append(pair)
    isbn13_list = [p[1] for p in unique_isbn_pairs if p[1]]
    candidates.extend(openbd_cover_bulk(isbn13_list))
    time.sleep(REQUEST_DELAY)
    for isbn10, isbn13 in unique_isbn_pairs:
        if isbn13:
            candidates.append(("cloudfront", f"https://dosbg3xlm0x1t.cloudfront.net/images/items/{isbn13}/1200/{isbn13}.jpg"))
            candidates.append(("hanmoto", f"https://www.hanmoto.com/bd/img/{isbn13}.jpg"))
        if isbn10:
            candidates.append(("amazon", f"https://images-fe.ssl-images-amazon.com/images/P/{isbn10}.09._SCLZZZZZZZ_SX500_.jpg"))
            candidates.append(("amazon2", f"https://m.media-amazon.com/images/P/{isbn10}.09._SCLZZZZZZZ_.jpg"))
            candidates.append(("openlibrary", f"https://covers.openlibrary.org/b/isbn/{isbn10}-L.jpg"))
    return candidates


def find_valid_url(title, author):
    """候補を順に検証し、最初に有効なURLを返す。"""
    for src, url in get_cover_url_candidates(title, author):
        referer = "https://www.amazon.co.jp/" if src in ("amazon", "amazon2") else None
        if head_ok(url, referer=referer):
            return url
        time.sleep(REQUEST_DELAY)
    return None


def process_file(path, dry_run=False):
    title = path.stem.replace("_", " ").strip()
    frontmatter, body = get_frontmatter_and_body(path)
    if frontmatter is None or not has_empty_image(frontmatter):
        return "skip", None
    author = get_author(frontmatter)
    url = find_valid_url(title, author)
    if not url:
        return "no_cover", title
    if not dry_run:
        new_fm = set_image_in_frontmatter(frontmatter, url)
        new_content = "---\n" + new_fm + "\n---" + body
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_content)
    return "ok", (title, url)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="書影記入スクリプト")
    parser.add_argument("--vault-root", type=str, default=None, help="Obsidian ボルトのルート（未指定時は OBSIDIAN_VAULT_ROOT）")
    parser.add_argument("--limit", type=int, default=0, help="処理する最大件数（0=無制限）")
    parser.add_argument("--dry-run", action="store_true", help="ファイルを書き換えずログのみ")
    parser.add_argument("--reset-ignore", action="store_true", help="ignore listを無視して全件再試行")
    args = parser.parse_args()

    vault = resolve_vault_root(args.vault_root)
    book_dir, ignore_file = book_paths(vault)
    log_file = Path(__file__).resolve().parent / "book_cover_fill_log.txt"

    ok, no_cover, skip = [], [], 0
    files = sorted(book_dir.glob("*.md"))
    total = 0
    limit = args.limit
    dry_run = args.dry_run
    processed = 0
    ignore_set = set() if args.reset_ignore else load_ignore_list(ignore_file)
    newly_ignored = {}
    print("書影取得を開始します。件数によっては15〜30分かかります。", flush=True)
    for path in files:
        if limit and total >= limit:
            break
        stem = path.stem
        if stem in ignore_set:
            skip += 1
            continue
        status, extra = process_file(path, dry_run=dry_run)
        if status == "ok":
            ok.append(extra)
            total += 1
            processed += 1
            print(f"  OK  {extra[0]}", flush=True)
        elif status == "no_cover":
            no_cover.append(extra)
            newly_ignored[stem] = datetime.now().timestamp()
            total += 1
            processed += 1
            if processed <= 5 or processed % 20 == 0:
                print(f"  --  {extra}", flush=True)
        else:
            skip += 1
        if processed > 0 and processed % 10 == 0:
            print(f"[進捗] {processed} 件処理済み... (キャッシュスキップ: {skip}件)", flush=True)
    if not dry_run and newly_ignored:
        save_ignore_list(ignore_file, newly_ignored)
    run_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(f"\n{'='*60}\n")
        f.write(f"書影記入 一括実行結果  {run_at}\n")
        f.write(f"{'='*60}\n")
        f.write(f"記入成功: {len(ok)} 件 / 書影なし: {len(no_cover)} 件 / スキップ: {skip} 件\n")
        for t, u in ok:
            f.write(f"  OK  {t}\n    {u}\n")
        if no_cover:
            f.write("[書影なし]\n")
            for t in no_cover:
                f.write(f"  --  {t}\n")
    if dry_run:
        print(f"[DRY RUN] OK={len(ok)}, NoCover={len(no_cover)}, Skip={skip}")
    else:
        print(f"Done. OK={len(ok)}, NoCover={len(no_cover)}, Skip={skip}. Log: {log_file}")


if __name__ == "__main__":
    main()
