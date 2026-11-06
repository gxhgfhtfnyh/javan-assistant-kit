# -*- coding: utf-8 -*-
"""Tests for watch_video.py. yt-dlp, ffmpeg and the speech engines are always faked: nothing is downloaded, decoded or installed."""
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import watch_video as wv  # noqa: E402


class Tools:
    """A stand-in for subprocess.run that behaves like yt-dlp, ffmpeg and whisper-cli by writing the files they would write."""

    def __init__(self, fail_on=None, frames=3):
        self.calls, self.fail_on, self.frames = [], fail_on, frames

    def __call__(self, argv, **kw):
        self.calls.append((list(argv), kw))
        exe = Path(str(argv[0])).name.lower()
        if self.fail_on and self.fail_on in exe:
            raise subprocess.CalledProcessError(2, argv, stderr=b"line one\nboom happened")
        if "yt-dlp" in exe or "yt_dlp" in argv:
            Path(argv[argv.index("-o") + 1].replace("%(ext)s", "mp4")).write_bytes(b"video")
        elif "ffmpeg" in exe:
            target = str(argv[-1])
            if target.endswith("audio.wav"):
                Path(target).write_bytes(b"wav")
            elif "frame_%04d.jpg" in target:
                for i in range(1, self.frames + 1):
                    Path(target.replace("%04d", "%04d" % i)).write_bytes(b"jpg")
        elif "whisper-cli" in exe:
            Path(argv[argv.index("-of") + 1] + ".srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n", encoding="utf-8")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")


class Seg:
    def __init__(self, s, e, t):
        self.start, self.end, self.text = s, e, t


class FakeModel:
    def transcribe(self, path, **kw):
        return iter([Seg(0, 2.5, " first "), Seg(3725.123, 3730, "second")]), object()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_SCRATCH", str(tmp_path / "scratch"))
    monkeypatch.delenv("WHISPER_CPP_CLI", raising=False)
    monkeypatch.delenv("WHISPER_CPP_MODEL", raising=False)
    monkeypatch.delenv("FFMPEG_PATH", raising=False)
    monkeypatch.setattr(wv, "find_ffmpeg", lambda: "ffmpeg")
    return tmp_path


@pytest.fixture
def clip(tmp_path):
    p = tmp_path / "My Clip (final).mp4"
    p.write_bytes(b"video")
    return p


# ---------------- pure helpers ----------------
@pytest.mark.parametrize("s,ok", [("https://example.com/v", True), ("http://example.com", True), ("ftp://example.com/x", False), ("file:///etc/passwd", False),
                                  ("clip.mp4", False), ("https://example.com/a b", False), ("https://example.com/a\nb", False), ("https://", False)])
def test_is_url(s, ok):
    assert wv.is_url(s) is ok


def test_source_slug_uses_the_video_id_and_ignores_query_parameters():
    a = wv.source_slug("https://www.youtube.com/watch?v=abcDEF12345&si=xyz")
    b = wv.source_slug("https://www.youtube.com/watch?v=abcDEF12345")
    assert a == b == "abcDEF12345"
    assert wv.source_slug("https://youtu.be/abcDEF12345?si=q") == "abcDEF12345"
    assert wv.source_slug("https://www.youtube.com/shorts/abcDEF12345") == "abcDEF12345"


def test_source_slug_hashes_other_urls_stably_and_distinctly():
    one, two = wv.source_slug("https://example.com/a"), wv.source_slug("https://example.com/b")
    assert one != two and len(one) == 12 and one == wv.source_slug("https://example.com/a")


@pytest.mark.parametrize("stem,expected", [("My Clip (final)", "My_Clip_final"), ("../../evil", "evil"), ("", "video"), ("..", "video"), ("a" * 100, "a" * 60), ("tab\there", "tab_here")])
def test_safe_name(stem, expected):
    assert wv.safe_name(stem) == expected


@pytest.mark.parametrize("t,expected", [(0, "00:00:00,000"), (2.5, "00:00:02,500"), (3725.123, "01:02:05,123"), (-1, "00:00:00,000"), (59.9996, "00:01:00,000")])
def test_srt_time(t, expected):
    assert wv.srt_time(t) == expected


# ---------------- finding tools ----------------
def test_find_ffmpeg_honours_ffmpeg_path_then_path(tmp_path, monkeypatch):
    monkeypatch.undo()
    fake = tmp_path / "myffmpeg"
    fake.write_text("x")
    monkeypatch.setenv("FFMPEG_PATH", str(fake))
    assert wv.find_ffmpeg() == str(fake)
    monkeypatch.setenv("FFMPEG_PATH", str(tmp_path / "nope"))
    monkeypatch.setattr(wv.shutil, "which", lambda n: "/opt/ffmpeg")
    assert wv.find_ffmpeg() == "/opt/ffmpeg"


def test_find_ytdlp_prefers_the_program_then_the_module_then_none(monkeypatch):
    monkeypatch.setattr(wv.shutil, "which", lambda n: "/usr/bin/yt-dlp")
    assert wv.find_ytdlp() == ["/usr/bin/yt-dlp"]
    monkeypatch.setattr(wv.shutil, "which", lambda n: None)
    monkeypatch.setattr(wv.importlib.util, "find_spec", lambda n: object())
    assert wv.find_ytdlp() == [sys.executable, "-m", "yt_dlp"]
    monkeypatch.setattr(wv.importlib.util, "find_spec", lambda n: None)
    assert wv.find_ytdlp() is None


def test_find_whisper_cpp_needs_both_variables_to_name_real_files(tmp_path, monkeypatch):
    cli, model = tmp_path / "whisper-cli", tmp_path / "ggml.bin"
    cli.write_text("x")
    monkeypatch.setenv("WHISPER_CPP_CLI", str(cli))
    assert wv.find_whisper_cpp() is None
    monkeypatch.setenv("WHISPER_CPP_MODEL", str(model))
    assert wv.find_whisper_cpp() is None                      # the model file does not exist yet
    model.write_text("m")
    assert wv.find_whisper_cpp() == (str(cli), str(model))


# ---------------- steps ----------------
def test_download_runs_ytdlp_as_a_list_with_safe_options(tmp_path):
    t = Tools()
    path = wv.download_video("https://example.com/v", tmp_path, ytdlp=["yt-dlp"], runner=t)
    argv, kw = t.calls[0]
    assert path == tmp_path / "source.mp4"
    assert argv[0] == "yt-dlp" and argv[-1] == "https://example.com/v"
    for flag in ("--no-playlist", "--force-overwrites", "--max-filesize"):
        assert flag in argv
    assert "--remote-components" not in argv                  # downloading the solver is opt-in
    assert "shell" not in kw and kw["check"] is True


def test_js_solver_flag_adds_node_options_only_when_node_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(wv.shutil, "which", lambda n: "/usr/bin/node")
    t = Tools()
    wv.download_video("https://example.com/v", tmp_path, ytdlp=["yt-dlp"], js_solver=True, runner=t)
    argv = t.calls[0][0]
    assert argv[argv.index("--js-runtimes") + 1] == "node" and "ejs:github" in argv
    monkeypatch.setattr(wv.shutil, "which", lambda n: None)
    with pytest.raises(wv.MissingDependency, match="node"):
        wv.download_video("https://example.com/v", tmp_path, ytdlp=["yt-dlp"], js_solver=True, runner=Tools())


def test_download_without_ytdlp_says_what_to_install(tmp_path, monkeypatch):
    monkeypatch.setattr(wv, "find_ytdlp", lambda: None)
    with pytest.raises(wv.MissingDependency, match="pip install yt-dlp"):
        wv.download_video("https://example.com/v", tmp_path, runner=Tools())


def test_download_with_no_file_produced_is_an_error(tmp_path):
    with pytest.raises(RuntimeError, match="no source"):
        wv.download_video("https://example.com/v", tmp_path, ytdlp=["yt-dlp"], runner=lambda a, **k: None)


def test_download_ignores_partial_files_and_takes_the_newest(tmp_path):
    (tmp_path / "source.mp4.part").write_text("x")
    old, new = tmp_path / "source.webm", tmp_path / "source.mp4"
    old.write_text("o")
    new.write_text("n")
    os.utime(old, (1, 1))
    assert wv.download_video("https://example.com/v", tmp_path, ytdlp=["x"], runner=lambda a, **k: None) == new


def test_extract_audio_and_frames_commands(tmp_path, clip):
    t = Tools(frames=4)
    audio = wv.extract_audio(clip, tmp_path, "ffmpeg", t)
    assert audio == tmp_path / "audio.wav" and "-ar" in t.calls[0][0] and Path(t.calls[0][0][t.calls[0][0].index("-i") + 1]).is_absolute()
    frames = wv.extract_frames(clip, tmp_path, 2.5, "ffmpeg", max_frames=50, runner=t)
    argv = t.calls[1][0]
    assert [f.name for f in frames] == ["frame_0001.jpg", "frame_0002.jpg", "frame_0003.jpg", "frame_0004.jpg"]
    assert "fps=1/2.5" in argv and argv[argv.index("-frames:v") + 1] == "50"


def test_no_frames_extracted_is_an_error(tmp_path, clip):
    with pytest.raises(RuntimeError, match="no frames"):
        wv.extract_frames(clip, tmp_path, 5, "ffmpeg", runner=Tools(frames=0))


def test_faster_whisper_writes_an_srt_with_stamps(tmp_path):
    srt = wv.transcribe_faster_whisper(tmp_path / "a.wav", tmp_path, "small", lambda size: FakeModel())
    assert srt.read_text(encoding="utf-8") == "1\n00:00:00,000 --> 00:00:02,500\nfirst\n\n2\n01:02:05,123 --> 01:02:10,000\nsecond\n\n"


def test_missing_speech_engine_names_both_options(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    with pytest.raises(wv.MissingDependency) as e:
        wv.transcribe(tmp_path / "a.wav", tmp_path)
    assert "pip install faster-whisper" in str(e.value) and "WHISPER_CPP_CLI" in str(e.value)


def test_whisper_cpp_is_used_when_configured(tmp_path, monkeypatch):
    cli, model = tmp_path / "whisper-cli", tmp_path / "m.bin"
    cli.write_text("x")
    model.write_text("m")
    monkeypatch.setenv("WHISPER_CPP_CLI", str(cli))
    monkeypatch.setenv("WHISPER_CPP_MODEL", str(model))
    t = Tools()
    srt = wv.transcribe(tmp_path / "a.wav", tmp_path, runner=t)
    assert srt == tmp_path / "transcript.srt" and srt.exists()
    assert t.calls[0][0][:3] == [str(cli), "-m", str(model)]


def test_whisper_cpp_that_makes_no_srt_is_an_error(tmp_path):
    with pytest.raises(RuntimeError, match="did not produce"):
        wv.transcribe_whisper_cpp(tmp_path / "a.wav", tmp_path, "whisper-cli", "m.bin", runner=lambda a, **k: None)


# ---------------- command line ----------------
def test_main_local_file_end_to_end(env, clip, capsys):
    assert wv.main([str(clip), "--interval", "10"], runner=Tools(), model_factory=lambda s: FakeModel()) == 0
    out = capsys.readouterr().out
    assert "READY" in out and "untrusted" in out and "Frames: 3 images" in out
    folder = env / "scratch" / "watch_video" / "My_Clip_final"
    assert (folder / "transcript.srt").exists() and (folder / "frames" / "frame_0001.jpg").exists()


def test_main_url_downloads_into_a_folder_named_by_the_video_id(env, monkeypatch, capsys):
    monkeypatch.setattr(wv, "find_ytdlp", lambda: ["yt-dlp"])
    assert wv.main(["https://www.youtube.com/watch?v=abcDEF12345&si=1"], runner=Tools(), model_factory=lambda s: FakeModel()) == 0
    assert (env / "scratch" / "watch_video" / "abcDEF12345" / "source.mp4").exists()


def test_main_respects_out_and_max_frames(env, clip):
    t = Tools()
    assert wv.main([str(clip), "--out", str(env / "mine"), "--max-frames", "7"], runner=t, model_factory=lambda s: FakeModel()) == 0
    assert (env / "mine" / "audio.wav").exists()
    frames_call = [c for c in t.calls if "-frames:v" in c[0]][0][0]
    assert frames_call[frames_call.index("-frames:v") + 1] == "7"


@pytest.mark.parametrize("args", [["--interval", "0"], ["--interval", "99999"], ["--max-frames", "0"], ["--max-frames", "100000"]])
def test_main_rejects_silly_numbers(env, clip, args, capsys):
    assert wv.main([str(clip)] + args, runner=Tools()) == 1
    assert "ERROR" in capsys.readouterr().out


def test_main_missing_local_file(env, tmp_path, capsys):
    assert wv.main([str(tmp_path / "nope.mp4")], runner=Tools()) == 1
    assert "file not found" in capsys.readouterr().out


def test_main_without_ffmpeg_says_install_it_and_exits_3(env, clip, monkeypatch, capsys):
    monkeypatch.setattr(wv, "find_ffmpeg", lambda: None)
    assert wv.main([str(clip)], runner=Tools()) == 3
    assert "MISSING: ffmpeg is not installed" in capsys.readouterr().out
    assert not (env / "scratch").exists()                      # nothing is created before the tools are known to exist


def test_main_without_ytdlp_for_a_url_exits_3(env, monkeypatch, capsys):
    monkeypatch.setattr(wv, "find_ytdlp", lambda: None)
    assert wv.main(["https://example.com/v"], runner=Tools()) == 3
    assert "pip install yt-dlp" in capsys.readouterr().out


def test_main_a_failing_tool_is_one_clear_line_and_exit_4(env, clip, capsys):
    assert wv.main([str(clip)], runner=Tools(fail_on="ffmpeg"), model_factory=lambda s: FakeModel()) == 4
    out = capsys.readouterr().out
    assert "FAILED: ffmpeg exited with code 2 boom happened" in out


def test_main_a_timeout_is_reported_not_raised(env, clip, capsys):
    def slow(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 1)
    assert wv.main([str(clip)], runner=slow) == 4
    assert "FAILED: TimeoutExpired" in capsys.readouterr().out


def test_nothing_is_ever_installed(env, clip):
    t = Tools()
    wv.main([str(clip)], runner=t, model_factory=lambda s: FakeModel())
    for argv, _ in t.calls:
        assert "pip" not in argv and "install" not in argv


def test_real_process_help_and_bad_input_need_no_tools(tmp_path):
    e = dict(os.environ, HARNESS_ROOT=str(tmp_path), HARNESS_LOGS=str(tmp_path / "logs"), HARNESS_VAULT=str(tmp_path / "vault"),
             HARNESS_SCRATCH=str(tmp_path / "scratch"), PYTHONUTF8="1")
    h = subprocess.run([sys.executable, "-B", str(HERE / "watch_video.py"), "--help"], capture_output=True, text=True, env=e, timeout=30, encoding="utf-8")
    assert h.returncode == 0 and "--js-solver" in h.stdout
    m = subprocess.run([sys.executable, "-B", str(HERE / "watch_video.py"), str(tmp_path / "none.mp4")], capture_output=True, text=True, env=e, timeout=30, encoding="utf-8")
    assert m.returncode == 1 and "file not found" in m.stdout
