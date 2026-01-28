# -*- coding: utf-8 -*-
"""
===================================
A股自选股智能分析系统 - 配置管理模块
===================================

职责：
1. 使用单例模式管理全局配置
2. 从 .env 文件加载敏感配置
3. 提供类型安全的配置访问接口
"""

import os
from pathlib import Path
from typing import List, Optional
from dotenv import load_dotenv
from dataclasses import dataclass, field


@dataclass
class Config:
    """
    系统配置类 - 单例模式
    
    设计说明：
    - 使用 dataclass 简化配置属性定义
    - 所有配置项从环境变量读取，支持默认值
    - 类方法 get_instance() 实现单例访问
    """
    
    # === 自选股配置 ===
    stock_list: List[str] = field(default_factory=list)
    
    # === 数据源 API Token ===
    tushare_token: Optional[str] = None
    
    # === AI 分析配置 ===
    gemini_api_key: Optional[str] = None
    gemini_model: str = "gemini-3-flash-preview"  # 主模型
    gemini_model_fallback: str = "gemini-2.5-flash"  # 备选模型
    
    # Gemini API 请求配置（防止 429 限流）
    gemini_request_delay: float = 2.0  # 请求间隔（秒）
    gemini_max_retries: int = 5  # 最大重试次数
    gemini_retry_delay: float = 5.0  # 重试基础延时（秒）
    
    # === 搜索引擎配置（支持多 Key 负载均衡）===
    tavily_api_keys: List[str] = field(default_factory=list)  # Tavily API Keys
    serpapi_keys: List[str] = field(default_factory=list)  # SerpAPI Keys
    
    # === 通知配置 ===
    wechat_webhook_url: Optional[str] = None
    
    # === 邮件通知配置 ===
    email_sender: Optional[str] = None      # 发件人邮箱
    email_password: Optional[str] = None    # 邮箱授权码
    email_receiver: Optional[str] = None    # 收件人邮箱
    smtp_server: str = "smtp.qq.com"        # SMTP 服务器
    smtp_port: int = 465                    # SMTP 端口
    
    # === 数据库配置 ===
    database_path: str = "./data/stock_analysis.db"
    
    # === 日志配置 ===
    log_dir: str = "./logs"  # 日志文件目录
    log_level: str = "INFO"  # 日志级别
    
    # === 系统配置 ===
    max_workers: int = 3  # 低并发防封禁
    debug: bool = False
    
    # === 定时任务配置 ===
    schedule_enabled: bool = False            # 是否启用定时任务
    schedule_time: str = "18:00"              # 每日推送时间（HH:MM 格式）
    market_review_enabled: bool = True        # 是否启用大盘复盘

    # === Telegram 监听配置 ===
    telegram_enabled: bool = False            # 是否启用 Telegram 监听
    telegram_api_id: Optional[int] = None     # Telegram API ID
    telegram_api_hash: Optional[str] = None   # Telegram API Hash
    telegram_phone: Optional[str] = None      # 手机号（用于登录验证）
    telegram_session: Optional[str] = None    # StringSession（用于无交互环境）
    telegram_channels: List[int] = field(default_factory=list)  # 监听的频道 ID 列表
    telegram_summary_interval: int = 60       # 汇总推送间隔（分钟）
    
    # === 流控配置（防封禁关键参数）===
    # Akshare 请求间隔范围（秒）
    akshare_sleep_min: float = 2.0
    akshare_sleep_max: float = 5.0
    
    # Tushare 每分钟最大请求数（免费配额）
    tushare_rate_limit_per_minute: int = 80
    
    # 重试配置
    max_retries: int = 3
    retry_base_delay: float = 1.0
    retry_max_delay: float = 30.0
    
    # 单例实例存储
    _instance: Optional['Config'] = None
    
    @classmethod
    def get_instance(cls) -> 'Config':
        """
        获取配置单例实例
        
        单例模式确保：
        1. 全局只有一个配置实例
        2. 配置只从环境变量加载一次
        3. 所有模块共享相同配置
        """
        if cls._instance is None:
            cls._instance = cls._load_from_env()
        return cls._instance
    
    @classmethod
    def _load_from_env(cls) -> 'Config':
        """
        从 .env 文件加载配置
        
        加载优先级：
        1. 系统环境变量
        2. .env 文件
        3. 代码中的默认值
        """
        # 加载项目根目录下的 .env 文件
        env_path = Path(__file__).parent / '.env'
        load_dotenv(dotenv_path=env_path)
        
        # 解析自选股列表（逗号分隔）
        stock_list_str = os.getenv('STOCK_LIST', '')
        stock_list = [
            code.strip() 
            for code in stock_list_str.split(',') 
            if code.strip()
        ]
        
        # 如果没有配置，使用默认的示例股票
        if not stock_list:
            stock_list = ['600519', '000001', '300750']
        
        # 解析搜索引擎 API Keys（支持多个 key，逗号分隔）
        # 兼容 TAVILY_API_KEYS 和 TAVILY_API_KEY 两种命名
        tavily_keys_str = os.getenv('TAVILY_API_KEYS') or os.getenv('TAVILY_API_KEY', '')
        tavily_api_keys = [k.strip() for k in tavily_keys_str.split(',') if k.strip()]

        # 兼容 SERPAPI_API_KEYS 和 SERPAPI_KEYS 两种命名
        serpapi_keys_str = os.getenv('SERPAPI_API_KEYS') or os.getenv('SERPAPI_KEYS', '')
        serpapi_keys = [k.strip() for k in serpapi_keys_str.split(',') if k.strip()]

        # 解析 Telegram 频道列表（逗号分隔的频道 ID）
        telegram_channels_str = os.getenv('TELEGRAM_CHANNELS', '')
        telegram_channels = []
        for ch in telegram_channels_str.split(','):
            ch = ch.strip()
            if ch:
                try:
                    telegram_channels.append(int(ch))
                except ValueError:
                    pass  # 忽略无效的频道 ID

        # 解析 Telegram API ID
        telegram_api_id = None
        api_id_str = os.getenv('TELEGRAM_API_ID', '')
        if api_id_str:
            try:
                telegram_api_id = int(api_id_str)
            except ValueError:
                pass
        
        return cls(
            stock_list=stock_list,
            tushare_token=os.getenv('TUSHARE_TOKEN'),
            gemini_api_key=os.getenv('GEMINI_API_KEY'),
            gemini_model=os.getenv('GEMINI_MODEL', 'gemini-3-flash-preview'),
            gemini_model_fallback=os.getenv('GEMINI_MODEL_FALLBACK', 'gemini-2.5-flash'),
            gemini_request_delay=float(os.getenv('GEMINI_REQUEST_DELAY', '2.0')),
            gemini_max_retries=int(os.getenv('GEMINI_MAX_RETRIES', '5')),
            gemini_retry_delay=float(os.getenv('GEMINI_RETRY_DELAY', '5.0')),
            tavily_api_keys=tavily_api_keys,
            serpapi_keys=serpapi_keys,
            wechat_webhook_url=os.getenv('WECHAT_WEBHOOK_URL'),
            email_sender=os.getenv('EMAIL_SENDER'),
            email_password=os.getenv('EMAIL_PASSWORD'),
            email_receiver=os.getenv('EMAIL_RECEIVER'),
            smtp_server=os.getenv('SMTP_SERVER', 'smtp.qq.com'),
            smtp_port=int(os.getenv('SMTP_PORT', '465')),
            database_path=os.getenv('DATABASE_PATH', './data/stock_analysis.db'),
            log_dir=os.getenv('LOG_DIR', './logs'),
            log_level=os.getenv('LOG_LEVEL', 'INFO'),
            max_workers=int(os.getenv('MAX_WORKERS', '3')),
            debug=os.getenv('DEBUG', 'false').lower() == 'true',
            schedule_enabled=os.getenv('SCHEDULE_ENABLED', 'false').lower() == 'true',
            schedule_time=os.getenv('SCHEDULE_TIME', '18:00'),
            market_review_enabled=os.getenv('MARKET_REVIEW_ENABLED', 'true').lower() == 'true',
            # Telegram 配置
            telegram_enabled=os.getenv('TELEGRAM_ENABLED', 'false').lower() == 'true',
            telegram_api_id=telegram_api_id,
            telegram_api_hash=os.getenv('TELEGRAM_API_HASH'),
            telegram_phone=os.getenv('TELEGRAM_PHONE'),
            telegram_session=os.getenv('TELEGRAM_SESSION'),  # StringSession 字符串
            telegram_channels=telegram_channels,
            telegram_summary_interval=int(os.getenv('TELEGRAM_SUMMARY_INTERVAL', '60')),
        )
    
    @classmethod
    def reset_instance(cls) -> None:
        """重置单例（主要用于测试）"""
        cls._instance = None
    
    def validate(self) -> List[str]:
        """
        验证配置完整性
        
        Returns:
            缺失或无效配置项的警告列表
        """
        warnings = []
        
        if not self.stock_list:
            warnings.append("警告：未配置自选股列表 (STOCK_LIST)")
        
        if not self.tushare_token:
            warnings.append("提示：未配置 Tushare Token，将使用其他数据源")
        
        if not self.gemini_api_key:
            warnings.append("警告：未配置 Gemini API Key，AI 分析功能将不可用")
        
        if not self.tavily_api_keys and not self.serpapi_keys:
            warnings.append("提示：未配置搜索引擎 API Key (Tavily/SerpAPI)，新闻搜索功能将不可用")
        
        # 通知配置检查
        has_notification = False
        
        if self.wechat_webhook_url:
            has_notification = True
        else:
            warnings.append("提示：未配置企业微信 Webhook")
            
        if self.email_sender and self.email_password and self.email_receiver:
            has_notification = True
        else:
            warnings.append("提示：未配置邮件通知")
            
        if not has_notification:
            warnings.append("警告：未配置任何通知渠道，将不发送推送通知")
        
        return warnings
    
    def get_db_url(self) -> str:
        """
        获取 SQLAlchemy 数据库连接 URL
        
        自动创建数据库目录（如果不存在）
        """
        db_path = Path(self.database_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{db_path.absolute()}"


# === 便捷的配置访问函数 ===
def get_config() -> Config:
    """获取全局配置实例的快捷方式"""
    return Config.get_instance()


if __name__ == "__main__":
    # 测试配置加载
    config = get_config()
    print("=== 配置加载测试 ===")
    print(f"自选股列表: {config.stock_list}")
    print(f"数据库路径: {config.database_path}")
    print(f"最大并发数: {config.max_workers}")
    print(f"调试模式: {config.debug}")
    
    # 验证配置
    warnings = config.validate()
    if warnings:
        print("\n配置验证结果:")
        for w in warnings:
            print(f"  - {w}")
