#!/usr/bin/env python3
"""Collect local timing data for Codex performance troubleshooting."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import platform
import shutil
import socket
import ssl
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


DEFAULT_HOSTS = ("chatgpt.com", "api.openai.com")
PROXY_ENV_NAMES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 1)


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, round((len(ordered) - 1) * pct))
    return ordered[index]


def summarize_timings(samples: list[dict[str, Any]]) -> dict[str, Any]:
    durations = [sample["duration_ms"] for sample in samples if sample.get("ok")]
    return {
        "runs": len(samples),
        "ok_runs": len(durations),
        "min_ms": min(durations) if durations else None,
        "median_ms": percentile(durations, 0.5),
        "max_ms": max(durations) if durations else None,
    }


def run_command(
    args: list[str],
    cwd: Path | None = None,
    timeout: float = 20.0,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        return {
            "ok": False,
            "duration_ms": elapsed_ms(started),
            "error": f"not found: {exc.filename}",
        }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "duration_ms": elapsed_ms(started),
            "error": f"timeout after {timeout}s",
        }

    stdout = completed.stdout.strip()
    stderr = completed.stderr.strip()
    return {
        "ok": completed.returncode == 0,
        "returncode": completed.returncode,
        "duration_ms": elapsed_ms(started),
        "stdout": stdout[-4000:],
        "stderr": stderr[-4000:],
    }


def discover_codex_commands() -> list[dict[str, Any]]:
    commands: list[dict[str, Any]] = []
    path_codex = shutil.which("codex")
    if path_codex:
        commands.append({"source": "PATH", "path": path_codex, "command": [path_codex]})

    if platform.system().lower() == "windows":
        extensions_dir = Path.home() / ".vscode" / "extensions"
        pattern = "openai.chatgpt-*-win32-x64/bin/windows-x86_64/codex.exe"
        candidates = sorted(extensions_dir.glob(pattern), reverse=True)
        for candidate in candidates[:3]:
            command = [str(candidate)]
            if command not in [item["command"] for item in commands]:
                commands.append(
                    {
                        "source": "vscode_extension",
                        "path": str(candidate),
                        "command": command,
                    }
                )

    return commands


def time_command(
    args: list[str],
    cwd: Path | None,
    repeats: int,
    timeout: float,
) -> dict[str, Any]:
    samples = [run_command(args, cwd=cwd, timeout=timeout) for _ in range(repeats)]
    return {
        "command": args,
        "summary": summarize_timings(samples),
        "samples": samples,
    }


def count_rg_files(repo: Path, timeout: float) -> dict[str, Any]:
    if not shutil.which("rg"):
        return {"ok": False, "error": "rg not found"}

    started = time.perf_counter()
    count = 0
    try:
        process = subprocess.Popen(
            ["rg", "--files"],
            cwd=str(repo),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert process.stdout is not None
        deadline = time.perf_counter() + timeout
        for _line in process.stdout:
            count += 1
            if time.perf_counter() > deadline:
                process.kill()
                return {
                    "ok": False,
                    "duration_ms": elapsed_ms(started),
                    "file_count": count,
                    "error": f"timeout after {timeout}s",
                }
        stderr = process.communicate(timeout=2)[1].strip()
    except subprocess.TimeoutExpired:
        process.kill()
        return {
            "ok": False,
            "duration_ms": elapsed_ms(started),
            "file_count": count,
            "error": "timeout while collecting rg output",
        }

    return {
        "ok": process.returncode == 0,
        "returncode": process.returncode,
        "duration_ms": elapsed_ms(started),
        "file_count": count,
        "stderr": stderr[-4000:],
    }


def probe_network_host(host: str, timeout: float) -> dict[str, Any]:
    result: dict[str, Any] = {"host": host}

    started = time.perf_counter()
    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        result["dns_ms"] = elapsed_ms(started)
        result["address_count"] = len(addresses)
    except OSError as exc:
        result["ok"] = False
        result["error"] = f"dns failed: {exc}"
        return result

    address = addresses[0][4]
    started = time.perf_counter()
    try:
        raw_sock = socket.create_connection(address, timeout=timeout)
        result["tcp_ms"] = elapsed_ms(started)
    except OSError as exc:
        result["ok"] = False
        result["error"] = f"tcp failed: {exc}"
        return result

    context = ssl.create_default_context()
    started = time.perf_counter()
    try:
        with context.wrap_socket(raw_sock, server_hostname=host) as tls_sock:
            result["tls_ms"] = elapsed_ms(started)
            conn = http.client.HTTPSConnection(host, timeout=timeout)
            conn.sock = tls_sock
            started = time.perf_counter()
            conn.request("HEAD", "/")
            response = conn.getresponse()
            response.read()
            result["https_head_ms"] = elapsed_ms(started)
            result["status"] = response.status
            conn.close()
    except OSError as exc:
        result["ok"] = False
        result["error"] = f"tls/http failed: {exc}"
        return result

    result["ok"] = True
    result["total_ms"] = round(
        result["dns_ms"] + result["tcp_ms"] + result["tls_ms"] + result["https_head_ms"],
        1,
    )
    return result


def collect_process_snapshot() -> dict[str, Any]:
    if platform.system().lower() == "windows" and shutil.which("powershell"):
        command = [
            "powershell",
            "-NoProfile",
            "-Command",
            (
                "$names = 'codex','node','Code'; "
                "$items = foreach ($name in $names) { "
                "Get-Process -Name $name -ErrorAction SilentlyContinue "
                "}; "
                "$items | "
                "Select-Object ProcessName,Id,CPU,WorkingSet64,Path | "
                "ConvertTo-Json -Compress"
            ),
        ]
        return run_command(command, timeout=10)
    if shutil.which("ps"):
        return run_command(["ps", "-eo", "pid,pcpu,pmem,comm,args"], timeout=10)
    return {"ok": False, "error": "no process snapshot command found"}


def collect_report(repo: Path, repeats: int, timeout: float, hosts: list[str]) -> dict[str, Any]:
    report: dict[str, Any] = {
        "generated_at": now_iso(),
        "repo": str(repo.resolve()),
        "system": {
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "machine": platform.machine(),
            "processor": platform.processor(),
            "cpu_count": os.cpu_count(),
        },
        "proxy_env_present": {
            name: bool(os.environ.get(name) or os.environ.get(name.lower()))
            for name in PROXY_ENV_NAMES
        },
        "tools": {},
        "codex_binaries": [],
        "shell_startup": {},
        "repo_io": {},
        "network": [],
        "processes": {},
    }

    for tool, args in {
        "git": ["git", "--version"],
        "rg": ["rg", "--version"],
        "node": ["node", "--version"],
    }.items():
        report["tools"][tool] = run_command(args, cwd=repo, timeout=timeout)

    for codex in discover_codex_commands():
        command = codex["command"]
        report["codex_binaries"].append(
            {
                "source": codex["source"],
                "path": codex["path"],
                "version": run_command([*command, "--version"], cwd=repo, timeout=timeout),
                "help_timing": time_command([*command, "--help"], repo, repeats, timeout),
            }
        )

    shell_commands = []
    if platform.system().lower() == "windows":
        shell_commands.extend(
            [
                ("powershell_no_profile", ["powershell", "-NoProfile", "-Command", "exit"]),
                ("pwsh_no_profile", ["pwsh", "-NoProfile", "-Command", "exit"]),
                ("cmd", ["cmd", "/c", "exit"]),
            ]
        )
    else:
        shell_commands.extend(
            [
                ("bash", ["bash", "-lc", "true"]),
                ("sh", ["sh", "-c", "true"]),
            ]
        )
    for name, command in shell_commands:
        report["shell_startup"][name] = time_command(command, repo, repeats, timeout)

    report["repo_io"]["git_status_short"] = time_command(
        ["git", "status", "--short"], repo, repeats, timeout
    )
    report["repo_io"]["git_rev_parse"] = time_command(
        ["git", "rev-parse", "--show-toplevel"], repo, repeats, timeout
    )
    report["repo_io"]["rg_files"] = count_rg_files(repo, timeout)

    report["network"] = [probe_network_host(host, timeout) for host in hosts]
    report["processes"] = collect_process_snapshot()
    return report


def add_finding(findings: list[str], condition: bool, message: str) -> None:
    if condition:
        findings.append(message)


def build_findings(report: dict[str, Any]) -> list[str]:
    findings: list[str] = []

    for name, probe in report["shell_startup"].items():
        median = probe["summary"].get("median_ms")
        add_finding(
            findings,
            median is not None and median > 1000,
            f"{name} startup median is {median} ms; shell startup is likely contributing.",
        )

    git_status = report["repo_io"]["git_status_short"]["summary"].get("median_ms")
    add_finding(
        findings,
        git_status is not None and git_status > 1500,
        f"git status median is {git_status} ms; check Git index, untracked files, or antivirus scanning.",
    )

    rg_files = report["repo_io"]["rg_files"]
    add_finding(
        findings,
        rg_files.get("ok") and rg_files.get("duration_ms", 0) > 1500,
        (
            f"rg --files took {rg_files.get('duration_ms')} ms for "
            f"{rg_files.get('file_count')} files; repository file scanning is slow."
        ),
    )

    for network in report["network"]:
        add_finding(
            findings,
            network.get("ok") and network.get("total_ms", 0) > 1200,
            f"{network['host']} network total is {network.get('total_ms')} ms; check DNS, proxy, VPN, or TLS inspection.",
        )
        add_finding(
            findings,
            not network.get("ok"),
            f"{network['host']} network probe failed: {network.get('error')}",
        )

    proxy_present = [name for name, present in report["proxy_env_present"].items() if present]
    add_finding(
        findings,
        bool(proxy_present),
        f"Proxy environment variables are present: {', '.join(proxy_present)}; compare with proxy/VPN disabled if possible.",
    )

    add_finding(
        findings,
        not report.get("codex_binaries"),
        "No Codex binary was found on PATH or in the VS Code extension folder, so Codex startup was not measured.",
    )

    if not findings:
        findings.append(
            "No obvious local bottleneck crossed the default thresholds; compare this report with one captured during a slow Codex run."
        )
    return findings


def print_human_report(report: dict[str, Any]) -> None:
    print("Codex performance probe")
    print(f"Generated: {report['generated_at']}")
    print(f"Repo: {report['repo']}")
    print(f"System: {report['system']['platform']} Python {report['system']['python']}")
    print()

    print("Findings")
    for finding in build_findings(report):
        print(f"- {finding}")
    print()

    print("Key timings")
    for codex in report["codex_binaries"]:
        version = codex["version"]
        summary = codex["help_timing"]["summary"]
        label = f"{codex['source']} {codex['path']}"
        print(
            f"- codex {label}: version_ok={version.get('ok')} "
            f"help_median={summary['median_ms']} ms"
        )
    for name, probe in report["shell_startup"].items():
        summary = probe["summary"]
        print(
            f"- shell {name}: median={summary['median_ms']} ms "
            f"min={summary['min_ms']} ms max={summary['max_ms']} ms"
        )
    git_summary = report["repo_io"]["git_status_short"]["summary"]
    print(
        f"- git status --short: median={git_summary['median_ms']} ms "
        f"min={git_summary['min_ms']} ms max={git_summary['max_ms']} ms"
    )
    rg_files = report["repo_io"]["rg_files"]
    print(
        f"- rg --files: ok={rg_files.get('ok')} duration={rg_files.get('duration_ms')} ms "
        f"files={rg_files.get('file_count')}"
    )
    for network in report["network"]:
        if network.get("ok"):
            print(
                f"- network {network['host']}: dns={network['dns_ms']} ms "
                f"tcp={network['tcp_ms']} ms tls={network['tls_ms']} ms "
                f"head={network['https_head_ms']} ms total={network['total_ms']} ms"
            )
        else:
            print(f"- network {network['host']}: failed: {network.get('error')}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Measure common local bottlenecks that make Codex feel slow."
    )
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="Repository path to probe.")
    parser.add_argument("--repeats", type=int, default=3, help="Repeat count for short timing tests.")
    parser.add_argument("--timeout", type=float, default=20.0, help="Timeout per probe in seconds.")
    parser.add_argument(
        "--host",
        action="append",
        dest="hosts",
        help="HTTPS host to probe. Can be passed multiple times.",
    )
    parser.add_argument("--json", action="store_true", help="Print the full JSON report.")
    parser.add_argument("--output", type=Path, help="Write the full JSON report to this file.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo = args.repo.resolve()
    if not repo.exists() or not repo.is_dir():
        print(f"Repository path does not exist or is not a directory: {repo}", file=sys.stderr)
        return 2
    if args.repeats < 1:
        print("--repeats must be at least 1", file=sys.stderr)
        return 2

    hosts = args.hosts or list(DEFAULT_HOSTS)
    report = collect_report(repo, args.repeats, args.timeout, hosts)

    if args.output:
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_human_report(report)
        if args.output:
            print()
            print(f"Full JSON report written to: {args.output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
