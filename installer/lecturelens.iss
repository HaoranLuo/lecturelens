; ============================================================================
; LectureLens 安装包脚本（Inno Setup 6）—— 需求 D4
;
; 设计口径（对齐 docs/notes-batch.md 的 D2/D4）：
;   · 每用户安装（PrivilegesRequired=lowest）→ 免管理员，默认落到
;     %LocalAppData%\LectureLens。**不支持 Program Files 全机安装**（权限坑，
;     且现有"数据随程序目录"模型依赖可写目录，见 app/main.py:44-45 的 ROOT）。
;   · 数据保留：**故意不写** [UninstallDelete] 的 transcripts / logs /
;     config.json / glossary —— Inno 只删除"自己装进去的文件"，运行期由用户
;     产生的数据文件天然不在删除清单里，卸载后目录因非空而保留。
;     本文件尾部有一段显式说明，scripts/verify_installer.py 有装机实测断言。
;   · 不签名（D5 明确：代码签名 £100-400/年，商业化再上）→ 首次运行会有
;     SmartScreen 提示，故随包附「新手指引.txt」。
;
; 编译（ISCC 不在 PATH，用绝对路径）：
;   "%LocalAppData%\Programs\Inno Setup 6\ISCC.exe" installer\lecturelens.iss
; 产出：dist\installer\LectureLens-<版本>-setup.exe
; ============================================================================

; —— 路径：SourcePath = 本 .iss 所在目录（installer\），故源根为 ..\dist\LectureLens
#define SourceRoot SourcePath + "\..\dist\LectureLens"
#define MyAppExeName "LectureLens.exe"

; 版本**单一来源** = dist 内 exe 的版本资源（由 scripts/build_exe.py 从
; app/branding.py 的 APP_VERSION 写入版本资源，实测 FileVersion=1.0.0）。
; 不在此处再写一份版本号，避免两处漂移。
;
; 为什么还要裁一刀：Windows 的**固定版本信息**恒为四段，build_exe.version_quad
; 把 APP_VERSION="1.0.0" 补成 "1.0.0.0"，而文件名口径要三段语义版本
; （产出 LectureLens-1.0.0-setup.exe）。故只在末段为 0 时去掉它；
; 末段非 0（如 1.2.3.4）原样保留，不会被误裁。
; 实测（Compression=none 探针）：GetVersionNumbersString → "1.0.0.0"，裁后 "1.0.0"。
#ifdef TEST_VERSION
; ── 测试专用钩子（**发版绝不能带**）─────────────────────────────────
; verify_installer.py --upgrade-from 验「跨版本覆盖升级」时需要"另一个版本"的安装包。
; 传 /DTEST_VERSION=0.9.0 即产出 0.9.0 **且不压缩**的测试包（不压缩只为省掉那 400 秒
; LZMA，与升级语义无关——Inno 的覆盖判定只看 AppId，与版本号无关）。
; 发版命令**不带**这个 define → 版本仍严格取自 exe 版本资源、压缩仍是 lzma2/max。
; tests/test_verify_installer.py 有断言钉住"不带 define 时版本必须来自 exe 资源"。
#define MyAppVersion TEST_VERSION
#else
#define Ver4 GetVersionNumbersString(SourceRoot + "\" + MyAppExeName)
#if Copy(Ver4, Len(Ver4) - 1, 2) == ".0"
#define MyAppVersion Copy(Ver4, 1, Len(Ver4) - 2)
#else
#define MyAppVersion Ver4
#endif
#endif

#define MyAppName "LectureLens"
#define MyAppPublisher "LectureLens"

[Setup]
; AppId 一经发布**永不变更**：覆盖安装/升级识别全靠它。
AppId={{8B4F2C1A-7D3E-4A96-B0C5-9E1F6A2D7C84}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}

; D2：每用户安装，免管理员。
PrivilegesRequired=lowest
DefaultDirName={localappdata}\LectureLens
; 允许用户改安装目录（但仍限每用户可写位置）。
DisableDirPage=auto
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
AllowNoIcons=yes

; 产物：dist\installer\LectureLens-<版本>-setup.exe
OutputDir=..\dist\installer
OutputBaseFilename=LectureLens-{#MyAppVersion}-setup
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName} {#MyAppVersion}

; 3GB 级 payload：lzma2/max + 固实压缩。CUDA DLL 压缩比高、模型权重压不动，
; 实测总产出远低于 GitHub Releases 的 2 GiB 单附件上限（见 D6）。
#ifdef TEST_VERSION
; 测试包不压缩：只为省掉 400 秒 LZMA，与「覆盖升级」语义无关（见 TEST_VERSION 注释）。
Compression=none
SolidCompression=no
#else
Compression=lzma2/max
SolidCompression=yes
#endif

; 64 位程序（PyInstaller 构建为 x64，Python 3.13 无 32 位打包目标）。
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

; 覆盖安装时若程序在运行，交给 Restart Manager 处理。
CloseApplications=yes
RestartApplications=no

WizardStyle=modern
; 不用 [Languages]：Inno Setup 6.7.3 自带语言包里**没有**简体中文
; （Languages\ 下只有 29 种，无 ChineseSimplified.isl），引用会直接编译失败。
; 故用默认英文向导 + 下方 [Messages] 覆盖关键中文文案；面向用户的正文
; 说明集中在随包「新手指引.txt」。

[Tasks]
; 桌面快捷方式 = 可选任务，**默认不勾**（用户想要才勾，也可整页跳过）。
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; ① 整个 dist\LectureLens（LectureLens.exe + _internal\ + models\small\）
;    小写通配 + recursesubdirs：保留内部目录结构。
;    构建前必须过 scripts/build_exe.py 的"用户数据/密钥扫描"，否则会把
;    config.json / transcripts 之类打进包（见该脚本 FORBIDDEN_DIST_ENTRIES）。
Source: "{#SourceRoot}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; ② 随包「新手指引.txt」（SmartScreen / 防火墙 / 三步向导 / 手机同屏扫码）
Source: "新手指引.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\新手指引"; Filename: "{app}\新手指引.txt"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

; ============================================================================
; [UninstallDelete] —— **故意缺席，这是设计要求，不是遗漏**
;
; Inno 的卸载器只删除"安装清单里记录过的文件"。transcripts\（笔记）、
; logs\、config.json、glossary\（术语表）都是**运行期**由程序自己创建的，
; 从未出现在 [Files] 清单里，因此卸载器不会碰它们；{app} 目录也会因为
; 残留数据非空而整体保留。
;
; 所以这里**不能**写 `Type: filesandordirs; Name: "{app}"` 之类的通配删除
; ——那会把用户整个学期的笔记一起抹掉。如需清理程序文件之外的缓存，也只能
; 逐条白名单，不得使用目录级通配。
;
; 卸载时向用户明说"笔记已保留"：见下方 [Messages] 的 ConfirmUninstall /
; UninstalledAll 覆盖。
; ============================================================================

[Messages]
; 卸载**前**就告知数据会保留（用户此刻还能反悔）。
ConfirmUninstall=确定要卸载 %1 吗？%n%n你的笔记（transcripts）、术语表（glossary）、日志（logs）与设置（config.json）都会被保留，不会被卸载程序删除。
; 卸载**后**再说明一次，并给出彻底清理的办法。
UninstalledAll=%1 已从你的电脑中移除。%n%n注意：你的笔记、术语表、日志与设置已保留在原来的安装目录里，方便你重装后继续用。%n如需彻底删除，请手动删除该目录。
UninstalledMost=%1 卸载完成。%n%n部分文件未能自动删除，可手动清理。%n%n你的笔记、术语表、日志与设置都已保留。
; 第三条收尾路径（评审补）：有文件被占用（最常见=卸载时程序还开着）→ 排到重启删除，
; 走的就是这条消息。不覆盖它，用户在这条**高频路径**上只会看到英文原文、且不知道
; 笔记被保留了。末句必须保留"是否现在重启"的询问语义（按钮映射到重启）。
UninstalledAndNeedsRestart=%1 已卸载，但要重启电脑才能完成清理。%n%n注意：你的笔记、术语表、日志与设置已保留在原来的安装目录里。%n%n要现在重启吗？

[CustomMessages]
CreateDesktopIcon=创建桌面快捷方式(&D)
AdditionalIcons=附加任务：
