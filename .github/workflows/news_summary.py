import feedparser
import anthropic
import os
import datetime

# ========== RSS源：美国/日本新闻 + 加密货币 + 海外投资财经 ==========
rss_list = [
    # 日本新闻（共同网中文，日本时政经济）
    "https://rsshub.app/kyodonews/china",
    # 美国国际时政新闻
    "https://rsshub.app/bbc/zh/international",
    # 海外财经、美股投资新闻
    "https://rsshub.app/finviz/news/SPY",
    # 加密货币 金色财经快讯
    "https://rsshub.app/jinse/timeline",
]

# 抓取RSS新闻
news_content = ""
for rss_url in rss_list:
    try:
        feed = feedparser.parse(rss_url)
        # 每个源取最新4条，防止内容太多token爆炸
        for entry in feed.entries[:4]:
            news_content += f"【来源】{feed.feed.get('title','未知来源')}\n标题：{entry.title}\n摘要：{entry.summary}\n\n"
    except Exception as e:
        print(f"读取RSS失败 {rss_url}: {e}")

# 调用Claude Haiku总结新闻
client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
prompt = f"""
下面是今日新闻，包含：美国时政、日本时政、海外投资市场、加密货币资讯。
请帮我提炼核心重点，精简总结，分大类整理：
1. 美国相关新闻
2. 日本相关新闻
3. 投资/美股市场
4. 加密货币资讯

要求：文字简短，去除废话，只保留有价值信息，适合手机邮箱快速阅读。
新闻原文：
{news_content}
"""

resp = client.messages.create(
    model="claude-3-haiku-20240307",
    max_tokens=1200,
    messages=[{"role":"user","content":prompt}]
)
result_text = resp.content[0].text

# 保存简报到md文件
today = datetime.date.today().strftime("%Y-%m-%d")
with open("news_result.md","w",encoding="utf-8") as f:
    f.write(f"# 📰 {today} 海外新闻&投资简报\n\n")
    f.write(result_text)

# 设置环境变量，给邮件使用
os.environ["TODAY_DATE"] = today
print("✅ 新闻简报生成完成")
