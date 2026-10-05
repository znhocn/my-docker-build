ARG BASE_IMAGE=php:8.1-apache
FROM ${BASE_IMAGE}

# 本地构建从 .env 读取
ARG MYBB_URL
ARG MYBB_VERSION=unknown

ENV TZ=Asia/Shanghai \
    MYBB_VERSION=${MYBB_VERSION}

# 安装基础依赖与 PHP 扩展
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    unzip \
    zip \
    libzip-dev \
    libpng-dev \
    libjpeg-dev \
    libjpeg62-turbo-dev \
    libfreetype6-dev \
    libxml2-dev \
    libcurl4-openssl-dev \
    libonig-dev \
    && docker-php-ext-configure gd --with-freetype --with-jpeg \
    && docker-php-ext-install gd mysqli pdo pdo_mysql zip mbstring xml curl opcache \
    && rm -rf /var/lib/apt/lists/*

# 开启 Apache rewrite，并设置时区
RUN a2enmod rewrite \
    && sed -i 's/AllowOverride None/AllowOverride All/' /etc/apache2/apache2.conf \
    && ln -sf /usr/share/zoneinfo/${TZ} /etc/localtime \
    && echo "${TZ}" > /etc/timezone

# 优化生产环境 PHP 配置
RUN mv "$PHP_INI_DIR/php.ini-production" "$PHP_INI_DIR/php.ini" \
    && { \
        echo 'opcache.memory_consumption=128'; \
        echo 'opcache.interned_strings_buffer=8'; \
        echo 'opcache.max_accelerated_files=4000'; \
        echo 'opcache.revalidate_freq=2'; \
    } > /usr/local/etc/php/conf.d/opcache-recommended.ini

WORKDIR /var/www/html

# 压缩包顶层目录名不固定（Upload/ 或直接就是文件），
# 这里按 install 目录定位真正的站点根目录
RUN set -eux; \
    mkdir -p /tmp/extract; \
    wget -nv --tries=3 -O /tmp/mybb.zip "${MYBB_URL}"; \
    unzip -q /tmp/mybb.zip -d /tmp/extract; \
    rm -f /tmp/mybb.zip; \
    root=""; \
    for d in $(find /tmp/extract -maxdepth 3 -type d -name install); do root="${d%/install}"; break; done; \
    if [ -z "${root}" ]; then root="/tmp/extract"; fi; \
    cp -a "${root}"/. /var/www/html/; \
    rm -rf /tmp/extract; \
    rm -rf /var/www/html/Documentation; \
    chown -R www-data:www-data /var/www/html

EXPOSE 80