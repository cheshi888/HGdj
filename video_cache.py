#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
视频流缓存模块
- LRU缓存策略
- 缓存过期控制
- 缓存大小限制
- 自动清理机制
"""
import json
import os
import threading
import time
from collections import OrderedDict
import hashlib

# 进程内线程锁：防止多线程并发写缓存文件冲突
_cache_lock = threading.Lock()

class VideoCache:
    def __init__(self, cache_dir=None, max_size=100, max_age=3600):
        """
        初始化视频缓存
        
        Args:
            cache_dir: 缓存目录
            max_size: 最大缓存条目数
            max_age: 缓存过期时间（秒）
        """
        self.cache_dir = cache_dir or os.path.join(os.path.dirname(__file__), "cache")
        self.max_size = max_size
        self.max_age = max_age
        self.cache = OrderedDict()
        self.stats = {
            'hits': 0,
            'misses': 0,
            'evictions': 0
        }
        
        # 确保缓存目录存在
        os.makedirs(self.cache_dir, exist_ok=True)
        
        # 加载现有缓存
        self._load_cache()
    
    def _load_cache(self):
        """从文件加载缓存（损坏时自动重建，不崩溃）"""
        cache_file = os.path.join(self.cache_dir, "video_cache.json")
        if os.path.exists(cache_file):
            try:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.cache = OrderedDict(data.get('cache', {}))
                    self.stats = data.get('stats', self.stats)
            except Exception as e:
                # 文件损坏：备份后重建
                try:
                    os.replace(cache_file, cache_file + ".bak")
                except Exception:
                    pass
                self.cache = OrderedDict()

    def _save_cache(self):
        """保存缓存到文件（原子写+线程锁：唯一tmp文件名，避免并发冲突）"""
        cache_file = os.path.join(self.cache_dir, "video_cache.json")
        tmp_file = os.path.join(self.cache_dir,
                                "video_cache_%d.tmp" % os.getpid())
        with _cache_lock:
            try:
                data = {
                    'cache': dict(self.cache),
                    'stats': self.stats
                }
                with open(tmp_file, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                os.replace(tmp_file, cache_file)
            except Exception as e:
                print(f"保存缓存失败: {e}")
                try:
                    if os.path.exists(tmp_file):
                        os.remove(tmp_file)
                except Exception:
                    pass
    
    def _get_cache_key(self, url, ep, quality):
        """生成缓存键"""
        key_data = f"{url}_{ep}_{quality}"
        return hashlib.md5(key_data.encode('utf-8')).hexdigest()
    
    def _cleanup_expired(self):
        """清理过期缓存"""
        current_time = time.time()
        expired_keys = []
        
        for key, (timestamp, data) in self.cache.items():
            if current_time - timestamp > self.max_age:
                expired_keys.append(key)
        
        for key in expired_keys:
            self.cache.pop(key, None)
            self.stats['evictions'] += 1
        
        if expired_keys:
            self._save_cache()
    
    def _cleanup_lru(self):
        """清理LRU缓存"""
        while len(self.cache) > self.max_size:
            self.cache.popitem(last=False)
            self.stats['evictions'] += 1
    
    def get(self, url, ep, quality):
        """获取缓存的视频流"""
        key = self._get_cache_key(url, ep, quality)
        
        if key in self.cache:
            timestamp, data = self.cache[key]
            # 检查是否过期
            if time.time() - timestamp <= self.max_age:
                # 移到最后（LRU）
                self.cache.move_to_end(key)
                self.stats['hits'] += 1
                return data
            else:
                # 删除过期缓存
                self.cache.pop(key, None)
                self.stats['evictions'] += 1
        
        self.stats['misses'] += 1
        return None
    
    def set(self, url, ep, quality, stream_data):
        """设置缓存的视频流"""
        key = self._get_cache_key(url, ep, quality)
        timestamp = time.time()
        
        self.cache[key] = (timestamp, stream_data)
        self.cache.move_to_end(key)
        
        # 清理过期缓存
        self._cleanup_expired()
        
        # 清理LRU缓存
        self._cleanup_lru()
        
        # 保存到文件
        self._save_cache()
    
    def clear(self):
        """清空缓存"""
        self.cache.clear()
        self.stats = {
            'hits': 0,
            'misses': 0,
            'evictions': 0
        }
        self._save_cache()
    
    def get_stats(self):
        """获取缓存统计信息"""
        total_requests = self.stats['hits'] + self.stats['misses']
        hit_rate = (self.stats['hits'] / total_requests * 100) if total_requests > 0 else 0
        
        return {
            'total_requests': total_requests,
            'hits': self.stats['hits'],
            'misses': self.stats['misses'],
            'hit_rate': round(hit_rate, 2),
            'cache_size': len(self.cache),
            'max_size': self.max_size,
            'max_age': self.max_age
        }
    
    def cleanup_expired_files(self):
        """清理缓存文件"""
        current_time = time.time()
        cleaned_files = []
        
        for filename in os.listdir(self.cache_dir):
            if filename.endswith('.m3u8') or filename.endswith('.ts'):
                file_path = os.path.join(self.cache_dir, filename)
                try:
                    file_mtime = os.path.getmtime(file_path)
                    if current_time - file_mtime > self.max_age:
                        os.remove(file_path)
                        cleaned_files.append(filename)
                except Exception as e:
                    print(f"清理文件 {filename} 失败: {e}")
        
        return cleaned_files

# 全局缓存实例
video_cache = VideoCache()

def get_cached_stream(url, ep, quality):
    """获取缓存的视频流"""
    return video_cache.get(url, ep, quality)

def set_cached_stream(url, ep, quality, stream_data):
    """设置缓存的视频流"""
    video_cache.set(url, ep, quality, stream_data)

def get_cache_stats():
    """获取缓存统计信息"""
    return video_cache.get_stats()

def clear_cache():
    """清空缓存"""
    video_cache.clear()

def cleanup_cache_files():
    """清理缓存文件"""
    return video_cache.cleanup_expired_files()