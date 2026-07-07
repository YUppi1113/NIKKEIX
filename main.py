import os
import sys
import time
import requests
import urllib.parse
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

BASE_URL = "https://xtrend.nikkei.com/js/2021/common/query.js"
SITE_ROOT = "https://xtrend.nikkei.com"
JST = ZoneInfo("Asia/Tokyo")

# ---- LINE Messaging API 設定 ----
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_TO = os.environ.get("LINE_TO", "")  # 通知先のユーザーID or グループID
# 記事が0件のときも「0件でした」と通知するかどうか（Actions側の環境変数で変更可）
LINE_NOTIFY_WHEN_EMPTY = os.environ.get("LINE_NOTIFY_WHEN_EMPTY", "false").lower() == "true"

LINE_MAX_MESSAGES_PER_REQUEST = 5   # LINE Push APIの1リクエストあたりの上限
LINE_MAX_TEXT_LENGTH = 4900         # 1メッセージのtextの上限(5000文字)に余裕を持たせる


def build_query_params():
    # 1つ目のfq: 記事(bparticle/bpauthor、親記事でない)またはカレンダー
    fq1 = "((type:bparticle OR type:bpauthor) AND ParentFlag_ja_b:false) OR type:bpcalendar"
    # 2つ目のfq: 各テーマ(マーケ・消費、技術・データ 等)に該当する記事
    theme_ids = [
        "nxr_thm_marketing",
        "nxr_thm_techdata",
        "nxr_thm_innovation",
        "nxr_thm_overseas",
        "nxr_thm_skillup",
        "nxr_thm_newitem",
        "nxr_thm_thisweekdata",
        "nxr_thm_movie",
        "nxr_thm_committee",
    ]
    theme_conditions = " OR ".join(
        f"ATypeId_ja_s:{t} OR ThemeId_ja_smv:{t}" for t in theme_ids
    )
    fq2 = f"({theme_conditions} OR ThemeId_ja_smv:nxr_thm_event-a)"
    query = (
        f"fq={fq1}"
        f"&fq={fq2}"
        f"&start=0&rows=40"
        f"&sort=PublishFrom_ja_dt desc,UniqContentsId_ja_s desc"
    )
    return {
        "output": "JSON",
        "query": query,
        "decode": "true",
        "_": str(int(time.time() * 1000)),  # キャッシュ回避用タイムスタンプ
    }


def fetch_docs():
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": "https://xtrend.nikkei.com/atcl/contents/new/",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
    }
    params = build_query_params()
    response = requests.get(BASE_URL, params=params, headers=headers, timeout=10)
    response.raise_for_status()
    data = response.json()
    if not data.get("success", False):
        raise RuntimeError(f"APIがエラーを返しました: {data.get('mes')}")
    return data.get("docs", [])


def is_today(publish_from_ms: str, today_date) -> bool:
    try:
        ts = int(publish_from_ms) / 1000
    except (TypeError, ValueError):
        return False
    dt_jst = datetime.fromtimestamp(ts, tz=JST)
    return dt_jst.date() == today_date


# ---------------------------------------------------------------------------
# LINE通知まわり
# ---------------------------------------------------------------------------

def build_line_text_blocks(header: str, results: list[tuple[str, str]]) -> list[str]:
    """
    記事一覧を LINE の1メッセージの文字数上限(5000文字)内に収まるよう
    複数の text ブロックに分割する。
    """
    blocks = []
    current = header
    for i, (title, url) in enumerate(results, start=1):
        entry = f"\n\n{i}. {title}\n{url}"
        if len(current) + len(entry) > LINE_MAX_TEXT_LENGTH:
            blocks.append(current)
            current = entry.lstrip("\n")
        else:
            current += entry
    if current:
        blocks.append(current)
    return blocks


def chunked(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def send_line_push(messages: list[str], token: str, to: str):
    """
    messages: テキスト文字列のリスト(1回のAPIコールで最大5件まで送れる)
    """
    if not token or not to:
        logger.warning("LINE_CHANNEL_ACCESS_TOKEN または LINE_TO が未設定のため、LINE通知をスキップします。")
        return

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    for chunk in chunked(messages, LINE_MAX_MESSAGES_PER_REQUEST):
        payload = {
            "to": to,
            "messages": [{"type": "text", "text": text} for text in chunk],
        }
        resp = requests.post(LINE_PUSH_URL, headers=headers, json=payload, timeout=10)
        if resp.status_code != 200:
            logger.error(f"LINE通知に失敗しました: status={resp.status_code}, body={resp.text}")
            resp.raise_for_status()
        else:
            logger.info(f"LINE通知を送信しました（{len(chunk)}件のメッセージ）")
        # レート制限に配慮して少し待つ
        time.sleep(0.5)


def notify_line(today_str: str, results: list[tuple[str, str]]):
    if not results:
        if LINE_NOTIFY_WHEN_EMPTY:
            send_line_push(
                [f"【日経クロストレンド】{today_str}\n本日の新着記事はありませんでした。"],
                LINE_CHANNEL_ACCESS_TOKEN,
                LINE_TO,
            )
        return

    header = f"【日経クロストレンド】{today_str} の新着記事（{len(results)}件）"
    blocks = build_line_text_blocks(header, results)
    send_line_push(blocks, LINE_CHANNEL_ACCESS_TOKEN, LINE_TO)


# ---------------------------------------------------------------------------
# メイン処理
# ---------------------------------------------------------------------------

def main():
    today_date = datetime.now(JST).date()
    today_str = today_date.strftime("%Y.%m.%d")
    logger.info(f"本日({today_str})の記事を抽出します")

    try:
        docs = fetch_docs()
        logger.info(f"取得した記事件数(全体): {len(docs)}")

        results = []
        for doc in docs:
            if is_today(doc.get("PublishFrom", ""), today_date):
                title = doc.get("Title", "(タイトル不明)")
                relative_url = doc.get("Url", "")
                full_url = urllib.parse.urljoin(SITE_ROOT, relative_url)
                results.append((title, full_url))

        if not results:
            print(f"{today_str} の記事は見つかりませんでした。")
            notify_line(today_str, results)
            return

        print(f"--- {today_str} の記事一覧（{len(results)}件） ---")
        for i, (title, full_url) in enumerate(results, start=1):
            print(f"{i}. {title}")
            print(f"   URL: {full_url}")
            print("-" * 30)

        notify_line(today_str, results)

    except Exception as e:
        logger.error(f"エラーが発生しました: {e}", exc_info=True)
        print(f"エラーが発生しました: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
