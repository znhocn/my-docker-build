ARG BASE_IMAGE=php:7.4-apache
FROM ${BASE_IMAGE}

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        libfreetype6-dev \
        libjpeg62-turbo-dev \
        libpng-dev \
        libzip-dev \
        libicu-dev \
        libxml2-dev \
        libonig-dev \
        libxslt1-dev \
    ; \
    docker-php-ext-configure gd --with-freetype --with-jpeg; \
    docker-php-ext-install -j"$(nproc)" \
        mysqli \
        pdo_mysql \
        mbstring \
        gd \
        zip \
        intl \
        opcache \
        xml \
        bcmath \
        sockets \
        soap \
        xsl \
    ; \
    rm -rf /var/lib/apt/lists/*; \
    a2enmod rewrite; \
    printf 'disable_functions=\n' > /usr/local/etc/php/conf.d/zz-disable.ini

WORKDIR /var/www/html

COPY --chown=www-data:www-data ./www/ /var/www/html/
RUN chmod -R 755 /var/www/html

EXPOSE 80
