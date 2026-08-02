"""Configure a Pi provider from a key file, without the key ever passing through
the console or a commit.

Usage (defaults shown):
    python configure_pi_provider.py \
        --key-file "C:\\Users\\23393\\Desktop\\密钥.txt" \
        --provider deepseek \
        --base-url https://api.deepseek.com/v1 \
        --model DeepSeek-V4-Flash

What it does
  1. Reads the API key from --key-file (or the DEEPSEEK_API_KEY env var).
     Accepts a bare key, `KEY=value`, or a JSON object with an apiKey/key field.
  2. Probes {base_url}/models and prints the exact model ids the endpoint serves,
     so "DeepSeek-V4-Flash" can be resolved to whatever string the API accepts.
  3. Merges a provider block into the Pi config dir's models.json, keeping every
     existing provider, after writing a timestamped backup.

The key is only ever printed masked (first 4 + last 2 chars).
Run with --probe-only to list models without writing anything.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_KEY_FILE = Path(r"C:\Users\23393\Desktop\密钥.txt")
DEFAULT_CONFIG_DIR = Path(r"C:\Users\23393\AppData\Local\Temp\pi-probe")
IDENTITY_THINKING = {
    "off": "off", "minimal": "minimal", "low": "low", "medium": "medium",
    "high": "high", "xhigh": "xhigh", "max": "max",
}


def mask(secret: str) -> str:
    if len(secret) <= 8:
        return "<too short to mask>"
    return f"{secret[:4]}...{secret[-2:]} (len={len(secret)})"


def read_key(key_file: Path) -> str:
    """Key from the env var if set, else from the file. Tolerates `K=v`, JSON,
    or a bare token, and ignores blank/comment lines."""
    env = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if env:
        print("key source: DEEPSEEK_API_KEY environment variable")
        return env
    if not key_file.is_file():
        raise SystemExit(f"FATAL: key file not found: {key_file}")
    raw = key_file.read_text(encoding="utf-8", errors="replace").strip()
    if not raw:
        raise SystemExit(
            f"FATAL: key file is empty: {key_file}\n"
            "Save the DeepSeek API key into that file (a bare key on one line is "
            "fine), or set DEEPSEEK_API_KEY, then re-run."
        )
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            for field in ("apiKey", "api_key", "key", "token", "DEEPSEEK_API_KEY"):
                if isinstance(data.get(field), str) and data[field].strip():
                    print(f"key source: {key_file.name} (JSON field {field})")
                    return data[field].strip()
    except json.JSONDecodeError:
        pass
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line and not line.startswith("sk-"):
            line = line.split("=", 1)[1].strip().strip("\"'")
        candidate = line.strip().strip("\"'")
        if re.fullmatch(r"[A-Za-z0-9_\-.]{16,}", candidate):
            print(f"key source: {key_file.name}")
            return candidate
    raise SystemExit(
        f"FATAL: no key-looking token found in {key_file}. "
        "Expected a bare key, KEY=value, or JSON with an apiKey field."
    )


def probe_models(base_url: str, key: str, timeout: int = 30) -> list[dict]:
    url = base_url.rstrip("/") + "/models"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")[:500]
        raise SystemExit(f"FATAL: {url} returned HTTP {error.code}: {body}")
    except urllib.error.URLError as error:
        raise SystemExit(f"FATAL: cannot reach {url}: {error.reason}")
    models = payload.get("data") if isinstance(payload, dict) else None
    return models if isinstance(models, list) else []


def resolve_model(models: list[dict], wanted: str) -> str:
    ids = [str(m.get("id", "")) for m in models if isinstance(m, dict) and m.get("id")]
    if not ids:
        print("!! endpoint listed no models; using --model verbatim")
        return wanted
    lowered = {i.lower(): i for i in ids}
    if wanted.lower() in lowered:
        return lowered[wanted.lower()]
    # "DeepSeek-V4-Flash" -> "deepseek-v4-flash" -> "deepseekv4flash"
    squashed = re.sub(r"[^a-z0-9]", "", wanted.lower())
    for identifier in ids:
        if re.sub(r"[^a-z0-9]", "", identifier.lower()) == squashed:
            return identifier
    partial = [i for i in ids if squashed in re.sub(r"[^a-z0-9]", "", i.lower())]
    if len(partial) == 1:
        print(f"resolved {wanted!r} -> {partial[0]!r} by partial match")
        return partial[0]
    raise SystemExit(
        f"FATAL: {wanted!r} is not served by this endpoint.\n"
        f"Available ids: {ids}\n"
        "Re-run with --model set to one of those."
    )


def write_config(
    config_dir: Path,
    provider: str,
    display: str,
    base_url: str,
    key: str,
    model_id: str,
    model_name: str,
    reasoning: bool,
    context_window: int,
    max_tokens: int,
) -> Path:
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "models.json"
    config: dict = {"providers": {}}
    if path.is_file():
        backup = path.with_suffix(f".json.bak{len(list(config_dir.glob('models.json.bak*')))}")
        shutil.copyfile(path, backup)
        print("backed up existing models.json ->", backup.name)
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("providers"), dict):
                config = loaded
        except json.JSONDecodeError:
            print("!! existing models.json is not valid JSON; starting fresh")

    model_entry: dict = {
        "id": model_id,
        "name": model_name,
        "reasoning": reasoning,
        "contextWindow": context_window,
        "maxTokens": max_tokens,
        "input": ["text"],
    }
    if reasoning:
        model_entry["thinkingLevelMap"] = dict(IDENTITY_THINKING)

    config["providers"][provider] = {
        "name": display,
        "baseUrl": base_url,
        "apiKey": key,
        "api": "openai-completions",
        "authHeader": True,
        "compat": {"maxTokensField": "max_tokens"},
        "models": [model_entry],
    }
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("wrote", path)
    print("providers now configured:", sorted(config["providers"]))
    return path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--key-file", type=Path, default=DEFAULT_KEY_FILE)
    parser.add_argument("--config-dir", type=Path, default=DEFAULT_CONFIG_DIR)
    parser.add_argument("--provider", default="deepseek")
    parser.add_argument("--display-name", default="DeepSeek")
    parser.add_argument("--base-url", default="https://api.deepseek.com/v1")
    parser.add_argument("--model", default="DeepSeek-V4-Flash")
    parser.add_argument("--model-name", default="")
    parser.add_argument("--context-window", type=int, default=128000)
    parser.add_argument("--max-tokens", type=int, default=8192)
    reasoning = parser.add_mutually_exclusive_group()
    reasoning.add_argument("--reasoning", dest="reasoning", action="store_true", default=None)
    reasoning.add_argument("--no-reasoning", dest="reasoning", action="store_false")
    parser.add_argument("--probe-only", action="store_true")
    parser.add_argument("--skip-probe", action="store_true",
                        help="trust --model verbatim instead of listing /models")
    args = parser.parse_args()

    key = read_key(args.key_file)
    print("key:", mask(key))
    print("endpoint:", args.base_url)

    model_id = args.model
    if not args.skip_probe:
        models = probe_models(args.base_url, key)
        print(f"--- {len(models)} model(s) served by {args.base_url} ---")
        for entry in models:
            if isinstance(entry, dict):
                print("  ", json.dumps(entry, ensure_ascii=False)[:300])
        if args.probe_only:
            return 0
        model_id = resolve_model(models, args.model)
    print("selected model id:", model_id)

    if args.reasoning is None:
        auto = bool(re.search(r"reason|think|r1|-r\d", model_id, re.IGNORECASE))
        print(f"reasoning (auto-detected from id): {auto}")
        is_reasoning = auto
    else:
        is_reasoning = args.reasoning

    write_config(
        config_dir=args.config_dir,
        provider=args.provider,
        display=args.display_name,
        base_url=args.base_url,
        key=key,
        model_id=model_id,
        model_name=args.model_name or args.model,
        reasoning=is_reasoning,
        context_window=args.context_window,
        max_tokens=args.max_tokens,
    )
    print()
    print("Next:")
    print(f'  $env:WORKLOOP_DOGFOOD_PROVIDER="{args.provider}"')
    print(f'  $env:WORKLOOP_DOGFOOD_MODEL="{model_id}"')
    if not is_reasoning:
        print('  $env:WORKLOOP_DOGFOOD_THINKING="off"')
    print("  D:\\python\\python.exe dogfood_self2.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
