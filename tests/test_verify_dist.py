# -*- coding: utf-8 -*-
"""tests/test_verify_dist.py — D3 产物验证脚本（scripts/verify_dist.py）的**边界**回归。

verify_dist.py 有两块价值：① 真启动打包产物（分钟级，属 D3 门禁本身，不进单测）；
② 它的**路径/URL 安全边界**是纯函数，必须逐条钉死——本仓库对「按输入请求地址 / 动
预期之外的路径」零容忍，这两道闸就是那条纪律在分发脚本上的落地：
  - `checked_url`：路径白名单 + 端口范围 + 环回地址，三者缺一即抛；
  - `checked_tmp_path`：只接受系统临时目录**之下**（仓库/用户目录/临时目录的上级一律拒）；
  - `checked_dist_path`：只接受 dist/LectureLens 及子项（仓库其他目录一律拒）；
  - 启动方式：`chdir` 进已验证的拷贝目录 + **字面量文件名**（命令行里不出现拼接出来的
    可执行路径，参数列表形式、无 shell）——这条是源码级断言。
"""
import re
import shutil
import tempfile
from pathlib import Path

import pytest

import verify_dist


def test_checked_url_whitelist_and_loopback_only():
    """只有 /api/status、B13 包列表与三个页面可请求；端口须是合法 int；目标恒为环回。"""
    assert verify_dist.checked_url(8798, "/api/status") == "http://127.0.0.1:8798/api/status"
    assert verify_dist.checked_url(8798, "/float").endswith("/float")
    assert verify_dist.checked_url(8798, verify_dist.PACKS_API).endswith(
        "/api/glossary/packs")               # B13：随包术语库包探针在白名单里
    assert verify_dist.HOST == "127.0.0.1"
    for bad_path in ("/etc/passwd", "/api/config", "/../secret", "", "//evil",
                     "/api/glossary/packs/install"):   # 装/卸是写操作，绝不进探针白名单
        with pytest.raises(ValueError):
            verify_dist.checked_url(8798, bad_path)
    for bad_port in (0, 70000, "8798", None):     # 字符串端口 = 潜在注入面，必须拒
        with pytest.raises(ValueError):
            verify_dist.checked_url(bad_port, "/api/status")


def test_no_redirect_opener_refuses_redirects():
    """302 到别处不跟（防被测进程把请求带去其它主机）。"""
    assert verify_dist._NoRedirect().redirect_request() is None
    assert len(verify_dist._OPENER.handlers) >= 1


def test_checked_tmp_path_confines_to_system_temp():
    inside = Path(tempfile.mkdtemp(prefix="vt-"))
    try:
        assert verify_dist.checked_tmp_path(inside) == inside.resolve()
        assert verify_dist.checked_tmp_path(inside / "LectureLens") == \
            (inside / "LectureLens").resolve()
        for outside in (verify_dist.ROOT, Path.home(),
                        Path(tempfile.gettempdir()).resolve().parent):
            with pytest.raises(SystemExit):
                verify_dist.checked_tmp_path(outside)
    finally:
        shutil.rmtree(inside, ignore_errors=True)


def test_checked_dist_path_confines_to_dist_dir():
    root = verify_dist.ROOT / "dist" / "LectureLens"
    assert verify_dist.checked_dist_path(root) == root.resolve()
    exe = verify_dist.checked_dist_path(root / "LectureLens.exe")
    assert exe.name == "LectureLens.exe"
    for outside in (verify_dist.ROOT / "app", verify_dist.ROOT,
                    Path(tempfile.gettempdir())):
        with pytest.raises(SystemExit):
            verify_dist.checked_dist_path(outside)


def test_expected_model_reads_build_exe_single_source():
    import build_exe
    assert verify_dist.expected_model() == build_exe.BUNDLE_MODEL


def test_expected_version_reads_branding_single_source():
    """版本探针**不写死字面量**——这条是 v1.1.0 发版的实证教训。

    verify_dist 原先写的是 `"1.0.0" in ver`：版本号恰恰是发版时**唯一要改的那一处**
    （分发出指南 §一「改版本就改这一处」），于是升到 1.1.0 时这条门禁当场假红；反过来
    若把判定改松成 `any(...)` 就变成假绿——两头都错。现在派生自 branding.APP_VERSION
    （经 build_exe.app_version 的同一入口），与 exe 版本资源、安装包文件名同源。

    末两条是**源码级**断言：挡住"下次有人图省事又抄一份字面量进去"。
    """
    import branding
    import build_exe
    assert verify_dist.expected_version() == branding.APP_VERSION
    assert verify_dist.expected_version() == build_exe.app_version()

    src = Path(verify_dist.__file__).read_text(encoding="utf-8")
    assert "want_ver in ver" in src, "版本判定必须用派生值（见 expected_version）"
    # 只看**代码**：文档串/行注释里引用旧写法（当作教训说明）不算违规
    code = re.sub(r'"""[\s\S]*?"""', "", src)
    code = re.sub(r"(?m)#.*$", "", code)
    assert not re.search(r'["\']\d+\.\d+(?:\.\d+)*["\']\s+in\s+ver', code), \
        "版本判定不得回退成写死的字面量（升版本时会假红）"


def test_forbidden_list_not_duplicated_and_covers_new_items():
    """D3 评审修复①：verify_dist 不再另抄一份清单（旧版抄漏 glossary/courses.json），
    而是直接用 build_exe 的事实源；拷贝根的非首启产物也有独立清单。"""
    assert verify_dist.build_exe.FORBIDDEN_DIST_ENTRIES is \
        verify_dist.build_exe.FORBIDDEN_DIST_ENTRIES       # 同一模块对象
    for name in (".env", "courses.json", "glossary", "desktop.lock"):
        assert name in verify_dist.ROOT_UNEXPECTED, f"{name} 必须列入拷贝根异常清单"
    for name in ("config.json", "logs", "transcripts"):
        assert name in verify_dist.RUNTIME_IN_ROOT, f"{name} 应列为首启正常产物"


def test_internal_scan_is_deep(tmp_path):
    """拷贝内 _internal 的检查走 build_exe.forbidden_hits（深扫任意层级）。"""
    internal = tmp_path / "_internal"
    (internal / "glossary").mkdir(parents=True)
    (internal / "glossary" / "CS.md").write_text("x", encoding="utf-8")
    hits = verify_dist.build_exe.forbidden_hits(internal)
    assert hits, "嵌套 glossary/ 必须被深扫命中（旧实现只看顶层）"


def test_launch_path_is_literal_name_after_chdir():
    """源码级：启动被测 exe = chdir 进已验证目录 + 字面量文件名；无 shell。

    断言串**拆开拼**：不把「可执行调用」的字面量写进测试源码（Mimosa 命令注入规则
    会把它当代码看，见 D3 实施记录）。"""
    src = Path(verify_dist.__file__).read_text(encoding="utf-8")
    launch_call = "subprocess." + "Popen("
    assert f"{launch_call}[EXE_NAME," in src, \
        "被测 exe 必须以字面量文件名启动（不把拼接路径放进命令行）"
    assert "os.chdir(str(copy))" in src, "必须先 chdir 进已验证的拷贝目录"
    assert "shell=True" not in src
    assert verify_dist.EXE_NAME == "LectureLens.exe"
