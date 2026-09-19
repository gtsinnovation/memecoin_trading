#!/usr/bin/env python3
"""
Cloudflare 403 diagnostic for the GMGN API.

The smoke test told us the request is being rejected at Cloudflare's edge
before GMGN's API sees it. This narrows down WHY, because the remedies are
completely different:

  - If curl succeeds where Python fails, the block is about how the Python
    HTTP client looks on the wire (TLS fingerprint / header ordering), not
    about you. Fixable on our side.
  - If curl fails the same way, the block is about your account, key or IP
    address. No amount of client-side cleverness helps; it's a dashboard
    setting or a question for GMGN support.

It also digs the Cloudflare error code and Ray ID out of the block page,
which is the single most informative thing available -- 1020 means a
firewall rule matched, 1010 means the client fingerprint was rejected,
1015 means rate limiting, and they point in different directions.

Run it exactly like the smoke test:
    python gmgn_diagnose.py
"""
import os
import re
import sys
import json
import time
import uuid
import shutil
import subprocess

try:
    import httpx
except ImportError:
    print("ERROR: httpx not installed. Run: pip install httpx")
    sys.exit(2)


def load_dotenv():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.isfile(path):
        return
    parsed = {}
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and v:
                parsed[k] = v
    for k, v in parsed.items():
        os.environ.setdefault(k, v)


load_dotenv()

API_KEY = os.environ.get("GMGN_API_KEY", "")
USER_AGENT = os.environ.get("GMGN_USER_AGENT", "gmgn-cli/1.6.1")
BASE = os.environ.get("GMGN_API_BASE", "https://openapi.gmgn.ai")

BODY = {"params": [{"label": "hot-search", "chain": "sol",
                      "interval": "1h", "limit": 50, "filters": []}]}


def cf_details(text: str, headers) -> str:
    """Pull the Cloudflare error code and Ray ID out of a block page."""
    out = []
    ray = None
    for k, v in headers.items():
        if k.lower() == "cf-ray":
            ray = v
        if k.lower() in ("cf-mitigated", "server"):
            out.append(f"{k}: {v}")
    if ray:
        out.append(f"CF-Ray: {ray}")

    # Cloudflare puts the code in several different places depending on
    # which block page you get: in prose ("Error 1010"), in a span
    # (class="cf-error-code">1020<), or in a data attribute. Try all of them
    # -- 1020 is the most common and only ever appears in the span form.
    code = None
    for pattern in (r'cf-error-code[^>]*>\s*(\d{4})',
                    r'[Ee]rror\s*(?:code)?\s*[: ]?\s*(\d{4})',
                    r'"errorCode"\s*:\s*"?(\d{4})'):
        m = re.search(pattern, text)
        if m:
            code = m.group(1)
            break
    if code is None and "used Cloudflare to restrict access" in text:
        # The signature title of a 1020 firewall-rule block, which some
        # variants render without the numeric code anywhere in the body.
        code = "1020"
        out.append("(code inferred from the 'used Cloudflare to restrict access' title)")
    if code:
        meaning = {
            "1010": "Cloudflare rejected the CLIENT FINGERPRINT (TLS signature / "
                    "browser integrity). This is about how the HTTP library looks "
                    "on the wire, NOT about your key or IP.",
            "1015": "You are being RATE LIMITED.",
            "1020": "A Cloudflare FIREWALL RULE matched and denied the request. "
                    "Commonly an IP/geo/ASN rule, or the site owner's own "
                    "allowlist -- i.e. likely about your IP or account, not the client.",
            "1006": "Your IP address has been banned.",
            "1009": "Your COUNTRY is blocked by the site owner.",
            "1012": "Access denied by the site owner's firewall.",
        }.get(code, "See https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-1xxx-errors/")
        out.append(f"Cloudflare error {code}: {meaning}")
    else:
        title = re.search(r"<title>(.*?)</title>", text, re.S | re.I)
        if title:
            out.append(f"Page title: {title.group(1).strip()[:120]}")
        if "challenge" in text.lower() or "captcha" in text.lower():
            out.append("Body mentions a challenge/CAPTCHA -- this is bot protection, "
                       "not an API-level rejection.")
    return "\n    ".join(out) if out else "(no Cloudflare markers found)"


def show_public_ip():
    """Cloudflare exposes /cdn-cgi/trace on every domain it fronts, so this
    shows the IP as Cloudflare sees it ON GMGN'S DOMAIN -- exactly the value
    an IP allowlist would be compared against."""
    print("\n" + "=" * 70)
    print("YOUR PUBLIC IP, AS CLOUDFLARE SEES IT ON GMGN'S DOMAIN")
    print("=" * 70)
    try:
        r = httpx.get(f"{BASE}/cdn-cgi/trace", timeout=15.0,
                        headers={"User-Agent": USER_AGENT})
        info = dict(line.split("=", 1) for line in r.text.strip().splitlines() if "=" in line)
        print(f"  IP:      {info.get('ip', '(unknown)')}")
        print(f"  Country: {info.get('loc', '(unknown)')}")
        print(f"  Colo:    {info.get('colo', '(unknown)')}")
        print("\n  ^ If GMGN's API key has an IP allowlist, THIS is the address that")
        print("    must be on it. Check https://gmgn.ai/ai -> your key -> IP settings.")
    except Exception as e:
        print(f"  Could not fetch: {e}")


def try_python():
    print("\n" + "=" * 70)
    print("TEST A: Python (httpx)")
    print("=" * 70)
    url = f"{BASE}/v1/market/hot_searches"
    params = {"timestamp": int(time.time()), "client_id": str(uuid.uuid4())}
    headers = {
        "X-APIKEY": API_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    print(f"  User-Agent: {USER_AGENT}")
    try:
        r = httpx.post(url, params=params, json=BODY, headers=headers, timeout=25.0)
    except Exception as e:
        print(f"  CONNECTION ERROR: {e}")
        return None
    print(f"  HTTP {r.status_code}")
    ctype = r.headers.get("content-type", "")
    print(f"  Content-Type: {ctype}")
    if "json" in ctype.lower():
        print(f"  JSON: {json.dumps(r.json())[:400]}")
        return r.status_code
    print(f"  Cloudflare markers:\n    {cf_details(r.text, r.headers)}")
    return r.status_code


def try_curl():
    print("\n" + "=" * 70)
    print("TEST B: curl (different TLS stack, same machine, same IP)")
    print("=" * 70)
    curl = shutil.which("curl") or shutil.which("curl.exe")
    if not curl:
        print("  curl not found -- skipping. (Windows 10+ normally ships it.)")
        return None
    url = (f"{BASE}/v1/market/hot_searches"
           f"?timestamp={int(time.time())}&client_id={uuid.uuid4()}")
    cmd = [
        curl, "-sS", "-o", "-", "-w", "\n__HTTP_STATUS__%{http_code}",
        "-X", "POST", url,
        "-H", f"X-APIKEY: {API_KEY}",
        "-H", "Content-Type: application/json",
        "-H", "Accept: application/json",
        "-H", f"User-Agent: {USER_AGENT}",
        "-d", json.dumps(BODY),
        "--max-time", "25",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
    except Exception as e:
        print(f"  curl failed to run: {e}")
        return None
    out = proc.stdout or ""
    status = None
    if "__HTTP_STATUS__" in out:
        out, _, tail = out.rpartition("__HTTP_STATUS__")
        status = tail.strip()
    print(f"  HTTP {status}")
    body = out.strip()
    if body.startswith("{") or body.startswith("["):
        print(f"  JSON: {body[:400]}")
    else:
        print(f"  Body (first 200 chars): {body[:200]}")
    if proc.stderr.strip():
        print(f"  stderr: {proc.stderr.strip()[:200]}")
    return status


def verdict(py_status, curl_status):
    print("\n" + "=" * 70)
    print("VERDICT")
    print("=" * 70)
    py_ok = py_status == 200
    curl_ok = str(curl_status) == "200"

    if py_ok:
        print("  Python got through. The earlier 403 is resolved -- re-run the smoke test.")
    elif curl_ok and not py_ok:
        print("  curl SUCCEEDS where Python FAILS, from the same machine and IP.")
        print("  => The block is about how the Python client looks on the wire")
        print("     (TLS fingerprint), not about your key, IP, or account.")
        print("  => Fix is on our side. Tell me this result and I'll switch the")
        print("     client to curl_cffi (impersonates a real browser's TLS) or")
        print("     shell out to curl for GMGN calls.")
    elif curl_status is None:
        print("  Python blocked; curl unavailable so we could not compare.")
        print("  => Check the Cloudflare error code above: 1010 points at the")
        print("     client fingerprint, 1020/1006/1009 point at your IP or region.")
    else:
        print(f"  BOTH Python and curl were blocked (curl: HTTP {curl_status}).")
        print("  => The block is NOT about the HTTP client. It is your IP, your")
        print("     key's configuration, or your region.")
        print("  => Most likely: your API key has an IP allowlist that does not")
        print("     include the address shown above. Add it at https://gmgn.ai/ai.")
        print("  => If there is no allowlist set, contact GMGN support with the")
        print("     CF-Ray ID above -- they can see exactly which rule fired.")


def main():
    print("GMGN CLOUDFLARE DIAGNOSTIC")
    if not API_KEY:
        print("\nERROR: GMGN_API_KEY not set (checked environment and .env).")
        return 2
    print(f"Key: {API_KEY[:4]}...{API_KEY[-4:]}  Base: {BASE}")
    show_public_ip()
    py_status = try_python()
    curl_status = try_curl()
    verdict(py_status, curl_status)
    return 0


if __name__ == "__main__":
    sys.exit(main())
