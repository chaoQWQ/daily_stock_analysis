# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

A股自选股智能分析系统 - 基于 AI 大模型的股票分析工具，每日自动分析并推送「决策仪表盘」到企业微信/邮件。

**技术栈**: Python 3.10+ | Google Gemini API | SQLite | AkShare/Tushare/Baostock

## 常用命令

```bash
# 安装依赖
pip install -r requirements.txt

# 运行分析
python main.py                      # 完整分析（个股+大盘）
python main.py --market-review      # 仅大盘复盘
python main.py --morning            # 早盘策略分析
python main.py --stocks 600519,000001  # 指定股票分析
python main.py --dry-run            # 仅获取数据，不调用AI
python main.py --no-notify          # 不发送推送通知
python main.py --schedule           # 定时任务模式
python main.py --debug              # 调试模式
python main.py --shaofu             # ShaoFu 选股策略
python main.py --shaofu --shaofu-targets 沪深300,中证500  # 指定目标

# Docker 部署
docker-compose up -d
docker-compose logs -f

# 测试环境变量配置
python test_env.py
```

## 架构概览

```
┌─────────────────────────────────────────────────────────────┐
│                     main.py (调度器)                         │
│  StockAnalysisPipeline: 协调各模块完成分析流程                │
└──────────────┬──────────────────────────────────────────────┘
               │
    ┌──────────┼──────────┬──────────────┬────────────────┐
    ▼          ▼          ▼              ▼                ▼
┌────────┐ ┌────────┐ ┌──────────┐ ┌───────────┐ ┌────────────┐
│config  │ │storage │ │data_     │ │analyzer   │ │notification│
│.py     │ │.py     │ │provider/ │ │.py        │ │.py         │
│        │ │        │ │          │ │           │ │            │
│单例配置│ │SQLite  │ │多数据源  │ │Gemini AI  │ │企业微信    │
│管理    │ │ORM     │ │策略模式  │ │决策仪表盘 │ │邮件推送    │
└────────┘ └────────┘ └──────────┘ └───────────┘ └────────────┘
                           │
          ┌────────────────┼────────────────┐
          ▼                ▼                ▼
    ┌──────────┐    ┌──────────┐    ┌──────────┐
    │akshare   │    │tushare   │    │baostock  │
    │_fetcher  │    │_fetcher  │    │_fetcher  │
    │(Primary) │    │(Backup1) │    │(Backup2) │
    └──────────┘    └──────────┘    └──────────┘
```

### 核心模块职责

| 模块 | 职责 |
|------|------|
| `main.py` | 主调度器，协调数据获取→分析→推送流程，线程池并发控制 |
| `config.py` | 单例模式配置管理，从 `.env` 加载敏感配置 |
| `analyzer.py` | Gemini AI 分析器，生成决策仪表盘格式报告 |
| `market_analyzer.py` | 大盘复盘分析，早盘策略推演 |
| `stock_analyzer.py` | 趋势分析器，MA均线排列/乖离率/量能判断 |
| `search_service.py` | 新闻搜索服务 (Tavily/SerpAPI) |
| `notification.py` | 消息推送 (企业微信 Webhook / 邮件) |
| `storage.py` | SQLAlchemy ORM，SQLite 数据持久化 |
| `data_provider/` | 策略模式管理多数据源，自动故障切换 |

### 数据源优先级

1. **AkShare** (Primary) - 东方财富爬虫，免费
2. **Tushare** (Backup1) - 需要 Token
3. **Baostock** (Backup2) - 证券宝
4. **YFinance** (Fallback) - Yahoo Finance

## 交易理念（已融入 AI Prompt）

- **严禁追高**: 乖离率 > 5% 自动标记「危险」
- **趋势交易**: 只做 MA5 > MA10 > MA20 多头排列
- **买点偏好**: 缩量回踩 MA5/MA10 支撑
- **风险排查**: 减持公告、业绩预亏、监管处罚

## 环境变量配置

关键配置项（在 `.env` 中设置）：

| 变量 | 说明 | 必填 |
|------|------|:----:|
| `GEMINI_API_KEY` | Google AI Studio API Key | ✅ |
| `STOCK_LIST` | 自选股代码，逗号分隔 (如 `600519,300750`) | ✅ |
| `WECHAT_WEBHOOK_URL` | 企业微信机器人 Webhook | 推荐 |
| `TAVILY_API_KEYS` | Tavily 搜索 API Key | 推荐 |
| `GEMINI_MODEL` | 主模型 (默认 `gemini-3-flash-preview`) | - |
| `GEMINI_MODEL_FALLBACK` | 备选模型 (默认 `gemini-2.5-flash`) | - |

## GitHub Actions

- **Workflow**: `.github/workflows/daily_analysis.yml`
- **定时**: 周一至周五 18:00 (北京时间) 自动执行
- **手动触发**: Actions → 每日股票分析 → Run workflow
- **运行模式**: `full` / `market-only` / `stocks-only`

## 代码规范

- 遵循 PEP 8
- 函数和类添加 docstring
- 使用 `logging` 模块记录日志，禁止 `print`
- 敏感配置使用环境变量，禁止硬编码

## 注意事项

1. **API 限流**: Gemini API 有速率限制，代码已实现重试和模型切换机制
2. **数据源防封**: `max_workers` 默认为 3，避免并发过高触发反爬
3. **时区**: 所有时间使用北京时间 (UTC+8)
4. **报告输出**: 保存到 `reports/` 目录，日志保存到 `logs/` 目录
