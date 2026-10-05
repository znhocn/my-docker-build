# My Docker Build

把几个上游项目的官方源码包/镜像打包成 Docker 镜像，每天定时检查上游 release，有新版本才重新构建并推送。

仓库根目录的 `build-latest.py` 用于**本地构建**：读各子项目的 `project.yaml`，取 GitHub 最新 release 版本，用该版本重新构建镜像，不推送远端。
镜像的自动构建与发布由 `.github/workflows/docker-build.yml` 独立完成，它不调用 `build-latest.py`。

## 配置

每个子项目一个 `project.yaml`，这是唯一的配置来源：

```yaml
enable: true                              # false 时检查和构建都跳过，不写默认启用
GitHub: https://github.com/mybb/mybb      # 上游仓库
version:                                  # 可选：从 tag 里提取版本号
  pattern: '[0-9]+\.[0-9]+[-_.][0-9]{8}'  # 例：tag 为 X5.0-20261001#2 时版本号取 5.0-20261001
```

- 版本号默认去掉 tag 里的构建号（`X5.0-20261001#2` → `X5.0-20261001`），需要别的规则就写 `version.pattern`。
- 镜像标签：上游版本号 + `latest`。

Dockerfile 里的构建参数按 `ARG` 名自动推断，不用额外配置：

| Dockerfile 里的 ARG | 传入的值 |
| --- | --- |
| `*_VERSION` | 上游版本号 |
| `*_TAG` / `*_REF` | 上游 tag |
| `GITHUB_URL` / `*_REPO_URL` | 上游仓库地址 |
| `*_URL` / `*_DOWNLOAD_URL` / `*_SRC_URL` | release 附件（按 Dockerfile 用的解压格式 + 仓库名匹配，例如 `unzip` 就找 `.zip`），没有合适附件就用该 tag 的源码压缩包 |
| `*_BASE` | 保持 Dockerfile 里的默认值 |
| 其他 | 不传，用 Dockerfile 里的默认值 |

`--build-arg K=V` 可以覆盖任意一项。

| 项目 | 实际传入 |
| --- | --- |
| discuz-x5 | `DISCUZ_URL`、`DISCUZ_VERSION` |
| mybb | `MYBB_URL`、`MYBB_VERSION` |
| qinglong | `QINGLONG_VERSION`（基础镜像用 Dockerfile 默认的 `ghcr.io/whyour/qinglong:debian`） |

声明了 `GITHUB_URL`、`*_REPO_URL`、`*_TAG`、`*_REF` 这类 ARG 的项目会一起传入对应值。

## 自动构建与发布（GitHub Actions）

`.github/workflows/docker-build.yml` 是独立的发布流水线，**不调用 `build-latest.py`**，两者互不依赖：

- 每天 04:00 UTC（北京时间 12:00）跑一次，也可以手动触发（Actions → Docker Build and Publish → Run workflow）
- 手动触发可填项目（逗号分隔，留空或 `all` = 全部，取值就是仓库里的子目录名）、目标平台、是否强制重建、是否只 dry-run
- 默认同时构建 `linux/amd64,linux/arm64`，用 buildx + QEMU 模拟，推送到 `ghcr.io/<owner>/<项目>:<版本>` 和 `:latest`
- 远端已有同版本镜像就跳过（勾 `force` 才会重建），所以定时任务只重新发布真正更新的项目
- 取版本、读 `project.yaml`、推断构建参数、找不到本地 Dockerfile 时 clone tag，都是 job 里的内联 python 完成的

## 本地构建（build-latest.py）

`build-latest.py` 只负责本地构建镜像，不推送任何远端；多平台镜像由上面的 workflow 构建发布。

依赖只有 python3 标准库 + `git` + `docker`。访问 GitHub 一律走 HTTP API
（`https://api.github.com`，`GH_TOKEN`/`GITHUB_TOKEN` 只作为 `Authorization` 头），不使用 `gh` 等 GitHub 命令行工具。

```bash
./build-latest.py --dry-run                       # 只查版本和构建计划，不克隆不构建
./build-latest.py --build                         # 构建全部启用的项目
./build-latest.py --only mybb --force
./build-latest.py --platform linux/amd64          # 本地只支持单个平台
./build-latest.py --build-arg MYBB_URL=http://内网地址/mybb.zip   # 覆盖推断出来的参数
```

- 子项目目录里有 `Dockerfile` 就用它构建；
- 没有就把上游对应版本 `git clone --depth 1 --branch <tag>` 到 `.build/<项目>/`（已 gitignore），用上游自带的 Dockerfile 构建；
- `enable: false` 的项目跳过；
- 本地已有同版本镜像时跳过，`--force` 强制重建；
- 任何项目失败都不会影响其他项目，最后打印汇总表并以非 0 退出码结束。

`discuz-x5/`、`mybb/`、`qinglong/` 里还有 `docker-compose.yml`，想跑容器时直接用：

```bash
cd mybb && docker compose up -d --build
cd ../discuz-x5 && docker compose up -d --build
cd ../qinglong && docker compose up -d --build
```