from urllib.parse import urlparse
import regex as re
from pydantic import BaseModel, Field
from loguru import logger
import os, sys, atexit
from pathlib import Path


# 全局字典，用于跟踪已创建的 logger 处理器
_logger_handlers = {}
_atexit_registered = False

def _env_to_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    v = value.strip().lower()
    return v in {"1", "true", "yes", "y", "on"}

def get_logger(logger_file_path: Path, logger_name: str):
    """
    创建一个配置好的 loguru 日志记录器，包含文件和控制台输出
    
    :param logger_name: 日志记录器名称
    :param logger_file_path: 日志文件存储路径
    :return: 配置好的 logger 实例
    """
    verbose = os.environ.get("WISEFLOW_VERBOSE", "").lower() in ["true", "1"]
    
    logger_file = logger_file_path / f"{logger_name}.log"
    
    # 如果该 logger 已经存在，先移除其所有处理器
    if logger_name in _logger_handlers:
        for handler_id in _logger_handlers[logger_name]:
            try:
                logger.remove(handler_id)
            except ValueError:
                pass  # 处理器可能已经被移除
    
    # 如果是第一次创建 logger，移除默认的控制台处理器
    if logger_name not in _logger_handlers:
        try:
            logger.remove(0)  # 移除默认控制台处理器
        except ValueError:
            pass  # 默认处理器可能已经被移除
    
    # 创建过滤器，只处理当前 logger_name 的消息
    logger_filter = lambda record: record.get("extra", {}).get("name") == logger_name

    # 运行环境检测与配置
    is_frozen = bool(getattr(sys, "frozen", False))
    is_child = os.environ.get("WISEFLOW_CHILD_PROCESS") == "1"
    # 动态决定文件写入是否使用 enqueue（可通过环境变量覆盖）
    env_enqueue = os.environ.get("WISEFLOW_LOG_ENQUEUE")
    effective_enqueue = _env_to_bool(env_enqueue, default=(not (is_frozen and is_child)))

    # 文件保留与压缩策略（可通过环境变量调整）
    # 默认保留 14 天，压缩 zip；设置为 0 或 "none" 可关闭
    try:
        retention_days = int(os.environ.get("WISEFLOW_LOG_RETENTION_DAYS", "14"))
    except ValueError:
        retention_days = 14
    retention_arg = (f"{retention_days} days" if retention_days > 0 else None)
    compression_env = os.environ.get("WISEFLOW_LOG_COMPRESSION", "zip").strip().lower()
    compression_arg = (None if compression_env in {"", "none", "false", "0"} else compression_env)
    # 添加文件处理器
    file_handler_id = logger.add(
        logger_file,
        level='INFO',
        backtrace=True,  # 异常时显示堆栈信息
        diagnose=verbose,
        rotation="12 MB",
        retention=retention_arg,
        compression=compression_arg,
        enqueue=effective_enqueue,
        encoding="utf-8",
        filter=logger_filter
    )
    
    # 添加控制台处理器（使用默认彩色格式）
    console_handler_id = logger.add(
        sys.stderr,
        level=('DEBUG' if verbose else 'INFO'),
        filter=logger_filter,
        colorize=True,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{message}</level>",
        enqueue=False  # 控制台同步输出，获得即时反馈
    )
    
    # 记录处理器 ID（存储为列表）
    _logger_handlers[logger_name] = [file_handler_id, console_handler_id]

    # 进程退出时确保刷新日志
    global _atexit_registered
    if not _atexit_registered:
        try:
            atexit.register(shutdown_logger)
            _atexit_registered = True
        except Exception:
            pass

    # 返回绑定了名称的 logger 实例
    return logger.bind(name=logger_name)


def shutdown_logger(logger_name: str | None = None):
    """
    Remove configured handlers to flush and stop background logging threads.
    If logger_name is None, remove all known handlers.
    """
    try:
        if logger_name is None:
            names = list(_logger_handlers.keys())
        else:
            names = [logger_name] if logger_name in _logger_handlers else []

        for name in names:
            handler_ids = _logger_handlers.get(name, [])
            for hid in handler_ids:
                try:
                    logger.remove(hid)
                except Exception:
                    pass
            _logger_handlers.pop(name, None)
    except Exception:
        # best-effort shutdown
        pass


def isURL(string):
    if string.startswith("www."):
        string = f"https://{string}"
    result = urlparse(string)
    return result.scheme != '' and result.netloc != ''

def isChinesePunctuation(char):
    # Define the Unicode encoding range for Chinese punctuation marks
    chinese_punctuations = set(range(0x3000, 0x303F)) | set(range(0xFF00, 0xFFEF))
    # Check if the character is within the above range
    return ord(char) in chinese_punctuations

def is_chinese(string):
    """
    :param string: {str} The string to be detected
    :return: {bool} Returns True if most are Chinese, False otherwise
    """
    pattern = re.compile(r'[^\u4e00-\u9fa5]')
    non_chinese_count = len(pattern.findall(string))
    # It is easy to misjudge strictly according to the number of bytes less than half.
    # English words account for a large number of bytes, and there are punctuation marks, etc
    return (non_chinese_count/len(string)) < 0.68

class Recorder(BaseModel):
    focus_id: str = ''
    max_urls_per_task: int = 200
    rss_source: int = 0
    web_source: int = 0
    catalog_count: dict[str, int] = Field(default_factory=dict)
    item_source: dict[str, int] = Field(default_factory=dict)
    url_queue: set[str] = Field(default_factory=set)
    processed_urls: set[str] = Field(default_factory=set)
    total_processed: int = 0
    crawl_failed: int = 0
    scrap_failed: int = 0
    successed: int = 0
    info_added: int = 0

    def add_url(self, urls, source, *, allow_forbidden=False):
        from core.wis.config import config
        from core.wis.material_urls import new_urls
        # FORBIDDEN_DOMAINS：URL 子串命中即排除（域名或域名+路径前缀），沿用旧引擎口径。
        # allow_forbidden=True 用于任务显式配置的 web 起点 URL（旧引擎对 depth 0 种子豁免）。
        forbidden = () if allow_forbidden else tuple(config.get('FORBIDDEN_DOMAINS', []))
        added = new_urls(urls, self.url_queue, self.processed_urls, forbidden)
        self.url_queue.update(added)
        self.item_source[source] = self.item_source.get(source, 0) + len(added)

    def source_summary(self):
        return (f'{self.focus_id}: sites={self.web_source}, feeds={self.rss_source}, '
                f'catalogues={self.catalog_count}, queued={len(self.url_queue)}')

    def scrap_summary(self):
        return (f'{self.focus_id}: processed={self.total_processed}, capture_failed={self.crawl_failed}, '
                f'extraction_failed={self.scrap_failed}, success={self.successed}, infos_added={self.info_added}')
