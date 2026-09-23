import feedparser
import requests
import smtplib
from email.mime.text import MIMEText
import os

# ========== 配置 ==========
API_KEY = os.getenv("ANTHROPIC_API_KEY")
API_URL = "https://api.storeapi.one/v1/messages"
MODEL = "claude-3-haiku-20240307"

MAIL_USER = os.getenv("MAIL_USER")
MAIL_PASS = os.getenv("MAIL_PASS")
MAIL_TO = os.getenv("MAIL_TO")

# RSS源，BTC/ETH/XRP/SLO/DOGE + 美股宏观
rss_list = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
]

# ========== 抓取新闻 ==========
news_blocks = []
for url in rss_list:
    feed = feedparser.parse(url)
    for entry in feed.entries[:5]: #每个源取最新5条
        news_blocks.append(f"""标题：{entry.get('title','')}
链接：{entry.get('link','')}
摘要：{entry.get('summary','')}
""")

all_news = "\n".join(news_blocks)

prompt = f"""下面是最新金融加密货币新闻，请筛选BTC、ETH、XRP、SLO、DOGE、美股、宏观政策相关内容，
整理成简洁中文摘要，剔除无关内容，分点列出重点行情与风险提示：
{all_news}
"""

# ========== 调用中转Claude API ==========
headers = {
    "x-api-key": API_KEY,
    "Content-Type": "application/json"
}
payload = {
    "model": MODEL,
    "max_tokens": 1024,
    "messages": [{"role":"user","content": prompt}]
}

resp = requests.post(API_URL, json=payload, headers=headers)
print(f"HTTP状态码：{resp.status_code}")
result_json = resp.json()
print(f"API完整返回：{result_json}")

# 适配两种返回格式：Anthropic原生content格式 / OpenAI兼容choices格式
if "content" in result_json:
    summary_text = result_json["content"][0]["text"]
elif "choices" in result_json:
    summary_text = result_json["choices"][0]["message"]["content"]
else:
    raise Exception(f"API返回异常，没有找到内容字段：{result_json}")

# ========== QQ邮箱SMTP发邮件 ==========
msg = MIMEText(summary_text, "plain", "utf-8")
msg["Subject"] = "每日加密货币&美股新闻摘要"
msg["From"] = MAIL_USER
msg["To"] = MAIL_TO

server = smtplib.SMTP_SSL("smtp.qq.com", 465)
server.login(MAIL_USER, MAIL_PASS)
server.sendmail(MAIL_USER, MAIL_TO, msg.as_string())
server.quit()
print("✅ 任务完成，邮件已发送")
