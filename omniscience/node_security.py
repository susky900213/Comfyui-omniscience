"""Rule-based security review for ComfyUI custom node projects.

A node project is executable code rather than prompt text, so this scanner walks
the tree read-only (it never imports, builds or runs anything) and reports the
file, line and snippet behind every hit. The report keeps the shape of a Skill
report -- ``verdict``/``severity``/``findings``/``domains`` -- so the panel and
the AI reviewer can reuse it, and adds the coverage counters a large repository
needs (DESIGN.md section 11.2).
"""

from __future__ import annotations

import bisect
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .skill_security import (
    MAX_EVIDENCE_PER_FINDING,
    MAX_FINDINGS,
    MINING_TERMS,
    SEND_VERBS,
    SUSPICIOUS_HOST_PATTERNS,
    SEVERITY_RANK,
    URL_PATTERN,
    VERDICT_BY_TOP_SEVERITY,
    Rule,
)

ENGINE_VERSION = "node-security-v1"

# Node repositories range from a single 1 KB script to hundreds of files and
# several megabytes, so the walk is ordered by how much a file reveals about
# behaviour and then capped; the report records what was left out.
MAX_DISCOVERED_PATHS = 8_000
MAX_SCANNED_FILES = 400
MAX_SCANNED_BYTES = 2_000_000
MAX_FILE_BYTES = 200_000
MAX_SNIPPET_CHARS = 200
MAX_DEPENDENCIES = 40
MAX_IMPORTS = 40
MAX_DOMAINS = 30
MAX_INVENTORY = 60

CODE_SUFFIXES = {
    ".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx",
    ".sh", ".bash", ".zsh", ".fish", ".ps1", ".psm1", ".bat", ".cmd",
}
CONFIG_SUFFIXES = {
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env",
    ".service", ".socket", ".plist", ".xml", ".html", ".htm", ".css",
}
DOC_SUFFIXES = {".md", ".txt", ".rst"}
TEXT_SUFFIXES = CODE_SUFFIXES | CONFIG_SUFFIXES | DOC_SUFFIXES
MANIFEST_NAMES = {
    "requirements.txt", "requirements-dev.txt", "pyproject.toml", "setup.py",
    "setup.cfg", "package.json", "environment.yml", "environment.yaml",
    "meta.yaml", "conda.yaml", "dockerfile", "makefile", "config.json", "settings.json",
}
SKIP_FILE_NAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "uv.lock",
    "pipfile.lock", "cargo.lock", "composer.lock",
}
MINIFIED_SUFFIXES = (".min.js", ".min.css", ".map")
BINARY_SUFFIXES = {
    ".so", ".dylib", ".dll", ".pyd", ".exe", ".o", ".a", ".class", ".jar", ".wasm",
}
PICKLE_SUFFIXES = {".pkl", ".pickle"}
SKIP_DIR_NAMES = {
    "node_modules", "__pycache__", "dist", "build", "venv", "site-packages",
    "mypy_cache", "ruff_cache", "pytest_cache", "web_modules", "bower_components",
    "target", "vendor",
}
# Rules whose hits come from the file inventory rather than from source text.
INVENTORY_RULES = {"binary-artifact", "bundled-pickle"}

# Code-flavoured credential sources. The Skill scanner's marker list also matches
# words like ``token`` or ``secret`` inside identifiers, which almost every torch
# file contains, so node rules use this narrower file-and-env oriented form.
CREDENTIAL_FILES = (
    r"\.ssh/(id_[a-z0-9]+|config)|id_rsa\b|known_hosts|\.aws/credentials|\.kube/config|"
    r"\.docker/config\.json|\.netrc\b|\.npmrc\b|\.pypirc",
    r"['\"][^'\"]*\.env['\"]|load_dotenv\s*\(",
    r"\b(keychain|find-generic-password|1password|lastpass|bitwarden)\b",
    r"\b(?:read|load|open|extract|dump|export)\b[^\n]{0,30}\b(private[_ -]?key|mnemonic|seed phrase)\b"
    r"(?![^\n]{0,12}-----)|钱包[^\n]{0,12}(助记词|私钥)",
    r"['\"][^'\"]*(Cookies|Login Data|Local State|Web Data|places\.sqlite)['\"]",
    r"(浏览?器|chrome|firefox|safari|edge)[^\n]{0,16}(cookies?|登录数据|密码库)|cookiejar|load_cookies",
)
CREDENTIAL_BULK_ENV = (
    r"\*\*\s*os\.environ|os\.environ\.(copy|items|keys|values|dict)|\.\.\.process\.env|"
    r"JSON\.stringify\s*\(\s*process\.env",
)
# Bulk environment reads stay a medium signal: forwarding ``os.environ`` to a
# subprocess is routine build tooling, while a named credential file is not.
CREDENTIAL_FILES_ANY = "(" + "|".join(CREDENTIAL_FILES) + ")"
LOCAL_HOST = r"(?!127\.0\.0\.1|localhost|\[::1\]|0\.0\.0\.0)"


@dataclass(frozen=True)
class _Matched:
    """One rule hit: where it happened and what the line looks like."""

    location: str
    snippet: str


RULES: tuple[Rule, ...] = (
    Rule(
        id="crypto-mining",
        title="挖矿或算力滥用代码",
        severity="critical",
        advice="该节点包含连接矿池或执行挖矿程序的代码，会占用本机算力并产生费用，已拒绝接管。",
        patterns=(MINING_TERMS,),
    ),
    Rule(
        id="remote-code-execution",
        title="下载远程代码并执行",
        severity="critical",
        advice="该节点把网络内容或解码结果当作代码执行，远端可随时替换为恶意逻辑；这是节点供应链劫持最常见的方式。",
        patterns=(
            r"(curl|wget)[^\n]{0,240}\|\s*(bash|zsh|sh|python\d?|node|ruby|perl)\b",
            r"\b(bash|sh)\s+<(curl|wget)\b",
            r"(powershell|pwsh)[^\n]{0,120}(-enc\b|-encodedcommand|invoke-expression|\biex\b)",
            r"(?<![\w.])exec\s*\([^\n]{0,90}(b64decode|base64|codecs\.decode|urlopen|requests\.|urlretrieve|fromhex|response|payload|read\(\))",
            r"(?<![\w.])eval\s*\([^\n]{0,90}(b64decode|base64|codecs\.decode|urlopen|requests\.|urlretrieve|fromhex|response|payload|read\(\)|\.text\b|\.content\b)",
            r"\bnew\s+Function\s*\(",
            r"\bgetattr\s*\(\s*__builtins__\s*,",
            r"\bos\.(system|popen)\s*\([^\n]{0,120}(curl|wget|https?://)",
        ),
    ),
    Rule(
        id="credential-exfiltration",
        title="读取凭据并外发到外部地址",
        severity="critical",
        advice="该节点同时存在读取密钥/凭据和把数据发送到外部的代码，可能造成泄露，已拒绝接管。",
        patterns=(CREDENTIAL_FILES_ANY, SEND_VERBS, r"https?://|\.onion|webhook|矿池"),
        all_of=True,
    ),
    Rule(
        id="callback-sink",
        title="指向回调/匿名接收服务",
        severity="critical",
        advice="该节点向 webhook 捕获、临时文件分享或匿名地址发送数据，这类接收端通常用于数据外传。",
        patterns=(SUSPICIOUS_HOST_PATTERNS["exfil / callback sink"],),
    ),
    Rule(
        id="unsafe-deserialization",
        title="反序列化不可信数据",
        severity="high",
        advice="该节点用 pickle/marshal/yaml.load 载入数据或权重，文件内容可在加载瞬间执行任意代码。",
        patterns=(
            r"\bpickle\.loads?\s*\(",
            r"\bmarshal\.loads?\s*\(",
            r"\byaml\.load\s*\((?![^\n]{0,120}(safe|Loader))",
            r"\bjoblib\.load\s*\(",
            r"\bshelve\.open\s*\(",
            r"\bnumpy\.load\s*\([^\n)]*\)\s*\[",
            r"\btorch\.load\s*\([^\n]{0,120}pickle_module",
        ),
    ),
    Rule(
        id="dynamic-eval",
        title="动态执行字符串代码",
        severity="high",
        advice="该节点用 eval/exec/__import__ 执行字符串；字符串若来自输入、文件或网络即可被注入。注意 PyTorch 的 model.eval() 不会被计入。",
        patterns=(
            r"(?<![\w.])(?<!def )eval\s*\(",
            r"(?<![\w.])(?<!def )exec\s*\(",
            r"\b__import__\s*\(",
            r"\b(window|global|globalThis)\s*\.\s*eval\s*\(",
        ),
    ),
    Rule(
        id="shell-command",
        title="以 shell 方式执行命令",
        severity="high",
        advice="该节点调用系统 shell 执行命令，命令字符串一旦来自输入或网络即可被注入；接管前请确认它实际会执行什么。",
        patterns=(
            r"\bshell\s*=\s*True\b",
            r"\bos\.(system|popen)\s*\(",
            r"\bsubprocess\.(getoutput|getstatusoutput)\s*\(",
            r"\bpty\.spawn\s*\(",
            r"\bchild_process\s*\.\s*exec(Sync)?\s*\(",
        ),
    ),
    Rule(
        id="credential-access",
        title="读取本机密钥、凭据或浏览器数据",
        severity="high",
        advice="该节点读取 SSH 私钥、云凭证、钥匙串、浏览器登录数据或 .env 文件；普通节点不应需要这些内容。",
        patterns=CREDENTIAL_FILES,
    ),
    Rule(
        id="environment-collection",
        title="批量读取环境变量",
        severity="medium",
        advice="该节点读取整个环境变量集合，其中通常包含其它服务的密钥；若只为配置自身，请改为让用户手动提供。",
        patterns=CREDENTIAL_BULK_ENV + (r"for\s+[a-z_]+\s+in\s+os\.environ\b",),
    ),
    Rule(
        id="filesystem-discovery",
        title="遍历用户目录收集文件信息",
        severity="medium",
        advice="该节点扫描主目录或桌面/文档等位置，可能把本机文件清单带出；请确认它只需要任务自身的文件。",
        patterns=(
            r"os\.walk\s*\([^\n]{0,60}(expanduser|Path\.home|\"~\"|'~'|/Users/|/home/)",
            r"Path\.(home|expanduser)\s*\(\)[^\n]{0,60}\.(glob|rglob|iterdir)",
            r"glob\.[a-z]+\s*\([^\n]{0,60}(Desktop|Documents|Downloads|\"~|'~)",
            r"os\.listdir\s*\([^\n]{0,40}(expanduser|Path\.home)",
        ),
    ),
    Rule(
        id="data-upload",
        title="把本地数据上传到外部服务",
        severity="medium",
        advice="该节点把本地文件、图片或请求体发送到外部地址，请先确认接收方和数据范围。",
        patterns=(
            rf"({SEND_VERBS})[^\n]{{0,60}}https?://{LOCAL_HOST}",
            r"\brequests\.(post|put)\s*\([^\n]{0,90}files\s*=",
            r"\b(put_object|upload_fileobj|upload_file)\s*\(|\b(boto3|oss2)\s*\.\s*(client|resource)\b",
            r"(read_bytes|read_text|open\s*\(|base64\.b64encode)[\s\S]{0,80}?(requests\.(post|put)|httpx\.(post|put)|urlopen)",
        ),
    ),
    Rule(
        id="network-egress",
        title="节点主动访问网络",
        severity="low",
        advice="该节点会自行发起网络请求（多为下载模型或调用远端服务），Omniscience 不会替它放行任何地址。",
        patterns=(
            r"\brequests\.(get|post|put|delete)\s*\(",
            r"\b(httpx|aiohttp)\b",
            r"\burllib\.request\.(urlopen|urlretrieve)\s*\(",
            r"\b(urlopen|urlretrieve)\s*\(",
            r"\bhttp\.client\b",
            r"\bfetch\s*\(|XMLHttpRequest|axios\s*\.|\bWebSocket\s*\(|new\s+EventSource",
            r"socket\.socket\s*\(",
        ),
    ),
    Rule(
        id="comfy-runtime-patch",
        title="改写 ComfyUI 运行时",
        severity="high",
        advice="该节点对 ComfyUI 内部模块做猴子补丁或热重载，会改变整体行为并可能影响其它节点；请确认补丁范围。",
        patterns=(
            r"\bsetattr\s*\(\s*(comfy|comfyui|nodes|execution|server|folder_paths|folder_client|folder_config|checkpoint_loader|clip_loader)\b",
            r"\b(comfy|comfy\.[a-z_]+|execution|folder_paths|samplers|model_management|checkpoint_pickle)\.[A-Za-z_][A-Za-z0-9_]*\s*=[^=~]",
            # ``server`` alone is a common local name for the app's own routes, so the
            # patch has to reach into the ComfyUI server instance to count.
            r"\bserver\.instance(?:\.[A-Za-z_][A-Za-z0-9_]*)*\.[A-Za-z_][A-Za-z0-9_]*\s*=[^=~]",
            r"\boriginal_([a-z0-9_]+_)?(forward|compute|apply|process|method|func|function|impl|call|fn)\b\s*=",
            r"importlib\.reload\s*\(",
        ),
    ),
    Rule(
        id="foreign-path-write",
        title="写入其它节点或 ComfyUI 核心文件",
        severity="high",
        advice="该节点会写入或删除 custom_nodes、ComfyUI 核心文件与 site-packages，影响范围超出自身；节点管理器类项目确实需要，其它情况请核对。",
        patterns=(
            r"custom_nodes[^\n]{0,60}(open|write_text|write_bytes|copy|copytree|move|rmtree|makedirs|mkdir|unlink|remove|rename)",
            r"(open|write_text|write_bytes|shutil\.[a-z_]+|os\.(remove|replace|rename))[^\n]{0,60}(nodes\.py|execution\.py|server\.py|main\.py|default_settings\.json|extra_model_paths)",
            r"(open|write_text|write_bytes|install|copy|shutil\.[a-z_]+|pip)[^\n]{0,60}site-packages",
        ),
    ),
    Rule(
        id="model-directory-write",
        title="下载或写入模型权重",
        severity="low",
        advice="该节点会下载或写入模型权重，请在确认前核对权重的来源。",
        patterns=(
            r"(urlretrieve|requests\.get|http\.client)[^\n]{0,120}(\.safetensors|\.ckpt|\.pth|\.pt\b|\.bin|models/)",
            r"torch\.save\s*\(",
            r"open\s*\([^\n]{0,60}(models|checkpoints)[^\n]{0,40}['\"]wb",
        ),
    ),
    Rule(
        id="persistence-and-privilege",
        title="持久化、提权或自启动改动",
        severity="high",
        advice="该节点会写入启动项、定时任务、shell 配置或授权密钥，长期改变本机环境。",
        patterns=(
            r"\bcrontab\b|systemctl\s+(enable|start)|/etc/(systemd|cron|profile)|launch ?agents?|~/Library/LaunchAgents?\b",
            r"launchctl\s+load|Register-ScheduledTask|schtasks\s+/create",
            r"authorized_keys|\bssh-copy-id\b",
            # The dot keeps shell config paths (``~/.zshrc``) distinct from ordinary
            # identifiers such as ``profiles.append(card)`` in UI code.
            r"\.(zshrc|bashrc|bash_profile|zprofile|profile)\b[^\n]{0,40}(open|write_text|write_bytes|>>|append|echo)",
            r"\b(sudo|doas)\b[^\n]{0,80}(rm|curl|wget|chown|tee|echo|install|defaults write|chmod)",
            r"git config\s+--global|\.gitconfig|\.githooks|修改 hosts",
        ),
    ),
    Rule(
        id="destructive-commands",
        title="破坏性或不可逆操作",
        severity="high",
        advice="该节点包含可能清空数据或覆盖磁盘的代码，请确认它不会在你接管后自行执行。",
        patterns=(
            r"\brm\s+-[a-z]*r[a-z]*f\b(?![^\n]{0,40}(apt/lists|/tmp/|\.cache/|__pycache__))",
            r"\bmkfs(\.|\s)|\bdd\b[^\n]{0,60}of=/dev/|\bchmod\s+-R\s+777\b",
            r"\bgit push\b[^\n]{0,60}--force\b|\bdrop (table|database)\b|\btruncate table\b",
            r"shutil\.rmtree\s*\(\s*['\"]?(\.|\.\.|/|~)",
            r"os\.(remove|unlink|rmdir)\s*\([^\n]{0,50}(expanduser|Path\.home|models|output|input|custom_nodes)",
            r"删除(所有|全部)(文件|数据|模型|工作流)",
        ),
    ),
    Rule(
        id="obfuscated-payload",
        title="编码或混淆的可执行内容",
        severity="high",
        advice="该节点含大段 Base64/十六进制或解码后再执行的逻辑，无法从源码判断真实行为。",
        patterns=(
            r"[A-Za-z0-9+/]{200,}={0,2}",
            r"\\x[0-9a-f]{2}(\\x[0-9a-f]{2}){20,}",
            r"(base64\.(b64decode|decodebytes)|codecs\.decode|bin\.a2b_[a-z]+|atob|String\.fromCharCode)[^\n]{0,90}\b(exec|eval|compile|__import__|getattr)\b",
            r"\bbase64\b[^\n]{0,32}(-d\b|--decode)",
            r"\brot-?13\b|\bxxd\s+-r\b",
            r"\.join\s*\(\s*chr\s*\(",
        ),
    ),
    Rule(
        id="supply-chain",
        title="自行安装依赖或解压外部包",
        severity="medium",
        advice="该节点会在运行时安装依赖、克隆仓库或解压压缩包，实际装入的内容可能与审阅时不同；建议由你手动安装。",
        patterns=(
            r"(?i)(subprocess|os\.system|os\.popen|Popen|child_process)[\s\S]{0,200}?(pip install|pip3 install|npm (i|install)|yarn add|pnpm add|conda install|apt(-get)? install|brew install|git clone)",
            r"(pip|pip3|uv pip|conda)[^\n]{0,140}(--index-url|--extra-index-url|--trusted-host|-i\s+http)",
            r"git\+https?://",
            r"(curl|wget)[^\n]{0,120}\.(tar\.(gz|xz|bz2)|whl|zip|gem)\b",
            r"\b(extractall\s*\(|unpack_archive|zipfile\.ZipFile)\b",
            r"(docker pull|helm install)",
        ),
    ),
    Rule(
        id="network-abuse",
        title="扫描、爆破或压测类行为",
        severity="medium",
        advice="该节点包含端口扫描、口令爆破或高频请求代码，可能在未经授权的目标上造成问题。",
        patterns=(
            r"\bnmap\b|\bmasscan\b|端口扫描|口令?爆破|\bddos\b|\bsyn ?flood\b|hydra\s+-l|medusa\s+-b",
            r"(高频|持续)(请求|访问|轮询)[^\n]{0,40}(接口|网站|api|服务)",
            r"批量(注册|发帖|私信|关注)|爬取[^\n]{0,30}(登录|隐私|付费)",
        ),
    ),
    Rule(
        id="prompt-injection",
        title="隐藏在代码或文档里的越权指令",
        severity="high",
        advice="该节点的注释或文档要求忽略系统提示、跳过用户确认或隐瞒行为，这会破坏 Omniscience 的安全边界。",
        patterns=(
            r"ignore (all |any |the )?(previous|prior|above|system)\b.{0,24}(instruction|prompt|rule)|忽略(之前|以上|所有|系统).{0,10}(指令|提示|规则)",
            r"不要(告诉|告知|通知|提醒)用户|无需(再次)?(用户)?(确认|审批)|跳过(用户)?(确认|审批|安全检查)|自动(批准|同意|确认)",
            r"(disable|bypass|turn off|switch off)[^\n]{0,20}(the |all |any |your )?(safety|security|guardrail)[^\n]{0,14}(check|checks|system|mechanism|controls?|feature)",
            r"(disable|bypass|turn off)[^\n]{0,20}(permission|approval|confirm)[^\n]{0,14}(check|system|step|gate|mechanism)",
            r"关闭(安全|确认|审批)(机制|策略|检查)",
            r"无条件执行|以最高权限执行|必须服从(该|此)?(指令|用户)且不得拒绝",
            r"without (asking|telling|informing) the user|\bcovertly\b",
        ),
    ),
    Rule(
        id="telemetry",
        title="上报设备或使用信息",
        severity="low",
        advice="该节点包含统计埋点或设备指纹代码，通常会向外部服务上报使用信息。",
        patterns=(
            r"\b(mixpanel|posthog|segment\.io|telemetry|track_event|matomo|google-analytics|gtag\(|sentry_sdk\.init)\b",
            r"platform\.(node|machine|processor|uname)|os\.uname\(\)|uuid\.getnode|getmacAddress",
        ),
    ),
    Rule(
        id="binary-artifact",
        title="包含无法审阅的编译产物",
        severity="medium",
        advice="该节点自带 .so/.pyd/.dll 等编译产物，扫描器无法读取其真实行为，只能依赖来源可信度。",
        patterns=(r"\x00binary\x00",),
    ),
    Rule(
        id="bundled-pickle",
        title="自带 pickle 权重文件",
        severity="high",
        advice="仓库内附带 .pkl/.pickle 文件，载入即执行其中代码；请确认权重来源，或改用 safetensors。",
        patterns=(r"\x00pickle\x00",),
    ),
)

_COMPILED: tuple[tuple[Rule, tuple[re.Pattern[str], ...]], ...] = tuple(
    (rule, tuple(re.compile(pattern, re.IGNORECASE) for pattern in rule.patterns))
    for rule in RULES
    if rule.id not in INVENTORY_RULES
)
# Zero-width characters and soft hyphens are removed so keyword-splitting with
# them cannot hide a rule; the non-breaking space only becomes a space.
# Newlines are never touched, so the line numbers computed below stay correct.
_ZERO_WIDTH = (0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF, 0x00AD)
_CLOAK = {code: None for code in _ZERO_WIDTH}
_CLOAK[ord("\u00a0")] = ord(" ")
_DATA_URI = re.compile(r"data:[\w.+-]+/[\w.+-]+;base64,[A-Za-z0-9+/=\s]+", re.IGNORECASE)
_NEWLINES = re.compile("\n")
_PY_IMPORT = re.compile(r"^ *(from|import) +([A-Za-z_][^\n#]*)", re.MULTILINE)
_JS_IMPORT = re.compile(r"""(?:from|require\()\s*['"]([^'"./][\w@/.-]+)""", re.IGNORECASE)
_DEP_BLOCK = re.compile(r"(?ms)^\s*(?:dependencies|optional-dependencies|install_requires)\s*[:=][^\n]*(\n(?=\s+\S)*)?")
_DEP_TOKEN = re.compile(r"""['"]([A-Za-z][\w.[-]{1,60})""")
_REQUIREMENT_LINE = re.compile(r"^\s*([A-Za-z][\w.[-]{1,60})\s*(?:\[[^\]]*\])?\s*(?:[<>=!~;(]|$)")
_STDLIB = set(getattr(sys, "stdlib_module_names", ())) | {"typing_extensions"}


LOW_VALUE_DIRECTORIES = {"docs", "doc", "examples", "example", "test", "tests", "trash", "benchmark", "assets", "locales", "i18n"}


def _in_low_value_directory(rel: str) -> bool:
    return any(part.lower() in LOW_VALUE_DIRECTORIES for part in rel.split("/")[:-1])


def _priority(name: str, suffix: str, rel: str) -> int:
    """Lower sorts first: manifests and code reveal more behaviour than docs."""
    if name in MANIFEST_NAMES:
        return 0
    base = 3 if _in_low_value_directory(rel) else 0
    if suffix in CODE_SUFFIXES:
        return base + 1
    if suffix in CONFIG_SUFFIXES:
        return base + 2
    return base + 3


def _shipped_elsewhere(rel: str) -> bool:
    """True for documentation and scratch code ComfyUI never imports."""
    return rel.lower().endswith(tuple(DOC_SUFFIXES)) or _in_low_value_directory(rel)


def _collect(root: Path) -> tuple[list[tuple[str, Path]], list[Path], int, int, bool]:
    """Order the readable files, then list the compiled artefacts found on disk."""
    candidates: list[tuple[int, str, Path, str]] = []
    binaries: list[Path] = []
    discovered = 0
    skipped = 0
    truncated = False
    for current, directories, filenames in os.walk(str(root), followlinks=False):
        directories[:] = sorted(
            name
            for name in directories
            if not name.startswith(".")
            and name.lower() not in SKIP_DIR_NAMES
            and not name.lower().endswith("egg-info")
        )
        base = Path(current)
        for name in sorted(filenames):
            discovered += 1
            if discovered > MAX_DISCOVERED_PATHS:
                truncated = True
                break
            path = base / name
            lowered = name.lower()
            suffix = path.suffix.lower()
            if suffix in BINARY_SUFFIXES or suffix in PICKLE_SUFFIXES:
                binaries.append(path)
                continue
            if lowered in SKIP_FILE_NAMES or lowered.endswith(MINIFIED_SUFFIXES):
                skipped += 1
                continue
            if suffix not in TEXT_SUFFIXES and lowered not in MANIFEST_NAMES:
                continue
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                if path.stat().st_size > MAX_FILE_BYTES:
                    skipped += 1
                    continue
            except OSError:
                continue
            rel = str(path.relative_to(root))
            candidates.append((_priority(lowered, suffix, rel), lowered, path, rel))
        if truncated:
            break
    candidates.sort(key=lambda item: (item[0], item[1], item[3]))
    return [(rel, path) for _prio, _name, path, rel in candidates], binaries, discovered, skipped, truncated


def _read(path: Path) -> str:
    raw = path.read_bytes()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1", errors="replace")


def _matchable(text: str) -> str:
    """Same-length copy of ``text`` with cloaking characters and inline blobs removed."""
    scrubbed = text.translate(_CLOAK)
    return _DATA_URI.sub(lambda match: "\x00" * len(match.group(0)), scrubbed)


def _matched(body: str, starts: list[int], rel: str, pattern: re.Pattern[str]) -> Iterator[_Matched]:
    for match in pattern.finditer(body):
        index = bisect.bisect_right(starts, match.start()) - 1
        begin = starts[index]
        end = starts[index + 1] if index + 1 < len(starts) else len(body)
        yield _Matched(f"{rel}:{index + 1}", " ".join(body[begin:end].split())[:MAX_SNIPPET_CHARS])


def _record(hits: dict[str, dict[str, Any]], rule: Rule, matched: _Matched) -> None:
    bucket = hits.setdefault(rule.id, {"rule": rule, "occurrences": 0, "files": set(), "seen": set(), "evidence": []})
    bucket["files"].add(matched.location.rsplit(":", 1)[0])
    # One line can satisfy several patterns of the same rule; count it once.
    if matched.location in bucket["seen"]:
        return
    bucket["seen"].add(matched.location)
    bucket["occurrences"] += 1
    if len(bucket["evidence"]) >= MAX_EVIDENCE_PER_FINDING:
        return
    bucket["evidence"].append(f"{matched.location} {matched.snippet}"[: MAX_SNIPPET_CHARS + 60])


def _scan_file(hits: dict[str, dict[str, Any]], rel: str, text: str) -> None:
    body = _matchable(text)
    starts = [0] + [match.end() for match in _NEWLINES.finditer(body)]
    for rule, patterns in _COMPILED:
        if rule.all_of:
            if any(pattern.search(body) is None for pattern in patterns):
                continue
            for matched in _matched(body, starts, rel, patterns[0]):
                _record(hits, rule, matched)
            continue
        for pattern in patterns:
            for matched in _matched(body, starts, rel, pattern):
                _record(hits, rule, matched)


def _dependency_names(rel: str, text: str) -> list[str]:
    name = rel.lower().rsplit("/", 1)[-1]
    if name == "package.json":
        try:
            payload = json.loads(text)
        except (ValueError, TypeError):
            return []
        collected: list[str] = []
        if isinstance(payload, dict):
            for key in ("dependencies", "devDependencies", "peerDependencies"):
                block = payload.get(key)
                if isinstance(block, dict):
                    collected.extend(str(item) for item in block)
        return collected
    if name.startswith("requirements") or name.endswith((".yml", ".yaml")) or name in {"environment.yml", "environment.yaml"}:
        lines: list[str] = []
        for line in text.splitlines():
            if line.lstrip().startswith(("#", "-r ", "--")):
                continue
            match = _REQUIREMENT_LINE.match(line)
            if match:
                lines.append(match.group(1))
        return lines
    return [token.group(1) for block in _DEP_BLOCK.finditer(text) for token in _DEP_TOKEN.finditer(block.group(0))]


def _external_urls(text: str) -> list[str]:
    hosts: set[str] = set()
    for url in URL_PATTERN.findall(text):
        authority = re.sub(r"^https?://", "", url, flags=re.IGNORECASE)
        host = re.split(r"[/:?#]", authority, maxsplit=1)[0].strip("@.").lower().split("@")[-1]
        if "." in host:
            hosts.add(host)
    return sorted(hosts)


def _inventory_finding(hits: dict[str, dict[str, Any]], root: Path, items: list[Path], rule_id: str) -> None:
    if not items:
        return
    rule = next(item for item in RULES if item.id == rule_id)
    paths = [str(item.relative_to(root)) for item in items]
    hits[rule_id] = {
        "rule": rule,
        "occurrences": len(paths),
        "files": set(paths[:MAX_EVIDENCE_PER_FINDING]),
        "evidence": list(paths[:MAX_EVIDENCE_PER_FINDING]),
    }


def _verdict(findings: list[dict[str, Any]]) -> tuple[str, str]:
    top = max((SEVERITY_RANK.get(str(item.get("severity")), 0) for item in findings), default=-1)
    if top < 0:
        return "clean", "clean"
    severity = next((key for key, rank in SEVERITY_RANK.items() if rank == top), "clean")
    return VERDICT_BY_TOP_SEVERITY.get(severity, "clean"), severity


def _note_python_imports(text: str, imports: dict[str, int]) -> None:
    for match in _PY_IMPORT.finditer(text):
        clause = match.group(2)
        names = [clause.split(" import ")[0]] if match.group(1) == "from" else clause.split(",")
        for raw in names:
            module = raw.strip().split(".")[0].split(" as ")[0].strip()
            if module and module not in _STDLIB:
                imports[module] = imports.get(module, 0) + 1


def _note_js_imports(text: str, imports: dict[str, int]) -> None:
    for match in _JS_IMPORT.finditer(text):
        name = match.group(1)
        module = name if name.startswith("@") else name.split("/")[0]
        if module:
            imports[module] = imports.get(module, 0) + 1


def scan_project(root: Any, *, source: str = "", checked_at: float | None = None) -> dict[str, Any]:
    """Scan one node project directory and return a Skill-compatible report."""
    path = Path(root).expanduser()
    if not path.is_dir():
        raise ValueError(f"{path} 不是一个可扫描的目录。")
    files, binaries, discovered, skipped, walk_truncated = _collect(path)

    hits: dict[str, dict[str, Any]] = {}
    imports: dict[str, int] = {}
    dependencies: list[str] = []
    urls: set[str] = set()
    scanned = 0
    total_bytes = 0
    inventory: list[str] = []
    truncated = walk_truncated
    for rel, file_path in files:
        if scanned >= MAX_SCANNED_FILES or total_bytes >= MAX_SCANNED_BYTES:
            truncated = True
            break
        try:
            text = _read(file_path)
        except OSError:
            skipped += 1
            continue
        scanned += 1
        total_bytes += len(text)
        if len(inventory) < MAX_INVENTORY:
            inventory.append(rel)
        _scan_file(hits, rel, text)
        _note_python_imports(text, imports)
        _note_js_imports(text, imports)
        for name in _dependency_names(rel, text):
            if len(dependencies) >= MAX_DEPENDENCIES:
                break
            if name not in dependencies:
                dependencies.append(name)
        urls.update(_external_urls(text))

    _inventory_finding(hits, path, [item for item in binaries if item.suffix.lower() in BINARY_SUFFIXES], "binary-artifact")
    _inventory_finding(hits, path, [item for item in binaries if item.suffix.lower() in PICKLE_SUFFIXES], "bundled-pickle")

    findings: list[dict[str, Any]] = []
    for bucket in hits.values():
        rule: Rule = bucket["rule"]
        severity = rule.severity
        advice = rule.advice
        # Documentation and scratch code are not what ComfyUI imports, so a critical
        # hit confined to them warns for review instead of blocking adoption.
        if severity == "critical" and bucket["files"] and all(_shipped_elsewhere(item) for item in bucket["files"]):
            severity = "medium"
            advice = f"{rule.advice}（仅出现在文档或示例代码中，未作为阻止接管的依据）"
        findings.append(
            {
                "rule": rule.id,
                "title": rule.title,
                "severity": severity,
                "advice": advice,
                "occurrences": bucket["occurrences"],
                "files": sorted(bucket["files"])[:MAX_EVIDENCE_PER_FINDING],
                "evidence": bucket["evidence"],
            }
        )
    if urls:
        findings.append(
            {
                "rule": "external-links",
                "title": f"引用 {len(urls)} 个外部域名",
                "severity": "low",
                "advice": "代码中存在外部地址；Omniscience 不会自动访问它们，但节点运行时可能会。",
                "occurrences": len(urls),
                "files": [],
                "evidence": sorted(urls)[:MAX_DOMAINS],
            }
        )
    if truncated or skipped:
        findings.append(
            {
                "rule": "coverage-limit",
                "title": "扫描范围受限",
                "severity": "low",
                "advice": (
                    f"项目较大，本次覆盖 {scanned}/{discovered} 个文件（约 {total_bytes // 1024} KB），"
                    f"另有 {skipped} 个文件未读取。"
                ),
                "occurrences": max(discovered - scanned, 0),
                "files": [],
                "evidence": [f"已扫描 {scanned} 个文件，共 {total_bytes} 字节"],
            }
        )
    findings.sort(key=lambda item: (-SEVERITY_RANK.get(str(item["severity"]), 0), -int(item.get("occurrences") or 0)))
    verdict, severity = _verdict(findings)
    return {
        "engine": ENGINE_VERSION,
        "verdict": verdict,
        "severity": severity,
        "findings": findings[:MAX_FINDINGS],
        "domains": sorted(urls)[:MAX_DOMAINS],
        "source": str(source or path.name),
        "root": str(path),
        "checked_at": checked_at if checked_at is not None else datetime.now(timezone.utc).timestamp(),
        "scripts_executed": False,
        "files_scanned": scanned,
        "files_total": discovered,
        "files_skipped": skipped,
        "bytes_scanned": total_bytes,
        "truncated": bool(truncated),
        "inventory": inventory,
        "dependencies": dependencies,
        "imports": [module for module, _count in sorted(imports.items(), key=lambda item: (-item[1], item[0]))[:MAX_IMPORTS]],
    }

