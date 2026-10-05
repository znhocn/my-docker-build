# 青龙面板 (whyour/qinglong) + Playwright Chromium
# 基于官方 debian 变体（glibc），Playwright 官方支持；Alpine 不支持。
#
# 设计要点：
# 1. /ql/data 为挂载 VOLUME，运行时任务以非 root 用户 qinglong 执行
#    （HOME=/home/qinglong），构建阶段 HOME=/root。
# 2. 通过 PLAYWRIGHT_BROWSERS_PATH 将浏览器固定到共享目录，
#    并系统级安装 playwright，供 qinglong 任务直接 import。
# 3. 基础镜像可用 BASE_IMAGE 覆盖，默认保持 debian 变体。
ARG BASE_IMAGE=ghcr.io/whyour/qinglong:debian
FROM ${BASE_IMAGE}

ARG QINGLONG_VERSION=unknown
ENV QINGLONG_VERSION=${QINGLONG_VERSION}

# 切换到 root，安装系统依赖需要 apt 权限
USER root

# 共享浏览器目录：构建与运行统一路径，qinglong 用户可读写
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN mkdir -p /ms-playwright && chmod 777 /ms-playwright

# 系统级安装 playwright + Chromium（含系统依赖），避免容器重建后丢失
RUN pip3 install --no-cache-dir "playwright" \
    && python3 -m playwright install --with-deps chromium \
    && chmod -R 777 /ms-playwright \
    && rm -rf /var/lib/apt/lists/*

# 将 qinglong 用户的 .ssh 同步给 root，方便 root 使用同一套密钥
RUN cp -rv /home/qinglong/.ssh /root/ \
    && ln -sf /home/qinglong/.ssh/config /root/.ssh/config \
    && chown -R root:root /root/.ssh \
    && chmod 700 /root/.ssh \
    && chmod 600 /root/.ssh/* || true

EXPOSE 5700