"""Douyin collection layer for 评论商机雷达.

The web UI and the CLI both talk to `collector.service`, which owns one long
-lived browser. Everything in `collector.douyin` that can be expressed as a
pure function is kept free of Playwright so it can be tested without a browser.
"""

from __future__ import annotations

__all__ = ["douyin", "service"]
