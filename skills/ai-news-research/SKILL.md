---
name: ai-news-digest
description: 抓取 Hugging Face Daily Papers、r/LocalLLaMA 及 The Batch、Import AI、Interconnects、Latent Space、Simon Willison 博客，生成中文 AI 前沿日报或周报摘要。
---

# AI 前沿摘要（日报 / 周报）

从 7 个固定来源抓取最新内容，去重合并后写成一份中文摘要。

## 1. 确定模式和时间窗口

先获取当前日期（用时间工具或 `date`，不要凭记忆）。

| 模式 | 触发说法 | HF Papers / Reddit | Newsletter / 博客 |
|---|---|---|---|
| 日报（默认） | “今天”“日报”“最新” | 过去 24 小时 | 过去 48 小时内发布的 |
| 周报 | “本周”“周报”“这周” | 过去 7 天 | 过去 7 天内发布的 |

用户指定了其他时间范围就按用户的来。

## 2. 数据源

所有来源互不依赖，**同一轮并行抓取**。每个源先用首选地址，失败再用备用地址；都失败就跳过，并在摘要末尾注明。

| 来源 | 首选地址 | 备用地址 | 频率 |
|---|---|---|---|
| HF Daily Papers | `https://huggingface.co/api/daily_papers`（JSON；可加 `?date=YYYY-MM-DD`） | `https://huggingface.co/papers` 页面 | 工作日每天 |
| r/LocalLLaMA | `https://www.reddit.com/r/LocalLLaMA/top/.json?t=day`（周报用 `t=week`） | `https://old.reddit.com/r/LocalLLaMA/top/.rss?t=day` | 实时 |
| The Batch | `https://www.deeplearning.ai/the-batch/` 列表页 | 搜索 “The Batch deeplearning.ai” + 当月 | 每周 |
| Import AI | `https://importai.substack.com/feed` | `https://jack-clark.net/` | 每周 |
| Interconnects | `https://www.interconnects.ai/feed` | `https://www.interconnects.ai/archive` | 不定期 |
| Latent Space | `https://www.latent.space/feed` | `https://www.latent.space/archive` | 每周数次（含播客） |
| Simon Willison | `https://simonwillison.net/atom/everything/` | `https://simonwillison.net/` | 几乎每天 |

抓取工具：优先用 WebFetch；有不受限的 shell 网络时可以用 curl（Reddit 需带 User-Agent 头）。不要用镜像、缓存站绕过被拒绝的站点。

## 3. 各来源的处理方法

**HF Daily Papers**
- JSON 每项含 `paper.id`（arXiv 编号）、`paper.title`、`paper.summary`、`paper.upvotes`。
- 按 upvotes 取前 8–10 篇。周末/节假日当天为空时，取最近一个有论文的日期，并注明日期。
- 链接用 `https://huggingface.co/papers/{id}`。
- 每篇用 1–2 句话说清：做了什么、关键结果或新意。

**r/LocalLLaMA**
- 取 `data.children[].data` 的 `title`、`score`、`num_comments`、`permalink`、`link_flair_text`、`url`。
- 按 score 取前 10，跳过 flair 为 Funny / meme 类的帖子。
- 重点关注：新模型发布、量化与本地部署、基准测试、推理框架更新。
- 帖子外链到模型页或博客时，以外链内容为准概括。

**Newsletter / 博客（后 5 个）**
- 解析 feed 里的发布时间，只保留时间窗口内的条目。
- 对保留的条目打开原文页面阅读，提炼 2–4 个要点。
- Simon Willison 更新多且短：只挑与新模型、新工具、重要观点相关的，合并成一段。
- Latent Space 的播客条目按节目简介概括嘉宾和主题。
- 窗口内没有新内容就写“本期无更新”，不要拿旧文章凑数。

## 4. 去重与合并

同一件事（例如某个模型发布）常同时出现在 HF、Reddit 和多个 newsletter 中：合并为一条，列出所有来源链接，并把它优先放进“要点”。

## 5. 输出格式

```markdown
# AI 前沿日报 · YYYY-MM-DD   （周报写：AI 前沿周报 · MM/DD–MM/DD）

## 今日要点
3–5 条最重要的事，每条一句话 + 链接。跨来源重复出现的事优先。

## 论文精选（Hugging Face Daily Papers）
1. **论文标题**（👍 upvotes）— 一两句中文概括。[链接]

## 社区热议（r/LocalLLaMA）
- **帖子标题**（⬆ score · 💬 评论数）— 一句概括讨论的核心。[链接]

## Newsletter 与博客
### The Batch
- 要点…
### Import AI
### Interconnects
### Latent Space
### Simon Willison

---
未能抓取：列出失败的来源及原因（没有就省略这一行）
```

## 6. 写作规则

- 全文中文，模型名、论文名、术语保留英文原文。
- 用自己的话概括，不要大段翻译原文；如需引用，单处不超过 15 个英文词，每个来源最多一处。
- 每条都附原文链接，方便读者点进去看。
- 只写抓到的内容里有的信息；不确定的数字、日期不写。
- 不加主观的“震撼”“炸裂”类修饰，语气平实。
- 日报控制在 800–1500 字，周报 1500–3000 字。

## 7. 交付

- 默认把摘要直接发在对话里。
- 用户要求保存时，存为 `ai-digest-YYYY-MM-DD.md`。
- 用户想每天/每周自动收到时，建议设成定时任务，任务指令写成：“使用 ai-news-digest skill 生成今天的 AI 前沿日报”。
