#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
错误处理模块
- 网络错误重试机制
- 播放错误处理
- 优雅的错误提示
- 自动恢复机制
"""
import time
import traceback
from functools import wraps
import logging

# 配置日志（只写文件，不打stderr，避免爬虫输出爆炸）
logging.basicConfig(
    level=logging.WARNING,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('error_handler.log', encoding='utf-8', delay=True)
    ]
)
logger = logging.getLogger(__name__)

class VideoError(Exception):
    """视频相关错误基类"""
    pass

class NetworkError(VideoError):
    """网络错误"""
    pass

class PlaybackError(VideoError):
    """播放错误"""
    pass

class ParseError(VideoError):
    """解析错误"""
    pass

class CacheError(VideoError):
    """缓存错误"""
    pass

class ErrorHandler:
    def __init__(self, max_retries=3, retry_delay=1):
        """
        初始化错误处理器
        
        Args:
            max_retries: 最大重试次数
            retry_delay: 重试延迟（秒）
        """
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.error_stats = {
            'total_errors': 0,
            'network_errors': 0,
            'parse_errors': 0,
            'playback_errors': 0,
            'cache_errors': 0,
            'recovered_errors': 0
        }
    
    def log_error(self, error, context=None):
        """记录错误日志"""
        self.error_stats['total_errors'] += 1
        
        # 分类统计
        if isinstance(error, NetworkError):
            self.error_stats['network_errors'] += 1
        elif isinstance(error, ParseError):
            self.error_stats['parse_errors'] += 1
        elif isinstance(error, PlaybackError):
            self.error_stats['playback_errors'] += 1
        elif isinstance(error, CacheError):
            self.error_stats['cache_errors'] += 1
        
        # 记录日志
        error_info = {
            'error_type': type(error).__name__,
            'error_message': str(error),
            'context': context,
            'traceback': traceback.format_exc()
        }
        
        logger.error(f"Error occurred: {error_info}")
        
        return error_info
    
    def recover_error(self, error, recovery_method=None):
        """尝试恢复错误"""
        logger.info(f"Attempting to recover from error: {error}")
        
        if recovery_method:
            try:
                result = recovery_method()
                self.error_stats['recovered_errors'] += 1
                logger.info(f"Error recovered successfully: {error}")
                return result
            except Exception as recovery_error:
                logger.error(f"Recovery failed: {recovery_error}")
                return None
        
        # 默认恢复策略
        if isinstance(error, NetworkError):
            # 网络错误：等待后重试
            time.sleep(self.retry_delay * 2)
            return True
        elif isinstance(error, PlaybackError):
            # 播放错误：尝试切换质量
            return 'switch_quality'
        elif isinstance(error, ParseError):
            # 解析错误：尝试重新解析
            return 'retry_parse'
        else:
            # 其他错误：记录并返回False
            return False
    
    def get_stats(self):
        """获取错误统计信息"""
        return self.error_stats.copy()
    
    def clear_error_stats(self):
        """清空错误统计"""
        self.error_stats = {
            'total_errors': 0,
            'network_errors': 0,
            'parse_errors': 0,
            'playback_errors': 0,
            'cache_errors': 0,
            'recovered_errors': 0
        }

# 全局错误处理器实例
error_handler_instance = ErrorHandler()

def handle_errors(max_retries=None, retry_delay=None):
    """错误处理装饰器"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            retries = max_retries or error_handler_instance.max_retries
            delay = retry_delay or error_handler_instance.retry_delay
            
            for attempt in range(retries + 1):
                try:
                    return func(*args, **kwargs)
                except NetworkError as e:
                    error_handler_instance.log_error(e, f"Network error in {func.__name__}")
                    if attempt < retries:
                        logger.info(f"Retrying {func.__name__} (attempt {attempt + 1}/{retries})")
                        time.sleep(delay)
                        continue
                    else:
                        recovery_result = error_handler_instance.recover_error(e)
                        if recovery_result == 'switch_quality':
                            logger.info("Switching quality due to network error")
                            return func(*args, quality='low', **kwargs)
                        raise e
                    
                except ParseError as e:
                    error_handler_instance.log_error(e, f"Parse error in {func.__name__}")
                    if attempt < retries:
                        logger.info(f"Retrying {func.__name__} (attempt {attempt + 1}/{retries})")
                        time.sleep(delay)
                        continue
                    else:
                        recovery_result = error_handler_instance.recover_error(e)
                        if recovery_result == 'retry_parse':
                            logger.info("Retrying parse due to parse error")
                            return func(*args, **kwargs)
                        raise e
                    
                except PlaybackError as e:
                    error_handler_instance.log_error(e, f"Playback error in {func.__name__}")
                    recovery_result = error_handler_instance.recover_error(e)
                    if recovery_result == 'switch_quality':
                        logger.info("Switching quality due to playback error")
                        return func(*args, quality='low', **kwargs)
                    raise e
                    
                except CacheError as e:
                    error_handler_instance.log_error(e, f"Cache error in {func.__name__}")
                    recovery_result = error_handler_instance.recover_error(e)
                    if recovery_result:
                        logger.info("Cache recovered")
                        return func(*args, **kwargs)
                    raise e
                    
                except Exception as e:
                    error_handler_instance.log_error(e, f"Unexpected error in {func.__name__}")
                    raise e
        
        return wrapper
    return decorator

def safe_network_request(func):
    """安全网络请求装饰器"""
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            raise NetworkError(f"Network request failed: {str(e)}")
    return wrapper

def safe_parse(func):
    """安全解析装饰器"""
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            raise ParseError(f"Parse failed: {str(e)}")
    return wrapper

def safe_playback(func):
    """安全播放装饰器"""
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            raise PlaybackError(f"Playback failed: {str(e)}")
    return wrapper

def safe_cache_operation(func):
    """安全缓存操作装饰器"""
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            raise CacheError(f"Cache operation failed: {str(e)}")
    return wrapper

def get_error_report():
    """生成错误报告"""
    stats = error_handler_instance.get_stats()
    report = {
        'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
        'error_statistics': stats,
        'error_rate': (stats['recovered_errors'] / stats['total_errors'] * 100) if stats['total_errors'] > 0 else 0,
        'success_rate': (stats['recovered_errors'] / (stats['total_errors'] + stats['recovered_errors']) * 100) if (stats['total_errors'] + stats['recovered_errors']) > 0 else 0
    }
    return report

def clear_error_logs():
    """清空错误日志"""
    error_handler_instance.clear_error_stats()
    # 清空日志文件
    try:
        with open('error_handler.log', 'w', encoding='utf-8') as f:
            f.write('')
        logger.info("Error logs cleared")
    except Exception as e:
        logger.error(f"Failed to clear error logs: {e}")

# 示例使用
if __name__ == '__main__':
    # 测试错误处理
    @handle_errors(max_retries=2, retry_delay=1)
    @safe_network_request
    def test_network_request():
        # 模拟网络错误
        raise Exception("Network connection failed")
    
    @handle_errors(max_retries=2, retry_delay=1)
    @safe_parse
    def test_parse():
        # 模拟解析错误
        raise Exception("JSON parse failed")
    
    try:
        test_network_request()
    except Exception as e:
        print(f"Caught error: {e}")
    
    try:
        test_parse()
    except Exception as e:
        print(f"Caught error: {e}")
    
    # 生成错误报告
    report = get_error_report()
    print("Error report:", report)