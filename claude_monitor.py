"""
claude_monitor.py — read-only monitor for running Claude Code instances.

Signals are derived from:
  - psutil process tree  (always available)
  - ~/.claude/projects/  JSONL session files (available after first tool call)
  - ~/.claude/stats-cache.json  (daily/weekly aggregates)

No instrumentation of Claude Code is required.
"""

import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

# ── pricing + context windows ─────────────────────────────────────────────────

CONTEXT_WINDOWS: dict[str, int] = {
    "claude-opus-4-6":        200_000,
    "claude-sonnet-4-6":      200_000,
    "claude-haiku-4-5-20251001": 200_000,
    "default":                200_000,
}

PRICING: dict[str, dict] = {   # USD per million tokens
    "claude-opus-4-6":        {"in": 15.00, "out": 75.00, "cr": 1.50,  "cw": 18.75},
    "claude-sonnet-4-6":      {"in":  3.00, "out": 15.00, "cr": 0.30,  "cw":  3.75},
    "claude-haiku-4-5-20251001": {"in": 0.80, "out":  4.00, "cr": 0.08, "cw":  1.00},
    "default":                {"in":  3.00, "out": 15.00, "cr": 0.30,  "cw":  3.75},
}

_PROJECTS = Path.home() / ".claude" / "projects"
_STATS    = Path.home() / ".claude" / "stats-cache.json"

# ── data model ────────────────────────────────────────────────────────────────

@dataclass
class ToolCall:
    name:    str
    summary: str
    status:  str   # "active" | "done" | "error"

@dataclass
class AgentInfo:
    agent_id:   str
    summary:    str
    status:     str   # "active" | "completed"
    tool_count: int
    last_tool:  ToolCall | None

@dataclass
class TokenStats:
    model:         str
    context_used:  int
    context_max:   int
    context_pct:   float
    session_in:    int
    session_out:   int
    cache_read:    int
    session_cost:  float
    cache_savings: float

@dataclass
class AttentionFlag:
    kind:    str   # "permission"|"input"|"ratelimit"|"stuck"|"context"|"errors"
    message: str

@dataclass
class ClaudeInstance:
    pid:          int
    project_name: str
    cwd:          str
    uptime_s:     int
    cpu:          float
    mem_mb:       float
    version:      str | None
    git_branch:   str | None
    session_id:   str | None
    terminal_app: str | None
    terminal_tty: str | None
    current_tool: ToolCall | None
    recent_tools: list[ToolCall] = field(default_factory=list)
    agents:       list[AgentInfo] = field(default_factory=list)
    tokens:       TokenStats | None = None
    attention:    list[AttentionFlag] = field(default_factory=list)

@dataclass
class DailyStats:
    cost_today:       float
    cost_week:        list[float]   # index 0 = today, 6 = 6 days ago
    tokens_today_in:  int
    tokens_today_out: int
    sessions_today:   int


# ── CPU measurement cache ─────────────────────────────────────────────────────
# psutil.cpu_percent(interval=None) returns 0 on first call per process —
# it needs two calls separated by time to compute a delta.
# We keep a persistent {pid: Process} cache and read cpu_percent at the start
# of each find_instances() call so by the time we build the instance the
# measurement covers the full interval between calls (~3 s in the web UI).

_proc_cache: dict[int, "psutil.Process"] = {}
_cpu_readings: dict[int, float]          = {}


# ── public API ────────────────────────────────────────────────────────────────

def find_instances() -> list[ClaudeInstance]:
    """Return all running Claude Code instances with enriched metadata."""
    if not HAS_PSUTIL:
        return []

    # 1. Read cpu_percent for all previously-seen processes (real delta values).
    for pid in list(_proc_cache):
        try:
            _cpu_readings[pid] = _proc_cache[pid].cpu_percent()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            del _proc_cache[pid]
            _cpu_readings.pop(pid, None)

    # 2. Discover current instances.
    instances  = []
    seen_pids: set[int] = set()
    for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'create_time',
                                      'cpu_percent', 'memory_info', 'status']):
        try:
            if not _is_claude_code(proc):
                continue
            pid = proc.pid
            if pid in seen_pids:
                continue
            seen_pids.add(pid)
            if pid not in _proc_cache:
                # Prime the measurement — returns 0 now, real on next call.
                _proc_cache[pid] = proc
                proc.cpu_percent()
                _cpu_readings[pid] = 0.0
            instances.append(_build_instance(proc, cpu=_cpu_readings.get(pid, 0.0)))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    # 3. Evict stale pids.
    for pid in list(_proc_cache):
        if pid not in seen_pids:
            del _proc_cache[pid]
            _cpu_readings.pop(pid, None)

    return instances


def _is_claude_code(proc) -> bool:
    """Identify a Claude Code CLI process (not Claude Desktop)."""
    try:
        # Fastest check: cmdline is exactly ['claude']
        cmdline = proc.cmdline()
        if cmdline == ['claude']:
            return True
        # Fallback: exe path contains the claude versions dir
        exe = proc.exe()
        if '.local/share/claude/versions/' in exe:
            return True
        return False
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def daily_stats() -> DailyStats:
    """Aggregate today's cost from live JSONL files; 7-day activity from JSONL mtimes."""
    import datetime

    today     = datetime.date.today()
    today_str = today.isoformat()
    day_starts = [
        datetime.datetime.combine(today - datetime.timedelta(days=d),
                                  datetime.time.min).timestamp()
        for d in range(8)   # 8 boundaries for 7 day buckets
    ]

    # ── session counts from stats-cache (best effort, may be stale) ───────────
    sessions_today = 0
    cache_week: list[int] = [0] * 7
    try:
        data = json.loads(_STATS.read_text())
        daily_list = data.get("dailyActivity", [])
        by_date = {entry["date"]: entry for entry in daily_list if "date" in entry}
        sessions_today = by_date.get(today_str, {}).get("sessionCount", 0)
        for delta in range(7):
            d = (today - datetime.timedelta(days=delta)).isoformat()
            cache_week[delta] = by_date.get(d, {}).get("messageCount", 0)
    except Exception:
        pass

    # ── scan JSONL files: cost today + 7-day session counts ──────────────────
    cost_today = 0.0
    in_today   = 0
    out_today  = 0
    jsonl_week = [0] * 7   # session file count per day (index 0=today)

    try:
        for jsonl in _PROJECTS.rglob("*.jsonl"):
            try:
                if jsonl.parent.name == 'subagents':
                    continue
                mtime = jsonl.stat().st_mtime

                # Which day bucket? day_starts[0]=start of today, [1]=yesterday…
                bucket = None
                for delta in range(7):
                    if mtime >= day_starts[delta]:
                        bucket = delta
                        break
                if bucket is None:
                    continue   # older than 7 days

                jsonl_week[bucket] += 1

                if bucket == 0:   # today's files — read for cost
                    for e in _tail_jsonl(jsonl, n_kb=32):
                        if e.get('type') != 'assistant':
                            continue
                        usage = e.get('message', {}).get('usage', {})
                        if not usage:
                            continue
                        model = e.get('message', {}).get('model', 'default')
                        p  = _price(model)
                        i  = usage.get('input_tokens', 0)
                        o  = usage.get('output_tokens', 0)
                        cr = usage.get('cache_read_input_tokens', 0)
                        cw = usage.get('cache_creation_input_tokens', 0)
                        cost_today += (i*p["in"] + o*p["out"] + cr*p["cr"] + cw*p["cw"]) / 1_000_000
                        in_today   += i
                        out_today  += o
            except Exception:
                pass
    except Exception:
        pass

    # Prefer cache data if it looks fresh (non-zero for recent days), else use JSONL counts
    cache_has_recent = any(cache_week[:3])
    week = cache_week if cache_has_recent else jsonl_week

    if sessions_today == 0:
        sessions_today = jsonl_week[0]

    return DailyStats(
        cost_today=round(cost_today, 3),
        cost_week=[float(v) for v in week],
        tokens_today_in=in_today,
        tokens_today_out=out_today,
        sessions_today=sessions_today,
    )


def claude_desktop_process():
    """Return the Claude Desktop psutil.Process, or None."""
    if not HAS_PSUTIL:
        return None
    for p in psutil.process_iter(['name', 'exe']):
        try:
            if p.info['name'] == 'Claude' and 'Claude.app' in (p.info.get('exe') or ''):
                return p
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return None


def focus_terminal(pid: int) -> bool:
    """Focus the terminal window/pane running the given PID. Returns True on success."""
    try:
        proc = psutil.Process(pid)
        tty  = proc.terminal()
        if not tty:
            return False
        tty_short = tty.replace('/dev/', '')
        app, _    = _detect_terminal(pid)

        if app == 'iTerm2':
            script = f'''
tell application "iTerm2"
  activate
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        if tty of s contains "{tty_short}" then
          tell w to select tab t
          select s
          return
        end if
      end repeat
    end repeat
  end repeat
end tell'''
        elif app == 'Terminal':
            script = f'''
tell application "Terminal"
  activate
  repeat with w in windows
    repeat with t in tabs of w
      if tty of t contains "{tty_short}" then
        set selected of t to true
        set frontmost of w to true
        return
      end if
    end repeat
  end repeat
end tell'''
        elif app == 'Warp':
            script = 'tell application "Warp" to activate'
        elif app == 'VS Code':
            script = 'tell application "Visual Studio Code" to activate'
        else:
            return False

        result = subprocess.run(['osascript', '-e', script],
                                capture_output=True, timeout=3)
        return result.returncode == 0
    except Exception:
        return False


# ── instance builder ──────────────────────────────────────────────────────────

def _build_instance(proc, cpu: float = 0.0) -> ClaudeInstance:
    try:
        cwd = proc.cwd()
    except Exception:
        cwd = "?"

    try:
        mem_mb = proc.memory_info().rss / (1024 * 1024)
    except Exception:
        mem_mb = 0.0

    try:
        uptime_s = int(time.time() - proc.create_time())
    except Exception:
        uptime_s = 0

    terminal_app, terminal_tty = _detect_terminal(proc.pid)
    jsonl_path, session_dir    = _match_session(cwd)
    entries                    = _tail_jsonl(jsonl_path) if jsonl_path else []

    # Pull metadata from first few entries
    version    = None
    git_branch = None
    session_id = None
    for e in entries[:10]:
        version    = version    or e.get('version')
        git_branch = git_branch or e.get('gitBranch')
        session_id = session_id or e.get('sessionId')

    current_tool, tool_ts = _current_tool(entries)
    recent_tools           = _recent_tools(entries)
    tokens                 = _token_stats(entries)
    agents                 = _get_agents(session_dir) if session_dir else []
    attention              = _detect_attention(proc, entries, tokens, current_tool, tool_ts)

    return ClaudeInstance(
        pid          = proc.pid,
        project_name = Path(cwd).name if cwd != "?" else "?",
        cwd          = cwd,
        uptime_s     = uptime_s,
        cpu          = cpu,
        mem_mb       = mem_mb,
        version      = version,
        git_branch   = git_branch,
        session_id   = session_id,
        terminal_app = terminal_app,
        terminal_tty = terminal_tty,
        current_tool = current_tool,
        recent_tools = recent_tools,
        agents       = agents,
        tokens       = tokens,
        attention    = attention,
    )


# ── JSONL helpers ─────────────────────────────────────────────────────────────

def _match_session(cwd: str) -> tuple[Path | None, Path | None]:
    """Map a working directory to its most-recent top-level JSONL file."""
    try:
        sanitized   = cwd.replace('/', '-')
        session_dir = _PROJECTS / sanitized
        if not session_dir.exists():
            return None, None
        jsonls = sorted(
            (f for f in session_dir.glob('*.jsonl') if f.parent == session_dir),
            key=lambda p: p.stat().st_mtime, reverse=True
        )
        return (jsonls[0] if jsonls else None), session_dir
    except Exception:
        return None, None


def _tail_jsonl(path: Path, n_kb: int = 12) -> list[dict]:
    """Read the last n_kb of a JSONL file, returning parsed entries."""
    try:
        size = path.stat().st_size
        with open(path, 'rb') as f:
            f.seek(max(0, size - n_kb * 1024))
            raw = f.read().decode('utf-8', errors='replace')
        lines = raw.split('\n')
        if size > n_kb * 1024:
            lines = lines[1:]   # skip potentially partial first line
        result = []
        for line in lines:
            line = line.strip()
            if line:
                try:
                    result.append(json.loads(line))
                except Exception:
                    pass
        return result
    except Exception:
        return []


def _current_tool(entries: list[dict]) -> tuple[ToolCall | None, str | None]:
    """Return the last tool_use that has no matching tool_result, plus its timestamp."""
    last_use  = None
    last_ts   = None
    seen_ids  = set()

    for e in entries:
        t = e.get('type')
        if t == 'assistant':
            for item in _content(e):
                if item.get('type') == 'tool_use':
                    last_use = item
                    last_ts  = e.get('timestamp')
        elif t == 'user':
            for item in _content(e):
                if item.get('type') == 'tool_result':
                    seen_ids.add(item.get('tool_use_id'))

    if last_use and last_use.get('id') not in seen_ids:
        return ToolCall(
            name    = last_use.get('name', '?'),
            summary = _summarise_tool(last_use.get('name', ''), last_use.get('input', {})),
            status  = 'active',
        ), last_ts
    return None, None


def _recent_tools(entries: list[dict], n: int = 5) -> list[ToolCall]:
    """Return last n completed tool calls (tool_use with matching tool_result)."""
    uses    = {}   # id → (name, input)
    results = {}   # id → is_error
    for e in entries:
        t = e.get('type')
        if t == 'assistant':
            for item in _content(e):
                if item.get('type') == 'tool_use':
                    uses[item['id']] = (item.get('name','?'), item.get('input',{}))
        elif t == 'user':
            for item in _content(e):
                if item.get('type') == 'tool_result':
                    tid = item.get('tool_use_id','')
                    results[tid] = item.get('is_error', False)

    completed = []
    for tid, (name, inp) in uses.items():
        if tid in results:
            completed.append(ToolCall(
                name    = name,
                summary = _summarise_tool(name, inp),
                status  = 'error' if results[tid] else 'done',
            ))
    return completed[-n:]


def _token_stats(entries: list[dict]) -> TokenStats | None:
    """Sum usage fields from all assistant messages."""
    total_in = total_out = cache_read = cache_write = 0
    latest_in = 0
    model = 'default'

    for e in entries:
        if e.get('type') != 'assistant':
            continue
        msg   = e.get('message', {})
        usage = msg.get('usage', {})
        if not usage:
            continue
        model     = msg.get('model', model)
        i         = usage.get('input_tokens', 0)
        o         = usage.get('output_tokens', 0)
        cr        = usage.get('cache_read_input_tokens', 0)
        cw        = usage.get('cache_creation_input_tokens', 0)
        total_in  += i
        total_out += o
        cache_read  += cr
        cache_write += cw
        # Real context fill = all tokens sent: uncached + cache-read + cache-created
        latest_in   = i + cr + cw

    if total_in == 0 and total_out == 0:
        return None

    p           = _price(model)
    ctx_max     = CONTEXT_WINDOWS.get(model, CONTEXT_WINDOWS['default'])
    cost        = (total_in*p["in"] + total_out*p["out"] +
                   cache_read*p["cr"] + cache_write*p["cw"]) / 1_000_000
    savings     = cache_read * (p["in"] - p["cr"]) / 1_000_000

    return TokenStats(
        model        = model,
        context_used = latest_in,
        context_max  = ctx_max,
        context_pct  = latest_in / ctx_max,
        session_in   = total_in,
        session_out  = total_out,
        cache_read   = cache_read,
        session_cost = round(cost, 4),
        cache_savings= round(savings, 4),
    )


def _get_agents(session_dir: Path) -> list[AgentInfo]:
    agents_dir = session_dir / 'subagents'
    if not agents_dir.exists():
        return []
    agents = []
    for meta_file in sorted(agents_dir.glob('*.meta.json')):
        try:
            meta      = json.loads(meta_file.read_text())
            agent_id  = meta_file.stem
            # Read the agent's JSONL for tool count and last tool
            jsonl = agents_dir / f"{agent_id}.jsonl"
            entries   = _tail_jsonl(jsonl, n_kb=6) if jsonl.exists() else []
            tc, _     = _current_tool(entries)
            recent    = _recent_tools(entries, n=1)
            last_tool = tc or (recent[-1] if recent else None)
            tool_count = sum(
                1 for e in entries
                if e.get('type') == 'user'
                for item in _content(e)
                if item.get('type') == 'tool_result'
            )
            # Determine status: active if there's an in-flight tool, else completed
            status = 'active' if tc else 'completed'
            # Summary from prompt in metadata or first assistant message
            summary = meta.get('prompt', '')[:60] or agent_id
            agents.append(AgentInfo(
                agent_id   = agent_id,
                summary    = summary,
                status     = status,
                tool_count = tool_count,
                last_tool  = last_tool,
            ))
        except Exception:
            pass
    return agents


# ── attention detection ───────────────────────────────────────────────────────

def _detect_attention(proc, entries, tokens, current_tool, tool_ts) -> list[AttentionFlag]:
    flags = []
    now   = time.time()

    # Rate limited
    for e in entries[-6:]:
        if e.get('type') == 'user':
            for item in _content(e):
                if item.get('type') == 'tool_result' and item.get('is_error'):
                    content = str(item.get('content', ''))
                    if 'rate' in content.lower() or '429' in content:
                        flags.append(AttentionFlag('ratelimit', 'Rate limited'))
                        break

    # Repeated errors (3+ consecutive)
    error_run = 0
    for e in reversed(entries):
        if e.get('type') == 'user':
            for item in _content(e):
                if item.get('type') == 'tool_result':
                    if item.get('is_error'):
                        error_run += 1
                    else:
                        error_run = 0
                    break
        if error_run >= 3:
            break
    if error_run >= 3:
        flags.append(AttentionFlag('errors', f'{error_run} consecutive errors'))

    # Permission waiting vs stuck
    if current_tool and tool_ts:
        try:
            ts_epoch = _parse_ts(tool_ts)
            elapsed  = now - ts_epoch
        except Exception:
            elapsed  = 0

        try:
            children    = proc.children(recursive=True)
            has_children = len(children) > 0
            cpu_active   = proc.cpu_percent() > 2.0
        except Exception:
            has_children = False
            cpu_active   = False

        if not has_children and not cpu_active and elapsed > 5:
            flags.append(AttentionFlag(
                'permission',
                f'Waiting for permission: {current_tool.name} {current_tool.summary}',
            ))
        elif has_children and elapsed > 180:
            flags.append(AttentionFlag(
                'stuck',
                f'Stuck for {int(elapsed//60)}m: {current_tool.name} {current_tool.summary}',
            ))

    # Waiting for user input
    if not current_tool:
        last_assistant = None
        last_ts        = None
        for e in reversed(entries):
            if e.get('type') == 'assistant':
                last_assistant = e
                last_ts        = e.get('timestamp')
                break
        if last_assistant:
            sr = last_assistant.get('message', {}).get('stop_reason', '')
            if sr == 'end_turn' and last_ts:
                try:
                    elapsed = now - _parse_ts(last_ts)
                    if elapsed > 30:
                        flags.append(AttentionFlag('input', 'Waiting for user input'))
                except Exception:
                    pass

    # Context high
    if tokens and tokens.context_pct > 0.80:
        flags.append(AttentionFlag(
            'context',
            f'Context {tokens.context_pct*100:.0f}% full ({tokens.context_used//1000}K / {tokens.context_max//1000}K)',
        ))

    return flags


# ── terminal detection ────────────────────────────────────────────────────────

def _detect_terminal(pid: int) -> tuple[str | None, str | None]:
    try:
        proc      = psutil.Process(pid)
        tty       = proc.terminal()
        tty_short = tty.replace('/dev/', '') if tty else None
        p         = proc
        for _ in range(10):   # walk up at most 10 levels
            try:
                name = p.name()
                exe  = ''
                try: exe = p.exe()
                except Exception: pass
                if 'iTerm2' in name or 'iterm' in name.lower():
                    return 'iTerm2', tty_short
                if name == 'Terminal':
                    return 'Terminal', tty_short
                # Warp's main process is named 'stable'
                if name == 'stable' or 'Warp' in name or 'warp' in exe.lower():
                    return 'Warp', tty_short
                if name in ('Code', 'Code Helper', 'Electron') or 'Visual Studio Code' in exe:
                    return 'VS Code', tty_short
                p = p.parent()
                if p is None:
                    break
            except Exception:
                break
        return None, tty_short
    except Exception:
        return None, None


# ── tool summariser ───────────────────────────────────────────────────────────

def _summarise_tool(name: str, inp: dict) -> str:
    try:
        if name == 'Bash':
            return (inp.get('description') or inp.get('command', ''))[:60]
        if name == 'Edit':
            fp  = Path(inp.get('file_path', '')).name
            old = inp.get('old_string', '')[:25].replace('\n', '↵')
            return f"{fp}  {old}" if old else fp
        if name in ('Read', 'Write', 'NotebookEdit'):
            return Path(inp.get('file_path', '')).name
        if name == 'Glob':
            return inp.get('pattern', '')
        if name == 'Grep':
            return inp.get('pattern', '')
        if name == 'Agent':
            return inp.get('prompt', inp.get('args', ''))[:50]
        if name == 'WebSearch':
            return inp.get('query', '')[:60]
        if name == 'WebFetch':
            url = inp.get('url', '')
            try:
                from urllib.parse import urlparse
                return urlparse(url).netloc
            except Exception:
                return url[:40]
        # fallback
        first_val = next(iter(inp.values()), '') if inp else ''
        return str(first_val)[:50]
    except Exception:
        return ''


# ── utilities ─────────────────────────────────────────────────────────────────

def _content(entry: dict) -> list:
    c = entry.get('message', {}).get('content', [])
    return c if isinstance(c, list) else []


def _price(model: str) -> dict:
    for key in PRICING:
        if key in model:
            return PRICING[key]
    return PRICING['default']


def _today_key() -> str:
    import datetime
    return datetime.date.today().isoformat()


def _parse_ts(ts: str) -> float:
    """Parse ISO-8601 timestamp to epoch float."""
    import datetime
    dt = datetime.datetime.fromisoformat(ts.replace('Z', '+00:00'))
    return dt.timestamp()
