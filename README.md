# CD2-Sync

CloudDrive2 目录同步刷新服务，接收文件变动 webhook 通知，自动刷新 CD2 目录缓存。

## 功能特性

- 接收文件系统变动 webhook，自动触发 CD2 目录刷新
- 防抖机制：合并短时间内的多次变动，减少刷新请求
- 并发刷新：多个目录同时刷新，提高效率
- 支持 Token 或用户名密码认证
- gRPC 连接自动重试
- 健康检查和统计接口
- 优雅退出，输出运行统计
- Docker 部署支持

## 快速开始

### Docker Compose 部署（推荐）

1. 创建 `docker-compose.yml`：

```yaml
services:
  cd2-sync:
    image: lfy1680/cd2-sync:latest
    container_name: cd2-sync
    restart: unless-stopped
    # ports:
    #   - "5000:5000"
    environment:
      - CD2_HOST=127.0.0.1:19798   # 修改为你的 CD2 地址
      - CD2_TOKEN=                 # 优先使用 token 认证（推荐）
      - CD2_USER=                  # 或使用用户名密码
      - CD2_PASS=
      - PORT=5000
      - SECURITY_TOKEN=fjwejaovnpavSe
      - DEBOUNCE_DELAY=5           # 防抖延迟（秒）
      - GRPC_RETRY_TIMES=5         # gRPC 连接重试次数
      - GRPC_RETRY_INTERVAL=10     # gRPC 重试间隔（秒）
      - REFRESH_CONCURRENCY=3      # 并发刷新数
      - TZ=Asia/Shanghai
      # - PATH_MAPPING='{"/source":"/dest"}'
    volumes:
      - ./log:/app/log
    # 如果 CD2 在同一 Docker 网络中，可以使用 network_mode
    network_mode: host
```

2. 启动服务：

```bash
docker-compose up -d
```

### 环境变量说明

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `CD2_HOST` | CD2 gRPC 地址 | `localhost:19798` |
| `CD2_TOKEN` | CD2 认证 Token（优先使用） | - |
| `CD2_USER` | CD2 用户名 | - |
| `CD2_PASS` | CD2 密码 | - |
| `PORT` | 服务监听端口 | `5000` |
| `SECURITY_TOKEN` | webhook 请求验证令牌 | `fjwejaovnpavSe` |
| `DEBOUNCE_DELAY` | 防抖延迟秒数 | `5` |
| `GRPC_RETRY_TIMES` | gRPC 连接重试次数 | `5` |
| `GRPC_RETRY_INTERVAL` | gRPC 重试间隔秒数 | `10` |
| `REFRESH_CONCURRENCY` | 并发刷新数 | `3` |
| `PATH_MAPPING` | 路径映射（JSON 格式） | `{"/":"/"}` |

## API 接口

### POST /refresh

触发目录刷新，需要在请求头中携带 `token` 或 `x-secret-token`。

请求体格式：
```json
{
  "data": [
    {
      "action": "CREATE",
      "source_file": "/path/to/file"
    }
  ]
}
```

支持的 action：`CREATE`, `DELETE`, `MKDIR`, `RMDIR`, `RENAME`, `MOVE`, `MODIFY`

### GET /health

健康检查接口，返回服务和 CD2 连接状态。

```json
{
  "status": "healthy",
  "cd2_connected": true,
  "cd2_host": "127.0.0.1:19798"
}
```

### GET /stats

统计信息接口。

```json
{
  "uptime_seconds": 3600,
  "webhook_received": 100,
  "refresh_success": 95,
  "refresh_failed": 3,
  "refresh_ignored": 2
}
```

## 配合 CloudDrive2 Webhook 使用

本服务需要配合 CloudDrive2 的 webhook 功能使用。在发送端（A 机器）的 CD2 配置文件中添加 webhook 配置，当文件发生变动时自动通知接收端（B 机器）刷新目录缓存。

### CD2 Webhook 配置模板

在发送端 CD2 的 `config/webhook.toml` 中配置：

```toml
# ================= 全局设置 =================
[global_params]
base_url = "http://localhost:19798"
enabled = true
time_format = "epoch"

[global_params.default_headers]
content-type = "application/json"
user-agent = "clouddrive2/{version}"

# ================= 文件系统监视器 =================
[file_system_watcher]
# 将 IP 替换为接收端（运行本服务的机器）的地址
url = "http://接收端IP:5000/refresh"
method = "POST"
enabled = true

body = '''
{
  "device_name": "{device_name}",
  "user_name": "{user_name}",
  "event_category": "{event_category}",
  "event_name": "{event_name}",
  "data": [
    {
      "action": "{action}",
      "is_dir": "{is_dir}",
      "source_file": "{source_file}",
      "destination_file": "{destination_file}"
    }
  ]
}
'''

# 安全令牌，需与本服务的 SECURITY_TOKEN 一致
[file_system_watcher.headers]
token = "fjwejaovnpavSe"

# ================= 挂载点监视器（可选）=================
[mount_point_watcher]
url = "http://接收端IP:5000/refresh"
method = "POST"
enabled = false

body = '''
{
  "device_name": "{device_name}",
  "event_category": "{event_category}",
  "event_name": "{event_name}",
  "data": [
    {
      "action": "{action}",
      "mount_point": "{mount_point}",
      "status": "{status}",
      "reason": "{reason}"
    }
  ]
}
'''
```

### 配置说明

1. 将 `url` 中的 `接收端IP` 替换为运行本服务的机器 IP
2. `headers` 中的 `token` 需要与本服务的 `SECURITY_TOKEN` 环境变量保持一致
3. 如果不需要监控挂载点变动，`mount_point_watcher.enabled` 设为 `false`

## 许可证

MIT License
