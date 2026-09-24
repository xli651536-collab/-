#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日新闻摘要自动化脚本
- 抓取美国、日本、加密投资类RSS新闻
- 筛选过去24小时内的新闻
- 周一额外增加上周会议与事件汇总
- 调用Claude API生成中文摘要
- 通过SMTP发送到多个邮箱
"""

import os
import sys
import json
import smtplib
import feedparser
import requests
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from dateutil import parser as date_parser
import pytz
from typing import List, Dict, Optional


# ==================== 配置 ====================

# RSS 订阅源
RSS_FEEDS = {
    "美国新闻": [
        "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
        "https://feeds.washingtonpost.com/rss/world",
        "https://www.reuters.com/rssfeed/worldNews",
    ],
    "日本新闻": [
        "https://www3.nhk.or.jp/rss/news/cat0.xml",
        "https://www.japantimes.co.jp/feed/",
    ],
    "加密投资": [
        "https://cointelegraph.com/rss",
        "https://decrypt.co/feed",
        "https://www.coindesk.com/arc/outboundfeeds/rss/",
        "https://cryptonews.com/news/feed/",
    ],
}

# 从环境变量读取配置（使用你已有的 Secrets 名称）
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MAIL_USER = os.environ.get("MAIL_USER", "")
MAIL_PASS = os.environ.get("MAIL_PASS", "")
MAIL_TO = os.environ.get("MAIL_TO", "")

# 解析收件人列表（支持逗号分隔）
RECIPIENT_EMAILS = [email.strip() for email in MAIL_TO.split(",") if email.strip()]

# SMTP 配置（根据发件邮箱自动判断）
def get_smtp_config(email: str):
    """根据邮箱地址自动判断SMTP配置"""
    email_lower = email.lower()
    if "gmail.com" in email_lower:
        return "smtp.gmail.com", 587
    elif "outlook.com" in email_lower or "hotmail.com" in email_lower:
        return "smtp-mail.outlook.com", 587
    elif "qq.com" in email_lower:
        return "smtp.qq.com", 587
    elif "163.com" in email_lower:
        return "smtp.163.com", 465
    elif "126.com" in email_lower:
        return "smtp.126.com", 465
    else:
        # 默认使用Gmail配置
        return "smtp.gmail.com", 587

SMTP_HOST, SMTP_PORT = get_smtp_config(MAIL_USER)

# 时区设置
BEIJING_TZ = pytz.timezone("Asia/Shanghai")


# ==================== RSS 抓取 ====================

def fetch_rss_entries(feed_url: str, hours: int = 24) -> List[Dict]:
    """
    抓取单个RSS源的条目，筛选指定小时内的新闻
    """
    try:
        feed = feedparser.parse(feed_url, timeout=10)
        if feed.bozo:
            print(f"⚠️  RSS解析警告 [{feed_url}]: {feed.bozo_exception}")

        cutoff_time = datetime.now(pytz.UTC) - timedelta(hours=hours)
        entries = []

        for entry in feed.entries[:50]:  # 只取前50条
            try:
                # 尝试解析发布时间
                pub_date = None
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    pub_date = datetime(*entry.published_parsed[:6], tzinfo=pytz.UTC)
                elif hasattr(entry, "updated_parsed") and entry.updated_parsed:
                    pub_date = datetime(*entry.updated_parsed[:6], tzinfo=pytz.UTC)
                elif hasattr(entry, "published"):
                    pub_date = date_parser.parse(entry.published)
                    if pub_date.tzinfo is None:
                        pub_date = pytz.UTC.localize(pub_date)

                # 筛选时间范围
                if pub_date and pub_date >= cutoff_time:
                    entries.append({
                        "title": entry.get("title", "无标题"),
                        "link": entry.get("link", ""),
                        "summary": entry.get("summary", entry.get("description", "")),
                        "published": pub_date.astimezone(BEIJING_TZ).strftime("%Y-%m-%d %H:%M"),
                        "source": feed.feed.get("title", feed_url),
                    })
            except Exception as e:
                print(f"⚠️  解析条目失败: {e}")
                continue

        return entries

    except Exception as e:
        print(f"❌ RSS抓取失败 [{feed_url}]: {e}")
        return []


def fetch_all_news(hours: int = 24) -> Dict[str, List[Dict]]:
    """
    抓取所有分类的新闻
    """
    all_news = {}

    for category, feed_urls in RSS_FEEDS.items():
        print(f"\n📰 抓取 {category}...")
        category_entries = []

        for feed_url in feed_urls:
            entries = fetch_rss_entries(feed_url, hours)
            category_entries.extend(entries)
            print(f"   ✓ {feed_url}: {len(entries)} 条")

        all_news[category] = category_entries
        print(f"   📊 {category} 合计: {len(category_entries)} 条")

    return all_news


# ==================== Claude API 调用 ====================

def call_claude_api(prompt: str, max_tokens: int = 4000) -> Optional[str]:
    """
    调用 Claude API 生成摘要
    """
    try:
        headers = {
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
        }

        payload = {
            "model": "claude-3-5-sonnet-20241022",
            "max_tokens": max_tokens,
            "temperature": 0.7,
            "messages": [
                {
                    "role": "user",
                    "content": prompt
                }
            ]
        }

        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers=headers,
            json=payload,
            timeout=60
        )
        response.raise_for_status()

        result = response.json()
        return result["content"][0]["text"]

    except requests.exceptions.RequestException as e:
        print(f"❌ Claude API 调用失败: {e}")
        if hasattr(e, "response") and e.response is not None:
            print(f"   响应内容: {e.response.text}")
        return None


def generate_news_summary(news_data: Dict[str, List[Dict]], is_monday: bool = False) -> str:
    """
    生成新闻摘要
    """
    # 构建提示词
    prompt_parts = [
        "你是一位专业的新闻编辑，请根据以下新闻内容生成一份结构清晰的**中文摘要报告**。\n",
        "## 要求：",
        "1. 按「美国新闻」「日本新闻」「加密投资」三个板块分别总结",
        "2. 每个板块提炼 3-5 条最重要的新闻，用简洁的语言概括",
        "3. 保留新闻标题和关键信息，附上原文链接",
        "4. 使用Markdown格式，排版清晰\n",
    ]

    if is_monday:
        prompt_parts.append("5. **今天是周一**，请在报告末尾额外增加「上周重要事件回顾」板块，总结上周的重大会议、政策发布、市场动态等\n")

    prompt_parts.append("\n---\n\n## 原始新闻数据：\n")

    for category, entries in news_data.items():
        if not entries:
            continue

        prompt_parts.append(f"\n### {category} ({len(entries)} 条)：\n")
        for idx, entry in enumerate(entries[:20], 1):  # 每个分类最多20条
            prompt_parts.append(
                f"{idx}. **{entry['title']}**\n"
                f"   - 来源: {entry['source']}\n"
                f"   - 时间: {entry['published']}\n"
                f"   - 链接: {entry['link']}\n"
                f"   - 摘要: {entry['summary'][:200]}...\n\n"
            )

    prompt = "".join(prompt_parts)

    print("\n🤖 调用 Claude API 生成摘要...")
    summary = call_claude_api(prompt)

    if summary:
        print("✅ 摘要生成成功")
        return summary
    else:
        print("❌ 摘要生成失败，返回原始数据")
        return "⚠️ Claude API 调用失败，以下是原始新闻数据：\n\n" + prompt


# ==================== 邮件发送 ====================

def send_email(subject: str, body: str, recipients: List[str]) -> bool:
    """
    通过 SMTP 发送邮件
    """
    try:
        # 创建邮件
        msg = MIMEMultipart("alternative")
        msg["From"] = MAIL_USER
        msg["To"] = ", ".join(recipients)
        msg["Subject"] = subject

        # HTML 格式邮件（支持Markdown渲染）
        html_body = f"""
        <html>
        <head>
            <meta charset="utf-8">
            <style>
                body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; line-height: 1.6; color: #333; max-width: 800px; margin: 0 auto; padding: 20px; background: #f9fafb; }}
                .container {{ background: white; padding: 32px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
                h2 {{ color: #2563eb; border-bottom: 2px solid #e5e7eb; padding-bottom: 8px; margin-top: 24px; }}
                h3 {{ color: #4b5563; margin-top: 20px; }}
                a {{ color: #2563eb; text-decoration: none; }}
                a:hover {{ text-decoration: underline; }}
                pre {{ background: #f3f4f6; padding: 16px; border-radius: 6px; overflow-x: auto; white-space: pre-wrap; word-wrap: break-word; }}
                .footer {{ margin-top: 32px; padding-top: 16px; border-top: 1px solid #e5e7eb; color: #6b7280; font-size: 14px; text-align: center; }}
            </style>
        </head>
        <body>
            <div class="container">
                <pre>{body}</pre>
                <div class="footer">
                    📧 此邮件由 GitHub Actions 自动发送 | ⏰ 每日 08:20 定时推送
                </div>
            </div>
        </body>
        </html>
        """

        msg.attach(MIMEText(body, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        # 发送邮件
        print(f"\n📧 连接 SMTP 服务器 {SMTP_HOST}:{SMTP_PORT}...")
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.set_debuglevel(0)  # 设置为1可以看到详细调试信息
            server.starttls()
            server.login(MAIL_USER, MAIL_PASS)
            server.send_message(msg)

        print(f"✅ 邮件发送成功！收件人: {', '.join(recipients)}")
        return True

    except Exception as e:
        print(f"❌ 邮件发送失败: {e}")
        import traceback
        traceback.print_exc()
        return False


# ==================== 主流程 ====================

def main():
    """
    主函数
    """
    print("=" * 60)
    print("📰 每日新闻摘要自动化脚本启动")
    print("=" * 60)

    # 检查必要的环境变量
    missing_configs = []
    if not ANTHROPIC_API_KEY:
        missing_configs.append("ANTHROPIC_API_KEY")
    if not MAIL_USER:
        missing_configs.append("MAIL_USER")
    if not MAIL_PASS:
        missing_configs.append("MAIL_PASS")
    if not MAIL_TO:
        missing_configs.append("MAIL_TO")

    if missing_configs:
        print(f"❌ 错误: 缺少以下必要的 GitHub Secrets 配置:")
        for config in missing_configs:
            print(f"   - {config}")
        sys.exit(1)

    print(f"\n✅ 配置检查通过:")
    print(f"   - API Key: {ANTHROPIC_API_KEY[:20]}...")
    print(f"   - 发件邮箱: {MAIL_USER}")
    print(f"   - SMTP服务器: {SMTP_HOST}:{SMTP_PORT}")
    print(f"   - 收件人: {', '.join(RECIPIENT_EMAILS)}")

    # 获取当前时间（北京时间）
    now_beijing = datetime.now(BEIJING_TZ)
    is_monday = now_beijing.weekday() == 0

    print(f"\n⏰ 当前时间: {now_beijing.strftime('%Y-%m-%d %H:%M:%S')} (周{'一' if is_monday else '二三四五六日'[now_beijing.weekday()]})")
    print(f"📅 是否周一: {'是，将生成上周汇总' if is_monday else '否'}")

    try:
        # 1. 抓取新闻
        print("\n" + "=" * 60)
        print("步骤 1/3: 抓取 RSS 新闻")
        print("=" * 60)
        news_data = fetch_all_news(hours=24)

        total_count = sum(len(entries) for entries in news_data.values())
        print(f"\n📊 总计抓取: {total_count} 条新闻")

        if total_count == 0:
            print("⚠️  警告: 未抓取到任何新闻，可能RSS源失效或网络问题")

        # 2. 生成摘要
        print("\n" + "=" * 60)
        print("步骤 2/3: 生成 AI 摘要")
        print("=" * 60)
        summary = generate_news_summary(news_data, is_monday)

        # 3. 发送邮件
        print("\n" + "=" * 60)
        print("步骤 3/3: 发送邮件")
        print("=" * 60)

        subject = f"📰 每日新闻摘要 - {now_beijing.strftime('%Y年%m月%d日')}"
        if is_monday:
            subject += " (含上周汇总)"

        success = send_email(subject, summary, RECIPIENT_EMAILS)

        if success:
            print("\n" + "=" * 60)
            print("✅ 任务执行成功！")
            print("=" * 60)
            sys.exit(0)
        else:
            print("\n" + "=" * 60)
            print("❌ 任务执行失败（邮件发送失败）")
            print("=" * 60)
            sys.exit(1)

    except Exception as e:
        print(f"\n❌ 发生未预期的错误: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()

主要调整说明：

✅ 适配你的 Secrets 配置：

1. 环境变量名称：
   - ANTHROPIC_API_KEY（你的）← 之前是 CLAUDE_API_KEY
   - MAIL_USER（你的）← 之前是 SMTP_USER
   - MAIL_PASS（你的）← 之前是 SMTP_PASSWORD
   - MAIL_TO（你的）← 之前是 RECIPIENT_EMAILS
2. 自动识别 SMTP 服务器：
   - 根据 MAIL_USER 的邮箱域名自动判断 SMTP 配置
   - 支持 Gmail、Outlook、QQ、163、126 邮箱
   - 不需要额外配置 SMTP_HOST 和 SMTP_PORT
3. 多收件人支持：
        print("步骤 3/3: 发送邮件")
        print("=" * 60)

        subject = f"📰 每日新闻摘要 - {now_beijing.strftime('%Y年%m月%d日')}"
        if is_monday:
            subject += " (含上周汇总)"

        success = send_email(subject, summary, RECIPIENT_EMAILS)

        if success:
            print("\n" + "=" * 60)
            print("✅ 任务执行成功！")
            print("=" * 60)
            sys.exit(0)
        else:
            print("\n" + "=" * 60)
            print("❌ 任务执行失败（邮件发送失败）")
            print("=" * 60)
            sys.exit(1)

    except Exception as e:
        print(f"\n❌ 发生未预期的错误: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
   - ANTHROPIC_API_KEY（你的）← 之前是 CLAUDE_API_KEY
   - MAIL_USER（你的）← 之前是 SMTP_USER
   - MAIL_PASS（你的）← 之前是 SMTP_PASSWORD
   - MAIL_TO（你的）← 之前是 RECI
2. 点击 Add file → Create new fil
