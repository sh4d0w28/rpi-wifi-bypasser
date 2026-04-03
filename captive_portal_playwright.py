#!/usr/bin/env python3

"""Browser-driven captive portal acknowledgement helper.

This script reads the current captive-portal target from the runtime status
file, renders it in a real browser via Playwright, fingerprints the visible
page, and clicks a previously learned or heuristically obvious action button.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path
from urllib.parse import urlparse


BUTTON_QUERY = 'button, input[type="submit"], input[type="button"], a, [role="button"]'
VISIBLE_BUTTON_COUNT_JS = """
() => {
  const isVisible = (el) => {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return !!(rect.width || rect.height)
      && style.display !== 'none'
      && style.visibility !== 'hidden'
      && style.opacity !== '0';
  };
  return Array.from(document.querySelectorAll(%r)).filter(isVisible).length;
}
""" % BUTTON_QUERY
SNAPSHOT_JS = """
() => {
  const isVisible = (el) => {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return !!(rect.width || rect.height)
      && style.display !== 'none'
      && style.visibility !== 'hidden'
      && style.opacity !== '0';
  };

  const textOf = (el) => {
    const raw = el.innerText || el.textContent || el.value || el.getAttribute('aria-label') || el.getAttribute('title') || '';
    return raw.replace(/\\s+/g, ' ').trim();
  };

  const cssPath = (el) => {
    if (!el || el.nodeType !== 1) {
      return '';
    }
    if (el.id) {
      return '#' + CSS.escape(el.id);
    }
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1 && parts.length < 8) {
      let part = node.tagName.toLowerCase();
      const classNames = Array.from(node.classList || []).slice(0, 2).map((name) => CSS.escape(name));
      if (classNames.length) {
        part += '.' + classNames.join('.');
      }
      const parent = node.parentElement;
      if (parent) {
        const siblings = Array.from(parent.children).filter((item) => item.tagName === node.tagName);
        if (siblings.length > 1) {
          part += ':nth-of-type(' + (siblings.indexOf(node) + 1) + ')';
        }
      }
      parts.unshift(part);
      node = parent;
      if (node && node.id) {
        parts.unshift('#' + CSS.escape(node.id));
        break;
      }
    }
    return parts.join(' > ');
  };

  const buttonLikes = Array.from(document.querySelectorAll(%r));
  const candidates = buttonLikes.map((el, index) => {
    const text = textOf(el);
    const rect = el.getBoundingClientRect();
    const labelContainer = el.closest('label');
    const dataset = {};
    for (const [key, value] of Object.entries(el.dataset || {})) {
      dataset[key] = value;
    }
    return {
      index,
      tag: el.tagName.toLowerCase(),
      type: (el.getAttribute('type') || '').toLowerCase(),
      text,
      id: el.id || '',
      name: el.getAttribute('name') || '',
      href: el.getAttribute('href') || '',
      role: el.getAttribute('role') || '',
      disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
      visible: isVisible(el),
      classes: Array.from(el.classList || []),
      css_path: cssPath(el),
      rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
      dataset,
      in_awing_screen: !!el.closest('#awing-captive__htmlscreen, #awing-captive__htmlcommon'),
      parent_text: textOf(labelContainer || el.parentElement || el),
    };
  }).filter((item) => item.visible && item.text);

  const checkboxes = Array.from(document.querySelectorAll('input[type="checkbox"]')).map((el, index) => {
    const label = el.closest('label');
    return {
      index,
      checked: !!el.checked,
      disabled: !!el.disabled,
      visible: isVisible(el),
      css_path: cssPath(el),
      text: textOf(label || el.parentElement || el),
    };
  }).filter((item) => item.visible);

  const htmlScreen = document.querySelector('#awing-captive__htmlscreen');
  const htmlCommon = document.querySelector('#awing-captive__htmlcommon');
  const bodyText = document.body ? (document.body.innerText || '').replace(/\\s+/g, ' ').trim() : '';
  return {
    title: document.title || '',
    url: window.location.href,
    body_text: bodyText,
    awing_htmlscreen_text: htmlScreen ? textOf(htmlScreen) : '',
    awing_htmlcommon_text: htmlCommon ? textOf(htmlCommon) : '',
    candidates,
    checkboxes,
  };
}
""" % BUTTON_QUERY

POSITIVE_PATTERNS = [
    ("access internet", 130),
    ("use wifi", 130),
    ("connect to internet", 125),
    ("continue to internet", 125),
    ("get online", 120),
    ("vao internet", 120),
    ("truy cap internet", 120),
    ("use internet", 115),
    ("continue", 95),
    ("tiep tuc", 95),
    ("connect", 90),
    ("ket noi", 90),
    ("agree", 85),
    ("dong y", 85),
    ("accept", 85),
    ("chap nhan", 85),
    ("login", 80),
    ("log in", 80),
    ("dang nhap", 80),
    ("start", 70),
    ("next", 60),
    ("ok", 55),
    ("xac nhan", 55),
]
NEGATIVE_PATTERNS = [
    ("cancel", -160),
    ("decline", -160),
    ("close", -120),
    ("back", -100),
    ("disconnect", -100),
    ("logout", -100),
    ("sign out", -100),
    ("skip", -70),
    ("watch ad", -70),
    ("register", -50),
]
AGREE_CHECKBOX_PATTERNS = [
    "agree",
    "accept",
    "dong y",
    "chap nhan",
    "terms",
    "policy",
    "privacy",
    "condition",
]


def env_float(name: str, default: float, minimum: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, float(raw))
    except ValueError:
        return default


DEFAULT_STATUS_PATH = Path(os.environ.get("STATUS_PATH", "/run/rpi_ap_tools_status.json"))
DEFAULT_RULES_PATH = Path(os.environ.get("CAPTIVE_PORTAL_RULES_PATH", "/var/lib/rpi_ap_tools/captive_portal_rules.json"))
DEFAULT_DEBUG_DIR = Path(os.environ.get("CAPTIVE_PORTAL_DEBUG_DIR", "/run/rpi_ap_tools_portal_action"))
DEFAULT_BROWSER_BIN = os.environ.get("CAPTIVE_PORTAL_BROWSER_BIN", "").strip()
DEFAULT_TIMEOUT_SEC = env_float("CAPTIVE_PORTAL_BROWSER_TIMEOUT_SEC", 45.0, 5.0)
DEFAULT_SETTLE_SEC = env_float("CAPTIVE_PORTAL_BROWSER_SETTLE_SEC", 6.0, 1.0)
DEFAULT_POST_CLICK_WAIT_SEC = env_float("CAPTIVE_PORTAL_POST_CLICK_WAIT_SEC", 5.0, 1.0)
DEFAULT_HEADLESS = os.environ.get("CAPTIVE_PORTAL_HEADLESS", "1").strip().lower() not in ("0", "false", "no")


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def atomic_write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temp_path.replace(path)


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(content, encoding="utf-8")
    temp_path.replace(path)


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.lower()
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def stable_hash(*parts: str, limit: int = 24) -> str:
    payload = "||".join(parts)
    return hashlib.sha256(payload.encode("utf-8", errors="ignore")).hexdigest()[:limit]


def current_portal_target(status_path: Path, fallback_url: str, fallback_ssid: str) -> dict:
    status = load_json(status_path, {})
    probe = status.get("probe", {}) if isinstance(status, dict) else {}
    portal = probe.get("portal_capture", {}) if isinstance(probe, dict) else {}
    active_wifi = status.get("active_wifi", {}) if isinstance(status, dict) else {}
    html_path = portal.get("html_path") or ""
    requested_url = portal.get("requested_url") or ""
    final_url = portal.get("final_url") or ""
    url = fallback_url or final_url or requested_url
    ssid = fallback_ssid or portal.get("wifi_name") or active_wifi.get("name") or ""
    return {
        "ssid": ssid.strip(),
        "url": url.strip(),
        "requested_url": requested_url.strip(),
        "final_url": final_url.strip(),
        "html_path": html_path,
        "status_path": str(status_path),
    }


def load_rules(rules_path: Path) -> dict:
    payload = load_json(rules_path, {})
    if isinstance(payload, dict) and isinstance(payload.get("rules"), list):
        return payload
    return {"version": 1, "rules": []}


def save_rules(rules_path: Path, payload: dict) -> None:
    try:
        atomic_write_json(rules_path, payload)
    except OSError:
        pass


def compute_page_keys(snapshot: dict) -> dict:
    url = snapshot.get("url", "")
    parsed = urlparse(url)
    title_norm = normalize_text(snapshot.get("title", ""))
    body_norm = normalize_text(snapshot.get("body_text", ""))[:4000]
    button_norms = sorted({normalize_text(item.get("text", "")) for item in snapshot.get("candidates", []) if item.get("text")})
    return {
        "host": parsed.netloc.lower(),
        "path": parsed.path or "/",
        "title_norm": title_norm,
        "button_signature": stable_hash(*button_norms) if button_norms else "",
        "body_signature": stable_hash(title_norm, body_norm),
        "button_count": len(button_norms),
    }


def match_rule(rules: dict, *, ssid: str, keys: dict) -> dict | None:
    best = None
    best_score = -1
    for rule in rules.get("rules", []):
        if rule.get("host") != keys.get("host"):
            continue
        if rule.get("button_signature") and rule.get("button_signature") != keys.get("button_signature"):
            continue
        if rule.get("body_signature") and rule.get("body_signature") != keys.get("body_signature"):
            continue
        score = 5
        if rule.get("ssid"):
            if ssid and rule.get("ssid") == ssid:
                score += 3
            else:
                continue
        if rule.get("path") == keys.get("path"):
            score += 1
        if rule.get("title_norm") == keys.get("title_norm"):
            score += 1
        if score > best_score:
            best = rule
            best_score = score
    return best


def candidate_score(candidate: dict) -> int:
    text_norm = normalize_text(candidate.get("text", ""))
    score = 0
    for token, weight in POSITIVE_PATTERNS:
        if token in text_norm:
            score = max(score, weight)
    for token, penalty in NEGATIVE_PATTERNS:
        if token in text_norm:
            score += penalty
    if candidate.get("tag") == "button":
        score += 12
    if candidate.get("type") in ("submit", "button"):
        score += 10
    if candidate.get("in_awing_screen"):
        score += 8
    if candidate.get("disabled"):
        score -= 80
    if 0 < len(text_norm) <= 3:
        score -= 10
    if len(text_norm) > 60:
        score -= 15
    return score


def choose_candidate(snapshot: dict) -> tuple[dict | None, str]:
    candidates = list(snapshot.get("candidates", []))
    if not candidates:
        return None, "no visible buttons or links found"
    scored = ranked_candidates(snapshot)
    positive = [item for item in scored if item["score"] >= 60]
    if len(positive) == 1:
        return positive[0]["candidate"], f"heuristic match: {positive[0]['candidate']['text']!r}"
    if len(positive) > 1:
        if positive[0]["score"] >= positive[1]["score"] + 20:
            return positive[0]["candidate"], f"best heuristic match: {positive[0]['candidate']['text']!r}"
        options = ", ".join(repr(item["candidate"]["text"]) for item in positive[:5])
        return None, f"ambiguous positive buttons: {options}"
    if len(candidates) == 1:
        return candidates[0], f"single visible clickable element: {candidates[0]['text']!r}"
    options = ", ".join(repr(item["candidate"]["text"]) for item in scored[:5])
    return None, f"no confident button match; top candidates: {options}"


def ranked_candidates(snapshot: dict) -> list[dict]:
    candidates = list(snapshot.get("candidates", []))
    return sorted(
        [{"score": candidate_score(item), "candidate": item} for item in candidates],
        key=lambda item: item["score"],
        reverse=True,
    )


def build_analysis(snapshot: dict, *, matched_rule: dict | None, candidate: dict | None, reason: str) -> dict:
    ranked = ranked_candidates(snapshot)
    top_candidates = []
    for item in ranked[:8]:
        current = dict(item["candidate"])
        current["score"] = item["score"]
        current["recommended"] = bool(
            candidate
            and current.get("text") == candidate.get("text")
            and current.get("css_path") == candidate.get("css_path")
        )
        top_candidates.append(current)
    recommended = None
    if candidate:
        recommended = {
            "text": candidate.get("text", ""),
            "css_path": candidate.get("css_path", ""),
            "tag": candidate.get("tag", ""),
            "type": candidate.get("type", ""),
            "source": "rule" if matched_rule else "heuristic",
        }
    return {
        "generated_at": time.time(),
        "reason": reason,
        "matched_rule_id": matched_rule.get("id", "") if matched_rule else "",
        "recommended": recommended,
        "top_candidates": top_candidates,
        "candidate_count": len(snapshot.get("candidates", [])),
        "checkbox_count": len(snapshot.get("checkboxes", [])),
    }


def maybe_accept_checkboxes(page, snapshot: dict) -> list[str]:
    clicked = []
    for checkbox in snapshot.get("checkboxes", []):
        if checkbox.get("checked") or checkbox.get("disabled"):
            continue
        text_norm = normalize_text(checkbox.get("text", ""))
        if not any(token in text_norm for token in AGREE_CHECKBOX_PATTERNS):
            continue
        css_path = checkbox.get("css_path")
        if not css_path:
            continue
        try:
            locator = page.locator(css_path).first
            locator.check(timeout=3000)
            clicked.append(checkbox.get("text", "checkbox"))
        except Exception:
            continue
    if clicked:
        page.wait_for_timeout(500)
    return clicked


def locate_candidate(page, candidate: dict):
    css_path = candidate.get("css_path") or ""
    if css_path:
        locator = page.locator(css_path).first
        if locator.count() > 0:
            return locator
    item_id = candidate.get("id") or ""
    if item_id:
        locator = page.locator(f"#{item_id}").first
        if locator.count() > 0:
            return locator
    text = candidate.get("text") or ""
    if text:
        locator = page.get_by_text(text, exact=True).first
        if locator.count() > 0:
            return locator
    raise RuntimeError(f"Unable to resolve click target for {candidate.get('text')!r}")


def learn_rule(existing: dict | None, *, ssid: str, snapshot: dict, keys: dict, candidate: dict, reason: str) -> dict:
    rule = dict(existing or {})
    rule.update(
        {
            "ssid": ssid or rule.get("ssid", ""),
            "host": keys.get("host", ""),
            "path": keys.get("path", ""),
            "title_norm": keys.get("title_norm", ""),
            "button_signature": keys.get("button_signature", ""),
            "body_signature": keys.get("body_signature", ""),
            "button_text": candidate.get("text", ""),
            "button_text_norm": normalize_text(candidate.get("text", "")),
            "button_tag": candidate.get("tag", ""),
            "button_type": candidate.get("type", ""),
            "button_css_path": candidate.get("css_path", ""),
            "reason": reason,
            "learned_at": time.time(),
            "sample_url": snapshot.get("url", ""),
        }
    )
    if not rule.get("id"):
        rule["id"] = stable_hash(rule.get("ssid", ""), rule.get("host", ""), rule.get("button_signature", ""), rule.get("button_text_norm", ""))
    return rule


def upsert_rule(rules: dict, rule: dict) -> None:
    items = rules.setdefault("rules", [])
    for index, existing in enumerate(items):
        if existing.get("id") == rule.get("id"):
            items[index] = rule
            return
    items.append(rule)


def save_debug(debug_dir: Path, prefix: str, snapshot: dict, page_html: str, page=None) -> None:
    try:
        debug_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(debug_dir / f"{prefix}.json", snapshot)
        atomic_write_text(debug_dir / f"{prefix}.html", page_html)
        if page is not None:
            page.screenshot(path=str(debug_dir / f"{prefix}.png"), full_page=True)
    except OSError:
        pass
    except Exception:
        pass


def load_page_snapshot(page) -> dict:
    snapshot = page.evaluate(SNAPSHOT_JS)
    snapshot["keys"] = compute_page_keys(snapshot)
    return snapshot


def wait_for_page_buttons(page, settle_sec: float) -> dict:
    deadline = time.monotonic() + settle_sec
    snapshot = load_page_snapshot(page)
    while time.monotonic() < deadline:
        if snapshot.get("candidates"):
            return snapshot
        try:
            count = int(page.evaluate(VISIBLE_BUTTON_COUNT_JS))
        except Exception:
            count = 0
        if count > 0:
            return load_page_snapshot(page)
        page.wait_for_timeout(1000)
        snapshot = load_page_snapshot(page)
    return snapshot


def open_browser(playwright, *, browser_bin: str, headless: bool):
    launch_kwargs = {
        "headless": headless,
        "args": [
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
        ],
    }
    if browser_bin:
        launch_kwargs["executable_path"] = browser_bin
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        launch_kwargs["args"].append("--no-sandbox")
    return playwright.chromium.launch(**launch_kwargs)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render and acknowledge a captive portal in a real browser.")
    parser.add_argument("--status-path", default=str(DEFAULT_STATUS_PATH))
    parser.add_argument("--rules-path", default=str(DEFAULT_RULES_PATH))
    parser.add_argument("--debug-dir", default=str(DEFAULT_DEBUG_DIR))
    parser.add_argument("--url", default="")
    parser.add_argument("--ssid", default="")
    parser.add_argument("--browser-bin", default=DEFAULT_BROWSER_BIN)
    parser.add_argument("--timeout-sec", type=float, default=DEFAULT_TIMEOUT_SEC)
    parser.add_argument("--settle-sec", type=float, default=DEFAULT_SETTLE_SEC)
    parser.add_argument("--post-click-wait-sec", type=float, default=DEFAULT_POST_CLICK_WAIT_SEC)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--preview-only", action="store_true")
    parser.add_argument("--no-save", action="store_true")
    parser.add_argument("--headless", dest="headless", action="store_true", default=DEFAULT_HEADLESS)
    parser.add_argument("--headed", dest="headless", action="store_false")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    status_path = Path(args.status_path)
    rules_path = Path(args.rules_path)
    debug_dir = Path(args.debug_dir)
    target = current_portal_target(status_path, args.url, args.ssid)
    if not target["url"]:
        print(f"No captive portal URL found in {status_path}", file=sys.stderr)
        return 2

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright is not installed. Install with: python3 -m pip install playwright && python3 -m playwright install chromium", file=sys.stderr)
        return 2

    rules = load_rules(rules_path)
    try:
        with sync_playwright() as playwright:
            browser = open_browser(playwright, browser_bin=args.browser_bin, headless=args.headless)
            context = browser.new_context(ignore_https_errors=True, viewport={"width": 1365, "height": 940})
            page = context.new_page()
            page.set_default_timeout(max(1000, int(args.timeout_sec * 1000)))
            before_url = target["url"]
            page.goto(before_url, wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=max(1000, int(min(args.settle_sec, 10.0) * 1000)))
            except Exception:
                pass
            snapshot = wait_for_page_buttons(page, args.settle_sec)
            keys = snapshot["keys"]
            matched_rule = match_rule(rules, ssid=target["ssid"], keys=keys)
            candidate = None
            reason = ""
            if matched_rule:
                candidate = {
                    "text": matched_rule.get("button_text", ""),
                    "tag": matched_rule.get("button_tag", ""),
                    "type": matched_rule.get("button_type", ""),
                    "css_path": matched_rule.get("button_css_path", ""),
                }
                reason = f"matched learned rule {matched_rule.get('id')}"
            else:
                candidate, reason = choose_candidate(snapshot)
            snapshot["analysis"] = build_analysis(snapshot, matched_rule=matched_rule, candidate=candidate, reason=reason)
            save_debug(debug_dir, "before", snapshot, page.content(), page=page)
            if args.preview_only:
                suggested = f"; suggested {candidate.get('text')!r}" if candidate else ""
                print(f"Preview captured for {target['ssid'] or '-'} at {snapshot.get('url')}{suggested}")
                browser.close()
                return 0
            if candidate is None:
                print(reason, file=sys.stderr)
                return 1
            checked = maybe_accept_checkboxes(page, snapshot)
            if checked:
                snapshot = load_page_snapshot(page)
            if args.dry_run:
                print(f"Would click {candidate.get('text')!r} on {snapshot.get('url')} ({reason})")
                browser.close()
                return 0
            locator = locate_candidate(page, candidate)
            locator.scroll_into_view_if_needed(timeout=3000)
            locator.click(timeout=5000)
            page.wait_for_timeout(int(args.post_click_wait_sec * 1000))
            try:
                page.wait_for_load_state("networkidle", timeout=max(1000, int(args.post_click_wait_sec * 1000)))
            except Exception:
                pass
            after = load_page_snapshot(page)
            changed = after.get("url") != snapshot.get("url") or after["keys"].get("body_signature") != snapshot["keys"].get("body_signature")
            final_rule = learn_rule(matched_rule, ssid=target["ssid"], snapshot=snapshot, keys=keys, candidate=candidate, reason=reason)
            after["analysis"] = build_analysis(after, matched_rule=matched_rule, candidate=candidate, reason=f"after click: {reason}")
            save_debug(debug_dir, "after", after, page.content(), page=page)
            if not args.no_save:
                upsert_rule(rules, final_rule)
                save_rules(rules_path, rules)
            browser.close()
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1

    clicked_label = candidate.get("text", "")
    after_url = after.get("url", target["url"])
    suffix = "page changed" if changed else "click sent"
    checked_text = f"; checked {len(checked)} agreement box(es)" if checked else ""
    print(f"Clicked {clicked_label!r} on {target['ssid'] or '-'} -> {after_url} ({suffix}{checked_text})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
