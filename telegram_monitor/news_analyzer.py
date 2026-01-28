# -*- coding: utf-8 -*-
"""
===================================
Telegram 消息 AI 分析器
===================================

职责：
1. 调用 Gemini AI 分析消息对 A股 的影响
2. 输出结构化的影响评估结果
"""

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any

from config import get_config

logger = logging.getLogger(__name__)


@dataclass
class NewsImpactResult:
    """
    新闻影响分析结果

    AI 分析后的结构化输出
    """
    summary: str  # 一句话总结
    impact_direction: str  # 利好/利空/中性
    impact_magnitude: int  # 影响程度 1-10
    affected_sectors: List[str] = field(default_factory=list)  # 受影响板块
    affected_stocks: List[str] = field(default_factory=list)  # 受影响个股（代码）
    action_suggestion: str = ""  # 操作建议
    confidence: float = 0.0  # 置信度 0-1
    reasoning: str = ""  # 分析理由
    raw_response: Optional[str] = None  # 原始响应
    success: bool = True
    error_message: Optional[str] = None


@dataclass
class BatchAnalysisResult:
    """批量分析结果"""
    items: List[Dict[str, Any]] = field(default_factory=list)  # 每条消息的分析结果
    total_count: int = 0
    valuable_count: int = 0
    raw_response: Optional[str] = None
    success: bool = True
    error_message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'summary': self.summary,
            'impact_direction': self.impact_direction,
            'impact_magnitude': self.impact_magnitude,
            'affected_sectors': self.affected_sectors,
            'affected_stocks': self.affected_stocks,
            'action_suggestion': self.action_suggestion,
            'confidence': self.confidence,
            'reasoning': self.reasoning,
            'success': self.success,
            'error_message': self.error_message,
        }

    def get_impact_emoji(self) -> str:
        """获取影响方向 emoji"""
        emoji_map = {
            '利好': '🟢',
            '利空': '🔴',
            '中性': '⚪',
        }
        return emoji_map.get(self.impact_direction, '⚪')

    def get_magnitude_bar(self) -> str:
        """获取影响程度进度条"""
        filled = '█' * self.impact_magnitude
        empty = '░' * (10 - self.impact_magnitude)
        return f"[{filled}{empty}]"

    def format_notification(self) -> str:
        """格式化为推送消息"""
        emoji = self.get_impact_emoji()
        bar = self.get_magnitude_bar()

        lines = [
            f"{emoji} **{self.summary}**",
            f"",
            f"📊 影响程度: {bar} {self.impact_magnitude}/10",
            f"📈 影响方向: {self.impact_direction}",
        ]

        if self.affected_sectors:
            lines.append(f"🏷️ 受影响板块: {', '.join(self.affected_sectors[:5])}")

        if self.action_suggestion:
            lines.append(f"💡 建议: {self.action_suggestion}")

        return '\n'.join(lines)


class TelegramNewsAnalyzer:
    """
    Telegram 消息 AI 分析器

    使用 Gemini 分析消息对 A股 的潜在影响

    使用方式：
        analyzer = TelegramNewsAnalyzer()
        result = analyzer.analyze("央行宣布降准0.5个百分点")
    """

    SYSTEM_PROMPT = """你是一位专注于全球宏观分析的 A股 投资顾问。

你的任务是分析新闻/消息对 A股 市场的潜在影响。

## 分析框架

1. **快速判断**：这条消息对 A股 有影响吗？影响有多大？
2. **方向判断**：利好还是利空？
3. **板块定位**：哪些板块/行业会受到影响？
4. **操作建议**：投资者应该如何应对？

## 输出格式

请严格按照以下 JSON 格式输出：

```json
{
    "summary": "一句话总结（30字以内）",
    "impact_direction": "利好/利空/中性",
    "impact_magnitude": 1-10的整数,
    "affected_sectors": ["板块1", "板块2", "板块3"],
    "affected_stocks": ["股票代码1", "股票代码2"],
    "action_suggestion": "简短的操作建议",
    "confidence": 0.0-1.0的置信度,
    "reasoning": "分析理由（50字以内）"
}
```

## 评分标准

- **10分**：重大政策变化（降准降息、重大改革）
- **8-9分**：行业重大利好/利空（新政策、大合同）
- **6-7分**：宏观数据超预期、国际市场联动
- **4-5分**：行业一般性消息
- **1-3分**：轻微影响或间接影响

## 注意事项

1. 如果消息与 A股 无关，impact_magnitude 设为 0
2. 只输出 JSON，不要有其他文字
3. affected_stocks 填写股票代码（如 600519），最多5个
4. 保持客观，不夸大影响"""

    def __init__(self, api_key: Optional[str] = None):
        """
        初始化分析器

        Args:
            api_key: Gemini API Key（可选，默认从配置读取）
        """
        self._api_key = api_key or get_config().gemini_api_key
        self._model = None

        if self._api_key:
            self._init_model()
        else:
            logger.warning("Gemini API Key 未配置，AI 分析功能将不可用")

    def _init_model(self):
        """初始化 Gemini 模型"""
        try:
            import google.generativeai as genai

            genai.configure(api_key=self._api_key)

            config = get_config()
            model_name = config.gemini_model

            self._model = genai.GenerativeModel(
                model_name=model_name,
                system_instruction=self.SYSTEM_PROMPT,
            )

            logger.info(f"Telegram 新闻分析器初始化成功 (模型: {model_name})")

        except Exception as e:
            logger.error(f"Gemini 模型初始化失败: {e}")
            self._model = None

    def is_available(self) -> bool:
        """检查分析器是否可用"""
        return self._model is not None

    def analyze(
        self,
        message_text: str,
        source_channel: str = "",
        category: str = ""
    ) -> NewsImpactResult:
        """
        分析消息对 A股 的影响

        Args:
            message_text: 消息文本
            source_channel: 来源频道名称
            category: 消息分类（由过滤器提供）

        Returns:
            NewsImpactResult 分析结果
        """
        if not self.is_available():
            return NewsImpactResult(
                summary="AI 分析不可用",
                impact_direction="中性",
                impact_magnitude=0,
                success=False,
                error_message="Gemini API 未配置"
            )

        try:
            # 构建 prompt
            prompt = self._build_prompt(message_text, source_channel, category)

            # 请求前延时（防止限流）
            config = get_config()
            delay = config.gemini_request_delay
            if delay > 0:
                time.sleep(delay)

            # 调用 API
            response = self._model.generate_content(
                prompt,
                generation_config={
                    "temperature": 0.3,  # 低随机性，追求准确
                    "max_output_tokens": 1024,
                },
                request_options={"timeout": 60}
            )

            if response and response.text:
                return self._parse_response(response.text)
            else:
                return NewsImpactResult(
                    summary="分析失败",
                    impact_direction="中性",
                    impact_magnitude=0,
                    success=False,
                    error_message="API 返回空响应"
                )

        except Exception as e:
            logger.error(f"消息分析失败: {e}")
            return NewsImpactResult(
                summary="分析出错",
                impact_direction="中性",
                impact_magnitude=0,
                success=False,
                error_message=str(e)
            )

    def _build_prompt(
        self,
        message_text: str,
        source_channel: str,
        category: str
    ) -> str:
        """构建分析 prompt"""
        from datetime import datetime, timezone, timedelta

        bj_time = datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M')

        prompt = f"""## 消息分析请求

**时间**: {bj_time}
**来源**: {source_channel or '未知频道'}
**分类**: {category or '未分类'}

**消息内容**:
```
{message_text}
```

请分析这条消息对 A股 市场的影响，输出 JSON 格式结果。"""

        return prompt

    def _parse_response(self, response_text: str) -> NewsImpactResult:
        """解析 AI 响应"""
        try:
            # 清理响应文本
            cleaned = response_text
            if '```json' in cleaned:
                cleaned = cleaned.replace('```json', '').replace('```', '')
            elif '```' in cleaned:
                cleaned = cleaned.replace('```', '')

            # 提取 JSON
            json_start = cleaned.find('{')
            json_end = cleaned.rfind('}') + 1

            if json_start >= 0 and json_end > json_start:
                json_str = cleaned[json_start:json_end]
                data = json.loads(json_str)

                return NewsImpactResult(
                    summary=data.get('summary', ''),
                    impact_direction=data.get('impact_direction', '中性'),
                    impact_magnitude=int(data.get('impact_magnitude', 0)),
                    affected_sectors=data.get('affected_sectors', []),
                    affected_stocks=data.get('affected_stocks', []),
                    action_suggestion=data.get('action_suggestion', ''),
                    confidence=float(data.get('confidence', 0.5)),
                    reasoning=data.get('reasoning', ''),
                    raw_response=response_text,
                    success=True
                )

        except (json.JSONDecodeError, ValueError) as e:
            logger.warning(f"JSON 解析失败: {e}")

        # 解析失败，返回默认结果
        return NewsImpactResult(
            summary=response_text[:50] if response_text else "解析失败",
            impact_direction="中性",
            impact_magnitude=0,
            raw_response=response_text,
            success=False,
            error_message="JSON 解析失败"
        )

    def batch_analyze(
        self,
        messages: List[str],
        delay_between: float = 2.0
    ) -> List[NewsImpactResult]:
        """
        批量分析多条消息（逐条调用）

        Args:
            messages: 消息文本列表
            delay_between: 每次分析之间的延迟

        Returns:
            分析结果列表
        """
        results = []

        for i, msg in enumerate(messages):
            if i > 0:
                time.sleep(delay_between)

            result = self.analyze(msg)
            results.append(result)

        return results

    def analyze_batch(
        self,
        batch_text: str,
        message_count: int
    ) -> BatchAnalysisResult:
        """
        批量分析多条消息（单次 API 调用）

        将多条消息合并成一个 prompt，让 AI 一次性分析

        Args:
            batch_text: 合并后的消息文本
            message_count: 消息数量

        Returns:
            BatchAnalysisResult 批量分析结果
        """
        if not self.is_available():
            return BatchAnalysisResult(
                success=False,
                error_message="Gemini API 未配置"
            )

        try:
            prompt = self._build_batch_prompt(batch_text, message_count)

            config = get_config()
            delay = config.gemini_request_delay
            if delay > 0:
                time.sleep(delay)

            response = self._model.generate_content(
                prompt,
                generation_config={
                    "temperature": 0.3,
                    "max_output_tokens": 4096,
                },
                request_options={"timeout": 120}
            )

            if response and response.text:
                return self._parse_batch_response(response.text, message_count)
            else:
                return BatchAnalysisResult(
                    success=False,
                    error_message="API 返回空响应"
                )

        except Exception as e:
            logger.error(f"批量分析失败: {e}")
            return BatchAnalysisResult(
                success=False,
                error_message=str(e)
            )

    def _build_batch_prompt(self, batch_text: str, message_count: int) -> str:
        """构建批量分析 prompt"""
        from datetime import datetime, timezone, timedelta

        bj_time = datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M')

        return f"""## 批量消息分析请求

**时间**: {bj_time}
**消息数量**: {message_count} 条

请分析以下消息对 **A股市场** 的影响。只筛选出有价值的消息（影响程度 >= 4）。

### 消息列表

{batch_text}

---

### 输出要求

请输出 JSON 格式，只包含有价值的消息分析结果：

```json
{{
    "items": [
        {{
            "index": 1,
            "summary": "一句话总结（30字以内）",
            "impact_direction": "利好/利空/中性",
            "impact_magnitude": 1-10,
            "affected_sectors": ["板块1", "板块2"],
            "action_suggestion": "操作建议"
        }}
    ],
    "total_analyzed": {message_count},
    "valuable_count": 有价值的消息数量
}}
```

### 评分标准
- **8-10分**: 重大政策/事件（降准降息、贸易战、制裁）
- **6-7分**: 行业重大消息、宏观数据超预期
- **4-5分**: 一般行业消息、国际市场联动
- **1-3分**: 轻微影响或间接相关
- **0分**: 与 A股 无关

只输出 JSON，不要有其他文字。如果没有有价值的消息，items 返回空数组。"""

    def _parse_batch_response(self, response_text: str, message_count: int) -> BatchAnalysisResult:
        """解析批量分析响应"""
        try:
            cleaned = response_text
            if '```json' in cleaned:
                cleaned = cleaned.replace('```json', '').replace('```', '')
            elif '```' in cleaned:
                cleaned = cleaned.replace('```', '')

            json_start = cleaned.find('{')
            json_end = cleaned.rfind('}') + 1

            if json_start >= 0 and json_end > json_start:
                json_str = cleaned[json_start:json_end]
                data = json.loads(json_str)

                items = data.get('items', [])

                return BatchAnalysisResult(
                    items=items,
                    total_count=data.get('total_analyzed', message_count),
                    valuable_count=data.get('valuable_count', len(items)),
                    raw_response=response_text,
                    success=True
                )

        except (json.JSONDecodeError, ValueError) as e:
            logger.warning(f"批量分析 JSON 解析失败: {e}")

        return BatchAnalysisResult(
            raw_response=response_text,
            success=False,
            error_message="JSON 解析失败"
        )
