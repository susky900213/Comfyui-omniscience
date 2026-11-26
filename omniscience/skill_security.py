"""Rule-based security review for Skill text (DESIGN.md section 11).

Skills are prompt instructions that an agent will follow, so the risk model is
the same as for prompt injection plus shell commands a Skill might tell the
agent to run. This module only reads text -- it never executes anything -- and
classifies a Skill as ``clean``, ``review`` (needs the user's explicit
confirmation) or ``blocked`` (refused unless the user insists).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

ENGINE_VERSION = "skill-security-v1"

# How a Skill is reviewed before it enters the library (settings key
# ``skill_security_mode``); ``ai`` and ``hybrid`` need one model call per save.
REVIEW_MODES = ("static", "ai", "hybrid")
DEFAULT_REVIEW_MODE = "static"

SUSPICIOUS_HOST_PATTERNS = {
    "short link": r"\b(bit\.ly|t\.co|tinyurl\.com|goo\.gl|is\.gd|rb\.gy|c\.cutt\.ly)\b",
    "paste / file drop": r"\b(pastebin\.com|paste\.ee|hastebin\.com|transfer\.sh|file\.io|catbox\.moe|ix\.io|0x0\.st|temp\.link)\b",
    "exfil / callback sink": r"\b(webhook\.site|requestbin\.[a-z.]+|hookbin\.com|burpcollaborator\.net|interact\.sh|dnslog\.cn|oast\.(fun|pro|online)|ipify\.org)\b",
    "tunnel": r"\b[a-z0-9-]+\.ngrok(app\.io|\.free|\.io)\b|\bngrok\b|\blocaltunnel\b|\bcloudflared\b",
    "anonymous network": r"\b[a-z0-9-]+\.onion\b|\bi2p\b",
    "raw IP endpoint": r"https?://\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?",
    "non-standard port": r"https?://[^\s/`'\"<>]+:(?!\d{2}\b)\d{3,5}\b",
}

MINING_TERMS = (
    r"\bxmrig\b|\bminerd\b|\bcpuminer\b|\bcpu\.mine\b|\bcoinhive\b|\bcryptonight\b|\bcryptoloot\b|"
    r"\bnicehash\b|\bhashvault\b|\bweb ?miner\b|\bnanopool\b|\bethermine\b|\bf2pool\b|\bunmineable\b|"
    r"stratum[0-9]?(ssl|tcp)?:\/\/|\bmine (monero|ethereum|bitcoin|xmr|eth|btc|coins?)\b|"
    r"\bcryptocurrency mining\b|\bcrypto mining\b|\b挖矿\b|\b矿池\b|\b算力变现\b|\b挂机挖\b"
)

SECRET_MARKERS = (
    r"\.ssh/(id_[a-z0-9]+|config)|id_rsa|\bknown_hosts\b|\.aws/credentials|\.kube/config|"
    r"\.netrc|\.npmrc|\.env\b|_token\b|\bapi[_ -]?key\b|access[_ -]?token|secret[_ -]?key|\bsecrets?\b|"
    r"(OPENAI|ANTHROPIC|OMNISCIENCE|OMNISCIENCE|AWS|GITHUB|GITLAB|STRIPE|GOOGLE)[A-Z_]*KEY|"
    r"keychain|1password|lastpass|browser cookies?|登录态|会话 ?cookie|钱包.*(助记词|私钥)|私钥|密钥|口令|密码"
)

SEND_VERBS = (
    r"\b(curl|wget|nc|netcat|http\.client|requests\.post|fetch)\b|上传|发送|回传|提交到|发到|上报|外发|"
    r"\bpost to\b|\bsend (to|over)\b|\bexfiltrat\w*\b|\bupload(ed|s|ing)?\b"
)


@dataclass(frozen=True)
class Rule:
    """One detection rule; ``patterns`` are OR-matched unless ``all_of`` is set."""

    id: str
    title: str
    severity: str
    advice: str
    patterns: tuple[str, ...]
    all_of: bool = False


RULES: tuple[Rule, ...] = (
    Rule(
        id="crypto-mining",
        title="挖矿或算力滥用指令",
        severity="critical",
        advice="该 Skill 要求连接矿池或执行挖矿程序，会占用本机算力并产生费用，已拒绝安装。",
        patterns=(MINING_TERMS,),
    ),
    Rule(
        id="remote-code-execution",
        title="下载远程脚本并直接执行",
        severity="critical",
        advice="该 Skill 让 Agent 从网络下载代码并立即执行，内容可被随时替换；请只保留说明文档，不要包含执行管道。",
        patterns=(
            r"(curl|wget)[^\n]{0,240}\|\s*(bash|zsh|sh|python\d?|node|ruby|perl)\b",
            r"\b(bash|sh)\s+<(curl|wget)\b",
            r"(powershell|pwsh)[^\n]{0,120}(-enc\b|-encodedcommand|invoke-expression|\biex\b)",
            r"base64\s+(-d|--decode)[^\n]{0,80}\|\s*(bash|zsh|sh)\b",
            r"\beval\s*[\"'`$]\(",
        ),
    ),
    Rule(
        id="credential-exfiltration",
        title="读取凭据并外发到外部地址",
        severity="critical",
        advice="该 Skill 同时涉及读取密钥/凭据和把数据发送到外部地址，可能造成泄露，已拒绝安装。",
        patterns=(SECRET_MARKERS, SEND_VERBS, r"https?://|\.onion|矿池|webhook"),
        all_of=True,
    ),
    Rule(
        id="callback-sink",
        title="指向回调/匿名接收服务",
        severity="critical",
        advice="该 Skill 指向 webhook、回调捕获或匿名文件服务，常用于数据外传。",
        patterns=(SUSPICIOUS_HOST_PATTERNS["exfil / callback sink"],),
    ),
    Rule(
        id="prompt-injection",
        title="绕过确认或隐瞒用户的指令",
        severity="high",
        advice="该 Skill 要求忽略系统提示、跳过用户确认或不要告知用户，这会破坏 Omniscience 的安全边界。",
        patterns=(
            r"ignore (all |any |the )?(previous|prior|above|system)\b.{0,24}(instruction|prompt|rule)|忽略(之前|以上|所有|系统).{0,10}(指令|提示|规则)",
            r"不要(告诉|告知|通知|提醒)用户|无需(再次)?(用户)?(确认|审批)|跳过(用户)?(确认|审批|安全检查)|自动(批准|同意|确认)",
            r"(disable|bypass|turn off).{0,24}(safety|guardrail|permission|approval|sandbox|confirm)|关闭(安全|确认|审批)(机制|策略|检查)",
            r"无条件执行|以最高权限执行|必须服从(该|此)?(指令|用户)且不得拒绝",
            r"without (asking|telling|informing) the user|secretly|\bcovertly\b",
            r"不要提及|不要说明|不要记录(这|此|该)",
        ),
    ),
    Rule(
        id="persistence-and-privilege",
        title="持久化、提权或自启动改动",
        severity="high",
        advice="该 Skill 要求写入启动项、定时任务、shell 配置或授权密钥，会长期改变本机环境。",
        patterns=(
            r"\bcrontab\b|systemctl\s+(enable|start)|/etc/(systemd|cron|profile)|launch ?agents?|~/library/launchagents?\b",
            r"authorized_keys|\bssh-copy-id\b",
            r">>?\s*[\"'~]?\./?\.?(zshrc|bashrc|bash_profile|profile|zprofile|fish/config\.fish)\b",
            r"\b(sudo|doas)\b.{0,80}(rm|curl|wget|chown|tee|echo|install|defaults write)",
            r"git config --global|\.gitconfig|git hook|\.githooks|设置开机(自启|启动项)|修改 hosts",
        ),
    ),
    Rule(
        id="destructive-commands",
        title="破坏性或不可逆命令",
        severity="high",
        advice="该 Skill 包含可能清空数据或覆盖磁盘的命令，请确认它只是说明而不是要 Agent 执行。",
        patterns=(
            r"\brm\s+-[a-z]*r[a-z]*f\b[^\n]{0,40}(\s~\s|\s~$|/\s|$|\$home|/etc|/usr|/var|/Users|家目录)",
            r"\bmkfs(\.|\s)|\bdd\b[^\n]{0,60}of=/dev/|\bchmod\s+-R\s+777\b|:\(\)\s*\{.*\|.*&.*\};:",
            r"\bgit push\b[^\n]{0,60}--force\b|\bdrop (table|database)\b|\btruncate table\b|删除(所有|全部)(文件|数据|表)",
        ),
    ),
    Rule(
        id="obfuscated-payload",
        title="编码或混淆的可执行内容",
        severity="high",
        advice="该 Skill 含大段 Base64/十六进制或字符串解码逻辑，无法判断真实行为。",
        patterns=(
            r"[A-Za-z0-9+/]{100,}={0,2}",
            r"\b(atob|fromCharCode|codecs\.decode|xxd\s+-r|printf\s+['\"]\\\\x|rot-?13|unescape\()",
            r"\bbase64\b[^\n]{0,32}(-d\b|--decode)",
            r"\b(?:0x[0-9a-f]{2}[,\s]{0,2}){12,}",
        ),
    ),
    Rule(
        id="supply-chain",
        title="从非官方源安装代码",
        severity="medium",
        advice="该 Skill 要求替换软件源或安装来路不明的包/镜像，请先核对来源。",
        patterns=(
            r"(pip|pip3) install[^\n]{0,120}(--index-url|--extra-index-url|--trusted-host|-i\s+http)",
            r"npm (config set|i|install)[^\n]{0,120}(registry=|http://)",
            r"(curl|wget)[^\n]{0,120}\.(tar\.(gz|xz|bz2)|whl|gem)\b",
            r"(docker pull|helm install)[^\n]{0,80}([a-z0-9.-]+\.[a-z]{2,}/|[0-9a-f]{12})",
            r"pip install\s+\S+\.py|python setup\.py install|bash install\.sh|\./install\.sh",
        ),
    ),
    Rule(
        id="network-abuse",
        title="扫描、爆破或压测类行为",
        severity="medium",
        advice="该 Skill 包含端口扫描、口令爆破或高频请求指令，可能在未经授权的目标上造成问题。",
        patterns=(
            r"\bnmap\b|\bmasscan\b|\bhydra\b|medusa|brute[- ]?force|端口扫描|口令?爆破|\bddos\b|\bsyn ?flood\b",
            r"(高频|持续)(请求|访问|轮询)[^\n]{0,40}(接口|网站|api|服务)",
            r"批量(注册|发帖|私信|关注)|爬取[^\n]{0,30}(登录|隐私|付费)",
        ),
    ),
    Rule(
        id="secret-readout",
        title="读取本地密钥或环境变量",
        severity="medium",
        advice="该 Skill 要求读取密钥、凭据或环境变量文件；若只为配置自身，请改为让用户手动提供。",
        patterns=(rf"(cat|read|读取|打印|输出|加载|load)\S{{0,20}}[^\n]{{0,40}}{SECRET_MARKERS}",),
    ),
    Rule(
        id="tunnel-or-shortlink",
        title="隧道、内网穿透或缩短链接",
        severity="medium",
        advice="该 Skill 引用了隧道、缩短链接或裸 IP 地址，实际指向的目标随时可能变化。",
        patterns=(
            SUSPICIOUS_HOST_PATTERNS["tunnel"],
            SUSPICIOUS_HOST_PATTERNS["short link"],
            SUSPICIOUS_HOST_PATTERNS["raw IP endpoint"],
        ),
    ),
    Rule(
        id="anonymous-hosting",
        title="匿名网络或临时文件托管地址",
        severity="medium",
        advice="该 Skill 引用了 Onion/I2P 或临时文件托管地址，来源不可长期验证。",
        patterns=(
            SUSPICIOUS_HOST_PATTERNS["anonymous network"],
            SUSPICIOUS_HOST_PATTERNS["paste / file drop"],
            SUSPICIOUS_HOST_PATTERNS["non-standard port"],
        ),
    ),
)

SEVERITY_RANK = {"clean": -1, "low": 0, "medium": 1, "high": 2, "critical": 3}
SEVERITY_ORDER = ("critical", "high", "medium", "low")
VERDICT_RANK = {"clean": 0, "review": 1, "blocked": 2}
VERDICT_ORDER = ("clean", "review", "blocked")
VERDICT_BY_TOP_SEVERITY = {"critical": "blocked", "high": "review", "medium": "review", "low": "clean"}
MAX_FINDINGS = 20
MAX_EVIDENCE_PER_FINDING = 3
URL_PATTERN = re.compile(r"\bhttps?://[^\s<>\"'`)\]]+", re.IGNORECASE)
_INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad"))


def _normalize(text: str) -> str:
    lowered = str(text or "").translate(_INVISIBLE).lower()
    return re.sub(r"[ \t]+", " ", lowered.replace("_x000d_", ""))


def _evidence(text: str, pattern: re.Pattern[str], limit: int = MAX_EVIDENCE_PER_FINDING) -> list[str]:
    snippets: list[str] = []
    for match in list(pattern.finditer(text))[:limit]:
        start = max(match.start() - 40, 0)
        end = min(match.end() + 40, len(text))
        snippet = re.sub(r"\s+", " ", text[start:end]).strip()
        if snippet and snippet not in snippets:
            snippets.append(snippet[:240])
    return snippets


def _domains(text: str) -> list[str]:
    hosts: set[str] = set()
    for url in URL_PATTERN.findall(str(text or "")):
        authority = re.sub(r"^https?://", "", url, flags=re.IGNORECASE)
        authority = re.split(r"[/:?#]", authority, maxsplit=1)[0]
        host = authority.strip("@.").lower().split("@")[-1]
        if "." in host:
            hosts.add(host)
    return sorted(hosts)


def _compiled(rule: Rule) -> list[re.Pattern[str]]:
    return [re.compile(pattern, re.IGNORECASE | re.DOTALL) for pattern in rule.patterns]


def scan_skill_text(
    *,
    name: str = "",
    description: str = "",
    content: str = "",
    source: str = "",
    checked_at: float | None = None,
) -> dict[str, Any]:
    """Scan one Skill's text and return a verdict plus the findings behind it."""
    haystack = _normalize("\n".join([name, description, content]))
    raw_text = "\n".join([name, description, content])
    findings: list[dict[str, Any]] = []
    for rule in RULES:
        patterns = _compiled(rule)
        matched = [pattern for pattern in patterns if pattern.search(haystack)]
        if not matched:
            continue
        if rule.all_of and len(matched) < len(patterns):
            continue
        evidence: list[str] = []
        for pattern in matched[:1]:
            evidence = _evidence(haystack, pattern)
        findings.append(
            {
                "rule": rule.id,
                "title": rule.title,
                "severity": rule.severity,
                "advice": rule.advice,
                "evidence": evidence,
            }
        )
        if len(findings) >= MAX_FINDINGS:
            break

    domains = _domains(raw_text)
    if domains:
        findings.append(
            {
                "rule": "external-links",
                "title": f"引用 {len(domains)} 个外部域名",
                "severity": "low",
                "advice": "外部链接仅作说明使用；Omniscience 不会自动打开或下载这些地址。",
                "evidence": domains[:MAX_EVIDENCE_PER_FINDING * 3],
            }
        )

    findings.sort(key=lambda item: -SEVERITY_RANK.get(str(item["severity"]), 0))
    top = max((SEVERITY_RANK.get(str(item["severity"]), 0) for item in findings), default=-1)
    top_severity = next((key for key, rank in SEVERITY_RANK.items() if rank == top), "clean") if top >= 0 else "clean"
    verdict = VERDICT_BY_TOP_SEVERITY.get(top_severity, "clean")
    return {
        "engine": ENGINE_VERSION,
        "verdict": verdict,
        "severity": top_severity if top >= 0 else "clean",
        "findings": findings,
        "domains": domains,
        "source": str(source or ""),
        "checked_at": checked_at if checked_at is not None else datetime.now(timezone.utc).timestamp(),
        "scripts_executed": False,
    }


def domains_in(text: str) -> list[str]:
    """External hostnames mentioned anywhere in the text."""
    return _domains(text)


def summarize(report: dict[str, Any], limit: int = 3) -> str:
    """One-line Chinese summary used in error messages and tool results."""
    risky = [item for item in report.get("findings") or [] if str(item.get("severity")) in {"critical", "high", "medium"}]
    if not risky:
        return "未发现高危特征。"
    names = "、".join(str(item.get("title") or item.get("rule")) for item in risky[:limit])
    extra = f" 等 {len(risky)} 项" if len(risky) > limit else ""
    return f"检测到 {names}{extra}。"


def resolve_review_mode(values: Any) -> str:
    """Read the configured review engine; unknown or missing values stay static."""
    data = values.load() if hasattr(values, "load") else dict(values or {})
    mode = str((data or {}).get("skill_security_mode") or DEFAULT_REVIEW_MODE).strip().lower()
    return mode if mode in REVIEW_MODES else DEFAULT_REVIEW_MODE


def scan_values(values: Any, *, source: str = "") -> dict[str, Any]:
    """Scan a Skill-shaped dict as stored or exchanged by the API."""
    if not isinstance(values, dict):
        values = {}
    return scan_skill_text(
        name=str(values.get("name") or ""),
        description=str(values.get("description") or ""),
        content=str(values.get("content", values.get("prompt", "")) or ""),
        source=source,
    )


def findings_of(values: Any, *, source: str = "") -> dict[str, Any]:
    """Scan without raising and keep only what a gate needs to decide."""
    report = scan_values(values, source=source)
    return {
        "verdict": report["verdict"],
        "severity": report["severity"],
        "findings": report["findings"],
        "domains": report["domains"],
        "source": report["source"],
        "checked_at": report["checked_at"],
        "engine": report["engine"],
        "scripts_executed": False,
    }


def findings_text(report: dict[str, Any], limit: int = 6) -> str:
    """Human readable list of findings, for the panel and for the model transcript."""
    lines: list[str] = []
    for item in (report.get("findings") or [])[:limit]:
        evidence = item.get("evidence") or []
        detail = f"（示例：{evidence[0]}）" if evidence else ""
        lines.append(f"- [{item.get('severity')}] {item.get('title')}{detail}")
    return "\n".join(lines) or "- 未发现风险项。"
