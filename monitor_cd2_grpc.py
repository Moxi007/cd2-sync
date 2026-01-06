import http.server
import socketserver
import json
import os
import grpc
import sys
import datetime
import threading
import time

# 导入 gRPC 模块
import clouddrive_pb2
import clouddrive_pb2_grpc

# ==================== 核心配置区域 ====================
# B机器 CD2 地址
CD2_HOST = "localhost:19798"
CD2_USER = "qaz199349@gmail.com"
CD2_PASS = "jtk2JZR5zuj.pjz_kpa"

# 路径映射
PATH_MAPPING = {
    "/": "/", 
}

# 监听端口
PORT = 5000

# [优化1] 安全Token
# 建议在 A 机器 webhook.toml 的 [file_system_watcher.headers] 下添加 token = "fjwejaovnpavSe"
SECURITY_TOKEN = "fjwejaovnpavSe" 

# [优化2] 防抖延迟 (秒)
# 收到变动后等待多久再刷新，期间收到的变动会合并
DEBOUNCE_DELAY = 5.0 

# [优化3] 忽略的文件后缀或名称 (小写)
IGNORE_EXTENSIONS = {".tmp", ".temp", ".crdownload", ".part", ".ds_store", "thumbs.db", ".aria2"}
# ====================================================

sys.stdout.reconfigure(line_buffering=True)

def log(message):
    current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{current_time}] {message}", flush=True)

class RefreshManager:
    """防抖动管理器：负责合并短时间内的多次刷新请求"""
    def __init__(self, client):
        self.client = client
        self.pending_paths = set()
        self.timer = None
        self.lock = threading.Lock()

    def schedule_refresh(self, path):
        with self.lock:
            # 如果已经在等待刷新，只需添加路径，不需要重启计时器
            self.pending_paths.add(path)
            if self.timer is None:
                # 启动倒计时
                self.timer = threading.Timer(DEBOUNCE_DELAY, self.execute_refresh)
                self.timer.start()
                log(f"[Buffer] 已进入缓冲池 (等待{DEBOUNCE_DELAY}秒): {path}")

    def execute_refresh(self):
        with self.lock:
            paths_to_process = list(self.pending_paths)
            self.pending_paths.clear()
            self.timer = None
        
        # 开始批量处理
        if not paths_to_process:
            return

        # [新增优化] 按路径长度排序，优先刷新短路径（父目录）
        # 这样如果父目录刷新了，子目录不存在的报错概率会降低
        paths_to_process.sort(key=len)

        log(f"[Batch] 缓冲结束，合并执行 {len(paths_to_process)} 个目录的刷新...")
        for path in paths_to_process:
            self.client.refresh_path_now(path)

class CD2Client:
    def __init__(self, host):
        self.host = host
        self.channel = None
        self.stub = None
        self.token = None
    
    def connect(self):
        self.channel = grpc.insecure_channel(self.host)
        self.stub = clouddrive_pb2_grpc.CloudDriveFileSrvStub(self.channel)

    def login(self, username, password):
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
            log(f"[gRPC] 连接异常: {e}")
            return False

    def refresh_path_now(self, path):
        """立即执行刷新"""
        if not self.token:
            if not self.login(CD2_USER, CD2_PASS): return

        try:
            metadata = [('authorization', f'Bearer {self.token}')]
            # 设置超时时间 180 秒，防止大目录卡死
            request = clouddrive_pb2.ListSubFileRequest(path=path, forceRefresh=True)
            response_iterator = self.stub.GetSubFiles(request, metadata=metadata, timeout=180)
            for _ in response_iterator:
                break 
            log(f"[gRPC] √ 已发送指令: {path}")

        except grpc.RpcError as e:
            error_detail = e.details() or ""
            
            if e.code() == grpc.StatusCode.UNAUTHENTICATED:
                log("[gRPC] Token过期，重试...")
                if self.login(CD2_USER, CD2_PASS):
                    self.refresh_path_now(path)
            
            # [新增优化] 忽略 "not found" 错误
            elif "not found" in error_detail.lower():
                log(f"[gRPC] 忽略: 目录已不存在 (无需刷新): {path}")
            
            else:
                log(f"[gRPC] × 失败: {error_detail}")
        except Exception as e:
            log(f"[gRPC] × 未知错误: {e}")

# 初始化
cd2_client = CD2Client(CD2_HOST)
refresh_manager = RefreshManager(cd2_client)

class WebhookHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return # 屏蔽 HTTP 访问日志，让控制台更清爽

    def do_POST(self):
        # [优化1] 安全验证
        if SECURITY_TOKEN:
            client_token = self.headers.get('token') or self.headers.get('x-secret-token')
            if client_token != SECURITY_TOKEN:
                log(f"[Security] 拒绝未授权访问: {self.client_address[0]}")
                self.send_response(403)
                self.end_headers()
                return

        if self.path == '/refresh':
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
            
            if not source_file: continue

            # [优化2] 过滤垃圾文件
            if self.should_ignore(source_file):
                # log(f"[Ignore] 忽略文件: {source_file}")
                continue

            action = raw_action.upper()
            local_path = self.map_path(source_file)
            local_dest = self.map_path(dest_file) if dest_file else None

            target_dirs = set()

            # 判定逻辑
            if action in ["CREATE", "CREATED", "DELETE", "DELETED", "MKDIR", "RMDIR"]:
                target_dirs.add(os.path.dirname(local_path))
                
            elif action in ["RENAME", "RENAMED", "MOVE", "MOVED"] and local_dest:
                target_dirs.add(os.path.dirname(local_path))
                target_dirs.add(os.path.dirname(local_dest))
                
            elif action in ["MODIFY", "MODIFIED", "WRITE", "CHANGE", "CHANGED"]:
                target_dirs.add(os.path.dirname(local_path))

            # 放入防抖管理器
            for path in target_dirs:
                refresh_manager.schedule_refresh(path)

    def should_ignore(self, path):
        """检查文件后缀是否在黑名单中"""
        filename = os.path.basename(path).lower()
        if filename.startswith("."): # 忽略隐藏文件
             return False 
        
        for ext in IGNORE_EXTENSIONS:
            if filename.endswith(ext) or filename == ext:
                return True
        return False

    def map_path(self, remote_path):
        for remote_prefix, local_prefix in PATH_MAPPING.items():
            if remote_path.startswith(remote_prefix):
                return remote_path.replace(remote_prefix, local_prefix, 1)
        return remote_path

if __name__ == '__main__':
    cd2_client.connect()
    cd2_client.login(CD2_USER, CD2_PASS)
    log(f"=== CD2 智能刷新服务 (防抖+过滤+容错) 启动于端口 {PORT} ===")
    
    server = socketserver.TCPServer(("", PORT), WebhookHandler)
    server.serve_forever()