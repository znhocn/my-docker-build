#!/usr/bin/env python3
"""Local build script: resolve the latest upstream release for every subproject declared in
project.yaml and rebuild its Docker image.

Images are built locally only and never pushed to a remote registry. Multi-platform images are
built and published by .github/workflows/docker-build.yml, which does not use this script.

- Subproject = a directory under the repo root that contains project.yaml (fixed directories
  such as node_modules are skipped).
- Switch: a subproject with enable: false (or enabled: false) in project.yaml is skipped
  entirely, no version lookup and no build. Missing the key means enabled.
- Version: the newest GitHub release that is neither draft nor prerelease; if there is none it
  falls back to tags. The build suffix (#2) is dropped, and a repeated project/repo name prefix
  is stripped (mybb_1841 -> 1841) while a leading v is kept (openbb-v5.0.0 -> v5.0.0).
  version.pattern overrides this with a custom regex.
- Build: the Dockerfile inside the subproject directory is used when present, otherwise the
  matching upstream version is cloned with git and built from its own Dockerfile.
- Build args are guessed from the ARG names in the Dockerfile (*_VERSION / *_TAG / *_URL,
  *_BASE keeps its default value); --build-arg overrides them. For *_URL the release asset is
  preferred (matched by the archive format used in the Dockerfile and by repo name) and the
  source archive of that tag is the fallback.

Requires nothing but the python3 standard library, git and docker. GitHub is only accessed over
HTTP (https://api.github.com, with GH_TOKEN/GITHUB_TOKEN sent as an Authorization header); no
GitHub command line tool such as gh is used.
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
SKIP_DIRS = {".git", "node_modules", "vendor", "__pycache__"}
BUILD_DIR = ".build"
REPO_URL_KEYS = ("github", "repo", "repository", "url", "source")
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


def parse_repo(value):
    text = str(value).strip().strip("\"'")
    match = re.match(r"(?:https?://)?(?:www\.)?github\.com[:/]+([^/\s]+)/([^/\s#?]+)", text, re.IGNORECASE)
    if not match:
        match = re.match(r"([^/\s]+)/([^/\s]+)", text)
    if not match:
        raise BuildError(f"cannot parse GitHub repository from {value!r}")
    return match.group(1), re.sub(r"\.git$", "", match.group(2))


def gh_api(path):
    request = urllib.request.Request(
        f"{GITHUB_API}{path}",
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "my-docker-build",
        },
    )
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as err:
        hint = " (set GH_TOKEN to raise the rate limit)" if err.code in (403, 429) else ""
        raise BuildError(f"GitHub API {path} returned {err.code} {err.reason}{hint}") from err
    except urllib.error.URLError as err:
        raise BuildError(f"GitHub API request failed: {err.reason}") from err


def resolve_version(owner, repo, ref=None, include_prerelease=False):
    if ref:
        return {"version": ref, "source": "given ref", "release": None}

    releases = gh_api(f"/repos/{owner}/{repo}/releases?per_page={PER_PAGE}")
    usable = [r for r in releases if not r.get("draft") and (include_prerelease or not r.get("prerelease"))]
    if usable:
        latest = usable[0]
        return {"version": latest["tag_name"], "source": "release", "release": latest}

    log("  no usable release (empty, draft or prerelease), falling back to tags")
    tags = [t.get("name") for t in gh_api(f"/repos/{owner}/{repo}/tags?per_page={PER_PAGE}") if t.get("name")]
    if not tags:
        raise BuildError(f"{owner}/{repo} has neither releases nor tags")
    if not include_prerelease:
        semantic = [t for t in tags if re.match(r"^v?\d", t)]
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
        name = asset.get("name") or ""
        url = asset.get("browser_download_url") or ""
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


def source_archive(release, repo_url, quoted_tag, prefer_ext):
    release = release or {}
    if prefer_ext == ".zip":
        return release.get("zipball_url") or f"{repo_url}/archive/refs/tags/{quoted_tag}.zip"
    return release.get("tarball_url") or f"{repo_url}/archive/refs/tags/{quoted_tag}.tar.gz"


def guess_build_args(dockerfile, version, tag_name, project, repo, repo_url, release):
    quoted = urllib.parse.quote(tag_name, safe="/")
    prefer_ext = preferred_ext(dockerfile)
    url = pick_asset_url((release or {}).get("assets") or [], version, project, repo, prefer_ext)
    if not url:
        url = source_archive(release, repo_url, quoted, prefer_ext)
    guessed = {}
    for name, default in ARG_RE.findall(dockerfile):
        upper = name.upper()
        if upper.endswith(("_VERSION", "_VERSION_TAG")) or upper == "VERSION":
            guessed[name] = version
        elif upper in ("GITHUB_URL", "SOURCE_URL", "REPO_URL") or upper.endswith("_REPO_URL"):
            guessed[name] = repo_url
        elif upper.endswith(("_TAG", "_REF")) and not default:
            guessed[name] = tag_name
        elif upper.endswith(("_URL", "_DOWNLOAD_URL", "_SRC_URL", "_ARCHIVE_URL")) and not default:
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


def clone_at(repo_url, version, dest):
    if shutil.which("git") is None:
        raise BuildError("git not found, cannot clone the upstream source")
    url = f"https://github.com/{repo_url}.git"
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    log(f"  git clone --depth 1 --branch {version}")
    proc = run(["git", "clone", "--depth", "1", "--branch", version, url, str(dest)], check=False)
    if proc.returncode == 0:
        return
    log(f"  cloning by tag failed, falling back to a full clone followed by checkout {version}")
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
    others = {key: value for key, value in config.items()
              if str(key).strip().lower() not in ENABLE_KEYS}
    for key, value in others.items():
        if str(key).strip().lower() in REPO_URL_KEYS and value:
            return parse_repo(value)
    for value in others.values():
        if "github.com" in str(value):
            return parse_repo(value)
    return None


def process_project(name, root, args):
    result = {"project": name, "version": "", "source": "", "context": "", "status": "", "note": ""}
    try:
        project_config = load_config(root / name / "project.yaml")
        if not project_enabled(project_config):
            result.update(status="skipped", note="enable is false in project.yaml")
            log(f"[{name}] disabled (project.yaml: enable false), skipping")
            return result

        owner_repo = repo_of(project_config)
        if not owner_repo:
            raise BuildError(f"{name}/project.yaml does not declare a GitHub repository")
        result["repo"] = "/".join(owner_repo)
        log(f"[{name}] resolving the latest version of {result['repo']} ...")
        info = resolve_version(*owner_repo, ref=args.ref, include_prerelease=args.include_prerelease)
        tag_name = info["version"]
        version_cfg = project_config.get("version")
        pattern = version_cfg.get("pattern", "") if isinstance(version_cfg, dict) else str(version_cfg or "")
        names = [name, owner_repo[0], owner_repo[1], re.sub(r"[-_.]+", "-", owner_repo[1]).lower()]
        version = docker_tag(clean_version(tag_name, pattern, names))
        result.update(version=version, source=f"{info['source']} {tag_name}")

        image = image_name(name, args.registry, args.owner or (git_owner(root) if args.registry else ""))
        tags = [f"{image}:{version}"]
        if not args.no_latest:
            tags.append(f"{image}:latest")

        if not args.force and not args.dry_run:
            current = local_image_version(f"{image}:latest")
            if (current and current == version) or local_image_exists(f"{image}:{version}"):
                result.update(status="skipped", note=f"local image already at {version}")
                log(f"[{name}] local image {image}:{version} already exists, use --force to rebuild")
                return result

        dockerfile = find_dockerfile(root / name)
        override_args = cli_build_args(args.build_arg)
        labels = {
            "org.opencontainers.image.title": name,
            "org.opencontainers.image.version": version,
            "org.opencontainers.image.source": f"https://github.com/{result['repo']}",
        }

        if dockerfile:
            result["context"] = f"{name}/{dockerfile.name}"
            log(f"[{name}] using local Dockerfile: {dockerfile.relative_to(root)}")
            build_args = guess_build_args(dockerfile.read_text(encoding="utf-8", errors="replace"),
                                          version, tag_name, name, result["repo"],
                                          f"https://github.com/{result['repo']}", info["release"])
            build_args.update(override_args)
            for key, value in build_args.items():
                log(f"  --build-arg {key}={value}")
            if args.dry_run:
                result.update(status="pending build", note="using local Dockerfile")
                return result
            build_image(root / name, dockerfile, tags, build_args, args.platform, labels)
        else:
            result["context"] = f"{args.build_dir}/{name}"
            log(f"[{name}] no local Dockerfile, cloning {result['repo']} @ {tag_name}")
            if args.dry_run:
                result.update(status="pending clone and build", note="clone the upstream source, then build")
                return result
            dest = root / args.build_dir / name
            clone_at(result["repo"], tag_name, dest)
            dockerfile = find_dockerfile(dest)
            if not dockerfile:
                raise BuildError(f"the cloned source has no Dockerfile either: {dest}")
            for key, value in override_args.items():
                log(f"  --build-arg {key}={value}")
            build_image(dest, dockerfile, tags, override_args, args.platform, labels)

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