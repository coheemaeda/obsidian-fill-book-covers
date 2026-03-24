# obsidian-fill-book-covers

Obsidian で **読書メモ** を管理するとき、フロントマターの `Image:` が空のままになっているファイルに、自動で**書影（表紙）の画像 URL**を入れるための Python スクリプトです。

## なぜこれが必要か

読書メモを Obsidian にまとめるとき、ノートにはタイトルや著者は書ける一方で、「表紙の画像 URL を自分で探して貼る」のは地味に手間がかかります。複数冊まとめて登録すると、さらに負担が増えます。

このスクリプトは、次のような流れを**対話なしで一括実行**します。

1. 指定したフォルダ内の Book 用 Markdown を走査する  
2. `Image:` が空のノートだけを対象にする  
3. タイトル・著者から書籍を特定し、複数の公開 API / 画像 URL を候補として集める  
4. HTTP の HEAD で「本当に画像として取れるか」を軽く確認する  
5. 問題なければ `Image:` に URL を書き込む  

運用に合わせて **定期実行（launchd など）** しておけば、新規に追加した「表紙まだ」のノートだけが少しずつ埋まっていく、という使い方に向いています。

## 前提

- Python 3（標準ライブラリのみ）
- ノートの YAML に `Image:` 行があり、空のときだけ埋める想定です（著者は `Author:` から取得）
- フォルダ構成はデフォルトで `（ボルトルート）/10_Zettelkasten/LiteratureNote/Book` を見ます。違う場合は環境変数で変更できます

## セットアップ

1. リポジトリを clone する  
2. `.env.example` を `.env` にコピーし、少なくとも `OBSIDIAN_VAULT_ROOT` を自分のボルトのパスに設定する  
3. 楽天ブックス API を使う場合は [楽天ウェブサービス](https://webservice.rakuten.co.jp/) でアプリ ID を取得し、`RAKUTEN_APP_ID` を `.env` に書く（任意だが取得率向上に有効）

`run_fill_book_covers.sh` は `.env` をシェルの `source` では読みません。**`KEY=VALUE` 形式の行だけ**をパースして環境変数にします（1行1変数。`#` から始まる行はコメント）。

## 使い方

```bash
# .env を読み込んだうえで実行（run_fill_book_covers.sh 経由）
chmod +x run_fill_book_covers.sh
./run_fill_book_covers.sh

# または直接 Python（環境変数は自分で export）
export OBSIDIAN_VAULT_ROOT="$HOME/path/to/vault"
python3 fill_book_covers.py --dry-run   # まずは dry-run
python3 fill_book_covers.py
```

主なオプション:

- `--vault-root` … ボルトのルート（未指定時は `OBSIDIAN_VAULT_ROOT`）
- `--dry-run` … ファイルを書き換えず結果のみ表示
- `--limit N` … 最大 N 件だけ処理
- `--reset-ignore` … 書影が取れなかったタイトルの一時スキップをリセット

環境変数 `BOOK_NOTES_SUBPATH` で、Book ノートの相対パスを変更できます（デフォルトは `10_Zettelkasten/LiteratureNote/Book`）。

## macOS で定期実行（例）

`com.example.bookcover.plist` のパスを自分の環境に合わせて編集し、`LaunchAgents` に配置する方法が一般的です。API キーは **plist に書かず**、スクリプトと同じディレクトリの `.env` に置いてください。

## 注意

- 外部サイトへリクエストを送ります。利用規約・レート制限に注意し、間隔はスクリプト内の `REQUEST_DELAY` 等で調整してください  
- 楽天 API は未設定でも動作しますが、候補が減る場合があります  
- ログ `book_cover_fill_log.txt` や `.book_cover_ignore.json` には個人の書誌情報が含まれる可能性があるため、`.gitignore` に含めています

## ライセンス

このリポジトリは個人の Obsidian 運用向けに作られたものを公開用に整理したものです。利用は自己責任でお願いします。
