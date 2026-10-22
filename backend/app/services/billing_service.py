"""Estimate recorded Token spend, preserving rate snapshots and rerun history.

No network calls and no use of Harbor/LiteLLM's provider-independent USD totals.
Only recognized usage records are counted, never arbitrary task output JSON.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

from ..config import settings
from ..models import Job, JobType, PROVIDERS, utcnow
from ..models.billing import (
    BillingArchive, BillingSnapshot, BillingSummary, ModelPrice, PriceSnapshot, PricingSettings,
    UsageCharge,
)
from ..store import store

logger = logging.getLogger(__name__)
_LOCK = threading.RLock()
_MAX_JSON = 64 * 1024 * 1024
_JOB_LOCKS = [threading.RLock() for _ in range(64)]
_BILLING_VERSION = 2


def price_catalog() -> list[ModelPrice]:
    return [ModelPrice.model_validate(row) for row in json.loads(
        (settings.resources_dir / "model_pricing.json").read_text(encoding="utf-8"))]


def pricing() -> PricingSettings:
    result = store.pricing_doc.read()
    if result.prices is None:
        result.prices = price_catalog()
    return result


def save_pricing(payload: PricingSettings, actor) -> PricingSettings:
    payload = payload.model_copy(deep=True)
    payload.updated_at, payload.updated_by = utcnow(), actor.email
    return store.pricing_doc.write(payload)


def _vendor(url: str) -> tuple[str, str]:
    host = (urlsplit(url).hostname or "").lower()
    if host == "api.atlascloud.ai":
        return "atlascloud", ""
    if host == "dashscope.aliyuncs.com" or host.endswith(".cn-beijing.maas.aliyuncs.com"):
        return "alibaba", "cn-beijing"
    if host.endswith(".aliyuncs.com"):
        return "alibaba", "other"
    if host == "api.deepseek.com":
        return "deepseek", ""
    if host == "tokenhub.tencentmaas.com":
        return "tencent", "guangzhou"
    if host == "ark.cn-beijing.volces.com":
        return "volcengine", "cn-beijing"
    return {"api.openai.com": ("openai", ""), "api.anthropic.com": ("anthropic", ""),
            "generativelanguage.googleapis.com": ("google", "")}.get(host, ("custom", ""))


def _matches(price: ModelPrice, model: str) -> bool:
    return model.lower() in {name.lower() for name in [price.model, *price.aliases]}


def _canonical_model(model: str, component: PriceSnapshot) -> str:
    price = component.price
    def matches(name):
        return name == component.model or bool(price and (_matches(price, name) or name == price.official_model))
    if matches(model):
        return component.model
    # LiteLLM adds a client protocol prefix, including to server IDs that
    # already have a vendor prefix. Only strip it when the entire ID matches.
    for prefix in ("openai/", "anthropic/", "dashscope/", "deepseek/", "gemini/"):
        if model.startswith(prefix) and matches(model[len(prefix):]):
            return component.model
    return model


def _component(job: Job, config: PricingSettings, phase: str) -> PriceSnapshot:
    v = job.verify
    if phase == "agent" and (v.agent in {"oracle", "nop"} or v.deployment_id):
        return PriceSnapshot(model=v.model or v.agent, free=True)
    if phase == "judge" and v.disable_verification:
        return PriceSnapshot(free=True)
    api_id = v.model_api_id if phase == "agent" else v.judge_model_api_id
    item = store.model_apis.get(api_id) if api_id else None
    model = item.model_id if item else v.model if phase == "agent" else ""
    url = (item.base_url or str(PROVIDERS[item.provider]["default_base_url"])) if item else ""
    vendor = "custom"
    if item and item.platform_id:
        platform = store.model_platforms.get(item.platform_id)
        if platform:
            vendor = platform.vendor
            protocol = ("responses" if v.use_openai_bridge else v.agent_api_protocol or ("anthropic" if v.agent == "claude-code" else "responses")) if phase == "agent" else v.judge_api_protocol
            url = platform.endpoints.get(protocol) or platform.endpoints.get("openai") or url
    env = {**(item.extra_env if item and phase == "agent" else {}), **(v.agent_env if phase == "agent" else v.verifier_env)}
    keys = ("JUDGE_BASE_URL",) if phase == "judge" else ("ANTHROPIC_BASE_URL",) if v.agent == "claude-code" and not v.use_openai_bridge else ("OPENAI_BASE_URL", "OPENAI_API_BASE", "DASHSCOPE_BASE_URL")
    override_url = next((env[key] for key in keys if env.get(key)), "")
    if override_url and override_url.rstrip("/") != url.rstrip("/"):
        url = override_url
        vendor, _ = _vendor(url)
    if phase == "judge" and env.get("JUDGE_MODEL"):
        model = env["JUDGE_MODEL"]
    detected, region = _vendor(url)
    if vendor == "custom":
        vendor = detected
    # A vendor label alone cannot establish the region or a subscription tariff.
    candidates = sorted(config.prices or [], key=lambda row: not row.vendor.startswith("platform:"))
    price = next((row.model_copy(deep=True) for row in candidates
                  if row.vendor in {vendor, f"platform:{item.platform_id}" if item else ""}
                  and _matches(row, model) and (not row.region or row.region == region)), None)
    if item and item.platform_id:
        platform = store.model_platforms.get(item.platform_id)
        if platform and platform.access_mode != "standard" and not (price and price.vendor.startswith("platform:")):
            price = None
    cache_mode = "explicit" if phase == "agent" and v.agent == "claude-code" and not v.use_openai_bridge else "implicit" if phase == "agent" and (v.agent == "codex" or v.use_openai_bridge) else "unknown"
    return PriceSnapshot(model=model, vendor=vendor, region=region, price=price, cache_mode=cache_mode)


def capture(job: Job, *, historical: bool = False) -> BillingSnapshot | None:
    if job.type != JobType.VERIFY:
        return None
    config = pricing()
    return BillingSnapshot(usd_cny=config.usd_cny, exchange_date=config.exchange_date,
                           exchange_source=config.exchange_source, historical=historical,
                           agent=_component(job, config, "agent"), judge=_component(job, config, "judge"))


def _read(path: Path) -> dict:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_JSON:
            return {}
        result = json.loads(path.read_text(encoding="utf-8"))
        return result if isinstance(result, dict) else {}
    except (OSError, ValueError, UnicodeError):
        return {}


def _count(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def normalized_usage(metrics: dict, *, final: bool = False) -> dict[str, int]:
    """ATIF prompt_tokens already INCLUDES cache read and cache creation."""
    extra = metrics.get("extra") or {}
    prefix = "total_" if final else ""
    prompt = _count(metrics.get(prefix + "prompt_tokens"))
    output = _count(metrics.get(prefix + "completion_tokens"))
    read = _count(extra.get(prefix + "cache_read_input_tokens", metrics.get(prefix + "cached_tokens")))
    write = _count(extra.get(prefix + "cache_creation_input_tokens", extra.get(prefix + "cache_write_input_tokens")))
    creation = extra.get("cache_creation") or {}
    long_write = _count(creation.get("ephemeral_1h_input_tokens")) if isinstance(creation, dict) else 0
    long_write = min(write, long_write)
    if read + write > prompt:
        raise ValueError("缓存 Token 大于总输入 Token，无法确认用量语义")
    return {"input": max(0, prompt - read - write), "output": output,
            "cache_read": read, "cache_write": write - long_write, "cache_write_long": long_write}


def estimate(tokens: dict[str, int], component: PriceSnapshot, snapshot: BillingSnapshot,
             *, aggregate: bool = False, model: str = "", timestamp: str = "") -> tuple[float | None, list[str]]:
    if component.free:
        return 0.0, []
    price = component.price
    model = _canonical_model(model, component) if model else ""
    if price is None or (model and not _matches(price, model) and model != component.model
                         and model != price.official_model):
        return None, [f"价格待配置：{component.vendor}/{model or component.model or '未知模型'}"]
    date = timestamp[:10] if timestamp else utcnow().date().isoformat()
    if price.valid_until and date > price.valid_until:
        return None, [f"价格已过有效期（{price.valid_until}）：{price.model}"]
    warnings = []
    if price.note and "按高峰价格上限估算" in price.note:
        warnings.append(price.note)
    total_input = sum(tokens.get(key, 0) for key in ("input", "cache_read", "cache_write", "cache_write_long"))
    if aggregate and len(price.tiers) > 1:
        tier = price.tiers[-1]
        warnings.append("仅有汇总用量，无法还原单次上下文长度；按最高输入阶梯估算")
    else:
        tier = next((tier for tier in price.tiers if tier.max_input is None or total_input <= tier.max_input), None)
    if tier is None:
        return None, ["单次上下文超出已核验的计价区间"]
    amount = Decimal(0)
    known = False
    for key, count in tokens.items():
        if count <= 0:
            continue
        rate = getattr(tier, key, None)
        if key == "cache_read" and component.vendor == "alibaba":
            if component.cache_mode == "explicit":
                rate = tier.explicit_cache_read
            elif component.cache_mode == "unknown" and rate != tier.explicit_cache_read:
                warnings.append("缓存模式不明，缓存读取按隐式价格上限估算")
        if rate is None:
            warnings.append(f"{price.model} 的 {key} 单价待配置，此部分未计入")
            continue
        known = True
        amount += Decimal(count) * Decimal(str(rate)) / Decimal(1_000_000)
    if price.currency == "USD":
        amount *= Decimal(str(snapshot.usd_cny))
    amount *= Decimal(str(price.discount))
    return float(amount) if known else None, warnings


def _paths(job: Job):
    from . import metrics_parser, runner
    output = Path(job.runtime.output_dir) if job.runtime.output_dir else runner.output_dir_for(job)
    native = output if (output / "config.json").is_file() else metrics_parser.harbor_job_dir(output, job.id)
    roots = [native] if native else []
    work = Path(job.runtime.work_dir) if job.runtime.work_dir else runner.work_dir_for(job)
    history = work / "rerun-history"
    if history.is_dir():
        roots.extend(p for p in history.iterdir() if p.is_dir() and not p.is_symlink())
    for root in roots:
        if root.is_symlink():
            continue
        for trial in root.iterdir():
            if not trial.is_dir() or trial.is_symlink():
                continue
            yield trial


def _trajectories(trial: Path):
    # Restrict traversal to native log areas; workspace/task output is untrusted.
    for area in (trial / "agent", trial / "verifier", trial / "steps"):
        if not area.is_dir() or area.is_symlink():
            continue
        for directory, dirs, files in os.walk(area, followlinks=False):
            dirs[:] = [name for name in dirs if name not in {"workspace", "artifacts", ".git", "sessions", "node_modules"}
                       and not (Path(directory) / name).is_symlink()]
            if "trajectory.json" in files:
                path = Path(directory) / "trajectory.json"
                parts = path.relative_to(trial).parts
                phase = "judge" if "verifier" in parts or "graded" in parts else "agent"
                yield path, phase


def _raw_sessions(trial: Path, snapshot: BillingSnapshot):
    """Read live Claude/Codex logs before Harbor publishes an ATIF trajectory.

    Use message IDs or cumulative token counters to deduplicate streaming events.
    Never add both Codex last_token_usage and total_token_usage.
    """
    for phase in ("agent", "judge"):
        area = trial / ("agent" if phase == "agent" else "verifier")
        if not area.is_dir() or area.is_symlink() or (area / "trajectory.json").is_file():
            continue
        groups = defaultdict(dict)
        claude_messages = {}
        claude_session = None
        for directory, dirs, files in os.walk(area, followlinks=False):
            dirs[:] = [name for name in dirs if name not in {"workspace", ".git", "node_modules", "artifacts", "setup"}
                       and not (Path(directory) / name).is_symlink()]
            for name in files:
                path = Path(directory) / name
                if not name.endswith(".jsonl") or "sessions" not in path.relative_to(area).parts or path.is_symlink():
                    continue
                if not path.is_file() or path.stat().st_size > _MAX_JSON:
                    continue
                session, model = "", getattr(snapshot, phase).model
                seen_totals = set()
                with path.open(encoding="utf-8", errors="replace") as handle:
                    for line in handle:
                        try:
                            event = json.loads(line)
                        except ValueError:
                            continue
                        if not isinstance(event, dict):
                            continue
                        if isinstance(event.get("sessionId"), str):
                            candidate = (str(event.get("timestamp") or ""), event["sessionId"])
                            if claude_session is None or candidate[0] < claude_session[0]:
                                claude_session = candidate
                        payload = event.get("payload") or {}
                        if event.get("type") == "session_meta" and isinstance(payload, dict):
                            session = payload.get("id") or session
                        if event.get("type") == "turn_context" and isinstance(payload, dict):
                            model = payload.get("model") or model
                        message = event.get("message")
                        if event.get("type") == "assistant" and isinstance(message, dict):
                            usage = message.get("usage")
                            session = event.get("sessionId") or session
                            if not isinstance(usage, dict) or not message.get("id") or not session:
                                continue
                            if usage.get("input_tokens") is None or usage.get("output_tokens") is None:
                                continue
                            read, write = _count(usage.get("cache_read_input_tokens")), _count(usage.get("cache_creation_input_tokens"))
                            metrics = {"prompt_tokens": _count(usage.get("input_tokens")) + read + write,
                                       "completion_tokens": usage.get("output_tokens"), "cached_tokens": read, "extra": usage}
                            claude_messages[message["id"]] = (message.get("model") or model, normalized_usage(metrics), event.get("timestamp") or "")
                        elif event.get("type") == "event_msg" and isinstance(payload, dict) and payload.get("type") == "token_count":
                            info = payload.get("info") or {}
                            total, usage = info.get("total_token_usage"), info.get("last_token_usage")
                            if not session or not isinstance(total, dict) or not isinstance(usage, dict):
                                continue
                            identity = json.dumps(total, sort_keys=True)
                            if identity in seen_totals or usage.get("input_tokens") is None or usage.get("output_tokens") is None:
                                continue
                            seen_totals.add(identity)
                            metrics = {"prompt_tokens": usage["input_tokens"], "completion_tokens": usage["output_tokens"],
                                       "cached_tokens": usage.get("cached_input_tokens"),
                                       "extra": {"cache_write_input_tokens": usage.get("cache_write_input_tokens")}}
                            groups[(phase, session, model)][identity] = (normalized_usage(metrics), False, event.get("timestamp") or "")
        if claude_session:
            for message_id, (model, tokens, timestamp) in claude_messages.items():
                groups[(phase, claude_session[1], model)][message_id] = (tokens, False, timestamp)
        for (phase, session, model), usages in groups.items():
            if usages:
                yield phase, session, model, list(usages.values())


@dataclass
class UsageRecord:
    phase: str
    session: str
    model: str
    usages: list[tuple[dict[str, int], bool, str]]
    warnings: list[str] = field(default_factory=list)


def _scan_trial(trial: Path, snapshot: BillingSnapshot) -> tuple[list[UsageRecord], set[str]]:
    # Only read logs here. Ledger ownership and writes are resolved at commit time.
    records: list[UsageRecord] = []
    obsolete: set[str] = set()
    found_agent = False
    # New LLM Judges record counters through the platform RewardKit entrypoint.
    for directory, dirs, files in os.walk(trial, followlinks=False):
        dirs[:] = [name for name in dirs if name in {"verifier", "steps", "graded"} or
                   ("steps" in Path(directory).relative_to(trial).parts and name not in {"workspace", "artifacts", ".git", "agent"})]
        dirs[:] = [name for name in dirs if not (Path(directory) / name).is_symlink()]
        if "prt-model-usage.jsonl" not in files:
            continue
        path = Path(directory) / "prt-model-usage.jsonl"
        if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_JSON:
            continue
        requests = {}
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(record, dict) or record.get("schema_version") != "prt-usage-v1" or not record.get("request_id"):
                    continue
                requests[record["request_id"]] = record
        for request_id, record in requests.items():
            tokens = normalized_usage(record.get("metrics") or {})
            records.append(UsageRecord("judge", "llm:" + request_id, record.get("model") or snapshot.judge.model,
                                       [(tokens, False, record.get("timestamp") or "")]))
    for phase, session, model, usages in _raw_sessions(trial, snapshot):
        found_agent |= phase == "agent"
        records.append(UsageRecord(phase, session, model, usages))
    for path, phase in _trajectories(trial):
        data = _read(path)
        if not str(data.get("schema_version", "")).startswith("ATIF"):
            continue
        session = data.get("session_id")
        if not isinstance(session, str) or not session:
            continue
        agent = data.get("agent") or {}
        default_model = agent.get("model_name") or getattr(snapshot, phase).model
        groups = defaultdict(list)
        seen = set()
        missing_usage = False
        for step in data.get("steps") or []:
            if not isinstance(step, dict) or step.get("source") != "agent":
                continue
            if not isinstance(step.get("metrics"), dict):
                missing_usage |= bool(step.get("llm_call_count"))
                continue
            metrics = step["metrics"]
            if metrics.get("prompt_tokens") is None or metrics.get("completion_tokens") is None:
                missing_usage = True
                continue
            identity = (step.get("extra") or {}).get("api_call_id") or step.get("step_id")
            if identity is None or identity in seen:
                continue
            seen.add(identity)
            tokens = normalized_usage(metrics)
            if any(tokens.values()):
                groups[step.get("model_name") or default_model].append((tokens, False, step.get("timestamp") or ""))
        if not groups and isinstance(data.get("final_metrics"), dict):
            metrics = data["final_metrics"]
            if metrics.get("total_prompt_tokens") is not None and metrics.get("total_completion_tokens") is not None:
                tokens = normalized_usage(metrics, final=True)
                if any(tokens.values()):
                    groups[default_model].append((tokens, True, ""))
        for model, usages in groups.items():
            found_agent |= phase == "agent"
            charge = UsageRecord(phase, session, model, usages)
            if missing_usage:
                charge.warnings.append("轨迹中部分模型请求缺少用量，此部分未计入")
            records.append(charge)
    if found_agent:
        data = _read(trial / "result.json") or _read(trial / "results.json")
        if data.get("id"):
            fallback_id = _identity("agent", "trial:" + str(data["id"]), snapshot.agent.model)
            obsolete.add(fallback_id)
    if found_agent or snapshot.agent.free:
        return records, obsolete
    # Old native results have no cache-write split. Be explicit about the gap.
    data = _read(trial / "result.json") or _read(trial / "results.json")
    metrics = data.get("agent_result") or {}
    if metrics.get("n_input_tokens") is None or metrics.get("n_output_tokens") is None or not data.get("id"):
        return records, obsolete
    tokens = normalized_usage({"prompt_tokens": metrics["n_input_tokens"], "completion_tokens": metrics["n_output_tokens"], "cached_tokens": metrics.get("n_cache_tokens")})
    if any(tokens.values()):
        result = UsageRecord("agent", "trial:" + str(data["id"]), snapshot.agent.model, [(tokens, True, data.get("started_at") or "")])
        result.warnings.append("仅有 Harbor 汇总用量，缺少缓存写入明细，费用可能不完整")
        records.append(result)
    return records, obsolete


def _identity(phase, session, model):
    return hashlib.sha256(json.dumps([phase, session, model], ensure_ascii=False).encode()).hexdigest()


def _charge(job, snapshot, phase, session, model, usages, ledger) -> UsageCharge:
    component = getattr(snapshot, phase)
    # Upstream may report the official model behind an Atlas alias.
    model = _canonical_model(model, component)
    identity = _identity(phase, session, model)
    previous = ledger.get(identity)
    charge_job_id = previous.job_id if previous else job.id
    if not previous and phase == "agent" and job.harbor_action and job.harbor_action.mode == "regrade":
        source = store.jobs.get(job.harbor_action.source_job_id)
        if source:
            charge_job_id = source.id
            snapshot = source.billing.snapshot or snapshot
            component = snapshot.agent
    # Copies from resume/regrade reuse the original session identity and tariff.
    if previous:
        snapshot = previous.snapshot
        component = getattr(snapshot, phase)
    total = defaultdict(int)
    amount = Decimal(0)
    known = False
    warnings = set()
    for tokens, aggregate, timestamp in usages:
        value, notes = estimate(tokens, component, snapshot, aggregate=aggregate, model=model, timestamp=timestamp)
        if value is not None:
            known = True
            amount += Decimal(str(value))
        warnings.update(notes)
        for key, count in tokens.items():
            total[key] += count
    return UsageCharge(id=identity, job_id=charge_job_id,
                       phase=phase, model=model, vendor=component.vendor,
                       amount_cny=float(amount) if known else None, tokens=dict(total),
                       requests=len(usages), warnings=sorted(warnings), snapshot=snapshot)


def _input_signature(job: Job) -> str:
    # Metrics/rename updates are unrelated to usage; run lifecycle changes invalidate the cache.
    value = [_BILLING_VERSION, job.status.value, job.runtime.started_at, job.runtime.finished_at,
             job.runtime.output_dir, job.runtime.work_dir, job.verify.model if job.verify else "",
             job.harbor_action.model_dump(mode="json") if job.harbor_action else None]
    return hashlib.sha256(json.dumps(value, default=str, sort_keys=True).encode()).hexdigest()


def needs_refresh(job: Job) -> bool:
    if job.type != JobType.VERIFY:
        return False
    saved = job.billing
    if not saved.updated_at or saved.input_signature != _input_signature(job):
        return True
    age = (utcnow() - saved.updated_at).total_seconds()
    # Recheck finished tasks occasionally for late-written/copied logs, including after restart.
    recent_finish = job.runtime.finished_at and (utcnow() - job.runtime.finished_at).total_seconds() < 60
    return age >= (15 if job.status.is_active or recent_finish else 3600)


def refresh(job: Job, *, force: bool = False, _ancestors: set[str] | None = None) -> Job:
    if job.type != JobType.VERIFY:
        return job
    ancestors = set(_ancestors or ())
    if job.id in ancestors:
        return job
    ancestors.add(job.id)
    if job.harbor_action and job.harbor_action.source_job_id != job.id:
        source = store.jobs.get(job.harbor_action.source_job_id)
        if source:
            refresh(source, force=force, _ancestors=ancestors)
    # The same job is never scanned twice concurrently; different jobs read in parallel.
    with _JOB_LOCKS[int(hashlib.sha256(job.id.encode()).hexdigest(), 16) % len(_JOB_LOCKS)]:
        job = store.jobs.get(job.id) or job
        if not force and not needs_refresh(job):
            return job
        signature = _input_signature(job)
        snapshot = job.billing.snapshot or capture(job, historical=True)
        records, obsolete, warnings = [], set(), set()
        try:
            trials = list(_paths(job))
        except (OSError, ValueError):
            trials = []
            warnings.add("部分用量记录无法读取，当前费用不完整")
        for trial in trials:
            try:
                collected, removed = _scan_trial(trial, snapshot)
                records.extend(collected)
                obsolete.update(removed)
            except (OSError, ValueError, TypeError, AttributeError):
                logger.warning("Could not read complete billing usage for %s", job.id, exc_info=True)
                warnings.add("部分用量记录无法读取，当前费用不完整")
        with _LOCK:
            current = store.jobs.get(job.id)
            if current is None or _input_signature(current) != signature:
                return current or job
            ledger = {row.id: row for row in store.usage_charges.all()}
            changes = {}
            for record in records:
                charge = _charge(job, snapshot, record.phase, record.session, record.model, record.usages, ledger)
                charge.warnings.extend(record.warnings)
                old = ledger.get(charge.id)
                if old and sum(charge.tokens.values()) < sum(old.tokens.values()):
                    continue
                if old != charge:
                    changes[charge.id] = charge
                    ledger[charge.id] = charge
            # Commit replacements before deleting aggregate fallbacks, so failed writes cannot erase spend.
            if changes:
                store.usage_charges.put_many(changes.values())
            for identity in obsolete:
                if identity in ledger:
                    store.usage_charges.delete(identity)
                    ledger.pop(identity, None)
            entries = [row for row in ledger.values() if row.job_id == job.id]
            amounts = [row.amount_cny for row in entries if row.amount_cny is not None]
            for row in entries:
                warnings.update(row.warnings)
            phases = {}
            for phase in ("agent", "judge"):
                component = getattr(snapshot, phase)
                rows = [row for row in entries if row.phase == phase]
                values = [row.amount_cny for row in rows if row.amount_cny is not None]
                phases[phase] = sum(values) if values else 0.0 if component.free else None
                if not component.free and not rows:
                    if phase == "agent" and job.harbor_action and job.harbor_action.mode == "regrade":
                        phases[phase] = 0.0
                    else:
                        warnings.add(f"{'Agent' if phase == 'agent' else 'Judge'} 用量缺失；仅统计有记录的调用")
            if snapshot.historical:
                warnings.add("历史任务按首次统计时的价格估算，无法还原历史账单")
            grouped = defaultdict(lambda: {"amount_cny": None, "requests": 0, "unpriced": 0})
            for row in entries:
                group = grouped[(row.vendor, row.model, row.phase)]
                group["requests"] += row.requests
                group["unpriced"] += int(row.amount_cny is None or bool(row.warnings))
                if row.amount_cny is not None:
                    group["amount_cny"] = (group["amount_cny"] or 0) + row.amount_cny
            amount = sum(amounts) if amounts else 0.0 if all(value == 0 for value in phases.values()) else None
            incomplete = any(value is None for value in phases.values()) or any(row.amount_cny is None or row.warnings for row in entries) or "部分用量记录无法读取，当前费用不完整" in warnings
            billing = BillingSummary(status="pending" if amount is None else "partial" if incomplete else "estimated",
                                     amount_cny=amount, agent_cny=phases["agent"], judge_cny=phases["judge"],
                                     snapshot=snapshot, requests=sum(row.requests for row in entries), warnings=sorted(warnings),
                                     by_model=[dict(vendor=vendor, model=model, phase=phase, **data) for (vendor, model, phase), data in grouped.items()],
                                     updated_at=utcnow(), input_signature=signature)
            updated = store.jobs.update(job.id, lambda item: setattr(item, "billing", billing)) or job
            return updated


class BillingWorker:
    """Bounded background queue; request handlers only enqueue IDs and read saved totals."""

    def __init__(self, workers: int = 4, capacity: int = 256):
        self.workers, self.capacity = workers, capacity
        self._lock = threading.RLock()
        self._executor: ThreadPoolExecutor | None = None
        self._pending: set[str] = set()
        self._retry_at: dict[str, float] = {}

    def start(self) -> None:
        with self._lock:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="prt-billing")

    def stop(self) -> None:
        with self._lock:
            executor, self._executor = self._executor, None
        if executor:
            executor.shutdown(wait=True, cancel_futures=True)
        with self._lock:
            self._pending.clear()
            self._retry_at.clear()

    def request(self, jobs: list[Job]) -> list[str]:
        waiting = []
        with self._lock:
            for job in jobs:
                if not needs_refresh(job):
                    continue
                waiting.append(job.id)
                if (self._executor is None or job.id in self._pending or len(self._pending) >= self.capacity
                        or time.monotonic() < self._retry_at.get(job.id, 0)):
                    continue
                self._pending.add(job.id)
                try:
                    self._executor.submit(self._run, job.id)
                except RuntimeError:
                    self._pending.discard(job.id)
        return waiting

    def _run(self, job_id: str) -> None:
        try:
            job = store.jobs.get(job_id)
            if job:
                refresh(job)
            with self._lock:
                self._retry_at.pop(job_id, None)
        except Exception:
            logger.exception("Background billing failed for %s", job_id)
            with self._lock:
                self._retry_at[job_id] = time.monotonic() + 30
        finally:
            with self._lock:
                self._pending.discard(job_id)


worker = BillingWorker()


def archive(job: Job) -> None:
    if job.type != JobType.VERIFY:
        return
    job = refresh(job, force=True)
    project = store.projects.get(job.project_id)
    store.billing_archives.put(BillingArchive(id=job.id, owner_id=job.owner_id, owner_email=job.owner_email,
        project_id=job.project_id, project_name=project.name if project else job.project_id,
        billing=job.billing))


def summary(jobs: list[Job], *, actor=None) -> dict:
    groups = {key: {} for key in ("projects", "people", "models")}
    project_names = {project.id: project.name for project in store.projects.all()}

    def add(kind, key, label, amount, job_id, incomplete):
        row = groups[kind].setdefault(key, dict(id=key, name=label, amount_cny=None, job_ids=set(), incomplete_jobs=set()))
        row["job_ids"].add(job_id)
        if incomplete:
            row["incomplete_jobs"].add(job_id)
        if amount is not None:
            row["amount_cny"] = (row["amount_cny"] or 0) + amount

    evaluations = [job for job in jobs if job.type == JobType.VERIFY]
    current_ids = {job.id for job in jobs}
    if actor is not None:
        archived = store.billing_archives.find(lambda row: row.id not in current_ids and (actor.is_admin or row.owner_id == actor.id))
        for row in archived:
            project_names.setdefault(row.project_id, row.project_name)
        evaluations.extend(archived)
    for job in evaluations:
        incomplete = job.billing.status in {"partial", "pending"}
        add("projects", job.project_id, project_names.get(job.project_id, job.project_id), job.billing.amount_cny, job.id, incomplete)
        add("people", job.owner_id, job.owner_email, job.billing.amount_cny, job.id, incomplete)
        for row in job.billing.by_model:
            add("models", row["vendor"] + "/" + row["model"], row["model"], row["amount_cny"], job.id, bool(row["unpriced"]))
        if job.billing.snapshot:
            recorded = {(row["vendor"], row["model"]) for row in job.billing.by_model}
            for phase in ("agent", "judge"):
                component = getattr(job.billing.snapshot, phase)
                if phase == "agent" and isinstance(job, Job) and job.harbor_action and job.harbor_action.mode == "regrade":
                    continue
                if component.model and not component.free and (component.vendor, component.model) not in recorded:
                    add("models", component.vendor + "/" + component.model, component.model, None, job.id, True)
    result = {}
    for kind, rows in groups.items():
        result[kind] = sorted([dict(id=row["id"], name=row["name"], amount_cny=row["amount_cny"],
                                  jobs=len(row["job_ids"]), incomplete_jobs=len(row["incomplete_jobs"]))
                               for row in rows.values()], key=lambda row: -(row["amount_cny"] or 0))
    values = [job.billing.amount_cny for job in evaluations if job.billing.amount_cny is not None]
    return dict(amount_cny=sum(values) if values else None, jobs=len(evaluations),
                incomplete_jobs=sum(job.billing.status in {"partial", "pending"} for job in evaluations),
                note="人民币预估费用，仅含已记录的模型 Token；不含工具、缓存存储、订阅及本地 GPU 费用。失败、重跑及已删除任务的历史调用仍计入，恢复/评分复制的记录不重复计费。", **result)
