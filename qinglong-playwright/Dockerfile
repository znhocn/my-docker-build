# 青龙面板 (whyour/qinglong) + Playwright Chromium
# 基于官方 debian 变体（glibc），Playwright 官方支持；Alpine 不支持。
#
# 设计要点：
# 1. /ql/data 为挂载 VOLUME，运行时任务以非 root 用户 qinglong 执行
#    （HOME=/home/qinglong），构建阶段 HOME=/root。
# 2. 通过 PLAYWRIGHT_BROWSERS_PATH 将浏览器固定到共享目录，
#    并系统级安装 playwright，供 qinglong 任务直接 import。
# 3. 基础镜像可用 BASE_IMAGE 覆盖，默认保持 debian 变体。
# 4. playwright 每次构建都装当时的最新版，但不写死版本号：装在独立目录并前置进
#    PYTHONPATH，运行时只解析这一份，保证和构建时下载的浏览器 revision 对得上。
#    官方镜像的 PYTHONPATH 指向 /ql/data/dep_cache/python3（运行时是挂载的 volume），
#    里面遗留的旧版 playwright 会盖过 site-packages，import 到的版本和浏览器对不上时报
#    "Executable doesn't exist at /ms-playwright/chromium_headless_shell-XXXX/..."。
#    参数名不要用 *_VERSION / *_URL / *_TAG 后缀，build-latest.py 会拿上游 release
#    的值覆盖它们；pypi 或浏览器 CDN 不通时用 PIP_INDEX / PLAYWRIGHT_DOWNLOAD_HOST 换源。
ARG BASE_IMAGE=ghcr.io/whyour/qinglong:debian
FROM ${BASE_IMAGE}

ARG QINGLONG_VERSION=unknown
ARG PIP_INDEX=https://pypi.org/simple
ARG PLAYWRIGHT_DOWNLOAD_HOST=
ENV QINGLONG_VERSION=${QINGLONG_VERSION}
# pypi / 浏览器 CDN 在国内很慢，默认 15s 的读取超时会直接失败
ENV PIP_DEFAULT_TIMEOUT=120
ENV PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT=120000

# 切换到 root，安装系统依赖需要 apt 权限
USER root

# 共享浏览器目录：构建与运行统一路径，qinglong 用户可读写
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN mkdir -p /ms-playwright && chmod 777 /ms-playwright

# 系统级安装 playwright 最新版（含系统依赖的 Chromium），避免容器重建后丢失。
# 浏览器 revision 与 playwright 版本严格绑定，两者必须来自同一个安装目录：
# 先装包，再用这份包去装浏览器，中途不换版本。
ENV PLAYWRIGHT_PY_DIR=/opt/playwright
RUN pip3 install --no-cache-dir --index-url "${PIP_INDEX}" \
        --target "${PLAYWRIGHT_PY_DIR}" --upgrade playwright \
    && printf '#!/bin/sh\nexec python3 -m playwright "$@"\n' > /usr/local/bin/playwright \
    && chmod +x /usr/local/bin/playwright

# 前置到 PYTHONPATH 最前面：运行时优先 import 这个版本，
# 忽略 /ql/data/dep_cache/python3 里遗留的旧版本
ENV PYTHONPATH=${PLAYWRIGHT_PY_DIR}:${PYTHONPATH}
RUN set -eu; \
    if [ -n "${PLAYWRIGHT_DOWNLOAD_HOST}" ]; then export PLAYWRIGHT_DOWNLOAD_HOST; fi; \
    python3 -m playwright install --with-deps chromium; \
    chmod -R 777 /ms-playwright; \
    rm -rf /var/lib/apt/lists/*

# 将 qinglong 用户的 .ssh 同步给 root，方便 root 使用同一套密钥
RUN cp -rv /home/qinglong/.ssh /root/ \
    && ln -sf /home/qinglong/.ssh/config /root/.ssh/config \
    && chown -R root:root /root/.ssh \
    && chmod 700 /root/.ssh \
    && chmod 600 /root/.ssh/* || true

EXPOSE 5700