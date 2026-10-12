#!/usr/bin/env python3
"""静的解析・脆弱性診断の結果（SARIF）を集約し、ゲート判定を行う（ADR-0008）.

ツールごとの出力形式の違いはSARIFに統一済みという前提に立つ。判定ロジックを
ツール数に比例して増やさないため、ここではSARIFだけを読む。

使い方:
    gate.py --sarif-dir reports --policy config/policy.yaml \
            --exceptions .platform/security-exceptions.yaml \
            --mode pr --summary-md summary.md --summary-json summary.json

終了コード:
    0  通過
    1  停止（ゲートに引っかかった）
    2  使い方の誤り、または設定ファイルの不備
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field, asdict
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.stderr.write("PyYAML が必要です: pip install pyyaml\n")
    raise SystemExit(2)


UNKNOWN = "UNKNOWN"


@dataclass
class Finding:
    tool: str
    rule_id: str
    severity: str
    message: str
    path: str
    line: int | None
    fixed_version: str | None
    fix_available: bool | None  # None = 判定不能
    sarif_file: str
    excepted_by: str | None = None
    blocking: bool = False
    block_reason: str | None = None


@dataclass
class ExceptionIssue:
    kind: str  # expired_exception / malformed_exception
    exception_id: str
    detail: str


@dataclass
class ReviewNotice:
    """恒久例外の見直し期限超過。警告であり停止条件ではない."""

    exception_id: str
    adr: str
    last_reviewed: str
    overdue_days: int


@dataclass
class Result:
    mode: str
    findings: list[Finding] = field(default_factory=list)
    exception_issues: list[ExceptionIssue] = field(default_factory=list)
    review_notices: list[ReviewNotice] = field(default_factory=list)
    tools_seen: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# SARIF の読み取り
# ---------------------------------------------------------------------------


def _normalize_tool(name: str) -> str:
    n = (name or "unknown").strip().lower()
    for known in ("trivy", "semgrep", "gitleaks", "ruff", "eslint", "spotbugs", "codeql"):
        if known in n:
            return known
    return n


def _collect_rules(run: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """driver と extensions の両方からルール定義を集める."""
    rules: dict[str, dict[str, Any]] = {}
    tool = run.get("tool", {}) or {}
    components = [tool.get("driver") or {}]
    components.extend(tool.get("extensions") or [])
    for comp in components:
        for rule in comp.get("rules") or []:
            rid = rule.get("id")
            if rid:
                rules[rid] = rule
    return rules


def _severity_from_rule(rule: dict[str, Any], policy: dict[str, Any]) -> str | None:
    """CVSSスコア（security-severity）から深刻度を決める."""
    props = rule.get("properties") or {}
    raw = props.get("security-severity")
    if raw is None:
        return None
    try:
        score = float(raw)
    except (TypeError, ValueError):
        return None

    thresholds = policy["severity"]["from_cvss"]
    # 高い方から判定する
    for label in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
        if label in thresholds and score >= float(thresholds[label]):
            return label
    return "LOW"


def _severity_from_level(level: str | None, policy: dict[str, Any]) -> str:
    mapping = policy["severity"]["from_level"]
    return mapping.get((level or "warning").lower(), UNKNOWN)


def _fix_info(result: dict[str, Any], rule: dict[str, Any]) -> tuple[str | None, bool | None]:
    """修正版の有無を判定する.

    scripts/trivy_to_sarif.py が result.properties.fixedVersion に入れる。
    SASTのように「修正版」の概念がない検出は判定不能（None）を返し、
    扱いは policy.fix_availability.unknown に委ねる。
    """
    for container in (result.get("properties") or {}, rule.get("properties") or {}):
        if "fixedVersion" in container:
            fv = container.get("fixedVersion")
            if fv:
                return str(fv), True
            # 空文字・null は「修正版なし」を明示したもの
            return None, False
        if "fixAvailable" in container:
            return None, bool(container["fixAvailable"])
    return None, None


def _location(result: dict[str, Any]) -> tuple[str, int | None]:
    locations = result.get("locations") or []
    if not locations:
        return "(場所不明)", None
    phys = (locations[0].get("physicalLocation") or {})
    uri = ((phys.get("artifactLocation") or {}).get("uri")) or "(場所不明)"
    line = (phys.get("region") or {}).get("startLine")
    return uri, line


def load_sarif(sarif_dir: str, policy: dict[str, Any]) -> tuple[list[Finding], list[str]]:
    findings: list[Finding] = []
    tools: set[str] = set()

    paths = sorted(glob.glob(os.path.join(sarif_dir, "**", "*.sarif"), recursive=True))
    for path in paths:
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            # レポートが壊れている状態を「問題なし」として通すと、
            # スキャンが失敗していることに気づけない
            raise SystemExit(f"SARIFの読み込みに失敗しました: {path}: {exc}")

        for run in doc.get("runs") or []:
            driver = ((run.get("tool") or {}).get("driver") or {})
            tool = _normalize_tool(driver.get("name", ""))
            tools.add(tool)
            rules = _collect_rules(run)

            for res in run.get("results") or []:
                rule_id = res.get("ruleId")
                if rule_id is None and isinstance(res.get("ruleIndex"), int):
                    rule_list = list(rules.values())
                    idx = res["ruleIndex"]
                    rule_id = rule_list[idx].get("id") if 0 <= idx < len(rule_list) else None
                rule_id = rule_id or "(ルールID不明)"
                rule = rules.get(rule_id, {})

                level = res.get("level") or ((rule.get("defaultConfiguration") or {}).get("level"))
                severity = _severity_from_rule(rule, policy) or _severity_from_level(level, policy)
                fixed_version, fix_available = _fix_info(res, rule)
                uri, line = _location(res)

                findings.append(
                    Finding(
                        tool=tool,
                        rule_id=rule_id,
                        severity=severity,
                        message=((res.get("message") or {}).get("text") or "").strip(),
                        path=uri,
                        line=line,
                        fixed_version=fixed_version,
                        fix_available=fix_available,
                        sarif_file=os.path.basename(path),
                    )
                )

    return findings, sorted(tools)


# ---------------------------------------------------------------------------
# 抑制（例外）の検証と適用
# ---------------------------------------------------------------------------


def _parse_date(value: Any) -> dt.date | None:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        try:
            return dt.date.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


def load_permanent_exceptions(
    doc: dict[str, Any], policy: dict[str, Any], today: dt.date
) -> tuple[dict[str, dict[str, Any]], list[ExceptionIssue], list[ReviewNotice]]:
    """恒久例外を読む（ADR-0008の補足）.

    期限ではなく「見直し周期」で管理する。見直し期限を超えても
    **停止させない**（警告のみ）。恒久的な設計判断を期限付きで表現すると、
    儀式的な更新か、失効による突然のCI停止のどちらかになるため。

    乱用を防ぐため adr フィールドを必須とし、判断の根拠となる記録への
    リンクがないものは記載不備として停止条件にする。
    """
    issues: list[ExceptionIssue] = []
    notices: list[ReviewNotice] = []
    valid: dict[str, dict[str, Any]] = {}

    config = policy.get("permanent_exceptions")
    entries = doc.get("permanent") or []

    if not entries:
        return valid, issues, notices

    if not config:
        issues.append(
            ExceptionIssue(
                "malformed_exception",
                "permanent",
                "policy.yaml に permanent_exceptions の定義がないため、恒久例外は使えません",
            )
        )
        return valid, issues, notices

    required = config["required_fields"]
    max_cycle = int(config["max_review_cycle_days"])
    adr_pattern = re.compile(config["adr_pattern"])

    for i, item in enumerate(entries):
        if not isinstance(item, dict):
            issues.append(ExceptionIssue("malformed_exception", f"permanent#{i}", "項目がマップ形式ではない"))
            continue

        ident = str(item.get("id") or f"permanent#{i}")
        missing = [k for k in required if not item.get(k)]
        if missing:
            issues.append(
                ExceptionIssue("malformed_exception", ident, f"必須項目が不足: {', '.join(missing)}")
            )
            continue

        # 根拠のない恒久例外を作れないようにする
        adr = str(item["adr"]).strip()
        if not adr_pattern.match(adr):
            issues.append(
                ExceptionIssue(
                    "malformed_exception",
                    ident,
                    f"adr の形式が不正: '{adr}'。判断の根拠となるADR番号（例 ADR-0001）が必要です",
                )
            )
            continue

        last_reviewed = _parse_date(item["last_reviewed"])
        if last_reviewed is None:
            issues.append(
                ExceptionIssue("malformed_exception", ident, f"last_reviewed の日付形式が不正: {item['last_reviewed']}")
            )
            continue
        if last_reviewed > today:
            issues.append(
                ExceptionIssue("malformed_exception", ident, f"last_reviewed が未来の日付です: {last_reviewed}")
            )
            continue

        try:
            cycle = int(item["review_cycle_days"])
        except (TypeError, ValueError):
            issues.append(ExceptionIssue("malformed_exception", ident, "review_cycle_days が整数ではない"))
            continue

        if cycle <= 0 or cycle > max_cycle:
            issues.append(
                ExceptionIssue(
                    "malformed_exception",
                    ident,
                    f"review_cycle_days が範囲外（1〜{max_cycle}日）: {cycle}",
                )
            )
            continue

        valid[ident] = item

        overdue = (today - last_reviewed).days - cycle
        if overdue > 0:
            # 停止させない。警告を出し続けることで見直しを促す
            notices.append(
                ReviewNotice(
                    exception_id=ident,
                    adr=adr,
                    last_reviewed=last_reviewed.isoformat(),
                    overdue_days=overdue,
                )
            )

    return valid, issues, notices


def load_exceptions(
    path: str | None, policy: dict[str, Any], today: dt.date
) -> tuple[dict[str, dict[str, Any]], list[ExceptionIssue], list[ReviewNotice]]:
    """抑制を読み、不備・期限切れを洗い出す.

    期限切れと記載不備はそれ自体がゲートの停止条件（ADR-0008）。
    抑制が一時的な対応のまま恒久化するのを防ぐため。

    恒久例外（permanent）は別の規則で扱う。load_permanent_exceptions を参照。
    """
    issues: list[ExceptionIssue] = []
    notices: list[ReviewNotice] = []
    valid: dict[str, dict[str, Any]] = {}

    if not path or not os.path.exists(path):
        return valid, issues, notices

    with open(path, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}

    perm_valid, perm_issues, notices = load_permanent_exceptions(doc, policy, today)
    for ident, item in perm_valid.items():
        valid[ident] = {**item, "_permanent": True}
    issues.extend(perm_issues)

    required = policy["exceptions"]["required_fields"]
    max_days = int(policy["exceptions"]["max_days_from_now"])

    for i, item in enumerate(doc.get("exceptions") or []):
        if not isinstance(item, dict):
            issues.append(ExceptionIssue("malformed_exception", f"#{i}", "項目がマップ形式ではない"))
            continue

        ident = str(item.get("id") or f"#{i}")
        missing = [k for k in required if not item.get(k)]
        if missing:
            issues.append(
                ExceptionIssue("malformed_exception", ident, f"必須項目が不足: {', '.join(missing)}")
            )
            continue

        expires = _parse_date(item["expires"])
        if expires is None:
            issues.append(
                ExceptionIssue("malformed_exception", ident, f"expires の日付形式が不正: {item['expires']}")
            )
            continue

        if expires < today:
            issues.append(
                ExceptionIssue("expired_exception", ident, f"{expires.isoformat()} に失効している")
            )
            continue

        if (expires - today).days > max_days:
            issues.append(
                ExceptionIssue(
                    "malformed_exception",
                    ident,
                    f"expires が上限（{max_days}日）を超えている: {expires.isoformat()}",
                )
            )
            continue

        valid[ident] = item

    return valid, issues, notices


def apply_exceptions(findings: list[Finding], exceptions: dict[str, dict[str, Any]]) -> None:
    for f in findings:
        exc = exceptions.get(f.rule_id)
        if not exc:
            continue
        paths = exc.get("paths")
        if paths and not any(f.path.endswith(p) or p in f.path for p in paths):
            continue
        if exc.get("_permanent"):
            f.excepted_by = f"{f.rule_id} (恒久例外 / {exc['adr']})"
        else:
            f.excepted_by = f"{f.rule_id} (期限 {exc['expires']})"


# ---------------------------------------------------------------------------
# ゲート判定
# ---------------------------------------------------------------------------


def judge(result: Result, policy: dict[str, Any]) -> None:
    gate = policy["gates"].get(result.mode)
    if gate is None:
        raise SystemExit(f"policy.yaml に mode '{result.mode}' のゲート定義がありません")

    block_severities = set(gate.get("block_severities") or [])
    require_fix = bool(gate.get("require_fix_available", True))
    unknown_fix = (policy.get("fix_availability") or {}).get("unknown", "block")

    always_tools = {
        a["tool"] for a in (policy.get("always_block") or []) if a.get("tool")
    }

    for f in result.findings:
        if f.excepted_by:
            continue

        # モードに関係なく停止させるツール（シークレット検出など）
        if f.tool in always_tools:
            f.blocking = True
            f.block_reason = f"{f.tool} の検出は常に停止条件"
            continue

        if f.severity not in block_severities:
            continue

        if require_fix:
            if f.fix_available is False:
                # 修正版がないものは停止させない。修正手段がないまま
                # リリースが永久に止まるのを防ぐ（ADR-0008）
                continue
            if f.fix_available is None and unknown_fix != "block":
                continue

        f.blocking = True
        f.block_reason = f"{f.severity} かつモード {result.mode} の停止条件に該当"


def blocking_count(result: Result) -> int:
    return sum(1 for f in result.findings if f.blocking) + len(result.exception_issues)


# ---------------------------------------------------------------------------
# 出力
# ---------------------------------------------------------------------------


def write_summary_md(result: Result, policy: dict[str, Any], path: str, report_url: str) -> None:
    order = policy["severity"]["order"]
    counts = Counter(f.severity for f in result.findings if not f.excepted_by)
    excepted = sum(1 for f in result.findings if f.excepted_by)
    blocked = [f for f in result.findings if f.blocking]

    lines: list[str] = []
    verdict = "停止" if blocking_count(result) else "通過"
    lines.append(f"## セキュリティゲート: **{verdict}**（モード: `{result.mode}`）")
    lines.append("")

    lines.append("| 深刻度 | 件数 |")
    lines.append("|---|---|")
    for sev in order:
        if counts.get(sev):
            lines.append(f"| {sev} | {counts[sev]} |")
    if not counts:
        lines.append("| — | 0 |")
    lines.append("")

    lines.append(f"- 実行したツール: {', '.join(result.tools_seen) or '(なし)'}")
    lines.append(f"- 抑制により除外: {excepted} 件")
    lines.append(f"- **停止の原因: {len(blocked)} 件の検出 + {len(result.exception_issues)} 件の抑制の不備**")
    lines.append("")

    permanent = [f for f in result.findings if f.excepted_by and "恒久例外" in f.excepted_by]
    if permanent:
        by_rule: dict[str, int] = {}
        for f in permanent:
            key = f.excepted_by or ""
            by_rule[key] = by_rule.get(key, 0) + 1
        lines.append("### 恒久例外により除外（意図した設計判断）")
        lines.append("")
        lines.append("| 件数 | 抑制 |")
        lines.append("|---|---|")
        for label, n in sorted(by_rule.items()):
            lines.append(f"| {n} | `{label}` |")
        lines.append("")

    if result.review_notices:
        lines.append("### 恒久例外の見直し期限超過（停止はしません）")
        lines.append("")
        lines.append("| ID | 根拠 | 最終レビュー | 超過日数 |")
        lines.append("|---|---|---|---|")
        for notice in result.review_notices:
            lines.append(
                f"| `{notice.exception_id}` | {notice.adr} | {notice.last_reviewed} | {notice.overdue_days} |"
            )
        lines.append("")

    if result.exception_issues:
        lines.append("### 抑制の不備（それ自体が停止条件）")
        lines.append("")
        lines.append("| 種別 | ID | 内容 |")
        lines.append("|---|---|---|")
        for issue in result.exception_issues:
            kind = "期限切れ" if issue.kind == "expired_exception" else "記載不備"
            lines.append(f"| {kind} | `{issue.exception_id}` | {issue.detail} |")
        lines.append("")

    if blocked:
        lines.append("### 停止の原因となった検出")
        lines.append("")
        lines.append("| 深刻度 | ルール | 場所 | 修正版 | ツール |")
        lines.append("|---|---|---|---|---|")
        for f in blocked[:30]:
            loc = f"{f.path}:{f.line}" if f.line else f.path
            fix = f.fixed_version or ("なし" if f.fix_available is False else "—")
            lines.append(f"| {f.severity} | `{f.rule_id}` | `{loc}` | {fix} | {f.tool} |")
        if len(blocked) > 30:
            lines.append("")
            lines.append(f"ほか {len(blocked) - 30} 件。全件はレポートを参照。")
        lines.append("")

    # ADR-0002: 詳細はpublicなPRコメントに載せず、privateなS3のみに置く
    lines.append(f"レポート全文（S3）: `{report_url}`")
    lines.append("")
    lines.append("<sub>基準は platform-cicd/config/policy.yaml で一元管理されており、アプリ側では変更できません（ADR-0008）。</sub>")

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def write_summary_json(result: Result, path: str, report_url: str) -> None:
    counts = Counter(f.severity for f in result.findings if not f.excepted_by)
    payload = {
        "mode": result.mode,
        "verdict": "blocked" if blocking_count(result) else "passed",
        "tools": result.tools_seen,
        "counts": dict(counts),
        "excepted": sum(1 for f in result.findings if f.excepted_by),
        "blocking_findings": [asdict(f) for f in result.findings if f.blocking],
        "exception_issues": [asdict(i) for i in result.exception_issues],
        "review_notices": [asdict(n) for n in result.review_notices],
        "total_findings": len(result.findings),
        "report_url": report_url,
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="SARIFを集約してゲート判定を行う")
    ap.add_argument("--sarif-dir", required=True)
    ap.add_argument("--policy", required=True)
    ap.add_argument("--exceptions", default=None)
    ap.add_argument("--mode", required=True, choices=["pr", "dev", "stage", "prod"])
    ap.add_argument("--summary-md", default="summary.md")
    ap.add_argument("--summary-json", default="summary.json")
    ap.add_argument("--report-url", default="(未設定)")
    args = ap.parse_args(argv)

    with open(args.policy, encoding="utf-8") as fh:
        policy = yaml.safe_load(fh)

    today = dt.date.today()
    findings, tools = load_sarif(args.sarif_dir, policy)
    exceptions, exception_issues, review_notices = load_exceptions(args.exceptions, policy, today)
    apply_exceptions(findings, exceptions)

    result = Result(
        mode=args.mode,
        findings=findings,
        exception_issues=exception_issues,
        review_notices=review_notices,
        tools_seen=tools,
    )
    judge(result, policy)

    write_summary_md(result, policy, args.summary_md, args.report_url)
    write_summary_json(result, args.summary_json, args.report_url)

    # 恒久例外の見直し期限超過は停止させない。
    # 警告を出し続けることで見直しを促す（ADR-0008の補足）
    for notice in result.review_notices:
        sys.stderr.write(
            f"::warning::恒久例外の見直し期限を {notice.overdue_days} 日超過しています: "
            f"{notice.exception_id}（{notice.adr} / 最終レビュー {notice.last_reviewed}）。"
            "まだ有効な判断かを確認し、last_reviewed を更新してください\n"
        )

    n = blocking_count(result)
    if n:
        sys.stderr.write(f"ゲート停止: {n} 件（モード {args.mode}）\n")
        return 1

    sys.stdout.write(f"ゲート通過（モード {args.mode}、検出 {len(findings)} 件）\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
