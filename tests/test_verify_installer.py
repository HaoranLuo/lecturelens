# -*- coding: utf-8 -*-
"""tests/test_verify_installer.py — D4 安装包的**静态契约**与 verify_installer 安全边界。

两块价值，各测各的：

① `installer/lecturelens.iss` 的**静态契约**（秒级、离线）：D4 的几条硬要求必须钉在
   源码里，而不是靠"跑一遍看起来没事"——尤其是「卸载不得删用户数据」这条，
   一旦有人手滑加了 `[UninstallDelete]` 目录级通配，整个学期的笔记就没了。
   所以本文件断言的是**去掉注释之后**的有效脚本内容：注释里解释"为什么不删"
   是对的，但注释绝不能替脚本满足断言（故 `_iss_code()` 先剥 `;` 注释）。

② `scripts/verify_installer.py` 的**路径闸门**（纯函数）：它是唯一会真装真卸的脚本，
   一旦闸门失效就可能拿用户真实的 `%LocalAppData%\\LectureLens` 做实验。
   `checked_install_dir` 的双重设防（临时目录之下 + 显式排除真实安装位置）逐条钉死。

真装真卸的那一遍（分钟级）是 D4 门禁本身，跑 `scripts/verify_installer.py`，不进单测。
"""
import ast
import re
import sys
import tempfile
from pathlib import Path

import pytest

import verify_installer as vi

ROOT = Path(__file__).resolve().parent.parent
ISS = ROOT / "installer" / "lecturelens.iss"
GUIDE = ROOT / "installer" / "新手指引.txt"


def _iss_raw() -> str:
    return ISS.read_text(encoding="utf-8")


def _iss_code() -> str:
    """剥掉 `;` 行注释后的有效脚本内容。

    必须剥离：本 .iss 里有一段**解释「[UninstallDelete] 为什么故意缺席」**的注释，
    里面自然会出现 transcripts/logs/glossary 这些词。不剥注释的话，
    「卸载不删数据」的断言会被自己的说明文字喂饱，等于没测。
    """
    out = [ln for ln in _iss_raw().splitlines() if not ln.strip().startswith(";")]
    return "\n".join(out)


def _section(code: str, name: str) -> str:
    """取某个 [Section] 的正文（到下一个 [Section] 或文件末尾止）。"""
    m = re.search(rf"^\[{name}\](.*?)(?=^\[|\Z)", code, re.S | re.M)
    return m.group(1) if m else ""


# —— ① installer/lecturelens.iss 静态契约 ————————————————————————————

def test_iss_installs_per_user_without_admin():
    """D2：每用户安装、免管理员、默认落到 %LocalAppData%\\LectureLens。"""
    code = _iss_code()
    assert re.search(r"^PrivilegesRequired=lowest$", code, re.M)
    assert re.search(r"^DefaultDirName=\{localappdata\}\\LectureLens$", code, re.M)
    # 不支持 Program Files 全机安装（权限坑，D2 明确）
    assert "Program Files" not in code


def test_iss_packages_whole_dist_plus_guide():
    """D4①：整个 dist/LectureLens 都要进包，随包还要有「新手指引.txt」。"""
    code = _iss_code()
    files = _section(code, "Files")
    assert "{#SourceRoot}\\*" in files, "必须整目录收 dist/LectureLens"
    assert "recursesubdirs" in files, "子目录（_internal/、models/）要跟着进去"
    assert "新手指引.txt" in files


def test_iss_uninstall_does_not_delete_user_data():
    """D2/D4 的核心：卸载**不得**删除 transcripts/logs/config.json/glossary。

    这是本套测试里最要紧的一条。用户一学期的笔记就在 transcripts\\ 里，
    误删不可逆。
    """
    code = _iss_code()
    section = _section(code, "UninstallDelete")
    for data in ("transcripts", "logs", "glossary", "config.json"):
        assert data not in section, f"[UninstallDelete] 不得触及 {data}"
    # 更狠的一条：不得对整个 {app} 做目录级通配（会连笔记一起抹掉）
    assert not re.search(r'Name:\s*"\{app\}\\?"', section), \
        "不得对整个 {app} 做目录级删除"


def test_iss_uninstall_keeps_data_intent_is_documented():
    """「不删数据」是**刻意设计**，必须在脚本里写明，免得后人当遗漏补上。

    这条断言注释原文（与上一条相反：这里要的就是注释存在）。
    """
    raw = _iss_raw()
    assert "[UninstallDelete]" in raw
    assert "故意" in raw
    # 卸载文案要明确告诉用户"笔记已保留"
    code = _iss_code()
    msgs = _section(code, "Messages")
    assert "ConfirmUninstall=" in msgs and "保留" in msgs
    assert "UninstalledAll=" in msgs and "保留" in msgs


def test_iss_desktop_icon_is_optional_task():
    """D4①：开始菜单项 + **可选**桌面快捷方式（Tasks 默认可取消）。"""
    code = _iss_code()
    tasks = _section(code, "Tasks")
    assert re.search(r'Name:\s*"desktopicon"', tasks)
    assert "unchecked" in tasks, "桌面快捷方式必须默认不勾（可选）"
    icons = _section(code, "Icons")
    assert "{group}" in icons, "要有开始菜单项"
    assert "{autodesktop}" in icons and "Tasks: desktopicon" in icons


def test_iss_output_name_matches_version_scheme():
    """产物名 = LectureLens-<版本>-setup.exe，且版本取自 exe 版本资源（单一来源）。"""
    code = _iss_code()
    assert re.search(r"^OutputBaseFilename=LectureLens-\{#MyAppVersion\}-setup$", code, re.M)
    assert re.search(r"^OutputDir=\.\.\\dist\\installer$", code, re.M)
    # 版本必须来自 exe 版本资源，不得在脚本里另写一份常量
    assert "GetVersionNumbersString(" in code
    assert not re.search(r'#define\s+MyAppVersion\s+"', code), \
        "版本号不得硬编码，必须从 exe 版本资源推导"


def test_iss_version_trims_windows_four_part_to_semver():
    """Windows 固定版本信息恒四段（1.0.0.0），文件名口径要三段（1.0.0）。

    这条逻辑是 ISPP 字符串裁剪，很容易写错（写错就会产出
    LectureLens-1.0.0.0-setup.exe，与 D4 约定的产物名不符）——故静态钉住：
    必须带「末段为 0 才裁」的守卫。
    """
    code = _iss_code()
    assert 'Copy(Ver4, Len(Ver4) - 1, 2) == ".0"' in code, "缺少末段为 0 的判断"
    assert "Copy(Ver4, 1, Len(Ver4) - 2)" in code, "缺少裁剪动作"


def test_iss_silent_install_surface_is_sane():
    """静默安装要能落到指定目录（verify_installer 靠 /DIR），且不允许降权选择。"""
    code = _iss_code()
    assert "PrivilegesRequiredOverridesAllowed" not in code, "不得放开权限选择（D2 固定每用户）"
    assert re.search(r"^Compression=\S+", code, re.M)


def test_guide_exists_utf8_without_bom_and_covers_four_points():
    """D4①：随包「新手指引.txt」必须覆盖四个要点。

    编码：UTF-8 **不带 BOM** —— 规则③要求新文件为 UTF-8，且本仓库一律以
    `encoding="utf-8"` 读文件（scripts/*.py 多处），带 BOM 会往内容里塞 \ufeff
    破坏断言。Inno Setup 6.7.3 实测能正确解析无 BOM 的 UTF-8 脚本与中文文件名
    （verify_installer.py 会断言装出来的 `新手指引.txt` 名字逐字正确）。
    """
    raw = GUIDE.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), "新手指引.txt 不得带 BOM（见 docstring）"
    t = raw.decode("utf-8")                      # 解不开即失败

    assert "仍要运行" in t and "更多信息" in t       # ① SmartScreen
    assert "防火墙" in t and "允许" in t             # ② 防火墙首次弹窗
    assert "向导" in t                             # ③ 首启三步向导
    assert "麦克风" in t and "DeepSeek" in t        #   三步的内容
    assert "跳过" in t                             #   key 可跳过（与 UI 一致）
    assert "扫码" in t or "二维码" in t              # ④ 手机同屏扫码（一句话）


def test_guide_does_not_promise_signing():
    """指引必须诚实说明"没有数字签名"，不能含糊成"可能会有提示"。"""
    t = GUIDE.read_text(encoding="utf-8")
    assert "签名" in t
    assert "病毒" in t, "要明确否认病毒，否则朋友不敢运行"


# —— ② scripts/verify_installer.py 路径闸门 ————————————————————————

def test_checked_tmp_path_confines_to_system_temp():
    inside = Path(tempfile.mkdtemp(prefix="vi-"))
    try:
        assert vi.checked_tmp_path(inside) == inside.resolve()
        assert vi.checked_tmp_path(inside / "installdir") == (inside / "installdir").resolve()
        for bad in (ROOT, ROOT / "dist", Path(tempfile.gettempdir()).resolve().parent,
                    ROOT / "installer" / "lecturelens.iss"):
            with pytest.raises(SystemExit):
                vi.checked_tmp_path(bad)
    finally:
        inside.rmdir()


def test_checked_installer_path_confines_to_dist_installer():
    """安装包只从 dist/installer 取（不搜全盘、不接受任意路径）。"""
    ok = vi.INSTALLER_DIR / "LectureLens-1.0.0-setup.exe"
    assert vi.checked_installer_path(ok) == ok.resolve()
    for bad in (ROOT / "dist" / "LectureLens" / "LectureLens.exe",
                ROOT / "config.json", ROOT, Path("C:/Windows/System32/cmd.exe")):
        with pytest.raises(SystemExit):
            vi.checked_installer_path(bad)


def test_checked_install_dir_refuses_the_real_user_install_location(monkeypatch, tmp_path):
    """**最关键的一道闸**：绝不允许拿用户真实的 %LocalAppData%\\LectureLens 做实验。

    D2 的默认安装位置就是这个目录。若这道理失效，一次参数写错就会把用户
    （或朋友）真实的笔记目录当试验田卸载掉。
    """
    fake_local = tmp_path / "LocalAppData"
    fake_local.mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(fake_local))

    real = fake_local / "LectureLens"
    for bad in (real, real / "sub"):            # 真实位置及其子路径全拒
        with pytest.raises(SystemExit):
            vi.checked_install_dir(bad)

    # 临时目录之下的自建子目录放行
    inside = Path(tempfile.mkdtemp(prefix="vi-ok-"))
    try:
        assert vi.checked_install_dir(inside) == inside.resolve()
    finally:
        inside.rmdir()


def test_checked_install_dir_still_blocks_non_temp(monkeypatch, tmp_path):
    """即使 LOCALAPPDATA 换成别处，临时目录之外依旧不放行。"""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "LA"))
    with pytest.raises(SystemExit):
        vi.checked_install_dir(ROOT / "dist" / "lecturelens-verify")


def test_probes_intact_detects_missing_and_modified(tmp_path):
    """卸载后「探针仍在」的判据必须能真的报出丢失/被改，不能恒真。

    多探针口径（评审②）：write_probes 返回 {相对路径: 内容}，probes_intact 收这一份。
    """
    d = tmp_path / "app"
    (d / "transcripts").mkdir(parents=True)
    want = vi.write_probes(d)

    ok, why = vi.probes_intact(d, want)
    assert ok and why == ""

    # 内容被改动（例如重装把 config.json 覆盖成默认值）→ 必须判失败
    (d / vi.PROBE_CONFIG).write_text("{}", encoding="utf-8")
    ok, why = vi.probes_intact(d, want)
    assert not ok and vi.PROBE_CONFIG in why

    # 文件被删（数据没保住）→ 必须判失败
    vi.write_probes(d)
    (d / vi.PROBE_NOTE).unlink()
    ok, why = vi.probes_intact(d, want)
    assert not ok and vi.PROBE_NOTE in why

    # 逐类探针单独也能判红：glossary（术语表）与 logs（日志）原先**无人验**（评审②）
    for rel in (vi.PROBE_GLOSSARY, vi.PROBE_LOG, vi.PROBE_NOTE, vi.PROBE_CONFIG):
        vi.write_probes(d)
        (d / rel).unlink()
        ok, why = vi.probes_intact(d, want)
        assert not ok and rel in why, f"{rel} 缺失必须判失败"


def test_probe_payload_carries_a_marker():
    """探针内容要有可辨识标记，便于人工核对现场。"""
    d = Path(tempfile.mkdtemp(prefix="vi-probe-"))
    try:
        vi.write_probes(d)
        for rel in vi.PROBES:
            assert vi.PROBE_MARKER in (d / rel).read_text(encoding="utf-8"), rel
        # 笔记探针要落在 transcripts/ 下（与运行期笔记同目录，才验得有意义）
        assert vi.PROBE_NOTE.startswith("transcripts/")
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def test_probe_set_covers_all_four_promises_and_uses_real_note_naming():
    """评审②：探针面必须**等于** .iss 对用户承诺保留的那四类，不得更窄。

    .iss 的 ConfirmUninstall 原话是「笔记（transcripts）、术语表（glossary）、
    日志（logs）与设置（config.json）都会被保留」——旧探针只验了其中两项，
    glossary 与 logs 无人验，而 glossary 是用户自己攒的、不可再生。
    """
    promised = ("transcripts", "glossary", "logs", "config.json")
    covered = " ".join(vi.PROBES)
    for kind in promised:
        assert kind in covered, f"探针未覆盖 .iss 承诺保留的 {kind}"
    # .iss 的承诺不能是空口：文案本身也要真的写着这四项
    code = _iss_code()
    m = re.search(r"^ConfirmUninstall=(.*)$", code, re.M)
    assert m, "ConfirmUninstall 覆盖文案不见了"
    for kind in ("transcripts", "glossary", "logs", "config.json"):
        assert kind in m.group(1), f"ConfirmUninstall 未写明保留 {kind}"


def test_probe_note_uses_the_real_note_naming_scheme():
    """评审②：探针名必须落在**真实笔记的命名空间**里，否则没有判别力。

    真实运行期笔记是 `transcripts/lecture_<stamp>.md`（见 app/main.py 的笔记落盘处）。
    若探针叫「探针笔记.md」这种真实命名法里不存在的名字，一条
    `[UninstallDelete] transcripts\\lecture_*.md` 式通配就能删光真实笔记而探针完好
    ——这正是本探针要挡的失效模式，所以它必须匹配 `lecture_*.md`。
    """
    note = vi.PROBE_NOTE
    assert note.startswith("transcripts/"), note
    assert re.fullmatch(r"transcripts/lecture_[0-9]{8}_probe\.md", note), \
        f"笔记探针必须形如 transcripts/lecture_<stamp>.md（收到 {note}）"


def test_required_installed_is_derived_from_build_exe():
    """评审②：装机清单**不许另抄一份**——两份清单迟早漂移。

    旧实现是本文件同级的一段 4 项字面量（且只含 index.html），弱于构建侧的 7 项：
    float.html / privacy.html / B13 术语库包都漏在装机断言之外——而这几个新文件
    恰恰是「新版真装上了没有」的判据。
    """
    import build_exe
    derived = build_exe.required_dist_files(build_exe.BUNDLE_MODEL)
    assert vi.REQUIRED_INSTALLED[:len(derived)] == derived, \
        "REQUIRED_INSTALLED 必须以 build_exe.required_dist_files 为前缀（同源）"
    # 构建侧 7 项之外，装机侧还多一项：新手指引.txt（由 .iss 从 installer/ 挂上去，
    # 不在 dist/ 里，故构建清单不含它）
    assert vi.REQUIRED_INSTALLED[len(derived):] == (vi.GUIDE_NAME,)
    for rel in ("_internal/web/float.html", "_internal/web/privacy.html",
                "_internal/glossary_packs/数学.md"):
        assert rel in vi.REQUIRED_INSTALLED, f"{rel} 必须进装机断言"


def test_version_matches_compares_via_build_exe_quad():
    """评审②：跨版本升级必须能断言「新版真装上了」——比较走 build_exe.version_quad。

    Windows 的 exe FileVersion 是四段（'1.1.0.0'），安装包名是三段（'1.1.0'），
    两边都经同一个解析器补零后比较；解析不了的一律判不等（不猜、不放过）。
    """
    assert vi.version_matches("1.1.0.0", "1.1.0")
    assert vi.version_matches("1.1.0", "1.1.0.0")
    assert not vi.version_matches("1.0.0.0", "1.1.0")
    assert not vi.version_matches("", "1.1.0")
    assert not vi.version_matches("banana", "1.1.0")


def test_missing_installed_reports_each_new_file(tmp_path):
    """覆盖后「关键产物齐全」必须能逐项报缺（含 1.1.0 的随包新文件）。"""
    d = tmp_path / "app"
    d.mkdir()
    missing = vi.missing_installed(d)
    assert set(missing) == set(vi.REQUIRED_INSTALLED)
    (d / "_internal" / "web").mkdir(parents=True)
    (d / "_internal" / "web" / "float.html").write_text("x", encoding="utf-8")
    assert "_internal/web/float.html" not in vi.missing_installed(d)


def test_upgrade_step_asserts_new_version_landed():
    """源码级（评审②）：⑦ 跨版本那一步**必须**留下「新版落地」的断言。

    只断言「exe 在位 + 探针还在 + --check 退 0」的话，新文件没覆盖上去也照样全绿
    ——这正是评审抓到的假绿通道。故这里钉住三条源码事实：读装好的版本、
    比对版本号、覆盖后重跑 REQUIRED_INSTALLED。
    """
    src = Path(vi.__file__).read_text(encoding="utf-8")
    assert "installed_exe_version(" in src, "必须读安装目录里 exe 的版本资源"
    assert "覆盖后 exe 版本 ==" in src, "必须断言覆盖后版本 == 新版号"
    assert "覆盖后关键产物齐全" in src, "覆盖后必须重跑 REQUIRED_INSTALLED"
    assert "missing_installed(" in src


def test_launch_uses_literal_exe_name_and_no_shell():
    """源码级断言：启动被测程序用**字面量文件名**、不经 shell、参数列表形式。

    用 AST 而不是字符串匹配：文件头与函数 docstring 里出现 `shell=False`
    是在讲口径、不是证据——第一版用字符串匹配就在这儿栽了两次
    （断言被自己的说明文字喂饱）。AST 只看真实调用节点。
    """
    src = (ROOT / "scripts" / "verify_installer.py").read_text(encoding="utf-8")
    assert vi.EXE_NAME == "LectureLens.exe"
    assert "/" not in vi.EXE_NAME and "\\" not in vi.EXE_NAME, "启动名不得含路径分隔符"
    assert "shell=True" not in src, "绝不能开 shell"

    tree = ast.parse(src)
    run_calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "run"]
    assert run_calls, "应当有 subprocess.run 调用（本脚本就是要真装真卸）"
    for call in run_calls:
        kwargs = {k.arg for k in call.keywords}
        assert "shell" not in kwargs, "subprocess.run 不得传 shell 参数"
        assert call.args, "必须以参数列表形式启动（不得靠字符串命令）"

    # exe_check 必须以 EXE_NAME 这个字面量名启动（不含路径拼接）
    def _callee(node: ast.Call) -> str:
        return getattr(node.func, "attr", None) or getattr(node.func, "id", "") or ""

    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "exe_check")
    first = next(n for n in ast.walk(fn)
                 if isinstance(n, ast.Call) and _callee(n) == "run_quiet")
    assert isinstance(first.args[0], ast.List)
    assert ast.unparse(first.args[0]).startswith("[EXE_NAME,")


def test_exe_check_chdirs_instead_of_passing_cwd():
    """Windows 启动口径：必须 `os.chdir` 进安装目录，**不能**只传 `cwd=`。

    CreateProcess 解析可执行文件名用的是「调用进程」的当前目录，不是
    lpCurrentDirectory —— 传 `cwd=` 会直接 WinError 2。这不是推测：本脚本第一版
    就是 `cwd=install_dir` + 裸文件名，实跑到 ③ `--check` 那一步挂掉的。
    这条钉住修法，免得后人当冗余"简化"回 `cwd=`。
    """
    src = (ROOT / "scripts" / "verify_installer.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "exe_check")

    def _callee(node):
        return getattr(node.func, "attr", None) or getattr(node.func, "id", "")

    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]
    assert any(_callee(c) == "chdir" for c in calls), "exe_check 必须先 os.chdir 进安装目录"
    launch = [c for c in calls if _callee(c) == "run_quiet"]
    assert launch, "exe_check 应当调 run_quiet"
    assert "cwd" not in {k.arg for k in launch[0].keywords}, \
        "不得靠 cwd= 解析裸文件名（Windows 会 WinError 2）"
    # chdir 必须在 try/finally 里还原，不给后续步骤留副作用
    assert any(isinstance(n, ast.Try) for n in ast.walk(fn)), "chdir 需在 try/finally 中还原"


def test_run_quiet_decodes_leniently():
    """subprocess 必须**宽容解码**。

    Inno 的 setup/unins 往控制台写的是系统 OEM 代码页（本机 cp936），不是 UTF-8。
    `text=True` 的严格解码会在 subprocess 的 reader 线程里抛 UnicodeDecodeError，
    线程一死 `r.stdout` 就是空的——`--check` 的诊断尾巴被静默吞掉（第一版实测：
    ③ 那行的 note 尾巴为空，同时刷了一屏 reader 线程 traceback）。
    """
    src = (ROOT / "scripts" / "verify_installer.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "run_quiet")
    call = next(n for n in ast.walk(fn)
                if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "run")
    kwargs = {k.arg: k.value for k in call.keywords}
    assert "errors" in kwargs, "必须传 errors= 宽容解码（见 docstring）"
    assert ast.unparse(kwargs["errors"]) == "'replace'", "errors 应为 'replace'"


def test_silent_flags_match_the_d4_spec():
    """D4②:静默旗标三件套 + /DIR，缺一不可（少了 /SUPPRESSMSGBOXES 会卡在弹窗）。"""
    src = (ROOT / "scripts" / "verify_installer.py").read_text(encoding="utf-8")
    for flag in ('"/VERYSILENT"', '"/SUPPRESSMSGBOXES"', '"/NORESTART"'):
        assert flag in src, f"缺少静默旗标 {flag}"
    assert 'f"/DIR={install_dir}"' in src
    # 卸载也要静默
    assert '"/VERYSILENT"' in src.split("def silent_uninstall")[1]


# —— 评审修复（2026-10-14）逐条回归 ——————————————————————————————
# 评审指出的六条都成立，这里每条都钉一个测试，免得修复被后来的改动"优化"掉。

def test_cleanup_is_asserted_not_just_printed():
    """评审①：清理必须进 RESULTS，不能只是 print。

    原先 finally 块里 0 个 rec()、目录用 rmtree(ignore_errors=True) 删除、失败只
    print 一行 ⚠ → 退出码与「全部通过」都不受影响，「清理不留残留」成了空头承诺。
    这条断言清理块里至少有 4 条 rec()（卸载器退出 / 临时目录删净 / 无残留进程 /
    无新增 Inno 临时目录）。
    """
    src = (ROOT / "scripts" / "verify_installer.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "main")
    finals = [n for n in ast.walk(fn) if isinstance(n, ast.Try) and n.finalbody]
    assert finals, "main 应当有 try/finally 做清理"
    recs = []
    for stmt in finals[0].finalbody:
        for n in ast.walk(stmt):
            if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "rec":
                recs.append(ast.unparse(n)[:60])
    assert len(recs) >= 4, f"清理块应有 ≥4 条 rec() 断言，实得 {len(recs)}: {recs}"
    # 且不能是 ignore_errors 一删了事——必须有对应断言
    assert any("临时目录已删净" in r for r in recs), "缺「临时目录已删净」断言"
    assert any("无残留进程" in r for r in recs), "缺「无残留进程」断言"


def test_leftover_processes_has_positive_and_negative_control():
    """残留进程检测不能恒空也不能恒真：拿当前解释器当阳性对照。"""
    me = Path(sys.executable).name
    assert me in vi.leftover_processes((me,)), "tasklist 解析失效（阳性对照未命中）"
    assert vi.leftover_processes(("definitely-not-a-real-process-xyz.exe",)) == [], \
        "阴性对照未命中——检测逻辑可能是恒真的"


def test_inno_temp_dirs_snapshot_excludes_our_own_workdirs():
    """快照只认 Inno 自己的临时目录名。

    不能把本脚本的工作区（ll-installer-verify-*）算进去，否则「无新增 Inno 临时
    目录」这条会被自己的工作区误报成残留。
    """
    work = Path(tempfile.mkdtemp(prefix="ll-installer-verify-"))
    try:
        assert work.name not in vi.inno_temp_dirs()
        assert isinstance(vi.inno_temp_dirs(), set)
    finally:
        work.rmdir()


def test_version_from_setup_parses_release_names():
    """旧版/新版版本号靠文件名解析——解析错就会把两侧当成同一个版本。"""
    assert vi.version_from_setup(Path("LectureLens-1.0.0-setup.exe")) == "1.0.0"
    assert vi.version_from_setup(Path("LectureLens-0.9.0-setup.exe")) == "0.9.0"
    assert vi.version_from_setup(Path("LectureLens-1.2.3.4-setup.exe")) == "1.2.3.4"
    # 解析不出来时回退整名（便于报错里看出是哪个文件），不是抛异常
    assert vi.version_from_setup(Path("weird-name.exe")) == "weird-name.exe"


def test_cross_version_upgrade_is_required_not_optional():
    """评审②：跨版本升级必须**真用两个版本**，且不传 --upgrade-from 要判失败。

    原先第 ⑦ 步拿**同一个**安装包原地再装一遍 = 同版本重装，却在分发指南里被写成
    「换新版覆盖安装不丢数据」的实测口径。修法：接受 --upgrade-from 指定旧版，
    且**不传就 rec(..., False, ...)**（不是默默跳过），免得那句话又变成没测也敢说。
    """
    src = (ROOT / "scripts" / "verify_installer.py").read_text(encoding="utf-8")
    assert "--upgrade-from" in src, "未提供旧版安装包入口"
    # 必须真的把 upgrade_from 装上去（两个不同安装包）
    assert "silent_install(upgrade_from" in src, "跨版本路径没有真的装旧版"
    # 必须先装旧版、在旧版下造探针，再装新版覆盖
    assert src.index("silent_install(upgrade_from") < src.index("rc_new = silent_install(setup"), \
        "顺序反了：必须先在旧版下造探针，再用新版覆盖"
    # 不传时必须判失败（而不是 print 或 rec(..., True, ...)）
    assert 'rec("跨版本覆盖升级已实测", False,' in src, \
        "不传 --upgrade-from 时未判失败——「跨版本已测」仍可能被没测也敢说"


def test_iss_overrides_all_four_uninstall_messages():
    """评审①：Inno 有**四条**卸载收尾消息，四条都要覆盖且写明"笔记已保留"。

    漏掉 `UninstalledAndNeedsRestart` 的后果很具体：卸载时有文件被占用（最常见的
    触发方式就是**程序还开着**）→ 文件排到重启删除 → 走的就是这条消息；不覆盖它，
    用户在这条高频路径上只看到英文原文、也不知道笔记被保留了。

    诚实边界：静默卸载**不显示任何消息**，所以这四条的实际渲染效果无法自动化验证
    （属 GUI 人工项）。这里只能钉住"四条都覆盖了、文案里都有数据保留的说明"。
    """
    msgs = _section(_iss_code(), "Messages")
    four = ("ConfirmUninstall", "UninstalledAll",
            "UninstalledMost", "UninstalledAndNeedsRestart")
    for name in four:
        line = next((l for l in msgs.splitlines() if l.startswith(name + "=")), None)
        assert line, f"[Messages] 未覆盖 {name}（漏了这条卸载路径就没中文提示）"
        assert "保留" in line, f"{name} 未写明「数据已保留」"
    # 重启那条必须保留"是否现在重启"的询问语义（按钮映射到重启）
    restart = next(l for l in msgs.splitlines()
                   if l.startswith("UninstalledAndNeedsRestart="))
    assert "重启" in restart and "要现在重启吗" in restart


def test_iss_test_version_hook_is_guarded():
    """TEST_VERSION 是测试专用钩子，必须包在 #ifdef / #else / #endif 里。

    verify_installer 的跨版本升级需要"另一个版本"的安装包；这个钩子只该在显式传
    /DTEST_VERSION 时生效。**发版分支必须仍然从 exe 版本资源取版本**——否则一次
    手滑就能产出与真版本不符的正式包。
    """
    code = _iss_code()
    assert "#ifdef TEST_VERSION" in code, "缺测试钩子的条件编译"
    assert re.search(r"^#endif\s*$", code, re.M), "缺 #endif"
    # 非测试分支仍必须走 exe 版本资源（判断 + 裁剪两步都在）
    assert "GetVersionNumbersString(" in code
    assert 'Copy(Ver4, Len(Ver4) - 1, 2) == ".0"' in code
    # 且 TEST_VERSION 分支必须在 GetVersionNumbersString **之前**（即 #else 才走真版本）
    assert code.index("#ifdef TEST_VERSION") < code.index("GetVersionNumbersString(")


def test_cleanup_removes_its_own_inno_temp_dirs():
    """评审①延伸：清理要**真的动手收**本次新增的 Inno 临时目录。

    实测发现：Inno 卸载器收尾时**不保证**删掉自留的
    `%TEMP%\\is-<id>-uninstall.tmp` —— 这条断言第一次跑就是被它判红的
    （`残留 ['is-5F4T7NM75W-uninstall.tmp']`）。修法**不是**把断言放宽成
    "允许残留"，而是本脚本自己收干净；且只收「本次新增 + 仍在系统临时目录之下」
    的（tmp_before 差集 + checked_tmp_path 双重限定），不碰并发任务的目录。
    """
    src = (ROOT / "scripts" / "verify_installer.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "main")
    finals = [n for n in ast.walk(fn) if isinstance(n, ast.Try) and n.finalbody]
    body = ast.unparse(finals[0])
    assert "inno_temp_dirs() - tmp_before" in body, "未针对「本次新增」的 Inno 临时目录"
    assert "rmtree(checked_tmp_path(" in body, "删除前未过临时目录闸门（可能越界删）"
    assert "无新增 Inno 临时目录残留" in body, "缺最终断言"


def test_guide_says_firewall_names_lecturelens_not_python():
    """评审③：成品里没有 python.exe，朋友防火墙允许列表里也找不到它。

    分发指南原先抄了 UI 里的旧措辞（"是否允许 python.exe 联网"），那是**开发态**
    跑源码时的名字。装好的成品是 `LectureLens.exe`（dist/LectureLens 下实测只有
    LectureLens.exe/_internal/models）。让朋友去找 Python 会永远找不到。
    """
    t = (ROOT / "docs" / "distribution-guide.md").read_text(encoding="utf-8")
    # 提到防火墙必须指名 LectureLens
    for seg in t.split("\n\n"):
        if "防火墙放行" in seg:
            assert "LectureLens" in seg, f"防火墙条目未指名 LectureLens: {seg[:80]}"
    assert "是否允许 python.exe 联网" not in t, "仍在让朋友去找 python.exe"


def test_fresh_install_guide_has_workable_phone_sync_steps():
    """评审④：随包新手指引的手机同屏步骤必须**按字面做得通**。

    默认 `lan_enabled=False`（app/main.py:210），此时面板**不画二维码**、三步排查
    也整块隐藏（app/web/index.html:4723）。原指引直接写"点面板扫二维码"，
    朋友第一次用照做必然找不到码。必须先开关 + **重启一次**才出码。
    """
    t = (ROOT / "installer" / "新手指引.txt").read_text(encoding="utf-8")
    assert "打开" in t and "重启" in t, "手机同屏缺「先开开关再重启」的前提"
    # 顺序必须对：开开关 → 重启 → 扫码
    # 锚在**§四 的标题**上（正文别处也提过"手机同屏/手机扫码"，取第一次出现会切错段）
    seg = t[t.index("四、手机同屏"):]
    seg = seg[:seg.index("五、")] if "五、" in seg else seg
    assert seg.index("打开") < seg.index("重启") < seg.index("扫"), \
        f"手机同屏步骤顺序不对（应先开开关→再重启→最后扫码）: {seg[:120]}"


def test_fresh_install_guide_names_the_setup_exe_not_the_installed_exe():
    """评审④：朋友下载并双击的是**安装包**，不是装完之后的 LectureLens.exe。

    原指引写"先确认文件名是不是 LectureLens.exe"——那会让人以为下载错了
    （LectureLens.exe 是装到 %LocalAppData%\\LectureLens 里的程序本体）。
    """
    t = (ROOT / "installer" / "新手指引.txt").read_text(encoding="utf-8")
    assert "-setup.exe" in t, "新手指引未提到安装包文件名（-setup.exe）"
    # 不能把"确认文件名是不是 LectureLens.exe"这种话说给下载者
    assert "文件名是不是\n   LectureLens.exe" not in t
    assert "文件名是不是 LectureLens.exe" not in t
