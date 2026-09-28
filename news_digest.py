#!/usr/bin/env python3
"""
每日加密货币 & 投资新闻简报
- 每天：过去 24 小时新闻 + 今日重要经济事件 + BTC/ETH/SOL/XRP 行情 → Claude 总结 → 邮件发送
- 周一：在日报后面附上周报（基于 archive/ 中过去 7 天的日报存档）
- 每天：生成 events/events.json（经济日历 + 新闻中的重要日程），供桌面日历程序同步提醒
"""

import hashlib
import html
import json
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
EVENTS_FILE = Path("events/events.json")

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
        "https://bitcoinmagazine.com/.rss/full/",     # Bitcoin Magazine
        "https://cryptobriefing.com/feed/",           # Crypto Briefing
        "https://beincrypto.com/feed/",               # BeInCrypto（全球视角）
    ],
    "美国市场与宏观": [
        "https://www.cnbc.com/id/10000664/device/rss/rss.html",   # CNBC Markets
        "https://www.cnbc.com/id/20910258/device/rss/rss.html",   # CNBC Economy
        "https://feeds.content.dowjones.io/public/rss/mw_topstories",  # MarketWatch
        "https://www.ft.com/?format=rss",             # Financial Times
        "https://www.bloomberg.com/feeds/podcasts/etf.xml",  # Bloomberg ETF
    ],
    "日本": [
        "https://coinpost.jp/?feed=rss2",             # CoinPost 日本加密
        "https://www.coindeskjapan.com/feed/",        # CoinDesk Japan
        "https://jp.cointelegraph.com/rss",           # Cointelegraph 日本
        "https://www3.nhk.or.jp/rss/news/cat5.xml",  # NHK 经济（有时无内容）
    ],
    "国际重大新闻": [
        "https://feeds.bbci.co.uk/news/world/rss.xml",   # BBC World
        "https://www.aljazeera.com/xml/rss/all.xml",     # Al Jazeera
        "https://rss.reuters.com/reuters/topNews",        # Reuters Top News
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
            price = c["current_price"]
            price_text = f"{price:,.2f}" if price >= 1 else f"{price:.4f}"
            lines.append(f"{COINS.get(c['id'], c['symbol'].upper())}: ${price_text} | "
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


def fetch_calendar_events() -> list | None:
    """ForexFactory 本周经济日历中美国/日本的中高影响事件；获取失败返回 None
    （该接口有频率限制，每次运行只请求一次）"""
    try:
        r = requests.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json", headers=UA, timeout=20)
        r.raise_for_status()
        raw = r.json()
    except Exception as e:
        print(f"⚠️ 经济日历获取失败: {e}")
        return None

    events = []
    for ev in raw:
        if ev.get("country") not in ("USD", "JPY") or ev.get("impact") not in ("High", "Medium"):
            continue
        try:
            t = datetime.fromisoformat(ev["date"]).astimezone(BJ)
        except Exception:
            continue
        events.append({**ev, "time": t})
    events.sort(key=lambda ev: ev["time"])
    return events


def format_calendar(events: list | None, whole_week: bool) -> str:
    if events is None:
        return "（经济日历获取失败）"
    end = NOW + (timedelta(days=7) if whole_week else timedelta(hours=24))
    lines = [f"{ev['time'].strftime('%m-%d %H:%M')} 北京时间 | {ev['country']} | {ev['impact']} | {ev['title']}"
             f" | 预期 {ev.get('forecast') or '-'} | 前值 {ev.get('previous') or '-'}"
             for ev in events if NOW - timedelta(hours=2) <= ev["time"] <= end]
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
- 加密货币是核心：BTC、ETH、SOL、XRP 必须覆盖；其他币种只写真正重大的事件（黑客攻击、ETF、监管、主网升级、暴涨暴跌等）
- 不限于美国和日本，全球范围内的加密货币重要资讯都要汇总
- 国际重大新闻（非加密货币）如有值得关注的，单独列出一小节
- 结尾加一行免责声明：以上内容仅供参考，不构成投资建议"""


def call_claude(prompt: str, max_tokens: int, system: str = SYSTEM_PROMPT) -> str | None:
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
                    "system": system,
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


# ==================== 日历事件（供桌面日历程序同步） ====================

COUNTRY_FLAG = {"USD": "🇺🇸", "JPY": "🇯🇵"}

EVENTS_SYSTEM_PROMPT = """你是资深的宏观与加密货币分析师，负责为投资者整理日程提醒。
只输出一个合法的 JSON 对象，不要输出任何其他文字、解释或 Markdown 代码块标记。
只依据提供的资料，不要编造日期、时间或事件。"""


def calendar_event_id(ev: dict) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", ev["title"].lower()).strip("-")
    return f"ff-{ev['time'].astimezone(timezone.utc):%Y%m%d%H%M}-{ev['country'].lower()}-{slug}"


def upcoming_calendar_items(events: list) -> list:
    return [{
        "id": calendar_event_id(ev),
        "time": ev["time"],
        "country": ev["country"],
        "impact": "high" if ev["impact"] == "High" else "medium",
        "title_en": ev["title"],
        "detail": f"预期 {ev.get('forecast') or '-'} | 前值 {ev.get('previous') or '-'}",
    } for ev in events if ev["time"] >= NOW - timedelta(hours=2)]


def load_events_file() -> dict:
    try:
        data = json.loads(EVENTS_FILE.read_text(encoding="utf-8"))
        return {e["id"]: e for e in data.get("events", [])}
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"⚠️ 读取旧的 events.json 失败，将重新生成: {e}")
        return {}


def build_events_prompt(cal_items: list, news: dict, known_news_events: list) -> str:
    cal_text = "\n".join(
        f"{it['id']} | {it['time']:%m-%d %H:%M} 北京时间 | {it['country']} | {it['impact']} | {it['title_en']} | {it['detail']}"
        for it in cal_items) or "（无）"
    news_text = "\n".join(
        f"- {it['title']}：{it['summary'][:160]}（{it['link']}）"
        for items in news.values() for it in items) or "（无）"
    known_text = "\n".join(f"- {e['start'][:16]} {e['title']}" for e in known_news_events) or "（无）"
    return f"""现在是北京时间 {NOW:%Y-%m-%d %H:%M}（周{WEEKDAY_CN}）。

任务 1：为【A. 经济日历】中的每一项写中文标题和注意事项。
- title：简短中文，不超过 20 字，例如"美国9月CPI月率"
- note：60~120 字，说明这项数据或会议是什么、预期与前值相比意味着什么、公布后对美元/美股/BTC 等风险资产可能的影响，以及需要警惕的情况

任务 2：从【B. 过去 24 小时新闻】中找出未来 14 天内、新闻里写明了具体日期的重要事件，
例如：BTC/ETH/SOL/XRP 相关的网络升级、ETF 审批截止或上市、大额代币解锁、监管投票或听证、央行官员讲话、重要公司财报、日本金融厅政策落地等。
- 只收录新闻里明确写出日期的事件，不要推测；没有就输出空数组
- 已在【C. 已记录事件】中的不要重复输出
- 新闻中的时间请换算成北京时间；新闻没有写具体时间就把 time 设为 null
- impact：对加密货币或投资市场影响大的填 "high"，其余填 "medium"
- note：60~120 字，写明事件内容、可能的影响和需要注意的地方

输出格式（只输出这个 JSON）：
{{"calendar": [{{"id": "A 中的 id 原样复制", "title": "...", "note": "..."}}],
 "news_events": [{{"date": "YYYY-MM-DD", "time": "HH:MM 或 null", "title": "...", "impact": "high 或 medium", "note": "...", "link": "新闻链接"}}]}}

==== A. 经济日历（美国/日本，中高影响） ====
{cal_text}

==== B. 过去 24 小时新闻 ====
{news_text}

==== C. 已记录事件 ====
{known_text}
"""


def parse_json_reply(text: str | None) -> dict:
    if not text:
        return {}
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        print("⚠️ 事件 JSON 解析失败：回复中没有 JSON")
        return {}
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        print(f"⚠️ 事件 JSON 解析失败: {e}")
        return {}


def clean_news_events(items: list) -> list:
    """校验 Claude 提取的新闻事件，丢弃日期不合理的条目"""
    result = []
    for it in items if isinstance(items, list) else []:
        try:
            title = str(it.get("title") or "").strip()
            day = datetime.strptime(str(it.get("date")), "%Y-%m-%d").date()
            time_text = it.get("time")
            time_known = isinstance(time_text, str) and re.fullmatch(r"\d{1,2}:\d{2}", time_text.strip()) is not None
            hour, minute = map(int, time_text.strip().split(":")) if time_known else (9, 0)
            start = datetime(day.year, day.month, day.day, hour, minute, tzinfo=BJ)
        except Exception:
            continue
        if not title or not (NOW - timedelta(hours=1) <= start <= NOW + timedelta(days=14)):
            continue
        note = str(it.get("note") or "").strip()
        if not time_known:
            note = (note + "\n" if note else "") + "（新闻未给出具体时间，按当天 09:00 提醒）"
        result.append({
            "id": "ai-" + hashlib.sha1(f"{day}|{title}".encode("utf-8")).hexdigest()[:12],
            "start": start.isoformat(),
            "impact": "high" if it.get("impact") == "high" else "medium",
            "title": title,
            "note": note,
            "detail": "",
            "link": str(it.get("link") or ""),
            "source": "news",
        })
    return result


def update_events_file(calendar_events: list | None, news: dict) -> None:
    old = load_events_file()
    cal_items = upcoming_calendar_items(calendar_events or [])
    known_news = [e for e in old.values() if e.get("source") == "news" and e["start"] >= NOW.isoformat()]

    reply = parse_json_reply(call_claude(build_events_prompt(cal_items, news, known_news),
                                         max_tokens=8000, system=EVENTS_SYSTEM_PROMPT))
    enrich = {c.get("id"): c for c in reply.get("calendar", []) if isinstance(c, dict)}

    events = {}
    for it in cal_items:
        prev, e = old.get(it["id"], {}), enrich.get(it["id"], {})
        flag = COUNTRY_FLAG.get(it["country"], "")
        events[it["id"]] = {
            "id": it["id"],
            "start": it["time"].isoformat(),
            "impact": it["impact"],
            # Claude 失败时沿用上次的中文标题和注意事项
            "title": f"{flag} {e['title']}" if e.get("title") else prev.get("title") or f"{flag} {it['title_en']}",
            "note": e.get("note") or prev.get("note") or "",
            "detail": f"{it['title_en']} | {it['detail']}",
            "link": "",
            "source": "calendar",
        }

    for ev in clean_news_events(reply.get("news_events", [])):
        events.setdefault(ev["id"], ev)

    cutoff = (NOW - timedelta(days=3)).isoformat()
    for eid, prev in old.items():
        if eid in events or prev["start"] < cutoff:
            continue
        # 经济日历获取成功时，本周日历里已经没有的未来事件视为改期或取消
        if prev.get("source") == "calendar" and calendar_events is not None and prev["start"] >= NOW.isoformat():
            continue
        events[eid] = prev

    EVENTS_FILE.parent.mkdir(exist_ok=True)
    EVENTS_FILE.write_text(json.dumps({
        "updated": NOW.isoformat(timespec="seconds"),
        "events": sorted(events.values(), key=lambda e: e["start"]),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"🗓️ events.json 已更新：共 {len(events)} 个事件（新闻事件新增 {len(reply.get('news_events', []) or [])} 条候选）")


# ==================== ICS 日历生成 ====================

ICS_FILE = Path("calendar/events.ics")


def ics_escape(text: str) -> str:
    """RFC 5545 文本转义 + 折行（每行不超过 75 字节）"""
    text = str(text or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")
    # 折行：按字节计，续行以空格开头
    line, out, col = "", [], 0
    for ch in text:
        b = len(ch.encode("utf-8"))
        if col + b > 74:
            out.append(line)
            line, col = " " + ch, 1 + b
        else:
            line += ch
            col += b
    if line:
        out.append(line)
    return "\r\n".join(out)


def ics_dt(dt: datetime) -> str:
    """datetime → iCalendar DTSTART 格式（带时区）"""
    utc = dt.astimezone(timezone.utc)
    return utc.strftime("%Y%m%dT%H%M%SZ")


def build_ics(events: list) -> str:
    """把事件列表渲染为 .ics 文件内容"""
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//news-bot//daily-digest//ZH",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:📈 投资日历",
        f"X-WR-TIMEZONE:Asia/Shanghai",
        f"REFRESH-INTERVAL;VALUE=DURATION:PT8H",
        f"X-PUBLISHED-TTL:PT8H",
    ]

    for ev in events:
        try:
            start = datetime.fromisoformat(ev["start"])
        except Exception:
            continue

        uid = f"{ev['id']}@news-bot"
        summary = ev.get("title") or ev.get("title_en", "")
        note_parts = []
        if ev.get("note"):
            note_parts.append(ev["note"])
        if ev.get("detail"):
            note_parts.append(ev["detail"])
        if ev.get("link"):
            note_parts.append(ev["link"])
        description = "\n".join(note_parts)

        impact = ev.get("impact", "medium")
        # 颜色：高影响用红色，中影响用黄色（iCalendar COLOR 属性，谷歌日历支持）
        color = "#E53935" if impact == "high" else "#F9A825"
        # 高影响：两次提醒（15分钟前 + 准时）；中影响：只提前 15 分钟
        alarms = [("提醒：{}", -15)]
        if impact == "high":
            alarms.append(("⏰ 时间到：{}", 0))

        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{ics_dt(NOW)}",
            f"DTSTART:{ics_dt(start)}",
            f"DTEND:{ics_dt(start + timedelta(minutes=30))}",
            f"SUMMARY:{ics_escape(('🔴 ' if impact == 'high' else '🟡 ') + summary)}",
            f"DESCRIPTION:{ics_escape(description)}",
            f"CATEGORIES:{'高影响' if impact == 'high' else '中影响'}",
            f"COLOR:{color}",
        ]

        for alarm_tmpl, offset_min in alarms:
            lines += [
                "BEGIN:VALARM",
                "ACTION:DISPLAY",
                f"DESCRIPTION:{ics_escape(alarm_tmpl.format(summary))}",
                f"TRIGGER:{'-' if offset_min <= 0 else '+'}PT{abs(offset_min)}M" if offset_min != 0 else "TRIGGER:PT0S",
                "END:VALARM",
            ]

        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def update_ics(calendar_events: list | None) -> None:
    """从 events.json 读取事件（也补入原始日历条目），写出 .ics 文件"""
    # 读取已生成的 events.json
    ev_map: dict = {}
    try:
        data = json.loads(EVENTS_FILE.read_text(encoding="utf-8"))
        for e in data.get("events", []):
            ev_map[e["id"]] = e
    except Exception as e:
        print(f"⚠️ 读取 events.json 失败，仅用日历原始数据: {e}")

    # 补入今天和明天的原始日历条目（防止 Claude 提取失败时这两天空白）
    tomorrow_end = NOW + timedelta(hours=48)
    if calendar_events:
        for ev in calendar_events:
            if ev["time"] >= NOW - timedelta(hours=2) and ev["time"] <= tomorrow_end:
                eid = calendar_event_id(ev)
                if eid not in ev_map:
                    flag = COUNTRY_FLAG.get(ev["country"], "")
                    ev_map[eid] = {
                        "id": eid,
                        "start": ev["time"].isoformat(),
                        "impact": "high" if ev["impact"] == "High" else "medium",
                        "title": f"{flag} {ev['title_en']}",
                        "note": f"{ev['title_en']} | 预期 {ev.get('forecast') or '-'} | 前值 {ev.get('previous') or '-'}",
                        "detail": "",
                        "link": "",
                        "source": "calendar",
                    }

    # 只保留未来 14 天的事件，过期的不写入
    cutoff_past = (NOW - timedelta(hours=2)).isoformat()
    cutoff_future = (NOW + timedelta(days=14)).isoformat()
    events_to_write = [
        e for e in ev_map.values()
        if cutoff_past <= e["start"] <= cutoff_future
    ]
    events_to_write.sort(key=lambda e: e["start"])

    ICS_FILE.parent.mkdir(exist_ok=True)
    ICS_FILE.write_text(build_ics(events_to_write), encoding="utf-8")
    print(f"📅 events.ics 已生成：共 {len(events_to_write)} 个事件")


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
    calendar_events = fetch_calendar_events()
    calendar_today = format_calendar(calendar_events, whole_week=False)

    daily = call_claude(build_daily_prompt(news, prices, calendar_today), max_tokens=6000)
    if daily:
        ARCHIVE_DIR.mkdir(exist_ok=True)
        (ARCHIVE_DIR / f"{TODAY}.md").write_text(daily, encoding="utf-8")
    else:
        daily = fallback_digest(news, prices, calendar_today)

    content = f"# 📰 每日投资简报 · {TODAY} 周{WEEKDAY_CN}\n\n{daily}"
    subject = f"📰 每日投资简报 {TODAY}"

    if IS_MONDAY:
        weekly = call_claude(build_weekly_prompt(load_week_history(), prices,
                                                 format_calendar(calendar_events, whole_week=True)),
                             max_tokens=8000)
        if weekly:
            content += f"\n\n---\n\n# 📅 上周周报\n\n{weekly}"
            subject += "（含周报）"

    send_email(subject, content)

    # 日历事件放在邮件之后生成，失败也不影响当天的邮件
    try:
        update_events_file(calendar_events, news)
        update_ics(calendar_events)
    except Exception as e:
        print(f"⚠️ 生成日历事件失败: {e}")


if __name__ == "__main__":
    main()
