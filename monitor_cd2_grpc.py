#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CD2-Sync 目录同步刷新服务
接收文件变动 webhook，通过 gRPC 通知 CloudDrive2 刷新目录缓存
支持防抖、过滤、容错、健康检查等特性
"""

import http.server
import socketserver
import json
import os
import grpc
import sys
import datetime
import threading
import time
import signal

# 导入 gRPC 模块
import clouddrive_pb2
import clouddrive_pb2_grpc

# ==================== 核心配置区域 ====================
CD2_HOST = os.environ.get("CD2_HOST", "localhost:19798")
CD2_TOKEN = os.environ.get("CD2_TOKEN", "")
CD2_USER = os.environ.get("CD2_USER", "")
CD2_PASS = os.environ.get("CD2_PASS", "")

PATH_MAPPING_STR = os.environ.get("PATH_MAPPING", '{"/":"/"}')
try:
    PATH_MAPPING = json.loads(PATH_MAPPING_STR.replace("：", ":"))
except:
    PATH_MAPPING = {"/": "/"}

PORT = int(os.environ.get("PORT", "5000"))
SECURITY_TOKEN = os.environ.get("SECURITY_TOKEN", "fjwejaovnpavSe")
DEBOUNCE_DELAY = float(os.environ.get("DEBOUNCE_DELAY", "5.0"))
GRPC_RETRY_TIMES = int(os.environ.get("GRPC_RETRY_TIMES", "5"))
GRPC_RETRY_INTERVAL = int(os.environ.get("GRPC_RETRY_INTERVAL", "10"))
REFRESH_CONCURRENCY = int(os.environ.get("REFRESH_CONCURRENCY", "3"))  # 并发刷新数
REFRESH_RETRY_TIMES = int(os.environ.get("REFRESH_RETRY_TIMES", "3"))  # 刷新失败重试次数
REFRESH_RETRY_INTERVAL = int(os.environ.get("REFRESH_RETRY_INTERVAL", "10"))  # 刷新重试间隔（秒）
IGNORE_EXTENSIONS = {".tmp", ".temp", ".crdownload", ".part", ".ds_store", "thumbs.db", ".aria2"}

# 忽略的目录列表，支持前缀匹配
IGNORE_PATHS_STR = os.environ.get("IGNORE_PATHS", "")
IGNORE_PATHS = [p.strip() for p in IGNORE_PATHS_STR.split(",") if p.strip()]
# ====================================================

sys.stdout.reconfigure(line_buffering=True)

class Stats:
    """统计信息管理"""
    def __init__(self):
        self.lock = threading.Lock()
        self.refresh_success = 0
        self.refresh_failed = 0
        self.refresh_ignored = 0
        self.webhook_received = 0
        self.start_time = datetime.datetime.now()

    def inc_success(self):
        with self.lock:
            self.refresh_success += 1
    
    def inc_failed(self):
        with self.lock:
            self.refresh_failed += 1
    
    def inc_ignored(self):
        with self.lock:
            self.refresh_ignored += 1
    
    def inc_webhook(self):
        with self.lock:
            self.webhook_received += 1
    
    def to_dict(self):
        with self.lock:
            uptime = datetime.datetime.now() - self.start_time
            return {
                "uptime_seconds": int(uptime.total_seconds()),
                "webhook_received": self.webhook_received,
                "refresh_success": self.refresh_success,
                "refresh_failed": self.refresh_failed,
                "refresh_ignored": self.refresh_ignored
            }

stats = Stats()

def log(message):
    current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{current_time}] {message}", flush=True)

def validate_config():
    """启动时校验配置完整性"""
    errors = []
    warnings = []
    
    if not CD2_HOST:
        errors.append("CD2_HOST 未配置")
    
    if not CD2_TOKEN and not (CD2_USER and CD2_PASS):
        warnings.append("未配置 CD2_TOKEN 或 CD2_USER/CD2_PASS，将在首次请求时尝试认证")
    
    if not SECURITY_TOKEN:
        warnings.append("SECURITY_TOKEN 为空，webhook 接口无认证保护")
    
    for err in errors:
        log(f"[配置错误] {err}")
    for warn in warnings:
        log(f"[配置警告] {warn}")
    
    return len(errors) == 0

class RefreshManager:
    """防抖动管理器：合并短时间内的多次刷新请求"""
    def __init__(self, client):
        self.client = client
        self.pending_paths = set()
        self.timer = None
        self.lock = threading.Lock()

    def schedule_refresh(self, path):
        with self.lock:
            self.pending_paths.add(path)
            if self.timer is None:
                self.timer = threading.Timer(DEBOUNCE_DELAY, self.execute_refresh)
                self.timer.start()
                log(f"[Buffer] 已进入缓冲池 (等待{DEBOUNCE_DELAY}秒): {path}")

    def execute_refresh(self):
        with self.lock:
            paths_to_process = list(self.pending_paths)
            self.pending_paths.clear()
            self.timer = None
        
        if not paths_to_process:
            return

        log(f"[Batch] 缓冲结束，并发刷新 {len(paths_to_process)} 个目录...")
        
        # 并发刷新
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=REFRESH_CONCURRENCY) as executor:
            futures = {executor.submit(self.client.refresh_path_now, path): path for path in paths_to_process}
            for future in as_completed(futures):
                pass  # 结果已在 refresh_path_now 中处理
    
    def cancel(self):
        """取消待执行的刷新任务"""
        with self.lock:
            if self.timer:
                self.timer.cancel()
                self.timer = None


class CD2Client:
    """CloudDrive2 gRPC 客户端"""
    def __init__(self, host, token=None):
        self.host = host
        self.channel = None
        self.stub = None
        self.token = token
        self.connected = False
    
    def connect(self, retry=True):
        """连接 gRPC 服务，支持重试"""
        max_retries = GRPC_RETRY_TIMES if retry else 1
        
        for attempt in range(max_retries):
            try:
                msg = f"[gRPC] 正在连接 {self.host}..."
                if attempt > 0:
                    msg += f" (第 {attempt + 1} 次尝试)"
                log(msg)
                
                self.channel = grpc.insecure_channel(self.host)
                self.stub = clouddrive_pb2_grpc.CloudDriveFileSrvStub(self.channel)
                grpc.channel_ready_future(self.channel).result(timeout=10)
                self.connected = True
                log(f"[gRPC] 连接成功")
                return True
            except Exception as e:
                log(f"[gRPC] 连接失败: {e}")
                if attempt < max_retries - 1:
                    log(f"[gRPC] {GRPC_RETRY_INTERVAL} 秒后重试...")
                    time.sleep(GRPC_RETRY_INTERVAL)
        
        log(f"[gRPC] 连接失败，已达最大重试次数")
        return False

    def close(self):
        """关闭 gRPC 连接"""
        if self.channel:
            self.channel.close()
            self.connected = False
            log("[gRPC] 连接已关闭")

    def set_token(self, token):
        """直接设置 token"""
        self.token = token
        log(f"[gRPC] 使用预设 Token 认证")

    def login(self, username, password):
        """通过用户名密码登录"""
        try:
            request = clouddrive_pb2.GetTokenRequest(userName=username, password=password)
            response = self.stub.GetToken(request)
            if response.success:
                self.token = response.token
                log(f"[gRPC] 登录成功")
                return True
            else:
                log(f"[gRPC] 登录失败: {response.errorMessage}")
                return False
        except Exception as e:
            log(f"[gRPC] 登录异常: {e}")
            return False

    def ensure_token(self):
        """确保有可用的 token"""
        if self.token:
            return True
        if CD2_USER and CD2_PASS:
            return self.login(CD2_USER, CD2_PASS)
        log("[gRPC] 错误: 未配置 Token 或用户名密码")
        return False

    def is_healthy(self):
        """检查连接是否健康"""
        if not self.connected or not self.stub:
            return False
        try:
            from google.protobuf import empty_pb2
            self.stub.GetSystemInfo(empty_pb2.Empty(), timeout=5)
            return True
        except:
            return False

    def refresh_path_now(self, path, retry_on_auth=True, retry_count=0):
        """立即执行刷新，支持网络错误重试"""
        if not self.ensure_token():
            stats.inc_failed()
            return

        try:
            metadata = [('authorization', f'Bearer {self.token}')]
            request = clouddrive_pb2.ListSubFileRequest(path=path, forceRefresh=True)
            response_iterator = self.stub.GetSubFiles(request, metadata=metadata, timeout=180)
            for _ in response_iterator:
                break 
            log(f"[gRPC] √ 已发送指令: {path}")
            stats.inc_success()

        except grpc.RpcError as e:
            error_detail = e.details() or ""
            
            if e.code() == grpc.StatusCode.UNAUTHENTICATED and retry_on_auth:
                log("[gRPC] Token 过期或无效，尝试重新登录...")
                self.token = None
                if self.ensure_token():
                    # 只重试一次，避免无限递归
                    self.refresh_path_now(path, retry_on_auth=False, retry_count=retry_count)
                else:
                    stats.inc_failed()
            elif "not found" in error_detail.lower():
                log(f"[gRPC] 忽略: 目录已不存在: {path}")
                stats.inc_ignored()
            elif self._is_retryable_error(e, error_detail) and retry_count < REFRESH_RETRY_TIMES:
                # 网络错误或超时，进行重试
                retry_count += 1
                log(f"[gRPC] × 请求失败，{REFRESH_RETRY_INTERVAL}秒后重试 ({retry_count}/{REFRESH_RETRY_TIMES}): {error_detail[:100]}")
                time.sleep(REFRESH_RETRY_INTERVAL)
                self.refresh_path_now(path, retry_on_auth=retry_on_auth, retry_count=retry_count)
            else:
                log(f"[gRPC] × 失败: {error_detail}")
                stats.inc_failed()
        except Exception as e:
            error_msg = str(e)
            if retry_count < REFRESH_RETRY_TIMES:
                retry_count += 1
                log(f"[gRPC] × 未知错误，{REFRESH_RETRY_INTERVAL}秒后重试 ({retry_count}/{REFRESH_RETRY_TIMES}): {error_msg[:100]}")
                time.sleep(REFRESH_RETRY_INTERVAL)
                self.refresh_path_now(path, retry_on_auth=retry_on_auth, retry_count=retry_count)
            else:
                log(f"[gRPC] × 未知错误: {error_msg}")
                stats.inc_failed()

    def _is_retryable_error(self, error, detail):
        """判断是否为可重试的错误（网络问题、超时等）"""
        retryable_codes = {
            grpc.StatusCode.UNAVAILABLE,
            grpc.StatusCode.DEADLINE_EXCEEDED,
            grpc.StatusCode.RESOURCE_EXHAUSTED,
            grpc.StatusCode.ABORTED,
        }
        if error.code() in retryable_codes:
            return True
        # 检查错误信息中的网络相关关键词
        network_keywords = ["timeout", "connection", "network", "request", "sending request"]
        detail_lower = detail.lower()
        return any(kw in detail_lower for kw in network_keywords)


# 全局实例
cd2_client = CD2Client(CD2_HOST, CD2_TOKEN if CD2_TOKEN else None)
refresh_manager = RefreshManager(cd2_client)
server = None

class WebhookHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def do_GET(self):
        """处理 GET 请求"""
        if self.path == '/health':
            self.handle_health()
        elif self.path == '/stats':
            self.handle_stats()
        else:
            self.send_response(404)
            self.end_headers()

    def handle_health(self):
        """健康检查接口"""
        is_healthy = cd2_client.is_healthy()
        status = 200 if is_healthy else 503
        response = {
            "status": "healthy" if is_healthy else "unhealthy",
            "cd2_connected": cd2_client.connected,
            "cd2_host": CD2_HOST
        }
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(response).encode())

    def handle_stats(self):
        """统计信息接口"""
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(stats.to_dict()).encode())

    def do_POST(self):
        if SECURITY_TOKEN:
            client_token = self.headers.get('token') or self.headers.get('x-secret-token')
            if client_token != SECURITY_TOKEN:
                log(f"[Security] 拒绝未授权访问: {self.client_address[0]}")
                self.send_response(403)
                self.end_headers()
                return

        if self.path == '/refresh':
            stats.inc_webhook()
            try:
                content_length = int(self.headers['Content-Length'])
                post_data = self.rfile.read(content_length)
                payload = json.loads(post_data.decode('utf-8'))
                self.process_events(payload)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"OK")
            except Exception as e:
                log(f"[Error] 解析异常: {e}")
                self.send_response(500)
                self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def process_events(self, payload):
        events = payload.get('data', [])
        
        for event in events:
            raw_action = event.get('action', '')
            source_file = event.get('source_file')
            dest_file = event.get('destination_file')
            
            if not source_file:
                continue

            if self.should_ignore(source_file):
                continue

            action = raw_action.upper()
            local_path = self.map_path(source_file)
            local_dest = self.map_path(dest_file) if dest_file else None

            target_dirs = set()

            if action in ["CREATE", "CREATED", "DELETE", "DELETED", "MKDIR", "RMDIR"]:
                target_dirs.add(os.path.dirname(local_path))
            elif action in ["RENAME", "RENAMED", "MOVE", "MOVED"] and local_dest:
                target_dirs.add(os.path.dirname(local_path))
                target_dirs.add(os.path.dirname(local_dest))
            elif action in ["MODIFY", "MODIFIED", "WRITE", "CHANGE", "CHANGED"]:
                target_dirs.add(os.path.dirname(local_path))

            for path in target_dirs:
                refresh_manager.schedule_refresh(path)

    def should_ignore(self, path):
        """检查文件是否应被忽略（临时文件、下载中文件、忽略目录等）"""
        # 检查是否在忽略目录中
        for ignore_path in IGNORE_PATHS:
            if path.startswith(ignore_path):
                return True
        
        # 检查文件扩展名
        filename = os.path.basename(path).lower()
        for ext in IGNORE_EXTENSIONS:
            if filename.endswith(ext) or filename == ext:
                return True
        return False

    def map_path(self, remote_path):
        for remote_prefix, local_prefix in PATH_MAPPING.items():
            if remote_path.startswith(remote_prefix):
                return remote_path.replace(remote_prefix, local_prefix, 1)
        return remote_path


def graceful_shutdown(signum, frame):
    """优雅退出处理"""
    sig_name = signal.Signals(signum).name
    log(f"[系统] 收到 {sig_name} 信号，正在优雅退出...")
    
    refresh_manager.cancel()
    
    if server:
        threading.Thread(target=server.shutdown).start()
    
    cd2_client.close()
    
    final_stats = stats.to_dict()
    log(f"[统计] 运行时长: {final_stats['uptime_seconds']}秒, "
        f"收到请求: {final_stats['webhook_received']}, "
        f"刷新成功: {final_stats['refresh_success']}, "
        f"刷新失败: {final_stats['refresh_failed']}, "
        f"已忽略: {final_stats['refresh_ignored']}")
    
    log("[系统] 服务已停止")

def main():
    global server
    
    if not validate_config():
        sys.exit(1)
    
    signal.signal(signal.SIGTERM, graceful_shutdown)
    signal.signal(signal.SIGINT, graceful_shutdown)
    
    if not cd2_client.connect(retry=True):
        log("[错误] 无法连接到 CD2，服务启动失败")
        sys.exit(1)
    
    if CD2_TOKEN:
        cd2_client.set_token(CD2_TOKEN)
    elif CD2_USER and CD2_PASS:
        cd2_client.login(CD2_USER, CD2_PASS)
    
    log(f"=== CD2-Sync 目录同步服务启动 ===")
    log(f"[配置] CD2 地址: {CD2_HOST}")
    log(f"[配置] 监听端口: {PORT}")
    log(f"[配置] 防抖延迟: {DEBOUNCE_DELAY}秒")
    log(f"[接口] 健康检查: GET /health")
    log(f"[接口] 统计信息: GET /stats")
    log(f"[接口] 刷新触发: POST /refresh")
    
    # 允许端口复用，避免重启时 "Address already in use" 错误
    socketserver.TCPServer.allow_reuse_address = True
    server = socketserver.TCPServer(("", PORT), WebhookHandler)
    server.serve_forever()

if __name__ == '__main__':
    main()
