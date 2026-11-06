# -*- coding: utf-8 -*-
"""Tests for leak_scan.py. Every test plants one leak and expects exactly that finding;
the clean-text tests make sure placeholders and ordinary code do not raise false alarms."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import leak_scan as ls  # noqa: E402


def kinds(text, deny=()):
    return [(k, h) for _n, k, h in ls.scan_text(text, deny)]


# ---------- generic patterns ----------
def test_a_real_email_is_found_but_example_and_noreply_are_not():
    assert kinds("write to someone@gmail.com now") == [("email", "someone@gmail.com")]
    assert kinds("a@example.com b@example.org c@example.net d@users.noreply.github.com no-reply@x.io") == []


def test_chat_ids_of_17_to_20_digits_are_found_but_shorter_numbers_and_decimals_are_not():
    assert kinds("owner id 123456789012345678 here") == [("chat_id", "123456789012345678")]
    assert kinds("ts 1793381384 and 1565780786219 and version 3.14159265358979323") == []


def test_windows_user_paths_are_found_but_placeholders_are_not():
    assert kinds(r"open C:\Users\someone\Documents\x") == [("user_path", r"C:\Users\someone")]
    assert kinds(r"C:\Users\<you>\x and C:\Users\%USERNAME%\y and C:\Users\Public\z") == []
    assert [k for k, _h in kinds("D:/Users/other/file")] == ["user_path"]


def test_private_ip_addresses_are_found_but_localhost_and_public_docs_ranges_are_not():
    assert [h for _k, h in kinds("router 192.168.1.1 and 10.0.0.7 and 172.20.4.5")] == ["192.168.1.1", "10.0.0.7", "172.20.4.5"]
    assert kinds("127.0.0.1:20128 and 0.0.0.0 and 192.0.2.4 and 8.8.8.8") == []


def test_mac_addresses_are_found():
    assert kinds("phone aa:bb:cc:dd:ee:ff") == [("mac_address", "aa:bb:cc:dd:ee:ff")]
    assert kinds("sha 0123456789abcdef0123456789abcdef") == []


def test_secret_assignments_and_key_formats_are_found_and_short_values_are_not():
    assert [k for k, _h in kinds('api_key = "abcdefghijklmnop1234"')] == ["secret_assignment"]
    assert [k for k, _h in kinds("password: 'correct-horse-battery-staple'")] == ["secret_assignment"]
    assert [k for k, _h in kinds("key sk-abcdefghijklmnopqrstuvwx")] == ["key_format"]
    assert [k for k, _h in kinds("AKIAABCDEFGHIJKLMNOP")] == ["key_format"]
    assert kinds('password = "short"') == []
    assert kinds("password = os.environ['X']") == []


def test_line_numbers_are_one_based_and_a_clean_text_has_no_findings():
    found = ls.scan_text("clean\nclean\nmail me@corp.io\n")
    assert [(n, k) for n, k, _h in found] == [(3, "email")]
    assert ls.scan_text("") == [] and ls.scan_text(None) == []
    assert ls.scan_text("def add(a, b):\n    return a + b\n") == []


# ---------- the denylist ----------
def test_denylist_syntax_plain_word_case_sensitive_and_regex():
    deny = ls.parse_denylist("# comment\n\nsecretword\nw:Zebra\ncs:Quokka\nre:\\bAB\\d{4}\\b\n")
    assert [label for label, _rx in deny] == ["secretword", "w:Zebra", "cs:Quokka", "re:\\bAB\\d{4}\\b"]
    assert kinds("xx SECRETWORD yy", deny) == [("denylist:secretword", "SECRETWORD")]
    assert kinds("the Zebra machine", deny) == [("denylist:w:Zebra", "Zebra")]
    assert kinds("finite unite", deny) == []                       # whole word only
    assert kinds("call me Quokka", deny) == [("denylist:cs:Quokka", "Quokka")]
    assert kinds("a quokka of thread", deny) == []                   # case-sensitive
    assert kinds("quote AB1234 sent", deny) == [("denylist:re:\\bAB\\d{4}\\b", "AB1234")]


def test_a_bad_regex_line_is_reported_with_its_line_number():
    with pytest.raises(ValueError, match="line 2"):
        ls.parse_denylist("fine\nre:(unclosed\n")


def test_denylist_ignores_blank_and_comment_lines_and_handles_none():
    assert ls.parse_denylist("\n   \n# only a comment\n") == []
    assert ls.parse_denylist(None) == []


# ---------- walking a folder ----------
def make_tree(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "ok.py").write_text("print('hello')\n", encoding="utf-8")
    (tmp_path / "scripts" / "leak.py").write_text("x = 1\nowner = 'me@corp.io'\n", encoding="utf-8")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "junk.py").write_text("me@corp.io", encoding="utf-8")
    (tmp_path / "pic.png").write_bytes(b"\x89PNG me@corp.io")
    (tmp_path / "blob.dat").write_bytes(b"\x00\x01 me@corp.io")
    return tmp_path


def test_tree_scan_finds_the_leak_with_its_path_and_line_and_skips_caches_images_and_binaries(tmp_path):
    found = ls.scan_tree(str(make_tree(tmp_path)))
    assert found == [(os.path.join("scripts", "leak.py"), 2, "email", "me@corp.io")]


def test_a_personal_name_in_a_file_name_is_found(tmp_path):
    (tmp_path / "notes for secretword.md").write_text("fine", encoding="utf-8")
    found = ls.scan_tree(str(tmp_path), ls.parse_denylist("secretword"))
    assert len(found) == 1 and found[0][2].startswith("denylist:secretword") and "(in the file name)" in found[0][2]


def test_unreadable_big_and_non_utf8_files_do_not_crash_the_scan(tmp_path, monkeypatch):
    (tmp_path / "latin.txt").write_bytes("caf\xe9 me@corp.io".encode("latin-1"))
    big = tmp_path / "big.txt"
    big.write_text("x", encoding="utf-8")
    monkeypatch.setattr(ls, "MAX_BYTES", 0)
    found = ls.scan_tree(str(tmp_path))
    assert {f[2] for f in found} == {"skipped"}
    monkeypatch.setattr(ls, "MAX_BYTES", 1024)
    assert [(f[0], f[2]) for f in ls.scan_tree(str(tmp_path))] == [("latin.txt", "email")]


def test_ignored_files_are_skipped_by_relative_path_and_everything_else_is_still_scanned(tmp_path):
    make_tree(tmp_path)
    leak = os.path.join("scripts", "leak.py")
    assert ls.scan_tree(str(tmp_path), (), ["scripts/leak.py"]) == []
    assert ls.scan_tree(str(tmp_path), (), ["./scripts\\leak.py"]) == []           # slashes and a leading ./ are normalised
    (tmp_path / "scripts" / "other.py").write_text("x = 'you@corp.io'\n", encoding="utf-8")
    assert [f[0] for f in ls.scan_tree(str(tmp_path), (), ["scripts/leak.py"])] == [os.path.join("scripts", "other.py")]
    assert [f[0] for f in ls.scan_tree(str(tmp_path))] == [leak, os.path.join("scripts", "other.py")]


# ---------- the command line ----------
def test_cli_exit_codes_and_report_do_not_echo_the_whole_line(tmp_path, capsys):
    make_tree(tmp_path)
    assert ls.main([str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "leak.py:2: email: me@corp.io" in out and "owner =" not in out and out.strip().endswith("(0 denylist term(s))")
    clean = tmp_path / "clean"
    clean.mkdir()
    (clean / "a.py").write_text("print(1)\n", encoding="utf-8")
    assert ls.main([str(clean)]) == 0


def test_cli_denylist_file_and_error_paths(tmp_path, capsys):
    (tmp_path / "a.txt").write_text("this mentions Zebra\n", encoding="utf-8")
    deny = tmp_path.parent / (tmp_path.name + "_deny.txt")
    deny.write_text("zebra\n", encoding="utf-8")
    assert ls.main([str(tmp_path), "--denylist", str(deny)]) == 1
    assert "denylist:zebra: Zebra" in capsys.readouterr().out
    assert ls.main([str(tmp_path / "missing")]) == 2
    assert ls.main([str(tmp_path), "--denylist", str(tmp_path / "nope.txt")]) == 2
    bad = tmp_path.parent / (tmp_path.name + "_bad.txt")
    bad.write_text("re:(oops\n", encoding="utf-8")
    assert ls.main([str(tmp_path), "--denylist", str(bad)]) == 2


def test_cli_ignore_option_skips_the_named_file_and_says_so(tmp_path, capsys):
    make_tree(tmp_path)
    assert ls.main([str(tmp_path), "--ignore", "scripts/leak.py"]) == 0
    out = capsys.readouterr().out
    assert "0 finding(s)" in out and "ignoring scripts/leak.py" in out
