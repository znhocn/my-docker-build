# My Docker Build

把几个上游项目的官方源码包/镜像打包成 Docker 镜像，每天定时检查上游 release，有新版本才重新构建并推送。

仓库根目录的 `build-latest.py` 用于**本地构建**：读各子项目的 `project.yaml`，取上游（GitHub / GitLab / Gitee）最新 release 版本，用该版本重新构建镜像，不推送远端。
镜像的自动构建与发布由 `.github/workflows/docker-build.yml` 独立完成，它不调用 `build-latest.py`。

## 配置

每个子项目一个 `project.yaml`，这是唯一的配置来源（键名大小写都认）：

```yaml
enable: true                              # false 时检查和构建都跳过，不写默认启用
GitHub: https://github.com/mybb/mybb      # 上游仓库，GitLab / Gitee 写法见下
version: v1.0.0                           # 可选：写死版本号，此时不再去上游查版本
# version:                                # 也可以写成映射，只从 tag 里提取版本号
#   pattern: '[0-9]+\.[0-9]+[-_.][0-9]{8}'  # 例：tag 为 X5.0-20261001#2 时版本号取 5.0-20261001
container_name: mybb                      # 可选：镜像名，不写默认用子项目目录名
build_context: "./lib"                    # 可选：构建上下文，相对源码根目录
dockerfile: "./lib/Dockerfile"            # 可选：Dockerfile，相对源码根目录
pre_build_cmd: ""                         # 可选：构建前在源码根目录执行的命令，一行一条
```

- **`version` 写成一个值就是固定版本号**：直接用它当镜像标签，不再请求上游接口，`--ref` 仍可指定克隆用的 ref（不写就用版本号本身当 ref）；因为没有 release 详情，`*_URL` 构建参数拿不到 release 附件，需要的话用 `--build-arg` 传。
- **`version` 写成映射只提供提取规则**，版本号仍然去上游查：默认去掉 tag 里的构建号（`X5.0-20261001#2` → `X5.0-20261001`），需要别的规则就写 `version.pattern`。
- 镜像标签：版本号 + `latest`。

### 构建位置：dockerfile / build_context / pre_build_cmd

子项目目录里有 `Dockerfile` 就用它构建；没有就把上游对应版本 `git clone --depth 1 --branch <tag>` 到 `.build/<项目>/`，用上游自带的 Dockerfile 构建。三个键用来覆盖这套默认行为：

- `dockerfile` / `build_context` 都是**相对源码根目录**的路径，源码根目录 = 声明的 Dockerfile 在子项目目录里时的子项目目录，否则是克隆出来的上游仓库根目录；
- 声明的路径先在子项目目录里找，找不到再在克隆结果里找，两处都没有就报错（不会退回上游自带的 Dockerfile）；
- 不写 `build_context` 时默认取 Dockerfile 所在目录，写了 `dockerfile` 但没写 `build_context` 就等于写了 `dirname(dockerfile)`；
- `pre_build_cmd` 在源码根目录（clone 后的仓库根目录）里 `sh -c` 执行，一行一条，按顺序执行，失败即中止构建；`--dry-run` 只打印不执行；
- 明确配置了 `dockerfile` 的项目会和仓库内的 Dockerfile 一样推断构建参数；纯克隆、用上游自带 Dockerfile 的项目保持上游默认值，只接受 `--build-arg` 覆盖。

`openbb/project.yaml` 就是这套配置的例子：上游的 `Dockerfile` 在子目录里，写明路径后不需要在仓库里再放一份。

### 上游仓库：GitHub / GitLab / Gitee

按平台选一个键，键名大小写都认：

```yaml
GitHub: https://github.com/owner/repo             # GitHub（企业版用 https://github.公司域名/owner/repo）
GitLab: https://gitlab.com/group/subgroup/repo    # GitLab，允许多级 group
Gitee:  https://gitee.com/owner/repo              # Gitee
git:    https://git.公司域名/team/repo            # 平台不认域名时，配一行 provider: gitlab
```

- 平台按域名自动判断：`github` / `gitlab` / `gitee`；域名看不出来时用 `provider: github|gitlab|gitee` 指定。
- 也兼容通用的 `repo` / `repository` / `url` / `source` / `upstream` 键，此时同样按域名判断平台。
- 认证（可选，公开仓库不用配）：GitHub 读 `GH_TOKEN` / `GITHUB_TOKEN`，GitLab 读 `GITLAB_TOKEN`，Gitee 读 `GITEE_TOKEN`。
- workflow 里 GitLab / Gitee 的 token 从仓库变量 `GITLAB_TOKEN` / `GITEE_TOKEN` 取（不是 secret），GitHub 直接用自动生成的 `github.token`。
- API 路径：GitHub `api.github.com/repos/{owner}/{repo}`（企业版 `/api/v3`），GitLab `/api/v4/projects/{url编码的路径}`，Gitee `/api/v5/repos/{owner}/{repo}`。
- **Gitee 的坑**：`/releases` 列表是按创建时间**升序**返回的，`/tags` 完全不按时间排序，所以取版本走 `/releases/latest`，并在列表/兜底路径上按 `created_at`、`tagger.date` 自己降序（这两个接口 `per_page=100`）；该端点 404 时自动退回列表。
- Gitee 的 `/archive/`、`/repository/archive/` 返回的是 HTML 页面而不是压缩包，所以 Gitee 项目不拼源码包地址：只用 release 附件，没有附件就用 Dockerfile 里的默认值。
- 附件：GitHub / Gitee 读 release 的 `assets[].browser_download_url`，GitLab 读 `assets.links[].direct_asset_url`；没有合适附件时按平台拼源码包地址（GitHub `/archive/refs/tags/`、GitLab `/-/archive/`、Gitee `/repository/archive/`）。

Dockerfile 里的构建参数按 `ARG` 名自动推断，不用额外配置：

| Dockerfile 里的 ARG | 传入的值 |
| --- | --- |
| `*_VERSION` | 版本号（`version` 写死时就是写死的那个） |
| `*_TAG` / `*_REF` | 上游 tag（`version` 写死时是版本号本身，或 `--ref` 给的值） |
| `GITHUB_URL` / `GITLAB_URL` / `GITEE_URL` / `GIT_URL` / `*_REPO_URL` | 上游仓库地址 |
| `*_URL` / `*_DOWNLOAD_URL` / `*_SRC_URL` | release 附件（按 Dockerfile 用的解压格式 + 仓库名匹配，例如 `unzip` 就找 `.zip`），没有合适附件就用该 tag 的源码压缩包 |
| `BASE_IMAGE` | 保持 Dockerfile 里的默认值 |
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
- 默认同时构建 `linux/amd64,linux/arm64`，用 buildx + QEMU 模拟，推送到 `ghcr.io/<owner>/<项目名>:<版本>` 和 `:latest`（`<项目名>` 是 `container_name`，没写就是子目录名）
- **不需要在仓库里配置任何 secret**：用 Actions 自动生成的 `github.token` 调 GitHub API 并登录 GHCR，配 `permissions: contents: read` + `packages: write` 即可推送
- 取版本、读 `project.yaml`、推断构建参数、按 `dockerfile` / `build_context` 定位构建位置、找不到本地 Dockerfile 时 clone tag、执行 `pre_build_cmd`，都是 job 里的内联 python / shell 完成的，`project.yaml` 的键和 `build-latest.py` 一致（包括 `version` 写死版本号时不查上游）

### 有新版本才构建

`plan` job 在决定要不要开构建之前，先比对已发布镜像的版本号和刚取到的最新 release 版本号：

1. 读 `ghcr.io/<owner>/<项目>:latest` 镜像的 `org.opencontainers.image.version` label，和最新版本号一致就说明镜像已是最新，直接不进 matrix，连构建 runner 都不会起；
2. 已发布版本号更旧（或镜像不存在）才进 matrix 重建并推送，新旧一致以外的第三种情况也会重建；
3. 多架构 manifest 的 label 读不出来，这时退化为检查 `:<版本>` 这个 tag 是否已存在，存在即视为已发布；
4. 勾 `force` 无视以上判断全部重建；勾 `dry-run` 不做版本比对，全部列进计划方便查看。

结果打在 Actions 日志里，例如 `mybb: ghcr.io/<owner>/mybb is already at 1841, skipping`。

`version` 写死版本号的项目比的是这个写死的值：上游发了新 release 也不会自动重建，要跟着上游更新就手动改 `project.yaml`（或者干脆删掉 `version` 让它继续自动查）。

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
- `dockerfile` / `build_context` / `pre_build_cmd` 可以覆盖上面两条，规则见上面「构建位置」一节；
- `container_name` 可以覆盖镜像名里的项目名；
- `enable: false` 的项目跳过；
- `version` 写死的项目不查上游，克隆时用写死的版本号当 ref（`--ref` 可以换掉）；
- 本地已有同版本镜像时跳过，`--force` 强制重建；
- 任何项目失败都不会影响其他项目，最后打印汇总表并以非 0 退出码结束。

`discuz-x5/`、`mybb/`、`qinglong/` 里还有 `docker-compose.yml`，想跑容器时直接用：

```bash
cd mybb && docker compose up -d --build
cd ../discuz-x5 && docker compose up -d --build
cd ../qinglong && docker compose up -d --build
```