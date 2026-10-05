# 基础镜像可用 --build-arg DISCUZ_BASE=php:8.3-apache 覆盖
ARG DISCUZ_BASE=php:8.4-apache
FROM ${DISCUZ_BASE}

# 由 CI 传入（见 ci/projects.json），本地构建从 .env 读取
ARG DISCUZ_URL
ARG DISCUZ_VERSION=unknown

ENV TZ=Asia/Shanghai \
    DISCUZ_VERSION=${DISCUZ_VERSION}

# 安装必要组件
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    unzip \
    zip \
    libzip-dev \
    libpng-dev \
    libjpeg62-turbo-dev \
    libfreetype6-dev \
    libcurl4-openssl-dev \
    libonig-dev \
    && docker-php-ext-configure gd --with-freetype --with-jpeg \
    && docker-php-ext-install gd mysqli pdo pdo_mysql zip mbstring curl opcache \
    && rm -rf /var/lib/apt/lists/*

# 开启 Apache rewrite 与 .htaccess 支持，并设置时区
RUN a2enmod rewrite \
    && sed -i 's/AllowOverride None/AllowOverride All/' /etc/apache2/apache2.conf \
    && ln -sf /usr/share/zoneinfo/${TZ} /etc/localtime \
    && echo "${TZ}" > /etc/timezone

WORKDIR /var/www/html

# 压缩包顶层目录名不固定（upload/、Discuz_X5.0_.../ 或直接就是文件），
# 这里按 install 目录 / 配置文件定位真正的站点根目录
RUN set -eux; \
    mkdir -p /tmp/extract; \
    wget -nv --tries=3 -O /tmp/discuz.zip "${DISCUZ_URL}"; \
    unzip -q /tmp/discuz.zip -d /tmp/extract; \
    rm -f /tmp/discuz.zip; \
    root=""; \
    for d in $(find /tmp/extract -maxdepth 3 -type d -name install); do root="${d%/install}"; break; done; \
    if [ -z "${root}" ]; then \
      f="$(find /tmp/extract -maxdepth 3 -type f -name config_global.php | head -n 1)"; \
      if [ -n "${f}" ]; then root="$(dirname "${f}")"; fi; \
    fi; \
    if [ -z "${root}" ]; then root="/tmp/extract"; fi; \
    cp -a "${root}"/. /var/www/html/; \
    rm -rf /tmp/extract; \
    chown -R www-data:www-data /var/www/html

EXPOSE 80