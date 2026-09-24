#!/usr/bin/env python3
"""
每日加密货币 & 投资新闻简报
- 每天：过去 24 小时新闻 + 今日重要经济事件 + BTC/ETH/SOL/XRP 行情 → Claude 总结 → 邮件发送
- 周一：在日报后面附上周报（基于 archive/ 中过去 7 天的日报存档）
"""

import html
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path

import feedparser
import markdown
import requests

# ==================== 配置 ====================

BJ = timezone(timedelta(hours=8))
NOW = datetime.now(BJ)
TODAY = NOW.strftime("%Y-%m-%d")
WEEKDAY_CN = "一二三四五六日"[NOW.weekday()]
IS_MONDAY = NOW.weekday() == 0 or os.environ.get("FORCE_WEEKLY", "").lower() == "true"
ARCHIVE_DIR = Path("archive")

API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
BASE_URL = (os.environ.get("ANTHROPIC_BASE_URL") or "https://kitool.ai").strip().rstrip("/")
MODELS = [m.strip() for m in (os.environ.get("CLAUDE_MODELS") or "claude-sonnet-5").split(",") if m.strip()]

MAIL_USER = os.environ.get("MAIL_USER", "").strip()
MAIL_PASS = os.environ.get("MAIL_PASS", "").strip()
MAIL_TO = [a.strip() for a in os.environ.get("MAIL_TO", "").split(",") if a.strip()]

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
}

RSS_FEEDS = {
    "加密货币": [
        "https://www.coindesk.com/arc/outboundfeeds/rss/",
        "https://cointelegraph.com/rss",
        "https://decrypt.co/feed",
        "https://www.theblock.co/rss.xml",
        "https://cryptoslate.com/feed/",
    ],
    "美国市场": [
        "https://www.cnbc.com/id/10000664/device/rss/rss.html",   # CNBC Markets
        "https://www.cnbc.com/id/20910258/device/rss/rss.html",   # CNBC Economy
        "https://feeds.content.dowjones.io/public/rss/mw_topstories",  # MarketWatch
        "https://finance.yahoo.com/news/rssindex",
    ],
    "日本": [
        "https://coinpost.jp/?feed=rss2",                # CoinPost 日本加密货币
        "https://www.coindeskjapan.com/feed/",           # CoinDesk Japan
        "https://www3.nhk.or.jp/rss/news/cat5.xml",      # NHK 经济
        "https://asia.nikkei.com/rss/feed/nar",          # Nikkei Asia
    ],
}

COINS = {"bitcoin": "BTC", "ethereum": "ETH", "solana": "SOL", "ripple": "XRP"}


# ==================== 数据抓取 ====================

def clean_text(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def fetch_feed(url: str, since: datetime) -> list:
    try:
        r = requests.get(url, headers=UA, timeout=20)
        r.raise_for_status()
        feed = feedparser.parse(r.content)
    except Exception as e:
        print(f"  ✗ {url}: {e}")
        return []

    source = clean_text(feed.feed.get("title", url))
    items = []
    for e in feed.entries[:80]:
        t = e.get("published_parsed") or e.get("updated_parsed")
        if not t:
            continue
        pub = datetime(*t[:6], tzinfo=timezone.utc)
        if pub < since:
            continue
        items.append({
            "title": clean_text(e.get("title", "")),
            "summary": clean_text(e.get("summary", ""))[:300],
            "link": e.get("link", ""),
            "source": source,
            "pub": pub,
        })
    print(f"  ✓ {source}: {len(items)} 条")
    return items


def fetch_news(hours: int, per_category: int = 40) -> dict:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    result = {}
    for category, urls in RSS_FEEDS.items():
        print(f"\n📰 抓取 {category}")
        seen, items = set(), []
        for url in urls:
            for it in fetch_feed(url, since):
                key = it["title"].lower()
                if it["title"] and key not in seen:
                    seen.add(key)
                    items.append(it)
        items.sort(key=lambda x: x["pub"], reverse=True)
        result[category] = items[:per_category]
    return result


def fetch_prices() -> str:
    lines = []
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/coins/markets",
            params={"vs_currency": "usd", "ids": ",".join(COINS), "price_change_percentage": "24h,7d"},
            headers=UA, timeout=20,
        )
        r.raise_for_status()
        for c in r.json():
            p24 = c.get("price_change_percentage_24h_in_currency") or 0
            p7 = c.get("price_change_percentage_7d_in_currency") or 0
            lines.append(f"{COINS.get(c['id'], c['symbol'].upper())}: ${c['current_price']:,.4g} | "
                         f"24h {p24:+.2f}% | 7d {p7:+.2f}% | 市值排名 #{c.get('market_cap_rank')}")
    except Exception as e:
        print(f"⚠️ 行情获取失败: {e}")
    try:
        r = requests.get("https://api.alternative.me/fng/", timeout=20)
        d = r.json()["data"][0]
        lines.append(f"恐惧贪婪指数: {d['value']} ({d['value_classification']})")
    except Exception as e:
        print(f"⚠️ 恐惧贪婪指数获取失败: {e}")
    return "\n".join(lines) or "（行情数据获取失败）"


def fetch_calendar(whole_week: bool) -> str:
    """ForexFactory 本周经济日历，筛选美国/日本的中高影响事件"""
    try:
        r = requests.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json", headers=UA, timeout=20)
        r.raise_for_status()
        events = r.json()
    except Exception as e:
        print(f"⚠️ 经济日历获取失败: {e}")
        return "（经济日历获取失败）"

    end = NOW + (timedelta(days=7) if whole_week else timedelta(hours=24))
    lines = []
    for ev in events:
        if ev.get("country") not in ("USD", "JPY") or ev.get("impact") not in ("High", "Medium"):
            continue
        try:
            t = datetime.fromisoformat(ev["date"]).astimezone(BJ)
        except Exception:
            continue
        if NOW - timedelta(hours=2) <= t <= end:
            lines.append(f"{t.strftime('%m-%d %H:%M')} 北京时间 | {ev['country']} | {ev['impact']} | {ev['title']}"
                         f" | 预期 {ev.get('forecast') or '-'} | 前值 {ev.get('previous') or '-'}")
    return "\n".join(lines) or "（该时段无中高影响事件）"


def format_news(news: dict) -> str:
    parts = []
    for category, items in news.items():
        parts.append(f"\n## {category}（{len(items)} 条）")
        for it in items:
            parts.append(f"- [{it['pub'].astimezone(BJ).strftime('%m-%d %H:%M')}] {it['title']}（{it['source']}）\n"
                         f"  {it['summary']}\n  {it['link']}")
    return "\n".join(parts)


# ==================== Claude ====================

SYSTEM_PROMPT = """你是一位资深的加密货币与宏观投资分析师，为中文读者撰写每日投资简报。
要求：
- 全部使用简体中文（专有名词可保留英文），语言精炼，适合手机阅读
- 只依据提供的资料总结，不要编造资料中没有的事实、数字或事件
- 重要新闻在句末附上原文链接，格式：[来源](链接)
- 使用 Markdown 输出：标题用 ##，表格用标准 Markdown 表格
- 重点关注 BTC、ETH、SOL、XRP；其他币种只写真正重大的事件（如大额黑客攻击、ETF、监管、主网升级、暴涨暴跌）
- 结尾加一行免责声明：以上内容仅供参考，不构成投资建议"""


def call_claude(prompt: str, max_tokens: int) -> str | None:
    url = BASE_URL + ("/messages" if BASE_URL.endswith("/v1") else "/v1/messages")
    headers = {
        "x-api-key": API_KEY,
        "Authorization": f"Bearer {API_KEY}",
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    for model in MODELS:
        for attempt in (1, 2):
            try:
                print(f"🤖 调用 {model}（第 {attempt} 次）...")
                r = requests.post(url, headers=headers, timeout=300, json={
                    "model": model,
                    "max_tokens": max_tokens,
                    "system": SYSTEM_PROMPT,
                    "messages": [{"role": "user", "content": prompt}],
                })
                if r.status_code == 200:
                    data = r.json()
                    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()
                    if text:
                        print(f"✅ {model} 生成成功")
                        return text
                    print(f"  ⚠ 返回内容为空: {r.text[:300]}")
                else:
                    print(f"  ✗ HTTP {r.status_code}: {r.text[:500]}")
                    if r.status_code in (400, 401, 403, 404):
                        break  # 这类错误重试无意义，换下一个模型
            except requests.RequestException as e:
                print(f"  ✗ 请求异常: {e}")
            time.sleep(5)
    return None


def build_daily_prompt(news: dict, prices: str, calendar: str) -> str:
    return f"""今天是 {TODAY}（周{WEEKDAY_CN}），请根据以下资料撰写【每日投资简报】，覆盖过去 24 小时与今天需要关注的事项。

输出结构：
## 📊 行情速览
（Markdown 表格：币种 | 价格 | 24h | 7d；表格下一句话点评恐惧贪婪指数与市场情绪）
## 🔥 今日最重要的 3 件事
## 🪙 主流币重点
（BTC / ETH / SOL / XRP 分别 1~3 条，没有重要消息就写"无重大消息"）
## 🌐 其他币种与行业大事
## 🇺🇸 美国：宏观、监管与美股
## 🇯🇵 日本：政策、监管与市场
（日文资料请翻译成中文）
## 📅 今日关注（北京时间）
（根据经济日历和新闻列出今天/未来 24 小时的重要数据发布、会议、解锁、升级等）
## ⚠️ 风险提示

==== 行情数据 ====
{prices}

==== 未来 24 小时美国/日本经济日历 ====
{calendar}

==== 过去 24 小时新闻 ====
{format_news(news)}
"""


def build_weekly_prompt(history: str, prices: str, calendar: str) -> str:
    return f"""今天是 {TODAY}（周一），请根据过去一周的每日简报存档撰写【上周周报】。

输出结构：
## 📈 上周市场回顾
（BTC / ETH / SOL / XRP 一周走势与驱动因素，结合 7d 涨跌幅）
## 🗞️ 上周十大要闻
## 🇺🇸 美国宏观与监管一周回顾
## 🇯🇵 日本一周回顾
## 🔭 本周前瞻
（根据本周经济日历列出关键日程，并说明可能的影响）
## 💡 本周操作关注点
（关键价位、需警惕的风险，保持客观）

==== 当前行情（含 7 日涨跌幅） ====
{prices}

==== 本周美国/日本经济日历 ====
{calendar}

==== 过去一周资料 ====
{history}
"""


def load_week_history() -> str:
    parts = []
    for i in range(7, 0, -1):
        day = (NOW - timedelta(days=i)).strftime("%Y-%m-%d")
        f = ARCHIVE_DIR / f"{day}.md"
        if f.exists():
            parts.append(f"\n\n######## {day} 日报 ########\n{f.read_text(encoding='utf-8')}")
    if parts:
        print(f"📚 读取到 {len(parts)} 天的日报存档")
        return "".join(parts)
    print("📚 没有日报存档，改用 RSS 抓取过去 7 天新闻（RSS 通常只保留最近一两天，覆盖有限）")
    return format_news(fetch_news(hours=168, per_category=60))


def fallback_digest(news: dict, prices: str, calendar: str) -> str:
    """Claude 调用失败时，发送原始资料，保证每天都能收到邮件"""
    parts = ["> ⚠️ 今日 AI 总结失败，以下为原始资料\n", "## 📊 行情", prices.replace("\n", "  \n"),
             "## 📅 未来 24 小时经济日历", calendar.replace("\n", "  \n")]
    for category, items in news.items():
        parts.append(f"## {category}")
        parts += [f"- [{it['title']}]({it['link']})（{it['source']}）" for it in items[:15]]
    return "\n\n".join(parts)


# ==================== 邮件 ====================

EMAIL_CSS = """
body{margin:0;padding:12px;background:#f4f5f7;font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;color:#222;line-height:1.7;font-size:15px}
.box{max-width:760px;margin:0 auto;background:#fff;border-radius:10px;padding:20px 22px}
h1{font-size:21px;margin:0 0 6px}
h2{font-size:17px;color:#1a56db;border-left:4px solid #1a56db;padding-left:8px;margin:26px 0 10px}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{border:1px solid #e3e6ea;padding:6px 8px;text-align:left}
th{background:#f0f4ff}
a{color:#1a56db}
blockquote{margin:0;padding:8px 12px;background:#fff7e6;border-left:4px solid #f5a623}
hr{border:none;border-top:2px dashed #ccc;margin:32px 0}
.footer{color:#999;font-size:12px;text-align:center;margin-top:24px}
"""


def send_email(subject: str, md_text: str) -> None:
    body_html = markdown.markdown(md_text, extensions=["tables", "sane_lists"])
    html_doc = (f"<html><head><meta charset='utf-8'><style>{EMAIL_CSS}</style></head><body>"
                f"<div class='box'>{body_html}<div class='footer'>由 GitHub Actions + Claude 自动生成</div></div>"
                f"</body></html>")

    msg = MIMEMultipart("alternative")
    msg["From"] = formataddr(("每日投资简报", MAIL_USER))
    msg["To"] = ", ".join(MAIL_TO)
    msg["Subject"] = subject
    msg.attach(MIMEText(md_text, "plain", "utf-8"))
    msg.attach(MIMEText(html_doc, "html", "utf-8"))

    print(f"📧 通过 smtp.qq.com:465 发送到 {', '.join(MAIL_TO)}")
    with smtplib.SMTP_SSL("smtp.qq.com", 465, timeout=30) as server:
        server.login(MAIL_USER, MAIL_PASS)
        server.sendmail(MAIL_USER, MAIL_TO, msg.as_string())
    print("✅ 邮件发送成功")


# ==================== 主流程 ====================

def main():
    missing = [k for k, v in {"ANTHROPIC_API_KEY": API_KEY, "MAIL_USER": MAIL_USER,
                              "MAIL_PASS": MAIL_PASS, "MAIL_TO": MAIL_TO}.items() if not v]
    if missing:
        sys.exit(f"❌ 缺少 Secrets：{', '.join(missing)}")

    print(f"⏰ 北京时间 {NOW:%Y-%m-%d %H:%M} 周{WEEKDAY_CN} | 周报：{'是' if IS_MONDAY else '否'}")
    print(f"🔗 API：{BASE_URL} | 模型：{', '.join(MODELS)}")

    news = fetch_news(hours=24)
    print(f"\n📊 共 {sum(len(v) for v in news.values())} 条新闻")
    prices = fetch_prices()
    print(prices)
    calendar_today = fetch_calendar(whole_week=False)

    daily = call_claude(build_daily_prompt(news, prices, calendar_today), max_tokens=6000)
    if daily:
        ARCHIVE_DIR.mkdir(exist_ok=True)
        (ARCHIVE_DIR / f"{TODAY}.md").write_text(daily, encoding="utf-8")
    else:
        daily = fallback_digest(news, prices, calendar_today)

    content = f"# 📰 每日投资简报 · {TODAY} 周{WEEKDAY_CN}\n\n{daily}"
    subject = f"📰 每日投资简报 {TODAY}"

    if IS_MONDAY:
        weekly = call_claude(build_weekly_prompt(load_week_history(), prices, fetch_calendar(whole_week=True)),
                             max_tokens=8000)
        if weekly:
            content += f"\n\n---\n\n# 📅 上周周报\n\n{weekly}"
            subject += "（含周报）"

    send_email(subject, content)


if __name__ == "__main__":
    main()
