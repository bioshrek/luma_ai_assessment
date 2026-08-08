#!/usr/bin/env python
"""Render the archived VS Code chat exports as readable markdown.

`docs/ai_chat_sessions/*.json` is the raw export VS Code writes: a flat stream of response
parts per request, carrying absolute paths and a lot of UI bookkeeping. It is archival and
immutable — the same relationship `raw/` has to `normalized/` in the pipeline itself. This
script derives `docs/ai/` from it: one markdown file per session plus an index, readable on
GitHub with nothing installed.

Two properties worth stating, because both are consequences of what the export does and does
not contain:

- **The export does not record tool results.** Of the tool invocations in this corpus only the
  terminal command line, the todo list and the subagent prompt survive as structured data; no
  tool's output is stored at all. What is rendered for a tool call is VS Code's own
  human-readable invocation message, which is a *rendering* and not the arguments. Nothing is
  reconstructed by parsing it.
- **Absolute paths are scrubbed**, so the derived tree carries no local home directory.

Run `python scripts/render_ai_sessions.py`; add `--check` to fail if the output is stale.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = REPO_ROOT / "docs" / "ai_chat_sessions"
DEFAULT_OUT = REPO_ROOT / "docs" / "ai"

# Parts that carry no information a reader needs: UI checkpoints and editor bookkeeping.
IGNORED_KINDS = frozenset({"undoStop", "codeblockUri", "mcpServersStarting"})

_SCHEME = r"(?:file|vscode-[\w-]+)://"
_MD_LINK = re.compile(rf"\[([^\]]*)\]\(({_SCHEME}[^)]*)\)")
_BARE_URI = re.compile(rf"{_SCHEME}[^\s)\]`\"']+")
_HOME = re.compile(r"/Users/[^/\s\"'`)]+")


@dataclass
class SessionStats:
    """What the index reports for one session."""

    number: int
    source: str
    slug: str
    title: str
    stage: str
    started: datetime
    requests: int = 0
    tools: Counter[str] = field(default_factory=Counter)
    edits: int = 0
    files_edited: set[str] = field(default_factory=set)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    credits: float = 0.0
    elapsed_ms: int = 0
    models: set[str] = field(default_factory=set)

    @property
    def tool_calls(self) -> int:
        return sum(self.tools.values())


def scrub(text: str) -> str:
    """Remove machine-specific paths: the workspace root becomes relative, `$HOME` a variable.

    VS Code escapes underscores in the paths it renders into invocation captions, so the root is
    stripped in both its plain and markdown-escaped spelling.
    """
    for root in (str(REPO_ROOT), str(REPO_ROOT).replace("_", r"\_")):
        text = text.replace(root + "/", "").replace(root, ".")
    return _HOME.sub("${HOME}", text)


def workspace_path(uri: str) -> str:
    """A `file:///abs/path#L1-L2` URI as a repo-relative path; the line fragment is UI detail."""
    return scrub(re.sub(_SCHEME, "", uri).split("#", 1)[0]) or "(workspace)"


def clean_uris(text: str) -> str:
    """Turn VS Code's `[label](file:///abs/path)` links into plain relative paths."""

    def link(match: re.Match[str]) -> str:
        label = match.group(1).strip()
        return label or f"`{workspace_path(match.group(2))}`"

    return _BARE_URI.sub(lambda m: f"`{workspace_path(m.group(0))}`", _MD_LINK.sub(link, text))


def fence(body: str, lang: str = "") -> str:
    """A fenced block whose fence is always longer than any run of backticks inside it."""
    longest = max((len(run) for run in re.findall(r"`+", body)), default=0)
    bars = "`" * max(3, longest + 1)
    return f"{bars}{lang}\n{body.rstrip()}\n{bars}"


def as_text(value: Any) -> str:
    """VS Code stores prose either as a bare string or as `{'value': ...}`."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("value", ""))
    return ""


def timestamp(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def duration(ms: int) -> str:
    minutes, seconds = divmod(round(ms / 1000), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m {seconds:02d}s"


def title_of(message: str) -> str:
    """The session's topic, taken from the first line of its first prompt."""
    for line in message.splitlines():
        stripped = clean_uris(line.strip().lstrip("#").strip())
        if not stripped:
            continue
        if len(stripped) <= 80:
            return stripped
        return stripped[:80].rsplit(" ", 1)[0] + "…"
    return "(no prompt)"


def stage_of(message: str) -> str:
    """The milestone or source the prompt names. Read out of the prompt, never assigned to it."""
    milestone = re.search(r"milestone (\d+)", message, re.IGNORECASE)
    if milestone:
        return f"M{milestone.group(1)}"
    source = re.search(r"source ([A-D])\b", message)
    return f"源 {source.group(1)}" if source else "—"


def describe_tool(part: dict[str, Any]) -> tuple[str, str]:
    """`(one-line summary, extra block)` for a tool invocation. The summary is VS Code's."""
    tool_id = str(part.get("toolId", "unknown"))
    summary = clean_uris(as_text(part.get("pastTenseMessage") or part.get("invocationMessage")))
    data = part.get("toolSpecificData") or {}
    extra = ""
    if data.get("kind") == "terminal":
        command = str((data.get("commandLine") or {}).get("original", "")).strip()
        if command:
            extra = fence(scrub(command), "sh")
    elif data.get("kind") == "todoList":
        items = "\n".join(
            f"- [{'x' if t.get('status') == 'completed' else ' '}] {t.get('title', '')}"
            for t in data.get("todoList", [])
        )
        extra = items
    elif data.get("kind") == "subagent":
        prompt = scrub(str(data.get("prompt", "")))
        extra = fence(prompt[:1500] + ("\n…" if len(prompt) > 1500 else ""))
    return tool_id, f"{summary}\n\n{extra}" if extra else summary


def describe_edits(parts: list[dict[str, Any]]) -> tuple[list[str], int, set[str]]:
    """Per-file edit summaries. The edit text itself lives in git history, not here."""
    per_file: dict[str, list[int]] = {}
    counts: Counter[str] = Counter()
    for part in parts:
        path = scrub(str((part.get("uri") or {}).get("path", "unknown")))
        for group in part.get("edits", []):
            for edit in group:
                counts[path] += 1
                line = (edit.get("range") or {}).get("startLineNumber")
                if isinstance(line, int):
                    per_file.setdefault(path, []).append(line)
    lines: list[str] = []
    for path, count in counts.items():
        span = per_file.get(path)
        where = f"，第 {min(span)}-{max(span)} 行" if span else ""
        lines.append(f"- `{path}` — {count} 处修改{where}")
    return lines, sum(counts.values()), set(counts)


def render_response(parts: list[dict[str, Any]], stats: SessionStats) -> str:
    """Walk the response stream in order, collapsing each run of same-kind parts into a block."""
    out: list[str] = []
    prose: list[str] = []
    thinking: list[str] = []
    tools: list[dict[str, Any]] = []
    edits: list[dict[str, Any]] = []

    def flush() -> None:
        if prose:
            text = clean_uris("".join(prose)).strip()
            if text:
                out.append(text)
            prose.clear()
        if thinking:
            body = "\n\n".join(clean_uris(t).strip() for t in thinking)
            out.append(
                f"<details>\n<summary><i>推理过程（{len(thinking)} 段）</i></summary>\n\n"
                f"{body}\n\n</details>"
            )
            thinking.clear()
        if tools:
            items = []
            for index, part in enumerate(tools, start=1):
                tool_id, body = describe_tool(part)
                stats.tools[tool_id] += 1
                indented = "\n".join(f"    {ln}" if ln else "" for ln in body.splitlines())
                items.append(f"{index}. **`{tool_id}`** — {indented.lstrip()}")
            by_tool = Counter(str(p.get("toolId")) for p in tools)
            head = ", ".join(f"{name} x{n}" for name, n in by_tool.most_common())
            out.append(
                f"<details>\n<summary><b>{len(tools)} 次工具调用</b> — {head}</summary>\n\n"
                + "\n".join(items)
                + "\n\n</details>"
            )
            tools.clear()
        if edits:
            summaries, count, files = describe_edits(edits)
            stats.edits += count
            stats.files_edited |= files
            out.append("**文件改动**\n\n" + "\n".join(summaries))
            edits.clear()

    for part in parts:
        kind = part.get("kind") or "markdown"
        if kind in IGNORED_KINDS:
            continue
        if kind == "markdown":
            if thinking or tools or edits:
                flush()
            prose.append(as_text(part))
        elif kind == "inlineReference":
            if thinking or tools or edits:
                flush()
            prose.append(f"`{part.get('name', '')}`")
        elif kind == "thinking":
            if prose or tools or edits:
                flush()
            thinking.append(as_text(part.get("value")))
        elif kind == "toolInvocationSerialized":
            if prose or thinking or edits:
                flush()
            tools.append(part)
        elif kind == "textEditGroup":
            if prose or thinking or tools:
                flush()
            edits.append(part)
        elif kind == "progressTaskSerialized":
            flush()
            out.append(f"> _{as_text(part.get('content'))}_")
    flush()
    return "\n\n".join(out)


def render_session(path: Path) -> tuple[str, SessionStats]:
    data = json.loads(path.read_text(encoding="utf-8"))
    requests = data.get("requests", [])
    number = int(re.search(r"(\d+)", path.stem).group(1))  # type: ignore[union-attr]
    stats = SessionStats(
        number=number,
        source=f"docs/ai_chat_sessions/{path.name}",
        slug=f"session_{number:02d}.md",
        title=title_of(requests[0]["message"].get("text", "")) if requests else "(empty)",
        stage=stage_of(requests[0]["message"].get("text", "")) if requests else "—",
        started=timestamp(requests[0]["timestamp"]) if requests else datetime.now(tz=UTC),
        requests=len(requests),
    )

    body: list[str] = []
    for index, request in enumerate(requests, start=1):
        stats.prompt_tokens += int(request.get("promptTokens") or 0)
        stats.completion_tokens += int(request.get("completionTokens") or 0)
        stats.credits += float(request.get("copilotCredits") or 0.0)
        stats.elapsed_ms += int(request.get("elapsedMs") or 0)
        stats.models.add(str(request.get("modelId", "")).removeprefix("copilot/"))

        when = timestamp(int(request["timestamp"])).strftime("%Y-%m-%d %H:%M UTC")
        prompt = scrub(request["message"].get("text", "")).strip()
        quoted = "\n".join(f"> {line}" if line else ">" for line in prompt.splitlines())
        body.append(
            f"## 第 {index} 轮 — {when}\n\n"
            f"{quoted}\n\n"
            f"<sub>{str(request.get('modelId', '')).removeprefix('copilot/')} · "
            f"输出 {request.get('completionTokens', 0):,} tokens · "
            f"{duration(int(request.get('elapsedMs') or 0))}</sub>\n\n"
            f"{render_response(request.get('response', []), stats)}"
        )

    header = "\n".join(
        [
            f"# Session {number} — {stats.title}",
            "",
            f"[← 回目录](index.md) · 由 `scripts/render_ai_sessions.py` 从 "
            f"[{path.name}](../ai_chat_sessions/{path.name}) 生成，请勿手改。",
            "",
            "| | |",
            "| --- | --- |",
            f"| 开始时间 | {stats.started.strftime('%Y-%m-%d %H:%M UTC')} |",
            f"| 阶段 | {stats.stage} |",
            f"| 轮次 | {stats.requests} |",
            f"| 模型 | {', '.join(sorted(stats.models))} |",
            f"| 工具调用 | {stats.tool_calls} |",
            f"| 改动文件 | {len(stats.files_edited)}（{stats.edits} 处）|",
            f"| 输出 token | {stats.completion_tokens:,} |",
            f"| 墙钟时间 | {duration(stats.elapsed_ms)} |",
            "",
        ]
    )
    return f"{header}\n---\n\n" + "\n\n---\n\n".join(body) + "\n", stats


def render_index(all_stats: list[SessionStats]) -> str:
    rows = [
        "| # | 开始时间 (UTC) | 阶段 | 主题（原始 prompt 首行） | 轮次 | 工具调用 | 改动文件 |"
        " 输出 token | 墙钟时间 |",
        "| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for s in all_stats:
        label = s.title.replace("[", "\\[").replace("]", "\\]")
        rows.append(
            f"| {s.number} | {s.started.strftime('%m-%d %H:%M')} | {s.stage} "
            f"| [{label}]({s.slug}) "
            f"| {s.requests} | {s.tool_calls} | {len(s.files_edited)} "
            f"| {s.completion_tokens:,} | {duration(s.elapsed_ms)} |"
        )
    total_tools: Counter[str] = Counter()
    for s in all_stats:
        total_tools += s.tools
    rows.append(
        f"| **{len(all_stats)}** | | | **合计** "
        f"| **{sum(s.requests for s in all_stats)}** "
        f"| **{sum(s.tool_calls for s in all_stats)}** "
        f"| **{len({f for s in all_stats for f in s.files_edited})}** "
        f"| **{sum(s.completion_tokens for s in all_stats):,}** "
        f"| **{duration(sum(s.elapsed_ms for s in all_stats))}** |"
    )

    tools = "\n".join(f"| `{name}` | {count} |" for name, count in total_tools.most_common())
    models = Counter(m for s in all_stats for m in s.models)

    return "\n".join(
        [
            "# AI 协作记录",
            "",
            "本仓库的全部对话记录，由 [docs/ai_chat_sessions/](../ai_chat_sessions/) 里的 VS Code",
            "原始导出渲染而来。导出文件是归档件、不可变；这里的一切都是派生的、可丢弃的，重新生成：",
            "",
            "```bash",
            "uv run python scripts/render_ai_sessions.py",
            "```",
            "",
            "主题一列是每个会话**第一条 prompt 的首行原文**，未作翻译或改写 —— 这些记录的价值在于",
            "它是原始过程，不是事后重建的版本。「阶段」一列由 prompt 文本里出现的 `milestone N` /",
            "`source X` 直接解析得到，同样不作追加解释。",
            "",
            "## 会话列表",
            "",
            *rows,
            "",
            "## 工具调用分布",
            "",
            "| 工具 | 次数 |",
            "| --- | ---: |",
            tools,
            "",
            "## 怎么读这些记录",
            "",
            "- **导出里没有工具的返回结果。** VS Code 记录了某个工具被调用、以及这次调用的",
            "  标题文案，但不记录它的输出。只有终端命令行、待办清单和子 agent 的 prompt 以结构",
            "  化数据留存。每次工具调用下面那行摘要是 VS Code 自己的标题文案，不是工具参数 ——",
            "  没有任何内容是靠解析它反推出来的。",
            "- **文件编辑只做汇总，不复现内容。** 改了什么以 git history 为准；在这里再抄一份，",
            "  只会多出一个会和代码对不上的副本。",
            "- **prompt token 不可累加。** 每一轮的 prompt token 数衡量的是那一轮重新发送的整个",
            "  上下文，跨轮相加等于把同一段上下文数很多遍。上面只累加输出 token。",
            "- **绝对路径已脱敏。** 工作区路径一律相对化，残余的 home 目录显示为 `${HOME}`。",
            "",
            f"<sub>{len(all_stats)} 个会话 · "
            f"{sum(s.requests for s in all_stats)} 轮对话 · "
            f"{', '.join(sorted(models))}</sub>",
            "",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit 1 if the rendered output differs from what is on disk",
    )
    args = parser.parse_args()

    sources = sorted(
        args.source.glob("session_*.json"),
        key=lambda p: int(re.search(r"(\d+)", p.stem).group(1)),  # type: ignore[union-attr]
    )
    if not sources:
        print(f"no session exports under {args.source}", file=sys.stderr)
        return 1

    rendered: dict[Path, str] = {}
    all_stats: list[SessionStats] = []
    for path in sources:
        markdown, stats = render_session(path)
        rendered[args.out / stats.slug] = scrub(markdown)
        all_stats.append(stats)
    all_stats.sort(key=lambda s: s.started)
    rendered[args.out / "index.md"] = render_index(all_stats)

    stale = [p for p, text in rendered.items() if not p.exists() or p.read_text("utf-8") != text]
    if args.check:
        for path in stale:
            print(f"stale: {path.relative_to(REPO_ROOT)}", file=sys.stderr)
        return 1 if stale else 0

    args.out.mkdir(parents=True, exist_ok=True)
    for path, text in rendered.items():
        path.write_text(text, encoding="utf-8")
    print(
        f"wrote {len(rendered)} files to {args.out.relative_to(REPO_ROOT)}/ — "
        f"{len(all_stats)} sessions, {sum(s.requests for s in all_stats)} requests, "
        f"{sum(s.tool_calls for s in all_stats)} tool calls"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
