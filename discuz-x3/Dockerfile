ARG BASE_IMAGE=php:7.4-apache
FROM ${BASE_IMAGE}

# 本地构建从 .env 读取
ARG DISCUZ_URL="https://gitee.com/Discuz/DiscuzX/releases/download/v3.5-20260504/Discuz_X3.5_SC_UTF8_20260504.zip"
ARG DISCUZ_VERSION="3.5-20260504"

ENV TZ=Asia/Shanghai \
    DISCUZ_VERSION=${DISCUZ_VERSION}

# 切换到 Debian 归档源（Buster 已 EOL）
RUN sed -i 's/deb.debian.org/archive.debian.org/g' /etc/apt/sources.list \
    && sed -i 's|security.debian.org|archive.debian.org|g' /etc/apt/sources.list \
    && sed -i '/buster-updates/d' /etc/apt/sources.list \
    && sed -i '/buster\/updates/d' /etc/apt/sources.list \
    && echo 'Acquire::Check-Valid-Until "false";' > /etc/apt/apt.conf.d/99no-check-valid-until

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

# 下载并解压 Discuz
RUN set -eux; \
    wget -nv --tries=3 -O /tmp/discuz.zip "${DISCUZ_URL}"; \
    && unzip /tmp/discuz.zip -d /tmp; \
    && mv /tmp/upload/* /var/www/html/; \
    && rm -rf /tmp/discuz.zip /tmp/upload; \
    && chown -R www-data:www-data /var/www/html

EXPOSE 80