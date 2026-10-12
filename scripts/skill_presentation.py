"""Concise local presentation; original skill metadata is never rewritten.

Rules only summarize descriptions with an explicit matching subject. Unknown
descriptions retain their first sentence rather than assigning invented abilities.
"""
import re
import hashlib
import json
from pathlib import Path

try:
    CATALOG = json.loads((Path(__file__).resolve().parents[1] / 'references' / 'purpose-zh.json').read_text(encoding='utf-8'))
except (OSError, ValueError):
    CATALOG = {}

EXACT = {
    'ffmpeg-editor': '通过 FFmpeg 剪辑、转码和处理音视频',
    'a-stock-analysis': '分析 A 股行情与股票信息',
    '10x-chat': '通过浏览器调用多个 AI，交叉核对和协作完成任务',
    '1password': '使用 1Password CLI 读取和注入密钥',
    'ai-news-collector': '收集 AI 资讯，整理每日选题素材',
    'ai-topic-selector': '筛选和评分 AI 资讯，生成创作选题',
    'code': '规划、编写和验证代码',
    'memory': '分类保存和检索长期记忆',
    'github-skill-publisher': '打包、检查并发布 Skill 到 GitHub',
    'agent-browser': '操作网页，填写表单、截图和提取内容',
    'ai-image-generator': '生成、编辑和调整 AI 图片',
    'academic-image-to-vba': '把参考图片重建为可编辑 PPT 或 Office VBA',
    'auto-video-maker': '把素材、配音和字幕组合成竖屏短视频',
    'author-style-system': '提炼作者写作风格，并用于新文章',
    'architecture-designer': '设计系统架构，评估技术方案',
    'automation-workflows': '设计和实现自动化工作流',
    'archive': '归档会话经验、调试结果和部署记录',
    'algorithmic-art': '使用 p5.js 创作可调参数的生成艺术',
    'skill-manager': '扫描、安装、更新和整理本机 Skills',
    'skill-creator': '创建和改进可复用的 Agent Skill',
    'skill-installer': '从仓库安装 Agent Skills',
    'banner-creator': '生成并调整横幅、封面和头图',
}

# Each subject must be present in the source description (not just the name).
SUBJECTS = [
    ('amazon competitor', '分析亚马逊竞品的商品、价格和策略'),
    ('amazon fba', '计算亚马逊 FBA 费用和利润'),
    ('amazon keyword', '研究亚马逊关键词和市场机会'),
    ('amazon product', '研究亚马逊商品需求和市场机会'),
    ('amazon profit', '分析亚马逊成本和利润'),
    ('amazon review', '分析亚马逊评论和用户需求'),
    ('amazon sales', '估算亚马逊商品销量和收入'),
    ('market research', '研究市场规模、竞品和用户需求'),
    ('seo', '分析和优化网站的搜索引擎表现'),
    ('social media', '规划和制作社交媒体内容'),
    ('backtest', '回测和验证量化交易策略'),
    ('acquisition channel', '评估获客渠道的成本、质量和扩张潜力'),
    ('unit economics', '分析单位经济模型和盈利能力'),
    ('product requirement', '编写和评估产品需求文档'),
    ('prd', '编写和评估产品需求文档'),
    ('cohort', '分析用户群组的留存和行为'),
    ('pricing strateg', '设计和评估产品定价策略'),
    ('go-to-market', '规划产品上市和推广策略'),
    ('competitor', '研究竞品，比较产品和市场策略'),
    ('customer journey', '梳理客户旅程和各阶段体验'),
    ('customer interview', '设计用户访谈，了解需求'),
    ('user interview', '设计用户访谈，了解需求'),
    ('sprint', '规划迭代任务和交付节奏'),
    ('roadmap', '规划和调整产品路线图'),
    ('retrospective', '复盘已完成工作，整理改进项'),
    ('feature priorit', '评估功能价值并安排开发优先级'),
    ('product strateg', '制定产品策略和发展方向'),
    ('business health', '诊断业务表现和增长问题'),
    ('leadership', '评估管理能力和团队协作问题'),
    ('ai maturity', '评估产品工作的 AI 成熟度'),
    ('ai-first', '评估产品工作的 AI 成熟度'),
    ('pm-to-director', '分析产品经理转向管理岗位的能力变化'),
    ('image generation', '生成和调整图片'),
    ('generates images', '生成和调整图片'),
    ('compresses images', '压缩和优化图片文件'),
    ('cover image', '生成文章封面图片'),
    ('illustrat', '分析内容并制作配图'),
    ('comic', '制作知识漫画和分镜'),
    ('infographic', '把内容整理成信息图'),
    ('diagram', '绘制结构图和流程图'),
    ('powerpoint', '制作或编辑 PowerPoint 演示文稿'),
    ('pptx', '制作或编辑 PPT 演示文稿'),
    ('presentation', '制作和调整演示文稿'),
    ('spreadsheet', '读取、编辑和分析电子表格'),
    ('word document', '创建和编辑 Word 文档'),
    ('pdf', '读取和处理 PDF 文档'),
    ('transcri', '将音视频转为文字'),
    ('subtitle', '生成和处理视频字幕'),
    ('video edit', '剪辑和处理视频'),
    ('video generation', '生成视频内容'),
    ('text-to-speech', '把文字转换为语音'),
    ('audio', '处理音频内容'),
    ('web search', '搜索网页并整理资料'),
    ('academic', '检索和整理学术资料'),
    ('web scraping', '抓取和提取网页内容'),
    ('browser automation', '自动操作浏览器和网页'),
    ('notebooklm', '通过 NotebookLM 整理资料和生成内容'),
    ('feishu', '管理飞书文档和内容'),
    ('wechat', '制作和发布微信内容'),
    ('xiaohongshu', '制作和管理小红书内容'),
    ('youtube', '分析和管理 YouTube 内容'),
    ('email', '编写和管理邮件内容'),
    ('content calendar', '规划内容日历和发布节奏'),
    ('blog', '撰写和优化博客文章'),
    ('copywriting', '撰写和优化营销文案'),
    ('article', '撰写、编辑和整理文章'),
    ('writing', '撰写和改进文字内容'),
    ('debug', '排查程序问题并定位原因'),
    ('software testing', '设计和执行软件验证'),
    ('code review', '审查代码并查找问题'),
    ('security', '检查安全风险和防护措施'),
    ('git repository', '管理代码仓库和版本变更'),
    ('database', '设计和管理数据库'),
    ('api', '接入和使用 API 服务'),
    ('frontend', '构建和调整前端界面'),
    ('data analysis', '分析数据并整理结论'),
    ('dashboard', '制作数据看板'),
    ('workflow', '编排并执行任务工作流'),
]


def concise(description, name='', metadata=None, override=None):
    text = re.sub(r'\s+', ' ', str(description or '')).strip()
    if override:
        return {'text': override, 'basis': 'user'}
    meta = metadata or {}
    for key in ('short_description', 'display_description'):
        value = meta.get(key)
        if isinstance(value, str) and value.strip():
            return {'text': value.strip()[:80], 'basis': 'metadata'}
    if not text or re.fullmatch(r'[>|][+-]?', text):
        return {'text': '简介待补充', 'basis': 'missing'}
    curated = CATALOG.get(hashlib.sha256((name + '\0' + str(description or '')).encode()).hexdigest())
    if curated:
        return {'text': curated['text'], 'basis': 'curated'}
    key = name.lower()
    if key in EXACT:
        return {'text': EXACT[key], 'basis': 'local-rule'}
    if re.search(r'[\u4e00-\u9fff]', text[:120]):
        first = re.split(r'[。！\n]|[；;]', text, maxsplit=1)[0]
        if len(first) > 52:
            parts = re.split(r'[，,]', first)
            first = parts[0] if 6 <= len(parts[0]) <= 52 else first[:51] + '…'
        return {'text': first, 'basis': 'original-excerpt'}
    # Restrict category matching to the leading purpose clause, ignoring trigger
    # examples that can mention abilities the skill itself does not provide.
    lead = re.split(r'\b(?:use when|use this when|triggers?|not for)\b', text, maxsplit=1, flags=re.I)[0][:280].lower()
    for subject, summary in SUBJECTS:
        if re.search(r'(?<![a-z])' + re.escape(subject), lead):
            return {'text': summary, 'basis': 'local-rule'}
    first = re.split(r'(?<=[.!?])\s', text, maxsplit=1)[0]
    if len(first) > 110:
        first = first[:107].rsplit(' ', 1)[0] + '…'
    return {'text': first, 'basis': 'original-excerpt'}
