"""项目规则文件测试：AGENTS.md 与 CLAUDE.md 同源，不维护两份副本。"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_CLAUDE_是指向AGENTS的软链接() -> None:
    claude = ROOT / "CLAUDE.md"
    assert claude.is_symlink(), "CLAUDE.md 必须是软链接（ln -s AGENTS.md CLAUDE.md）"
    assert claude.readlink() == Path("AGENTS.md")
    assert claude.read_text(encoding="utf-8") == (ROOT / "AGENTS.md").read_text(encoding="utf-8")


def test_AGENTS_记录独立性与验证约束() -> None:
    text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "独立" in text and "复读插件" in text, "必须写明与复读插件的独立性约束"
    assert "uv run --no-project --with pytest pytest tests/" in text
    assert "uv run --no-project --with pyright pyright" in text
    assert "config_version" in text, "必须写明宿主强制的 plugin.config_version"
