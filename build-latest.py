#!/usr/bin/env python3
"""Local build script: resolve the latest upstream release for every subproject declared in
project.yaml and rebuild its Docker image.

Images are built locally only and never pushed to a remote registry. Multi-platform images are
built and published by .github/workflows/docker-build.yml, which does not use this script.

- Subproject = a directory under the repo root that contains project.yaml (fixed directories
  such as node_modules are skipped).
- Switch: a subproject with enable: false (or enabled: false) in project.yaml is skipped
  entirely, no version lookup and no build. Missing the key means enabled.
- Upstream: GitHub, GitLab (gitlab.com and self hosted, subgroups allowed) and Gitee are
  supported. The repository is taken from the github / gitlab / gitee / git key, or from the
  generic repo / repository / url / source / upstream key with the provider detected from the
  host name; provider: forces it when the host is unrecognisable.
- Version: the newest release that is neither draft nor prerelease; if there is none it
  falls back to tags. The build suffix (#2) is dropped, and a repeated project/repo name prefix
  is stripped (mybb_1841 -> 1841) while a leading v is kept (openbb-v5.0.0 -> v5.0.0).
  version.pattern overrides this with a custom regex.
- Build: the Dockerfile inside the subproject directory is used when present, otherwise the
  matching upstream version is cloned with git and built from its own Dockerfile. dockerfile and
  build_context override that: both are relative to the source root (the subproject directory when
  the declared dockerfile is there, the clone otherwise) and build_context defaults to the directory
  of the dockerfile. pre_build_cmd runs in the source root before the build, one command per line.
- Image: container_name overrides the subproject directory name in the image name, so the tags
  become <registry>/<owner>/<container_name>:<version> instead of using the directory name.
- Build args are guessed from the ARG names in the Dockerfile (*_VERSION / *_TAG / *_URL,
  BASE_IMAGE keeps its default value); --build-arg overrides them. For *_URL the release asset is
  preferred (matched by the archive format used in the Dockerfile and by repo name) and the
  source archive of that tag is the fallback.

Requires nothing but the python3 standard library, git and docker. The upstreams are only
accessed over HTTP (https://api.github.com, https://<host>/api/v4 for GitLab,
https://<host>/api/v5 for Gitee, with GH_TOKEN/GITHUB_TOKEN, GITLAB_TOKEN or GITEE_TOKEN sent
as an Authorization header when they are set); no GitHub command line tool such as gh is used.
"""

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_ROOT = SCRIPT_DIR

GITHUB_API = "https://api.github.com"
PER_PAGE = 1
GITEE_PAGE = 100  # gitee does not sort releases or tags, the newest one has to be picked by date
SKIP_DIRS = {".git", "node_modules", "vendor", "__pycache__"}
BUILD_DIR = ".build"
PROVIDERS = ("github", "gitlab", "gitee")
TOKEN_ENV = {"github": ("GH_TOKEN", "GITHUB_TOKEN"),
             "gitlab": ("GITLAB_TOKEN", "GL_TOKEN"),
             "gitee": ("GITEE_TOKEN",)}
REPO_URL_KEYS = ("github", "gitlab", "gitee", "git", "repo", "repository", "url", "source", "upstream")
URL_ARG_NAMES = ("GITHUB_URL", "GITLAB_URL", "GITEE_URL", "GIT_URL", "SOURCE_URL", "REPO_URL", "UPSTREAM_URL")
REPO_URL_RE = re.compile(
    r"^(?:(?:https?|ssh|git)://)?(?:[^@/\s]+@)?(?P<host>[^:/\s]+)(?::\d+)?[:/]+(?P<path>[^#?\s]+?)/?$")
ENABLE_KEYS = ("enable", "enabled")
TRUE_VALUES = {"true", "yes", "on", "1", "y"}
ARG_RE = re.compile(r"^\s*ARG\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:=\s*(\S*))?\s*$", re.MULTILINE)
DOCKERFILE_NAMES = ("Dockerfile", "Dockerfile.*", "*.Dockerfile")
TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$")
SKIP_ASSET_RE = re.compile(r"\.(sha1|sha256|sha512|md5|sum|txt|asc|sig|log|deb|rpm|dmg|exe|msi|iso)$", re.IGNORECASE)


class BuildError(Exception):
    pass


def log(message):
    print(message, file=sys.stderr, flush=True)


def run(cmd, cwd=None, capture=False, check=True):
    argv = shlex.split(cmd) if isinstance(cmd, str) else list(cmd)
    proc = subprocess.run(
        argv,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
    )
    if check and proc.returncode != 0:
        raise BuildError(f"command failed ({proc.returncode}): {shlex.join(argv)}")
    return proc


def strip_comment(line):
    out, quote = [], None
    for char in line:
        if quote:
            out.append(char)
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
            out.append(char)
        elif char == "#" and (not out or out[-1] in " \t"):
            break
        else:
            out.append(char)
    return "".join(out).strip()


def clean_value(value):
    return value.strip().rstrip(",").strip().strip("\"'")


def parse_simple_yaml(text, name="project.yaml"):
    """Fallback parser used when pyyaml is missing: key: value plus one nesting level."""
    data = {}
    parent = None
    for raw in text.splitlines():
        line = strip_comment(raw)
        if not line or line.startswith("- "):
            continue
        key, sep, value = line.partition(":")
        if not sep:
            continue
        if raw[:1] in (" ", "\t"):
            if parent is None or not isinstance(data.get(parent), dict):
                raise BuildError(f"{name} contains nested structures, please install pyyaml")
            data[parent][key.strip().strip("\"'")] = clean_value(value)
            continue
        key = key.strip().strip("\"'")
        value = clean_value(value)
        if value:
            data[key] = value
            parent = None
        else:
            data[key] = {}
            parent = key
    return data


def load_config(path):
    if not path.is_file():
        raise BuildError(f"config file not found: {path}")
    text = path.read_text(encoding="utf-8")
    try:
        import yaml
    except ImportError:
        return parse_simple_yaml(text, path.name)
    data = yaml.safe_load(text)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise BuildError(f"malformed config file: {path}")
    return data


def pick_key(config, key):
    """Read a config key case insensitively, an empty value counts as missing."""
    for name in (key, key.capitalize(), key.upper()):
        value = config.get(name)
        if value not in (None, ""):
            return value
    return ""


def config_text(config, key):
    """Read a scalar config value as text, an empty value counts as missing."""
    value = pick_key(config, key)
    if isinstance(value, dict):
        # the pyyaml-less parser turns a valueless key into an empty mapping
        return ""
    return str(value or "").strip().strip("\"'")


def config_commands(config, key):
    """Read a command list from project.yaml, one command per line, empty when there is none."""
    return [line.strip() for line in config_text(config, key).splitlines() if line.strip()]


def detect_provider(host, declared=""):
    name = str(declared or "").strip().lower()
    if name:
        if name not in PROVIDERS:
            raise BuildError(f"unknown provider {name!r}, use one of {', '.join(PROVIDERS)}")
        return name
    low = str(host).lower()
    for candidate in PROVIDERS:
        if candidate in low:
            return candidate
    raise BuildError(f"cannot tell the provider of host {host!r}: use a github:/gitlab:/gitee: key or set provider:")


def parse_repo(value, declared=""):
    """Parse a github, gitlab or gitee repository URL into a descriptor dict."""
    text = str(value).strip().strip("\"'")
    match = REPO_URL_RE.match(text)
    if not match:
        raise BuildError(f"cannot parse a repository URL from {value!r}")
    host = match.group("host")
    parts = [part for part in match.group("path").split("/") if part]
    if len(parts) < 2:
        raise BuildError(f"{value!r} does not look like an owner/repo URL")
    provider = detect_provider(host, declared)
    if provider == "gitlab":
        path = "/".join(parts)
        api = f"https://{host}/api/v4"
        ref = urllib.parse.quote(path, safe="")
        releases = f"{api}/projects/{ref}/releases?per_page={PER_PAGE}"
        tags = f"{api}/projects/{ref}/repository/tags?per_page={PER_PAGE}"
        latest = ""
    elif provider == "gitee":
        # gitee returns the releases oldest first and does not sort the tags at all, so a
        # single item page is useless there: read a full page and let resolve_version sort it.
        path = f"{parts[0]}/{parts[1]}"
        api = f"https://{host}/api/v5"
        releases = f"{api}/repos/{path}/releases?per_page={GITEE_PAGE}"
        tags = f"{api}/repos/{path}/tags?per_page={GITEE_PAGE}"
        latest = f"{api}/repos/{path}/releases/latest"
    else:
        path = f"{parts[0]}/{parts[1]}"
        api = GITHUB_API if "github.com" in host.lower() else f"https://{host}/api/v3"
        releases = f"{api}/repos/{path}/releases?per_page={PER_PAGE}"
        tags = f"{api}/repos/{path}/tags?per_page={PER_PAGE}"
        latest = ""
    web = f"https://{host}/{path}"
    return {"provider": provider, "host": host, "path": path, "web": web,
            "clone": f"{web}.git", "api": api, "releases": releases, "tags": tags, "latest": latest}


def api_json(url, provider):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "my-docker-build"}
    if provider == "github":
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    token = next((os.environ.get(name) for name in TOKEN_ENV[provider] if os.environ.get(name)), "")
    if token:
        if provider == "gitlab":
            headers["PRIVATE-TOKEN"] = token
        elif provider == "gitee":
            headers["Authorization"] = f"token {token}"
        else:
            headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as err:
        hint = " (set GH_TOKEN to raise the rate limit)" if provider == "github" and err.code in (403, 429) else ""
        raise BuildError(f"{provider} API {url} returned {err.code} {err.reason}{hint}") from err
    except urllib.error.URLError as err:
        raise BuildError(f"{provider} API request failed: {err.reason}") from err


def release_entry(item, provider):
    """Normalize a github, gitlab or gitee release payload."""
    assets = []
    if provider == "gitlab":
        for link in (item.get("assets") or {}).get("links") or []:
            assets.append({"name": link.get("name") or "",
                           "url": link.get("direct_asset_url") or link.get("url") or ""})
    else:
        for asset in item.get("assets") or []:
            assets.append({"name": asset.get("name") or "", "url": asset.get("browser_download_url") or ""})
    return {"tag": item.get("tag_name") or "", "draft": bool(item.get("draft")),
            "prerelease": bool(item.get("prerelease")), "assets": assets, "raw": item}


def tag_date(item):
    """Return the date of a tag payload, gitee does not order its tag list."""
    return str((item.get("tagger") or {}).get("date") or (item.get("commit") or {}).get("date") or "")


def resolve_version(repo, ref=None, include_prerelease=False):
    provider = repo["provider"]
    if ref:
        return {"version": ref, "source": "given ref", "release": None}

    usable = []
    if repo["latest"] and not include_prerelease:
        # gitee /releases/latest is the newest published one, its list endpoint is the oldest first
        try:
            payload = api_json(repo["latest"], provider)
        except BuildError as err:
            log(f"  {err}, falling back to the release list")
            payload = None
        entry = release_entry(payload, provider) if isinstance(payload, dict) else None
        if entry and entry["tag"] and not entry["draft"] and not entry["prerelease"]:
            usable.append(entry)

    if not usable:
        payload = api_json(repo["releases"], provider)
        items = payload if isinstance(payload, list) else []
        if provider == "gitee":
            items = sorted(items, key=lambda item: str(item.get("created_at") or ""), reverse=True)
        for item in items:
            entry = release_entry(item, provider)
            if entry["tag"] and not entry["draft"] and (include_prerelease or not entry["prerelease"]):
                usable.append(entry)
        if usable and provider == "gitee":
            log(f"  the gitee release list is not ordered, picked {usable[0]['tag']} as the newest one by date")
    if usable:
        return {"version": usable[0]["tag"], "source": "release", "release": usable[0]}

    log("  no usable release (empty, draft or prerelease), falling back to tags")
    payload = api_json(repo["tags"], provider)
    items = payload if isinstance(payload, list) else []
    if provider == "gitee":
        items = sorted(items, key=tag_date, reverse=True)
    tags = [item.get("name") for item in items if isinstance(item, dict) and item.get("name")]
    if not tags:
        raise BuildError(f"{repo['path']} has neither releases nor tags")
    if not include_prerelease:
        semantic = [tag for tag in tags if re.match(r"^v?\d", tag)]
        if semantic:
            tags = semantic
    return {"version": tags[0], "source": "tag", "release": None}


def strip_name_prefix(version, names):
    """Strip a repeated project/repo name prefix from a version: mybb_1841 -> 1841,
    openbb-v5.0.0 -> v5.0.0.

    Only the prefix itself is removed, a v behind it is kept (mybb_v1.8.41 -> v1.8.41).
    Nothing is stripped unless what follows starts with a digit or v+digit (or with another
    known prefix), so a tag like v2.22.0 stays v2.22.0.
    """
    known = sorted({name for name in names if name}, key=len, reverse=True)
    version = str(version)
    while version:
        matched = None
        for name in known:
            match = re.match(rf"{re.escape(name)}[-_.:/ ]*", version, re.IGNORECASE)
            if match and match.end() < len(version):
                matched = match
                break
        if matched is None:
            break
        rest = version[matched.end():]
        if re.match(r"[vV]?\d", rest):
            return rest
        if not any(re.match(rf"{re.escape(name)}[-_.:/ ]*[^\s]", rest, re.IGNORECASE) for name in known):
            return version
        version = rest
    return version


def clean_version(tag_name, pattern="", names=()):
    if pattern:
        try:
            match = re.search(pattern, tag_name)
        except re.error:
            match = None
        if match:
            version = match.group(0)
            if match.groups():
                version = next((group for group in match.groups() if group), version)
            return version
        log(f"  tag {tag_name} does not match pattern {pattern}, using the tag name as the version")
    version = re.split(r"#", tag_name, 1)[0].strip(" -_/vV") if "#" in tag_name else tag_name.strip()
    return strip_name_prefix(version, names)


def normalize(text):
    return re.sub(r"[^a-z0-9]+", "", str(text).lower())


def preferred_ext(dockerfile):
    zip_like = bool(re.search(r"unzip|\bunar\b|\b7z\b", dockerfile))
    tar_like = bool(re.search(r"\btar\b|gunzip", dockerfile))
    if zip_like and not tar_like:
        return ".zip"
    if tar_like and not zip_like:
        return ".tar.gz"
    return ""


def pick_asset_url(assets, version, project, repo, prefer_ext):
    best, best_score = "", 0
    repo_key = normalize(repo.split("/")[-1])
    project_key = normalize(project)
    version_key = normalize(version)
    for asset in assets:
        name = str(asset.get("name") or "")
        url = str(asset.get("url") or "")
        if not url or SKIP_ASSET_RE.search(name):
            continue
        if prefer_ext and not name.lower().endswith(prefer_ext):
            continue
        score = 0
        if (repo_key and repo_key in normalize(name)) or (project_key and project_key in normalize(name)):
            score += 3
        if version_key and version_key in normalize(name):
            score += 1
        if score > best_score:
            best, best_score = url, score
    return best if best_score >= 3 else ""


def source_archive(release, repo, quoted_tag, prefer_ext):
    """Return the source archive of a tag, using the layout of the provider.

    Gitee is left out on purpose: its /archive/ and /repository/archive/ endpoints answer with an
    HTML page instead of the archive, so an empty string is returned and the release attachment
    or the Dockerfile default is used.
    """
    raw = (release or {}).get("raw") or {}
    provider = repo["provider"]
    if provider == "github":
        if prefer_ext == ".zip":
            return raw.get("zipball_url") or f"{repo['web']}/archive/refs/tags/{quoted_tag}.zip"
        return raw.get("tarball_url") or f"{repo['web']}/archive/refs/tags/{quoted_tag}.tar.gz"
    if provider != "gitlab":
        return ""
    name = quoted_tag.rsplit("/", 1)[-1]
    ext = ".zip" if prefer_ext == ".zip" else ".tar.gz"
    stem = urllib.parse.quote(f"{repo['path'].rsplit('/', 1)[-1]}-{name}")
    return f"{repo['web']}/-/archive/{quoted_tag}/{stem}{ext}"


def guess_build_args(dockerfile, version, tag_name, project, repo, release):
    quoted = urllib.parse.quote(tag_name, safe="/")
    prefer_ext = preferred_ext(dockerfile)
    asset = pick_asset_url((release or {}).get("assets") or [], version, project, repo["path"], prefer_ext)
    url = asset or source_archive(release, repo, quoted, prefer_ext)
    guessed = {}
    for name, default in ARG_RE.findall(dockerfile):
        upper = name.upper()
        if upper.endswith(("_VERSION", "_VERSION_TAG")) or upper == "VERSION":
            guessed[name] = version
        elif upper in URL_ARG_NAMES or upper.endswith("_REPO_URL"):
            guessed[name] = repo["web"]
        elif upper.endswith(("_TAG", "_REF")) and not default:
            guessed[name] = tag_name
        elif upper.endswith(("_URL", "_DOWNLOAD_URL", "_SRC_URL", "_ARCHIVE_URL")):
            # a release attachment that matched the version replaces a hardcoded default,
            # a made up archive url never does
            if url and (not default or asset):
                guessed[name] = url
    return guessed


def cli_build_args(pairs):
    args = {}
    for item in pairs or []:
        key, sep, value = item.partition("=")
        key = key.strip()
        if not key:
            raise BuildError(f"invalid --build-arg: {item!r}")
        if sep:
            args[key] = value
        elif key in os.environ:
            args[key] = os.environ[key]
        else:
            log(f"  --build-arg {key} has no value and no matching environment variable, ignored")
    return args


def find_dockerfile(directory):
    candidates = []
    for pattern in DOCKERFILE_NAMES:
        candidates.extend(sorted(p for p in directory.glob(pattern) if p.is_file()))
    seen, unique = set(), []
    for path in candidates:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique[0] if unique else None


def local_build_paths(project_dir, dockerfile_cfg, context_cfg):
    """Resolve the (dockerfile, context) of a subproject that builds from its own directory.

    Returns (None, None) when the project has no usable Dockerfile there and the upstream clone has
    to provide one.
    """
    if dockerfile_cfg:
        dockerfile = project_dir / dockerfile_cfg
        if not dockerfile.is_file():
            return None, None
        context = (project_dir / context_cfg) if context_cfg else dockerfile.parent
        return dockerfile, context
    found = find_dockerfile(project_dir)
    return (found, project_dir) if found else (None, None)


def cloned_build_paths(dest, dockerfile_cfg, context_cfg):
    """Same as local_build_paths for a clone of the upstream source, errors out when nothing fits."""
    if dockerfile_cfg:
        dockerfile = dest / dockerfile_cfg
        if not dockerfile.is_file():
            raise BuildError(f"project.yaml declares dockerfile {dockerfile_cfg}, "
                             f"but the cloned source has no such file: {dockerfile}")
        context = (dest / context_cfg) if context_cfg else dockerfile.parent
        return dockerfile, context
    found = find_dockerfile(dest)
    if not found:
        raise BuildError(f"the cloned source has no Dockerfile either: {dest}")
    return found, dest


def relative(path, base):
    """Display a path relative to base when it is below it, the plain path otherwise."""
    try:
        return str(path.relative_to(base))
    except ValueError:
        return str(path)


def planned_clone_paths(name, build_dir, dockerfile_cfg, context_cfg):
    """The paths a clone of the upstream source will build from, reported by --dry-run."""
    base = Path(build_dir) / name
    if not dockerfile_cfg:
        return str(base)
    dockerfile = base / dockerfile_cfg
    return str(base / context_cfg) if context_cfg else str(dockerfile)


def run_commands(commands, cwd):
    """Run the pre_build_cmd commands of a project in its source root, one shell command per line."""
    for command in commands:
        log(f"  $ {command}")
        run(["sh", "-c", command], cwd=cwd)


def clone_at(url, version, dest):
    if shutil.which("git") is None:
        raise BuildError("git not found, cannot clone the upstream source")
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    log(f"  git clone --depth 1 --branch {version}")
    proc = run(["git", "clone", "--depth", "1", "--branch", version, url, str(dest)], check=False)
    if proc.returncode == 0:
        return
    log("  cloning by tag failed, falling back to a full clone followed by checkout " + version)
    shutil.rmtree(dest, ignore_errors=True)
    run(["git", "clone", "--filter=blob:none", url, str(dest)])
    run(["git", "-C", str(dest), "checkout", version])


def git_owner(root):
    proc = run(["git", "remote", "get-url", "origin"], cwd=root, capture=True, check=False)
    if proc.returncode != 0:
        return ""
    match = re.search(r"github\.com[:/]+([^/\s]+)/", proc.stdout or "")
    return match.group(1) if match else ""


def image_name(project, registry, owner):
    if not registry:
        return project
    return f"{registry}/{owner or 'library'}/{project}"


def docker_tag(version):
    tag = re.sub(r"[^A-Za-z0-9._-]", "-", version)
    if not TAG_RE.match(tag):
        raise BuildError(f"version cannot be used as an image tag: {version}")
    return tag


def local_image_version(image):
    proc = run(["docker", "image", "inspect", "--format",
                "{{ index .Config.Labels \"org.opencontainers.image.version\" }}", image],
               capture=True, check=False)
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def local_image_exists(image):
    return run(["docker", "image", "inspect", image], capture=True, check=False).returncode == 0


def build_image(context, dockerfile, tags, build_args, platforms, labels):
    cmd = ["docker"]
    if platforms:
        cmd.append("buildx")
    cmd.append("build")
    for key, value in build_args.items():
        cmd += ["--build-arg", f"{key}={value}"]
    for key, value in labels.items():
        cmd += ["--label", f"{key}={value}"]
    for tag in tags:
        cmd += ["--tag", tag]
    if platforms:
        cmd += ["--platform", platforms, "--load"]
    cmd += ["--file", str(dockerfile), str(context)]
    log("  " + " ".join(shlex.quote(part) for part in cmd))
    run(cmd)


def discover_projects(root, only, skip):
    names = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name.startswith(".") or entry.name in SKIP_DIRS:
            continue
        if (entry / "project.yaml").is_file():
            names.append(entry.name)
    if only:
        wanted = {n.strip() for n in only.split(",") if n.strip()}
        missing = wanted - set(names)
        if missing:
            raise BuildError(f"projects passed to --only do not exist: {', '.join(sorted(missing))}")
        names = [n for n in names if n in wanted]
    if skip:
        dropped = {n.strip() for n in skip.split(",") if n.strip()}
        names = [n for n in names if n not in dropped]
    if not names:
        raise BuildError(f"no subproject containing project.yaml found under {root}")
    return names


def parse_bool(value, default=True):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().strip("\"'").lower() in TRUE_VALUES


def project_enabled(config):
    for key in ENABLE_KEYS:
        if key in config:
            return parse_bool(config[key])
    return True


def repo_of(config):
    """Return the repository descriptor declared in project.yaml, None when there is none."""
    declared = pick_key(config, "provider")
    problems = []
    for key in REPO_URL_KEYS:
        value = pick_key(config, key)
        if value in (None, ""):
            continue
        try:
            return parse_repo(value, declared)
        except BuildError as err:
            problems.append(str(err))
    if problems:
        raise BuildError("; ".join(problems))
    return None


def process_project(name, root, args):
    result = {"project": name, "version": "", "source": "", "context": "", "status": "", "note": ""}
    try:
        project_config = load_config(root / name / "project.yaml")
        if not project_enabled(project_config):
            result.update(status="skipped", note="enable is false in project.yaml")
            log(f"[{name}] disabled (project.yaml: enable false), skipping")
            return result

        repo = repo_of(project_config)
        if not repo:
            raise BuildError(f"{name}/project.yaml does not declare a repository "
                             f"(use a github:, gitlab:, gitee: or git: key)")
        result["repo"] = repo["path"]
        log(f"[{name}] resolving the latest {repo['provider']} version of {repo['path']} ...")
        info = resolve_version(repo, ref=args.ref, include_prerelease=args.include_prerelease)
        tag_name = info["version"]
        version_cfg = project_config.get("version")
        pattern = version_cfg.get("pattern", "") if isinstance(version_cfg, dict) else str(version_cfg or "")
        names = [name, *repo["path"].split("/"), re.sub(r"[-_.]+", "-", repo["path"].split("/")[-1]).lower()]
        version = docker_tag(clean_version(tag_name, pattern, names))
        result.update(version=version, source=f"{info['source']} {tag_name}")

        project_dir = root / name
        container = config_text(project_config, "container_name") or name
        dockerfile_cfg = config_text(project_config, "dockerfile")
        context_cfg = config_text(project_config, "build_context")
        commands = config_commands(project_config, "pre_build_cmd")

        image = image_name(container, args.registry, args.owner or (git_owner(root) if args.registry else ""))
        tags = [f"{image}:{version}"]
        if not args.no_latest:
            tags.append(f"{image}:latest")

        if not args.force and not args.dry_run:
            current = local_image_version(f"{image}:latest")
            if (current and current == version) or local_image_exists(f"{image}:{version}"):
                result.update(status="skipped", note=f"local image already at {version}")
                log(f"[{name}] local image {image}:{version} already exists, use --force to rebuild")
                return result

        override_args = cli_build_args(args.build_arg)
        labels = {
            "org.opencontainers.image.title": container,
            "org.opencontainers.image.version": version,
            "org.opencontainers.image.source": repo["web"],
        }

        # a declared dockerfile is looked up in the subproject directory first and inside the clone
        # afterwards, without one the local Dockerfile is used and otherwise the clone provides it
        dockerfile, context = local_build_paths(project_dir, dockerfile_cfg, context_cfg)
        needs_clone = dockerfile is None
        source = root / args.build_dir / name if needs_clone else project_dir
        # the upstream Dockerfile of a plain clone keeps its own defaults, only an explicitly
        # configured or repository owned dockerfile gets the guessed build args
        guess = bool(dockerfile_cfg) or not needs_clone

        if needs_clone:
            missing = f", {dockerfile_cfg} is not there" if dockerfile_cfg else ""
            log(f"[{name}] no local Dockerfile{missing}, cloning {result['repo']} @ {tag_name}")
            planned = planned_clone_paths(name, args.build_dir, dockerfile_cfg, context_cfg)
        else:
            log(f"[{name}] using Dockerfile: {relative(dockerfile, root)}")
            planned = relative(context, root)
        log(f"[{name}] build context: {planned}")

        if args.dry_run:
            result.update(context=planned,
                          status="pending clone and build" if needs_clone else "pending build",
                          note=f"clone {result['repo']} @ {tag_name}, then build" if needs_clone
                          else f"Dockerfile {relative(dockerfile, root)}")
            for command in commands:
                log(f"  pre_build_cmd: {command}")
            return result

        if needs_clone:
            clone_at(repo["clone"], tag_name, source)
            dockerfile, context = cloned_build_paths(source, dockerfile_cfg, context_cfg)

        result["context"] = relative(context, root)
        run_commands(commands, source)
        if guess:
            build_args = guess_build_args(dockerfile.read_text(encoding="utf-8", errors="replace"),
                                          version, tag_name, name, repo, info["release"])
            build_args.update(override_args)
        else:
            build_args = dict(override_args)
        for key, value in build_args.items():
            log(f"  --build-arg {key}={value}")
        build_image(context, dockerfile, tags, build_args, args.platform, labels)

        result.update(status="ok", note=", ".join(tags))
        return result
    except BuildError as err:
        result.update(status="failed", note=str(err))
        log(f"[{name}] {err}")
        return result


EPILOG = """\
Examples:
  ./build-latest.py --dry-run                                    # only show the plan
  ./build-latest.py --build                                      # build every enabled project
  ./build-latest.py --only mybb --force                          # only mybb, rebuild even if unchanged
  ./build-latest.py --ref mybb_1841                              # use a given ref, version becomes 1841
  ./build-latest.py --registry ghcr.io --platform linux/amd64    # target one platform locally
"""


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="build-latest.py",
        description="Resolve the latest upstream version declared in project.yaml and rebuild the Docker image",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EPILOG,
    )
    parser.add_argument("-b", "--build", action="store_true", help="start building (default mode)")
    parser.add_argument("--force", action="store_true", help="rebuild even if a local image with the same version exists")
    parser.add_argument("--dry-run", action="store_true", help="only print the plan, do not clone or build")
    parser.add_argument("--only", help="only handle the given projects, comma separated")
    parser.add_argument("--skip", help="skip the given projects, comma separated")
    parser.add_argument("--root", default=str(DEFAULT_ROOT), help="repository root (defaults to the script directory)")
    parser.add_argument("--ref", help="use the given ref (tag/branch/commit) instead of looking the version up")
    parser.add_argument("--include-prerelease", action="store_true", help="also accept prerelease versions")
    parser.add_argument("--registry", default="", help="image registry such as ghcr.io; empty means local tags only")
    parser.add_argument("--owner", help="registry owner, defaults to the owner of the git remote origin")
    parser.add_argument("--platform", help="target platform, comma separated; only one platform is supported locally, uses buildx")
    parser.add_argument("--no-latest", action="store_true", help="do not tag the image as latest")
    parser.add_argument("--build-arg", action="append", metavar="K=V", help="extra build argument, repeatable")
    parser.add_argument("--build-dir", default=BUILD_DIR, help=f"directory for cloned sources (default {BUILD_DIR}/)")
    return parser.parse_args(argv)


SUMMARY_COLUMNS = ("Project", "Upstream", "Version", "Source", "Build context", "Result")


def shorten(text, limit=72):
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def display_width(text):
    return sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in str(text))


def print_summary(results):
    rows = [list(SUMMARY_COLUMNS)]
    for item in results:
        note = item["status"]
        if item["note"]:
            note += f" ({shorten(item['note'])})"
        rows.append([item["project"], item.get("repo") or "-", item["version"] or "-",
                     item["source"] or "-", item["context"] or "-", note])
    widths = [max(display_width(row[index]) for row in rows) for index in range(len(SUMMARY_COLUMNS))]
    for row in rows:
        cells = [row[index] + " " * (widths[index] - display_width(row[index]))
                 for index in range(len(widths))]
        print("  ".join(cells).rstrip())


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        # Print the help instead of running a full build when no argument is given
        parse_args(["--help"])
    args = parse_args(argv)
    root = Path(args.root).resolve()

    if args.build and args.dry_run:
        log("error: --build and --dry-run cannot be used together")
        return 2

    platforms = [item for item in (args.platform or "").split(",") if item]
    if len(platforms) > 1 and not args.dry_run:
        log(f"error: a local image holds a single platform (got {args.platform}); "
            "multi-platform images are built and pushed by .github/workflows/docker-build.yml")
        return 2

    try:
        names = discover_projects(root, args.only, args.skip)
    except BuildError as err:
        log(f"error: {err}")
        return 2

    log(f"repo: {root}")
    log(f"projects: {', '.join(names)}")
    if not args.dry_run and shutil.which("docker") is None:
        log("error: docker not found, cannot build")
        return 2

    results = [process_project(name, root, args) for name in names]
    print()
    print_summary(results)
    return 1 if any(item["status"] == "failed" for item in results) else 0


if __name__ == "__main__":
    sys.exit(main())