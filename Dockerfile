FROM python:3.11-slim

WORKDIR /app

# 创建日志目录
RUN mkdir -p /app/log

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用文件
COPY clouddrive.proto .
COPY clouddrive_pb2.py .
COPY clouddrive_pb2_grpc.py .
COPY monitor_cd2_grpc.py .

# 环境变量配置 (可在运行时覆盖)
ENV CD2_HOST="localhost:19798"
ENV CD2_USER=""
ENV CD2_PASS=""
ENV PORT=5000
ENV SECURITY_TOKEN="fjwejaovnpavSe"
ENV DEBOUNCE_DELAY=5.0
ENV LOG_FILE="/app/log/monitor.log"

# 声明日志卷，方便持久化
VOLUME ["/app/log"]

EXPOSE 5000

# 同时输出到控制台和日志文件
CMD ["sh", "-c", "python -u monitor_cd2_grpc.py 2>&1 | tee -a ${LOG_FILE}"]
