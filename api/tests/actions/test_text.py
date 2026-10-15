from ic.actions.text import ZWSP, code_span, inert, inert_discord


def test_mentions_neutralized():
    out = inert("ping @everyone and @bob", 200)
    assert "@everyone" not in out and "@bob" not in out
    assert f"@{ZWSP}everyone" in out


def test_links_images_and_backticks_escaped():
    out = inert("[x](http://evil) ![i](http://e/p.png) `rm -rf`", 200)
    assert "[x](" not in out and "![" not in out
    assert "\\[x\\]\\(" in out and "\\`rm" in out
    assert "`" not in out.replace("\\`", "")


def test_control_chars_stripped():
    out = inert("a\x00b\x1b[31m\nc‮d​e", 200)
    assert "\x00" not in out and "\x1b" not in out and "\n" not in out
    assert "‮" not in out and "​" not in out


def test_truncation_with_ellipsis():
    out = inert("[" * 50, 11)
    assert len(out) <= 11 and out.endswith("…")
    assert out[:-1] == "\\[" * 5  # whole escape pairs only, never a dangling backslash
    assert len(inert("x" * 10, 10)) == 10
    assert inert("anything", 0) == ""


def test_code_span_cannot_be_closed_from_inside():
    s = code_span("a ``` b ` c")
    fence = s[: len(s) - len(s.lstrip("`"))]
    inner = s[len(fence) : -len(fence)]
    assert s.endswith(fence) and len(fence) == 4
    assert fence not in inner
    assert code_span("`x`") == "`` `x` ``"


def test_discord_text_defangs_bare_urls():
    out = inert_discord("see https://evil.example/x and www.evil.example and HTTP://a.b", 200)
    assert "://" not in out and "www." not in out
    assert f":{ZWSP}//evil.example" in out and f"www{ZWSP}.evil" in out


def test_discord_text_respects_max_len():
    out = inert_discord("https://a.b/" * 100, 50)
    assert len(out) <= 50


def test_github_inert_leaves_urls_alone():
    assert "https://example.com" in inert("https://example.com", 100)
